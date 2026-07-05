#!/usr/bin/env python3
"""
main.py - audio-score-follower entry point.

Threads:

    Main thread
      └─ Tkinter GUI

    Worker threads
      ├─ oltw-worker          (FollowerWorker — CENS + OLTW)
      ├─ slide-controller     (Playwright)
      └─ trigger-executor     (polls AppState, fires slide presses)

Tk timer callbacks:
    silence-gate              (every 50ms, freezes/unfreezes OLTW)

Usage::

    python -m audio_score_follower.main config.json \\
        --slide-url "https://docs.google.com/presentation/d/<ID>/present"

    # config を省略するとランチャー GUI が開く (ui/launcher.py):
    python -m audio_score_follower.main

Keys (on the operator GUI window):
    N            : load next movement
    R            : reload current movement
    L            : force OLTW lock-in (operator says "music has started";
                   arms inertia mode immediately so silence-gate freezes
                   become inertia progression instead of position-fix)
    → / Space    : manual slide advance
    ←            : manual slide back
    ↑ / ↓        : nudge silence-gate threshold by ±0.2 dB (mic mode only)
"""

from __future__ import annotations

import argparse
import logging
import math
import sys
import threading
import time
import tkinter as tk
from collections import deque
from pathlib import Path
from typing import Dict, Optional

# Windows cp932/cp1252 stdout cannot encode em-dashes or Japanese. Force
# UTF-8 so logging, argparse help, and Japanese error messages survive.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from audio_score_follower import launch_options
from audio_score_follower.config.loader import ConfigError, ConfigLoader
from audio_score_follower.core.audio_level import AudioLevelMonitor
from audio_score_follower.core.cooldown_timer import CooldownTimer
from audio_score_follower.core.follower_worker import FileWorker, FollowerWorker
from audio_score_follower.core.oltw_follower import (
    FollowResult,
    OnlineDTWFollower,
)
from audio_score_follower.core.score_mapper import ScoreMapper
from audio_score_follower.core.slide_controller import NullSlideController, SlideController
from audio_score_follower.core.state_manager import AppState
from audio_score_follower.core.warp_lookup import (
    WarpLookup,
    load_reference_cens,
    load_reference_onset,
)
from audio_score_follower.ui.gui_tkinter import FollowerGUI

logger = logging.getLogger(__name__)

# How often the trigger executor checks for measure hits.
_TRIGGER_POLL_HZ = 20
# How often we poll the silence gate from the Tk main loop.
_GATE_POLL_MS = 50
# Maximum measure jump allowed between consecutive OLTW frames without a
# preceding user seek. At 200 BPM 4/4 with 4× warp slope (the build-time
# limit) the measure advances <1 per 0.093s frame — so jumps >3 are anomalous.
_MAX_FRAME_MEASURE_JUMP = 3
# Suppress the jump-anomaly alert for this long after a user-initiated seek
# (jumps right after a seek are expected, not a warp path anomaly).
_SEEK_GRACE_SEC = 2.0
# Step size for the operator's runtime silence-threshold nudge (↑/↓ keys).
_THRESHOLD_STEP_DB = 0.2
# Minimum smoothed OLTW confidence before triggers are allowed to fire.
# Acts as the "lock-in" condition that InertiaEngine provided in
# live-score-sync; below this, alignment hasn't stabilised yet and
# firing the measure-1 trigger at startup would be spurious.
_TRIGGER_CONFIDENCE_FLOOR = 0.30
# Operator-facing (display) confidence: match-quality ramp over the
# smoothed ABSOLUTE fused local cost. The OLTW's internal confidence is
# band-relative and floors at ~0.6-0.8 even on unrelated audio (the
# non-negative chroma cosine floor), which misleads the operator ("piano
# BGM reads 70%"). This ramp maps smoothed cost LO→HI onto 1→0.
# Calibrated on 幻想4 measurements (2026-07): same recording p50=0.014,
# alt performance p50=0.082/p90=0.159, wrong movement p50=0.189,
# unrelated piano p50=0.300 → same ≈100%, alt ≈40-90%, piano ≈0%.
# Display only — lock-in / trigger floor / resync keep the internal scale.
_DISPLAY_CONF_COST_LO = 0.05
_DISPLAY_CONF_COST_HI = 0.22
# Frames of cost smoothing for the display ramp (matches the OLTW's own
# confidence_smoothing default; ~0.46s at 10.77 Hz).
_DISPLAY_CONF_SMOOTHING = 5


def display_confidence_from_cost(smoothed_cost: float) -> float:
    """Map a smoothed fused local cost to the operator-facing confidence.

    Linear ramp: cost <= LO → 1.0, cost >= HI → 0.0. NaN (frozen frames,
    where OLTW reports no cost) → 0.0.
    """
    if math.isnan(smoothed_cost):
        return 0.0
    span = _DISPLAY_CONF_COST_HI - _DISPLAY_CONF_COST_LO
    return max(0.0, min(1.0, (_DISPLAY_CONF_COST_HI - smoothed_cost) / span))


class AudioScoreFollowerApp:
    """Top-level orchestrator."""

    def __init__(
        self,
        config_path: str,
        slide_url: str | None,
        *,
        input_wav: Path | None = None,
        play_audio: bool = False,
        loopback: bool = False,
        loopback_device=None,
    ) -> None:
        logger.info(
            "Initialising AudioScoreFollowerApp (config=%s, input_wav=%s, "
            "play_audio=%s, loopback=%s)",
            config_path, input_wav, play_audio, loopback,
        )

        self.config = ConfigLoader(config_path)
        self.slide_url = slide_url
        self.input_wav = input_wav
        self.play_audio = play_audio
        self.loopback = loopback
        # loopback_device: CLI wins; fall back to config; None = OS default output
        self.loopback_device = (
            loopback_device
            if loopback_device is not None
            else self.config.get_loopback_device()
        )

        self.state = AppState()
        self.cooldown = CooldownTimer(self.config.get_cooldown_seconds())
        self.audio_monitor = AudioLevelMonitor(
            threshold_db=self.config.get_silence_threshold_db(),
            device=self.config.get_mic_device(),
            activation_hold_sec=self.config.get_gate_activation_sec(),
            release_hold_sec=self.config.get_gate_release_sec(),
        )

        # Per-movement objects (recreated each load)
        self.score_mapper: ScoreMapper | None = None
        self.warp_lookup: WarpLookup | None = None
        self.oltw: OnlineDTWFollower | None = None
        # Worker is either the live FollowerWorker (mic) or the FileWorker
        # (--input-wav diagnostic mode). Both share the same lifecycle
        # interface so callers don't need to special-case them.
        self.worker: FollowerWorker | FileWorker | None = None

        self._fired_trigger_measures: set[int] = set()

        # Manual performance start (mic mode only): the follower stays
        # frozen and ignores the silence gate until the operator presses
        # 「▶ 演奏開始」 (or L). wav/loopback modes auto-start as before.
        self._mic_mode = input_wav is None and not loopback
        self._performance_started = not self._mic_mode
        # One-shot gate release (Issue #13): after the start press, the
        # FIRST sustained sound confirms the performance is underway and
        # the gate stops governing freeze/unfreeze. Quiet openings (e.g.
        # 幻想交響曲 4th mvt) straddle the threshold, and every pre-lock-in
        # freeze rewinds the provisional advance and clears the confidence
        # streak — the follower can never lock in. Once the operator has
        # pressed start, any threshold crossing means music, so the gate's
        # job (blocking pre-performance noise) is done.
        self._performance_confirmed = not self._mic_mode

        # Runtime jump detection: track previous measure and the wall-clock
        # time of the most recent user-initiated seek. Jumps right after a
        # seek are expected; outside the grace period they indicate a warp
        # path anomaly.
        self._prev_oltw_measure: int = 0
        self._last_seek_time: float = 0.0

        if slide_url:
            self.slide_controller = SlideController(slide_url=slide_url)
        else:
            logger.warning(
                "--slide-url 未指定: ドライランモードで起動します。"
                "スライドは操作されません。"
            )
            self.slide_controller = NullSlideController()  # type: ignore[assignment]

        # Tk root + GUI (built before worker so update callbacks have
        # something to push into).
        self.root = tk.Tk()
        self.gui = FollowerGUI(
            self.root,
            self.state,
            on_start=self.manual_start,
        )

        self._workers_stop = threading.Event()
        self._trigger_thread: threading.Thread | None = None
        self._prev_gate_active = False

        # Diagnostic log throttle: emit one OLTW state log per wall-clock
        # second. Set from _on_oltw_result, which fires per CENS frame
        # (~10 Hz) and would otherwise flood the log.
        self._last_diag_log_sec = 0

        # Rolling window of fused local costs feeding the operator-facing
        # display confidence (see display_confidence_from_cost). Only
        # touched from the OLTW worker thread (_on_oltw_result).
        self._display_cost_window: deque[float] = deque(
            maxlen=_DISPLAY_CONF_SMOOTHING
        )

        logger.info("Initialisation complete")

    # ---------------------------------------------------- lifecycle
    def run(self) -> None:
        if self.input_wav is None and not self.loopback:
            logger.info("Launching AudioLevelMonitor …")
            # Surface the configured gate threshold so the GUI can show
            # it next to the live dBFS readout — the operator can then
            # see at a glance whether ambient noise sits above it.
            self.state.set_silence_threshold(
                self.config.get_silence_threshold_db()
            )
            try:
                self.audio_monitor.start()
            except BaseException as exc:  # noqa: BLE001
                logger.warning(
                    "AudioLevelMonitor.start raised (%s: %s); continuing "
                    "without silence gate",
                    type(exc).__name__, exc,
                )
        elif self.loopback:
            logger.info(
                "--loopback mode: skipping AudioLevelMonitor "
                "(loopback stream has clean silence; gate not needed)"
            )
        else:
            logger.info(
                "--input-wav mode: skipping AudioLevelMonitor "
                "(no mic to gate on)"
            )

        logger.info("Launching SlideController …")
        self.slide_controller.start()
        if not self.slide_controller.wait_ready(timeout=30.0):
            logger.error(
                "SlideController not ready: %s", self.slide_controller.last_error
            )

        self._bind_keys()

        logger.info("Loading first movement …")
        self._load_current_movement()

        self._trigger_thread = threading.Thread(
            target=self._trigger_loop, name="trigger-executor", daemon=True
        )
        self._trigger_thread.start()

        if self.input_wav is None and not self.loopback:
            self.root.after(_GATE_POLL_MS, self._check_silence_gate)
        else:
            # File-input / loopback mode: no separate mic monitor stream.
            # Mark as unavailable so the GUI dBFS readout shows "n/a".
            self.state.set_mic_level(-120.0, gate_active=False, monitor_available=False)

        logger.info(
            "Ready. N=next movement, R=reload, L=force lock-in, "
            "→/Space=manual next slide, ←=manual back."
        )
        self.root.protocol("WM_DELETE_WINDOW", self._on_gui_closing)
        try:
            self.root.mainloop()
        except KeyboardInterrupt:
            logger.info("Interrupted")
        finally:
            self._cleanup()

    def _on_gui_closing(self) -> None:
        logger.info("GUI closing")
        self._cleanup()
        try:
            self.root.destroy()
        except Exception:  # noqa: BLE001
            pass

    def _cleanup(self) -> None:
        logger.info("Shutting down …")
        self._workers_stop.set()
        if self.worker is not None:
            self.worker.stop()
            self.worker = None
        if self.input_wav is None and not self.loopback:
            self.audio_monitor.stop()
        self.slide_controller.stop()
        logger.info("Shutdown complete")

    # ---------------------------------------------------- movement loading
    def _load_current_movement(self) -> None:
        movement = self.config.get_current_movement()
        if not movement:
            logger.error("No movement available")
            return
        self._load_movement(movement)

    def _load_next_movement(self) -> None:
        if not self.config.next_movement():
            logger.warning("Already at last movement")
            self.state.set_next_trigger(None)
            return
        movement = self.config.get_current_movement()
        if movement:
            self._load_movement(movement)

    def _load_movement(self, movement: dict) -> None:
        xml_raw = movement.get("xml_file")
        built_raw = movement.get("built_dir")
        if not xml_raw or not built_raw:
            logger.error("Movement missing xml_file or built_dir: %s", movement)
            return

        xml_file = Path(self.config.resolve_path(xml_raw))
        built_dir = Path(self.config.resolve_path(built_raw))

        if not xml_file.exists():
            msg = f"楽譜ファイルが見つかりません。\n  → {xml_file}\n  に置いてください"
            logger.error(msg)
            self.state.set_load_error(f"ファイルが見つかりません\n{xml_file}")
            return
        if not built_dir.exists():
            msg = (
                f"ビルド済みリファレンスが見つかりません: {built_dir}\n"
                f"  asf-build を実行してから再起動してください"
            )
            logger.error(msg)
            self.state.set_load_error(f"asf-build 出力なし\n{built_dir}")
            return

        logger.info("Loading movement: xml=%s built=%s", xml_file, built_dir)

        # Stop previous worker
        if self.worker is not None:
            logger.info("Stopping previous OLTW worker …")
            self.worker.stop()
            self.worker = None

        try:
            self.score_mapper = ScoreMapper(str(xml_file))
            self.warp_lookup = WarpLookup.load(built_dir)
            reference_cens = load_reference_cens(built_dir)
            reference_onset = load_reference_onset(built_dir)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Failed to load movement artifacts: %s", exc)
            self.state.set_load_error(f"読込失敗: {exc}")
            return

        logger.info(
            "Loaded: %s, %s, reference_cens=(%d,%d)",
            self.score_mapper, self.warp_lookup,
            reference_cens.shape[0], reference_cens.shape[1],
        )

        chroma_weight, onset_weight = self.config.get_feature_fusion()
        onset_enabled = reference_onset is not None and onset_weight > 0.0
        logger.info(
            "Feature fusion: %s (chroma=%.2f onset=%.2f)%s",
            "enabled" if onset_enabled else "disabled",
            chroma_weight, onset_weight,
            "" if onset_enabled else
            " — rebuild with asf-build to generate reference_onset.npy",
        )

        # Validate warp path consistency before starting OLTW.
        try:
            self.warp_lookup.validate(self.score_mapper)
        except ValueError as exc:
            logger.error("Warp path validation failed: %s", exc)
            self.state.set_load_error(
                f"warp path 検証エラー:\n{exc}\nasf-build をやり直してください。"
            )
            return
        except Exception as exc:  # noqa: BLE001
            logger.exception("Warp path validation error: %s", exc)
            self.state.set_load_error(f"warp path 検証中にエラー: {exc}")
            return

        oltw_kwargs = self.config.get_oltw_kwargs()
        if self._mic_mode:
            # Manual-start correction for a LATE press: widen the
            # first-frame search window so tracking can land several
            # seconds into the piece. (Most late-press recovery goes
            # through the armed post-unfreeze catchup, but this covers
            # the corner where no freeze preceded the first frame.)
            fr = self.warp_lookup.feature_config.effective_frame_rate()
            start_width = int(round(self.config.get_start_search_seconds() * fr))
            oltw_kwargs["init_search_width"] = max(
                int(oltw_kwargs.get("init_search_width") or 0), start_width
            )
        try:
            self.oltw = OnlineDTWFollower(
                reference_cens=reference_cens,
                feature_config=self.warp_lookup.feature_config,
                reference_onset=reference_onset,
                chroma_weight=chroma_weight,
                onset_weight=onset_weight,
                **oltw_kwargs,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("Failed to construct OLTW: %s", exc)
            self.state.set_load_error(f"OLTW 初期化失敗: {exc}")
            return

        if self._mic_mode:
            # Park until the operator presses 「▶ 演奏開始」. The gate
            # poll keeps the follower frozen while _performance_started
            # is False; freeze here as well so no frame slips through
            # between worker start and the first poll.
            self._performance_started = False
            self._performance_confirmed = False
            self.oltw.freeze()
            self._prev_gate_active = True
            self.state.set_waiting_for_start(True)
            logger.info("Waiting for operator start (▶ 演奏開始 / L key)")

        self.cooldown.cleanup_old()
        self._fired_trigger_measures.clear()
        self._display_cost_window.clear()

        triggers = movement.get("triggers", [])
        total_measures = self.score_mapper.get_total_measures()
        self.state.set_movement(
            movement_id=movement.get("id"),
            xml_file=str(xml_file),
            triggers=triggers,
            movement_number=self.config.current_movement_number(),
            total_movements=self.config.total_movements(),
            total_measures=total_measures,
        )
        if triggers:
            self.state.set_next_trigger(min(t["measure"] for t in triggers))

        if self.input_wav is not None:
            self.worker = FileWorker(
                oltw_follower=self.oltw,
                feature_config=self.warp_lookup.feature_config,
                input_wav=self.input_wav,
                on_result=self._on_oltw_result,
                realtime=True,
                play_audio=self.play_audio,
                onset_enabled=onset_enabled,
            )
        elif self.loopback:
            self.worker = FollowerWorker(
                oltw_follower=self.oltw,
                feature_config=self.warp_lookup.feature_config,
                mic_device=self.loopback_device,
                on_result=self._on_oltw_result,
                loopback=True,
                onset_enabled=onset_enabled,
            )
        else:
            self.worker = FollowerWorker(
                oltw_follower=self.oltw,
                feature_config=self.warp_lookup.feature_config,
                mic_device=self.config.get_mic_device(),
                on_result=self._on_oltw_result,
                onset_enabled=onset_enabled,
            )
        self.worker.start()

        def _ready_check() -> None:
            assert self.worker is not None
            if not self.worker.wait_ready(timeout=10.0):
                logger.error("FollowerWorker not ready: %s", self.worker.last_error)
        threading.Thread(target=_ready_check, daemon=True, name="oltw-ready-check").start()

        logger.info("Movement loaded.")

    # ---------------------------------------------------- result callback
    def _on_oltw_result(self, result: FollowResult) -> None:
        """Called from the OLTW worker thread per CENS frame."""
        mapper = self.score_mapper
        lookup = self.warp_lookup
        if mapper is None or lookup is None:
            return
        try:
            measure, beat_in_measure, continuous_beat = lookup.ref_to_measure_and_beat(
                result.ref_time_sec, mapper
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("ref→measure failed: %s", exc)
            return

        # GUI displays 1-indexed beat-in-measure (downbeat = 1.0).
        beat_in_measure_display = beat_in_measure + 1.0
        self.state.update_beat_measure(
            continuous_beat, measure, beat_in_measure_display
        )
        self.state.set_confidence(result.confidence)
        # Operator-facing confidence: absolute match quality from the
        # smoothed fused cost. Frozen frames report NaN cost → display 0
        # without polluting the smoothing window.
        if math.isnan(result.raw_local_cost):
            self.state.set_display_confidence(0.0)
        else:
            self._display_cost_window.append(result.raw_local_cost)
            smoothed = sum(self._display_cost_window) / len(self._display_cost_window)
            self.state.set_display_confidence(display_confidence_from_cost(smoothed))
        # Mirror OLTW follower mode into AppState so the GUI tracking
        # panel reflects lock-in / inertia transitions in real time.
        if self.oltw is not None:
            self.state.set_follower_mode(
                is_locked_in=self.oltw.is_locked_in,
                is_in_inertia=self.oltw.is_in_inertia,
                inertia_elapsed_sec=self.oltw.inertia_elapsed_sec,
                inertia_cap_sec=self.oltw.max_inertia_seconds,
            )

        # Runtime jump detection: large measure jumps between consecutive
        # frames (outside the seek grace period) indicate a warp path
        # anomaly that should have been caught by asf-build --validate.
        jump = abs(measure - self._prev_oltw_measure)
        if (
            jump > _MAX_FRAME_MEASURE_JUMP
            and self._prev_oltw_measure != 0  # skip first frame (initialisation)
            and (time.monotonic() - self._last_seek_time) > _SEEK_GRACE_SEC
        ):
            logger.error(
                "異常な小節ジャンプを検出: %d → %d (+%d 小節) at ref_t=%.2fs。"
                "warp path の勾配が異常です。asf-build をやり直してください。",
                self._prev_oltw_measure, measure, jump, result.ref_time_sec,
            )
        self._prev_oltw_measure = measure

        # Throttled diagnostic log: emit once per wall-clock second so
        # `--verbose` doesn't drown in per-frame entries. Lets the
        # operator watch measure / confidence / cost / band live to
        # diagnose stuck or skipping behaviour. Includes mic dBFS in
        # live-mic mode so the user can spot "mic too quiet → noise
        # dominates chroma → OLTW stuck" failure modes.
        now_sec = int(time.time())
        if now_sec != self._last_diag_log_sec:
            self._last_diag_log_sec = now_sec
            try:
                snap = self.state.get_all()
                mic_db = snap.get("mic_level_db")
                mic_part = (
                    f" mic={mic_db:+.0f}dBFS" if mic_db is not None else ""
                )
            except Exception:  # noqa: BLE001
                mic_part = ""
            logger.info(
                "OLTW: m=%d β=%.2f conf=%.2f disp=%.2f raw_cost=%.3f "
                "ref_t=%.1fs band=[%d,%d)%s",
                measure, beat_in_measure_display, result.confidence,
                self.state.get_all().get("display_confidence", 0.0),
                result.raw_local_cost, result.ref_time_sec,
                result.band_lo, result.band_hi, mic_part,
            )

    # ---------------------------------------------------- silence gate
    def _check_silence_gate(self) -> None:
        try:
            mic_available = self.audio_monitor.is_available()
            mic_db = self.audio_monitor.get_level_db()
            gate_active = mic_available and not self.audio_monitor.is_active()

            if not self._performance_started:
                # Waiting for the operator's start press: hold the
                # follower frozen regardless of the gate, but keep the
                # level display live. _prev_gate_active stays True so
                # the first post-start poll re-evaluates the transition
                # (unfreezes immediately if sound is already present —
                # the late-press case).
                if self.oltw is not None and not self.oltw.is_frozen:
                    self.oltw.freeze()
                self._prev_gate_active = True
            elif self._performance_confirmed:
                # Performance confirmed (Issue #13): the gate no longer
                # freezes the follower. The level display stays live so
                # the operator can still see quiet passages dip below
                # the threshold, but tracking is now trusted to the DP
                # (pp passages that straddle the threshold must not
                # trigger pre-lock-in rewinds or inertia churn).
                pass
            elif gate_active != self._prev_gate_active and self.oltw is not None:
                if gate_active:
                    self.oltw.freeze()
                else:
                    self.oltw.unfreeze()
                    # First sustained sound after the start press: the
                    # performance is underway. Release the gate for the
                    # rest of the movement (one-shot).
                    self._performance_confirmed = True
                    logger.info(
                        "Performance confirmed (first sustained sound "
                        "after start press) — silence gate released; "
                        "tracking now governed by the DP alone"
                    )
                self._prev_gate_active = gate_active

            self.state.set_mic_level(mic_db, gate_active, mic_available)
        except Exception as exc:  # noqa: BLE001
            logger.error("Silence-gate poll failed: %s", exc)

        if not self._workers_stop.is_set():
            self.root.after(_GATE_POLL_MS, self._check_silence_gate)

    # ---------------------------------------------------- trigger loop
    def _trigger_loop(self) -> None:
        logger.info("Trigger loop started (%.0f Hz)", _TRIGGER_POLL_HZ)
        interval = 1.0 / _TRIGGER_POLL_HZ
        while not self._workers_stop.is_set():
            try:
                snapshot = self.state.get_all()
                triggers = self.state.current_triggers
                current_measure = snapshot["measure"]

                if not triggers:
                    time.sleep(interval)
                    continue

                upcoming = [
                    t["measure"] for t in triggers
                    if t["measure"] > current_measure
                    and t["measure"] not in self._fired_trigger_measures
                ]
                self.state.set_next_trigger(min(upcoming) if upcoming else None)

                # Don't fire until OLTW has locked in.
                if snapshot["confidence"] < _TRIGGER_CONFIDENCE_FLOOR:
                    time.sleep(interval)
                    continue

                if snapshot["cooldown_active"]:
                    time.sleep(interval)
                    continue

                for trig in triggers:
                    if trig["measure"] != current_measure:
                        continue
                    if current_measure in self._fired_trigger_measures:
                        continue
                    if not self.cooldown.should_trigger(current_measure):
                        continue

                    action = trig.get("action", "right")
                    self._execute_action(action, source="auto", trigger=trig)
                    self.cooldown.mark_triggered(current_measure)
                    self.state.activate_cooldown(self.config.get_cooldown_seconds())
                    self._fired_trigger_measures.add(current_measure)
                    break
            except Exception as exc:  # noqa: BLE001
                logger.error("Trigger loop error: %s", exc, exc_info=True)
            time.sleep(interval)
        logger.info("Trigger loop exiting")

    def _execute_action(
        self,
        action: str,
        *,
        source: str = "auto",
        trigger: Optional[Dict] = None,
    ) -> None:
        """Send a single slide keypress and log it with provenance.

        Args:
            action: "right" or "left".
            source: "auto" (fired by trigger loop from OLTW position) or
                "manual" (user pressed ← / → / Space). Goes into the
                log line so post-hoc review can tell which advances
                were the human compensating for tracking drift.
            trigger: the trigger dict (with measure, note) when known;
                included in the log for context.
        """
        try:
            self.slide_controller.press(action)
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "Slide action %s failed [%s]: %s", action, source, exc, exc_info=True
            )
            return
        if trigger is not None:
            logger.info(
                "Slide %s [%s] measure=%d note=%s",
                action, source, trigger.get("measure"),
                trigger.get("note", ""),
            )
        else:
            logger.info("Slide %s [%s]", action, source)

    # ----------------------- manual override helpers -----------------
    def _find_next_pending_trigger(self) -> Optional[Dict]:
        """Lowest-measure trigger that hasn't been fired yet.

        Returns None when every trigger in the current movement has
        already fired.
        """
        triggers = self.state.current_triggers
        pending = [
            t for t in triggers
            if t["measure"] not in self._fired_trigger_measures
        ]
        if not pending:
            return None
        return min(pending, key=lambda t: t["measure"])

    def _find_last_fired_trigger(self) -> Optional[Dict]:
        """Highest-measure trigger that has been fired so far.

        We use "highest measure" rather than "most-recently-fired by
        wall-clock time" because triggers naturally fire in score order
        during live performance; if the user has been pressing manual
        overrides out of order they probably want to undo the latest
        position in the score, not the latest in time.
        """
        if not self._fired_trigger_measures:
            return None
        last_measure = max(self._fired_trigger_measures)
        triggers = self.state.current_triggers
        for t in triggers:
            if t["measure"] == last_measure:
                return t
        return None

    def _seek_oltw_to_ref_time(
        self, ref_time_sec: float, *, allow_catchup: bool = True
    ) -> None:
        if self.oltw is None or self.warp_lookup is None:
            return
        fr = self.warp_lookup.feature_config.effective_frame_rate()
        target_frame = int(round(ref_time_sec * fr))
        self.oltw.seek(target_frame, allow_catchup=allow_catchup)
        self._last_seek_time = time.monotonic()

    def _manual_advance_to_next_trigger(self) -> None:
        """User pressed →: send slide right, then re-sync OLTW.

        The user is saying "the performance is at or past the next
        trigger measure" — so we (1) send the press, (2) mark that
        trigger as fired so the auto loop doesn't double-fire, and
        (3) seek OLTW to that measure's reference time so future
        auto-triggers fire from the correct downstream context.
        """
        nxt = self._find_next_pending_trigger()
        if nxt is None or self.warp_lookup is None or self.score_mapper is None:
            # No trigger to consume — fall back to bare slide press.
            self._execute_action("right", source="manual")
            return

        measure = int(nxt["measure"])
        try:
            ref_t = self.warp_lookup.measure_to_ref_time(measure, self.score_mapper)
        except Exception as exc:  # noqa: BLE001
            logger.error("measure_to_ref_time failed for m=%d: %s", measure, exc)
            self._execute_action("right", source="manual")
            return

        self._execute_action(nxt.get("action", "right"), source="manual", trigger=nxt)
        self.cooldown.mark_triggered(measure)
        self.state.activate_cooldown(self.config.get_cooldown_seconds())
        self._fired_trigger_measures.add(measure)
        self._seek_oltw_to_ref_time(ref_t)
        logger.info(
            "Manual sync: OLTW re-anchored to measure %d (ref_t=%.2fs)",
            measure, ref_t,
        )

    def _manual_back_to_prev_trigger(self) -> None:
        """User pressed ←: send slide left, then re-sync OLTW backwards.

        The user is saying "the performance is BEFORE the most-recent
        slide change". We (1) send the left press, (2) un-fire the
        most-recently fired trigger so it can fire again when the
        music re-enters that region, and (3) seek OLTW back to just
        BEFORE that measure so we won't immediately re-trigger it
        on the next live frame.
        """
        last = self._find_last_fired_trigger()
        if last is None or self.warp_lookup is None or self.score_mapper is None:
            self._execute_action("left", source="manual")
            return

        measure = int(last["measure"])
        try:
            ref_t = self.warp_lookup.measure_to_ref_time(measure, self.score_mapper)
        except Exception as exc:  # noqa: BLE001
            logger.error("measure_to_ref_time failed for m=%d: %s", measure, exc)
            self._execute_action("left", source="manual")
            return

        self._execute_action("left", source="manual", trigger=last)
        self.cooldown.unmark_triggered(measure)
        self._fired_trigger_measures.discard(measure)
        # Seek to a frame slightly BEFORE the measure's start so the
        # auto loop won't immediately re-fire on the next OLTW tick.
        # No post-seek catchup: the operator says the music is BEFORE
        # this point, so an automatic forward scan would re-defeat
        # the back-step.
        fr = self.warp_lookup.feature_config.effective_frame_rate()
        pre_frame = max(0, int(round(ref_t * fr)) - max(1, int(round(0.2 * fr))))
        if self.oltw is not None:
            self.oltw.seek(pre_frame, allow_catchup=False)
        logger.info(
            "Manual sync: OLTW re-anchored before measure %d "
            "(ref_frame=%d, ~%.2fs)",
            measure, pre_frame, pre_frame / fr,
        )

    def manual_start(self) -> None:
        """Operator start press (L key or GUI 「▶ 演奏開始」 button).

        Mic mode, first press: releases the manual-start hold. The
        silence gate then governs tracking only until the FIRST
        sustained sound — that crossing confirms the performance is
        underway and releases the gate for good (Issue #13: quiet
        openings straddle the threshold, and repeated pre-lock-in
        freezes rewind the follower forever). An EARLY press costs
        nothing (the follower stays frozen until sustained sound), a
        LATE press is corrected by the armed post-unfreeze catchup /
        widened initial search (``start_search_seconds``). Lock-in
        still latches automatically once real music is confidently
        tracked — we do NOT force it here, because an early press +
        forced lock-in would let silence-gate freezes advance inertia
        over silence.

        Subsequent presses (and all presses in wav/loopback auto-start
        modes) fall through to the legacy force-lock-in, which arms
        inertia at the conductor's downbeat.

        Public (no leading underscore) so the GUI can wire a button
        click directly to it.
        """
        if self.oltw is None:
            logger.warning("start pressed but OLTW not initialised yet")
            return
        if self._mic_mode and not self._performance_started:
            self._performance_started = True
            self.state.set_waiting_for_start(False)
            logger.info(
                "Performance start pressed — silence gate governs "
                "tracking until the first sustained sound, then "
                "releases (early/late press auto-corrected)"
            )
            return
        if self.oltw.is_locked_in:
            logger.info("start pressed but already locked in (no-op)")
            return
        self.oltw.force_lock_in()
        logger.info("Manual lock-in triggered by operator")

    def adjust_silence_threshold(self, delta_db: float) -> None:
        """Nudge the silence-gate threshold at runtime (↑/↓ keys).

        Mic mode only (wav/loopback have no gate — the monitor is never
        started, so ``is_available()`` is False and this is a no-op).
        Runtime-only: does not persist to config.json, so a re-launch
        reverts to the measured/configured value. Works before and after
        「▶ 演奏開始」— the operator may want to react to a rehearsal
        room's actual noise floor as soon as the meter is live.
        """
        if not self.audio_monitor.is_available():
            logger.info(
                "Silence threshold adjust ignored (mic monitor unavailable "
                "— wav/loopback mode has no gate)"
            )
            return
        new_threshold = self.audio_monitor.threshold_db + delta_db
        new_threshold = min(max(new_threshold, -120.0), 0.0)
        self.audio_monitor.set_threshold_db(new_threshold)
        self.state.set_silence_threshold(new_threshold)
        logger.info(
            "Silence threshold adjusted to %.1f dBFS (%+.1f)",
            new_threshold, delta_db,
        )

    # ---------------------------------------------------- key bindings
    def _bind_keys(self) -> None:
        def _on_n(_e: tk.Event) -> None:
            self._load_next_movement()

        def _on_r(_e: tk.Event) -> None:
            self._load_current_movement()

        def _on_l(_e: tk.Event) -> None:
            self.manual_start()

        def _on_next(_e: tk.Event) -> None:
            self._manual_advance_to_next_trigger()

        def _on_prev(_e: tk.Event) -> None:
            self._manual_back_to_prev_trigger()

        def _on_thr_up(_e: tk.Event) -> None:
            self.adjust_silence_threshold(_THRESHOLD_STEP_DB)

        def _on_thr_down(_e: tk.Event) -> None:
            self.adjust_silence_threshold(-_THRESHOLD_STEP_DB)

        self.root.bind("<KeyPress-n>", _on_n)
        self.root.bind("<KeyPress-N>", _on_n)
        self.root.bind("<KeyPress-r>", _on_r)
        self.root.bind("<KeyPress-R>", _on_r)
        self.root.bind("<KeyPress-l>", _on_l)
        self.root.bind("<KeyPress-L>", _on_l)
        self.root.bind("<KeyPress-Right>", _on_next)
        self.root.bind("<KeyPress-space>", _on_next)
        self.root.bind("<KeyPress-Left>", _on_prev)
        self.root.bind("<KeyPress-Up>", _on_thr_up)
        self.root.bind("<KeyPress-Down>", _on_thr_down)
        logger.info("Keys bound: N R L → ← Space ↑ ↓")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="audio-score-follower — orchestral audio-to-audio score following"
    )
    parser.add_argument(
        "config", nargs="?", default=None,
        help="Path to config.json。省略するとランチャー画面（GUI）を表示。",
    )
    parser.add_argument(
        "--slide-url", required=False, default=None,
        help="Google Slides /present URL。省略するとドライランモード（スライド操作なし）。",
    )
    parser.add_argument(
        "--input-wav", type=Path, default=None,
        help="マイクの代わりに指定の音源ファイル (WAV/MP3/...) を OLTW に流す。"
             "切り分けデバッグ用。silence gate は自動的に無効化される。"
             "ファイル名のみ指定した場合は data/reference_audio/<filename> に解決される。",
    )
    parser.add_argument(
        "--play-audio", action="store_true",
        help="--input-wav と組み合わせて使用。OLTW に流しながら同時に"
             "デフォルト出力デバイスからも再生する（テスト時に耳で確認する用）。",
    )
    parser.add_argument(
        "--loopback", action="store_true",
        help="PC の出力音声 (WASAPI ループバック) を OLTW に流す。"
             "Windows のみ。--loopback-device 未指定時は OS デフォルト出力を使用。"
             "silence gate は自動的に無効化される。",
    )
    parser.add_argument(
        "--loopback-device", default=None,
        help="ループバック取得元の出力デバイス番号または名前。"
             "省略すると OS デフォルト出力デバイスを使用。"
             "利用可能なデバイス一覧は python -c \"import sounddevice; print(sounddevice.query_devices())\" で確認。",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    # INFO first so launcher-phase logs are visible; the root logger level
    # is raised to DEBUG after --verbose / the launcher checkbox resolves.
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    for noisy in ("numba", "matplotlib", "asyncio", "PIL", "fontTools", "librosa"):
        logging.getLogger(noisy).setLevel(logging.INFO)

    if args.config is None:
        # No config argument → launcher GUI. Lazy import keeps the CLI
        # path free of launcher/sounddevice imports.
        from audio_score_follower.ui.launcher import run_launcher

        try:
            opts = run_launcher()
        except tk.TclError as exc:
            logger.error(
                "ランチャーを表示できません (%s)。config を引数で指定してください。", exc
            )
            return 1
        if opts is None:
            logger.info("ランチャーがキャンセルされました")
            return 0
    else:
        # CLI mode: behaviour unchanged; the persisted settings.launcher
        # block is intentionally ignored here.
        if not Path(args.config).exists():
            logger.error("Config not found: %s", args.config)
            return 1
        # wav × loopback exclusion must be checked before the enum mapping
        # collapses both flags into input_source.
        if args.input_wav is not None and args.loopback:
            logger.error("--input-wav と --loopback は同時に指定できません")
            return 1
        opts = launch_options.from_cli_args(args)
        errors = launch_options.validate(opts)
        if errors:
            for err in errors:
                logger.error("%s", err)
            return 1

    if opts.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    try:
        app = AudioScoreFollowerApp(
            str(opts.config_path),
            slide_url=opts.slide_url,
            input_wav=opts.effective_input_wav,
            play_audio=opts.play_audio,
            loopback=(opts.input_source == launch_options.INPUT_SOURCE_LOOPBACK),
            loopback_device=opts.loopback_device,
        )
        app.run()
        return 0
    except ConfigError as exc:
        logger.error("設定ファイルエラー: %s", exc)
        return 1
    except Exception as exc:  # noqa: BLE001
        logger.exception("Fatal: %s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
