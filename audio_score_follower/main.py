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
    E            : end performance (operator says "music has stopped";
                   drops the OLTW worker so tracking + triggers halt.
                   Re-follow with R / N)
    → / Space    : manual slide advance
    ←            : manual slide back
    ↑ / ↓        : nudge silence-gate threshold by ±0.2 dB (mic mode only)
"""

from __future__ import annotations

import argparse
import logging
import sys
import threading
import time
import tkinter as tk
from pathlib import Path

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
from audio_score_follower.core.mic_effects_probe import probe_capture_effects
from audio_score_follower.core.movement_loader import (
    MovementLoadError,
    load_movement,
)
from audio_score_follower.core.oltw_follower import OnlineDTWFollower
from audio_score_follower.core.result_handler import (
    _DISPLAY_CONF_COST_HI,
    _DISPLAY_CONF_COST_LO,
    OltwResultHandler,
    display_confidence_from_cost,  # re-exported for tests / external callers
)
from audio_score_follower.core.score_mapper import ScoreMapper
from audio_score_follower.core.slide_controller import NullSlideController, SlideController
from audio_score_follower.core.state_manager import AppState
from audio_score_follower.core.trigger_engine import TriggerEngine
from audio_score_follower.core.viz_feed import VizFeed, VizThresholds
from audio_score_follower.core.warp_lookup import WarpLookup
from audio_score_follower.ui.gui_tkinter import FollowerGUI
from audio_score_follower.ui.viz_window import VizWindow

logger = logging.getLogger(__name__)

# How often we poll the silence gate from the Tk main loop.
_GATE_POLL_MS = 50
# Step size for the operator's runtime silence-threshold nudge (↑/↓ keys).
_THRESHOLD_STEP_DB = 0.2


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
        viz: bool = False,
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
        self.viz = viz
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
        # Operator pressed 「■ 演奏終了」 (or E): the OLTW worker is stopped
        # so no more frames feed the follower (measure count freezes) and
        # triggers are suppressed. Terminal for the movement — cleared on
        # the next _load_movement. Applies to every input mode.
        self._performance_ended = False

        # Wall-clock time of the most recent user-initiated forward seek.
        # Written by _record_seek (via TriggerEngine.notify_seek), read by
        # the result handler's jump detector to suppress the expected
        # post-seek jump during the grace period.
        self._last_seek_time: float = 0.0

        # 見切りスタート (Issue #41): monotonic time of the operator's
        # first start press. If the silence gate has not opened within
        # start_gate_timeout_sec of the press (quiet opening below the
        # threshold), _check_silence_gate confirms the performance and
        # unfreezes anyway — the press itself is the operator saying
        # "the music is starting". None until the press.
        self._start_press_time: float | None = None
        self._start_gate_timeout_sec = self.config.get_start_gate_timeout_sec()

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
            on_end=self.end_performance,
            on_adjust_threshold=self.adjust_silence_threshold,
        )

        # Optional realtime feature/confidence visualiser (--viz). The feed
        # is a pure data channel; the window is a separate Toplevel consumer
        # so a future audience-facing screen can share the same feed. When
        # disabled, viz_feed stays None and the result handler skips the push.
        self.viz_feed: VizFeed | None = None
        if self.viz:
            self.viz_feed = VizFeed(
                VizThresholds(
                    display_conf_cost_lo=_DISPLAY_CONF_COST_LO,
                    display_conf_cost_hi=_DISPLAY_CONF_COST_HI,
                    mismatch_cost=self.config.get_oltw_kwargs().get(
                        "mismatch_cost_threshold", 0.18
                    ),
                )
            )
            VizWindow(self.root, self.viz_feed)
            logger.info("Realtime visualiser enabled (--viz)")

        self._workers_stop = threading.Event()
        self._prev_gate_active = False

        # Slide triggering + manual overrides live in TriggerEngine. The
        # per-movement objects (oltw / warp_lookup / score_mapper) are
        # recreated on each load, so the engine reads them via getters
        # rather than a captured reference. notify_seek records the seek
        # time the jump detector uses for its grace period.
        self.trigger_engine = TriggerEngine(
            state=self.state,
            cooldown=self.cooldown,
            slide_controller=self.slide_controller,
            stop_event=self._workers_stop,
            get_oltw=lambda: self.oltw,
            get_warp_lookup=lambda: self.warp_lookup,
            get_score_mapper=lambda: self.score_mapper,
            get_cooldown_seconds=self.config.get_cooldown_seconds,
            notify_seek=self._record_seek,
        )

        # Per-frame OLTW result → AppState / viz / diagnostics. Reads the
        # per-movement objects via getters and the seek time via a getter
        # so it always sees the current values.
        self.result_handler = OltwResultHandler(
            state=self.state,
            viz_feed=self.viz_feed,
            get_oltw=lambda: self.oltw,
            get_warp_lookup=lambda: self.warp_lookup,
            get_score_mapper=lambda: self.score_mapper,
            get_last_seek_time=lambda: self._last_seek_time,
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
            self._check_mic_effects()
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

        self.trigger_engine.start()

        if self.input_wav is None and not self.loopback:
            self.root.after(_GATE_POLL_MS, self._check_silence_gate)
        else:
            # File-input / loopback mode: no separate mic monitor stream.
            # Mark as unavailable so the GUI dBFS readout shows "n/a".
            self.state.set_mic_level(-120.0, gate_active=False, monitor_available=False)

        logger.info(
            "Ready. N=next movement, R=reload, L=force lock-in, "
            "E=end performance, →/Space=manual next slide, ←=manual back."
        )
        self.root.protocol("WM_DELETE_WINDOW", self._on_gui_closing)
        try:
            self.root.mainloop()
        except KeyboardInterrupt:
            logger.info("Interrupted")
        finally:
            self._cleanup()

    def _check_mic_effects(self) -> None:
        """One-shot mic-mode startup check for OS-level noise suppression.

        The feature space (CENS + onset) assumes an unprocessed signal —
        NC distorts both chroma and attack envelopes and silently
        degrades tracking (see CLAUDE.md). This does not block startup
        (detection has known blind spots: hardware-embedded NC and
        upstream virtual mics are invisible to this probe), it only
        surfaces a GUI warning so the operator can check Windows sound
        settings before a performance.
        """
        try:
            report = probe_capture_effects(self.config.get_mic_device())
        except BaseException as exc:  # noqa: BLE001
            logger.warning("Mic effects probe raised: %s", exc)
            return
        if report.has_noise_suppression or report.suspicious_name:
            logger.warning("Mic effects check: %s", report.headline_ja())
            self.state.set_mic_effects_warning(report.headline_ja())
        else:
            logger.info("Mic effects check: %s", report.headline_ja())

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
        prev_idx = self.config.current_movement_idx
        if not self.config.next_movement():
            logger.warning("Already at last movement")
            self.state.set_next_trigger(None)
            return
        movement = self.config.get_current_movement()
        if not (movement and self._load_movement(movement)):
            # Load failed (or no movement): roll the index back so the
            # internal movement counter stays in sync with the GUI. Otherwise
            # the index silently advances past a movement that never loaded,
            # and the next N press skips the following one.
            logger.warning("Next-movement load failed; reverting to movement %d", prev_idx + 1)
            self.config.current_movement_idx = prev_idx

    def _stop_worker(self) -> None:
        """Stop and drop the current OLTW worker (if any)."""
        if self.worker is not None:
            logger.info("Stopping previous OLTW worker …")
            self.worker.stop()
            self.worker = None

    def _load_movement(self, movement: dict) -> bool:
        # Pure loading / construction lives in movement_loader; app-side
        # orchestration (worker teardown, mic parking, state updates,
        # worker wiring) stays here. stop_previous is invoked from inside
        # the loader at the original point so a bad reload keeps the
        # current worker running. Returns True on success so callers can
        # keep the movement index in sync with what actually loaded.
        try:
            loaded = load_movement(
                self.config,
                movement,
                mic_mode=self._mic_mode,
                viz=self.viz,
                stop_previous=self._stop_worker,
            )
        except MovementLoadError as exc:
            if exc.state_message is not None:
                self.state.set_load_error(exc.state_message)
            return False

        self.score_mapper = loaded.score_mapper
        self.warp_lookup = loaded.warp_lookup
        self.oltw = loaded.oltw
        onset_enabled = loaded.onset_enabled

        # A (re)load resumes tracking: clear any prior 「■ 演奏終了」 stop
        # (all modes). set_movement() clears the state-side flag too.
        self._performance_ended = False

        if self._mic_mode:
            # Park until the operator presses 「▶ 演奏開始」. The gate
            # poll keeps the follower frozen while _performance_started
            # is False; freeze here as well so no frame slips through
            # between worker start and the first poll.
            self._performance_started = False
            self._performance_confirmed = False
            self._start_press_time = None
            self.oltw.freeze()
            self._prev_gate_active = True
            self.state.set_waiting_for_start(True)
            self.state.set_awaiting_first_sound(False)
            logger.info("Waiting for operator start (▶ 演奏開始 / L key)")

        self.cooldown.cleanup_old()
        self.trigger_engine.reset_for_movement()
        self.result_handler.reset_for_movement()

        triggers = loaded.triggers
        self.state.set_movement(
            movement_id=movement.get("id"),
            xml_file=str(loaded.xml_file),
            triggers=triggers,
            movement_number=self.config.current_movement_number(),
            total_movements=self.config.total_movements(),
            total_measures=loaded.total_measures,
        )
        if triggers:
            self.state.set_next_trigger(min(t["measure"] for t in triggers))

        if self.input_wav is not None:
            self.worker = FileWorker(
                oltw_follower=self.oltw,
                feature_config=self.warp_lookup.feature_config,
                input_wav=self.input_wav,
                on_result=self.result_handler.on_result,
                realtime=True,
                play_audio=self.play_audio,
                onset_enabled=onset_enabled,
            )
        elif self.loopback:
            self.worker = FollowerWorker(
                oltw_follower=self.oltw,
                feature_config=self.warp_lookup.feature_config,
                mic_device=self.loopback_device,
                on_result=self.result_handler.on_result,
                loopback=True,
                onset_enabled=onset_enabled,
            )
        else:
            self.worker = FollowerWorker(
                oltw_follower=self.oltw,
                feature_config=self.warp_lookup.feature_config,
                mic_device=self.config.get_mic_device(),
                on_result=self.result_handler.on_result,
                onset_enabled=onset_enabled,
            )
        self.worker.start()

        def _ready_check() -> None:
            assert self.worker is not None
            if not self.worker.wait_ready(timeout=10.0):
                logger.error("FollowerWorker not ready: %s", self.worker.last_error)
        threading.Thread(target=_ready_check, daemon=True, name="oltw-ready-check").start()

        logger.info("Movement loaded.")
        return True

    # ---------------------------------------------------- silence gate
    def _check_silence_gate(self) -> None:
        try:
            mic_available = self.audio_monitor.is_available()
            mic_db = self.audio_monitor.get_level_db()
            gate_active = mic_available and not self.audio_monitor.is_active()

            if self._performance_ended:
                # Operator pressed 「■ 演奏終了」: the worker is stopped, so
                # leave the follower alone (no freeze/unfreeze) and just
                # keep the level display live below.
                pass
            elif not self._performance_started:
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
                    self.state.set_awaiting_first_sound(False)
                    logger.info(
                        "Performance confirmed (first sustained sound "
                        "after start press) — silence gate released; "
                        "tracking now governed by the DP alone"
                    )
                self._prev_gate_active = gate_active
            elif (
                gate_active
                and self.oltw is not None
                and self._start_gate_timeout_sec > 0
                and self._start_press_time is not None
                and time.monotonic() - self._start_press_time
                    >= self._start_gate_timeout_sec
            ):
                # 見切りスタート (Issue #41): the gate never opened —
                # the opening is quieter than the threshold. The
                # operator's press is trusted over the meter: confirm
                # the performance and unfreeze. Deliberately NOT
                # force_lock_in(): lock-in on unheard input would arm
                # inertia over potential silence; the latch engages on
                # its own once real music is confidently tracked.
                if self.oltw.is_frozen:
                    self.oltw.unfreeze()
                self._performance_confirmed = True
                self.state.set_awaiting_first_sound(False)
                logger.info(
                    "見切りスタート: gate did not open within %.1fs of the "
                    "start press (level below threshold) — performance "
                    "confirmed, tracking started anyway",
                    self._start_gate_timeout_sec,
                )

            self.state.set_mic_level(mic_db, gate_active, mic_available)
        except Exception as exc:  # noqa: BLE001
            logger.error("Silence-gate poll failed: %s", exc)

        if not self._workers_stop.is_set():
            self.root.after(_GATE_POLL_MS, self._check_silence_gate)

    # ---------------------------------------------------- seek grace
    def _record_seek(self) -> None:
        """Record the wall-clock time of a forward re-anchor.

        Called by TriggerEngine after a forward seek so the result
        handler's jump detector suppresses the (expected) post-seek
        measure jump during the grace period.
        """
        self._last_seek_time = time.monotonic()

    def manual_start(self) -> None:
        """Operator start press (L key or GUI 「▶ 演奏開始」 button).

        Mic mode, first press: releases the manual-start hold. The
        silence gate then governs tracking only until the FIRST
        sustained sound — that crossing confirms the performance is
        underway and releases the gate for good (Issue #13: quiet
        openings straddle the threshold, and repeated pre-lock-in
        freezes rewind the follower forever). If the gate never opens
        within ``start_gate_timeout_sec`` of the press (opening quieter
        than the threshold), tracking starts anyway — 見切りスタート,
        Issue #41. An EARLY press therefore costs nothing only until
        the timeout fires; press at (or just after) the downbeat. A
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
        if self._performance_ended:
            # Follower stopped by 「■ 演奏終了」 — the worker is gone, so a
            # start press here would only force_lock_in a dead follower.
            # Re-follow via R (reload) or N (next movement) instead.
            logger.info("start pressed but performance ended — press R/N to re-follow")
            return
        if self._mic_mode and not self._performance_started:
            self._performance_started = True
            self._start_press_time = time.monotonic()
            self.state.set_waiting_for_start(False)
            self.state.set_awaiting_first_sound(
                True, self._start_gate_timeout_sec
            )
            logger.info(
                "Performance start pressed — silence gate governs "
                "tracking until the first sustained sound or the "
                "%.1fs start-gate timeout (見切りスタート), whichever "
                "comes first",
                self._start_gate_timeout_sec,
            )
            return
        if self.oltw.is_locked_in:
            logger.info("start pressed but already locked in (no-op)")
            return
        self.oltw.force_lock_in()
        logger.info("Manual lock-in triggered by operator")

    def end_performance(self) -> None:
        """Operator end press (E key or GUI 「■ 演奏終了」 button).

        The follower keeps tracking after the music stops (applause and
        ambient noise advance the measure count and can fire triggers on
        the way, Issue #44). This is the operator's hard stop: we drop the
        OLTW worker so no more frames feed the follower — the measure count
        freezes and triggers are suppressed (TriggerEngine skips firing
        while ``state.performance_ended``). ``freeze()`` is deliberately
        NOT used: after lock-in it arms inertia progression (see CLAUDE.md
        「二段構えの lock-in」), which would keep advancing rather than stop.

        Terminal for the current movement. Re-follow with R (reload) or N
        (next movement); both rebuild the worker and reset this flag.

        Public (no leading underscore) so the GUI can wire a button click
        directly to it.
        """
        if self._performance_ended:
            logger.info("end pressed but performance already ended (no-op)")
            return
        if self.worker is None:
            logger.info("end pressed but no follower worker is running (no-op)")
            return
        self._performance_ended = True
        self._stop_worker()
        self.state.set_performance_ended(True)
        # Clear the mic-mode start-waiting flags so the GUI doesn't show a
        # start affordance under the ended banner.
        self.state.set_waiting_for_start(False)
        self.state.set_awaiting_first_sound(False)
        self.state.set_next_trigger(None)
        logger.info("Performance ended by operator — follower stopped; press R/N to re-follow")

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

        def _on_e(_e: tk.Event) -> None:
            self.end_performance()

        def _on_next(_e: tk.Event) -> None:
            self.trigger_engine.advance_to_next_trigger()

        def _on_prev(_e: tk.Event) -> None:
            self.trigger_engine.back_to_prev_trigger()

        def _on_thr_up(_e: tk.Event) -> None:
            self.adjust_silence_threshold(_THRESHOLD_STEP_DB)

        def _on_thr_down(_e: tk.Event) -> None:
            self.adjust_silence_threshold(-_THRESHOLD_STEP_DB)

        # bind_all (not root.bind): the hotkeys must fire no matter which of
        # the app's Tk windows holds keyboard focus. With --viz the visualiser
        # is a separate Toplevel whose bindtags do NOT include the main root,
        # so a root-only binding goes dead the moment the operator clicks into
        # the viz window — N/R/L/arrows silently do nothing until focus returns
        # to the main window. The "all" bindtag is present on every widget in
        # the interpreter, so bind_all covers both windows. (No Entry widgets
        # exist in the follow GUI, so intercepting plain keys is safe here.)
        self.root.bind_all("<KeyPress-n>", _on_n)
        self.root.bind_all("<KeyPress-N>", _on_n)
        self.root.bind_all("<KeyPress-r>", _on_r)
        self.root.bind_all("<KeyPress-R>", _on_r)
        self.root.bind_all("<KeyPress-l>", _on_l)
        self.root.bind_all("<KeyPress-L>", _on_l)
        self.root.bind_all("<KeyPress-e>", _on_e)
        self.root.bind_all("<KeyPress-E>", _on_e)
        self.root.bind_all("<KeyPress-Right>", _on_next)
        self.root.bind_all("<KeyPress-space>", _on_next)
        self.root.bind_all("<KeyPress-Left>", _on_prev)
        self.root.bind_all("<KeyPress-Up>", _on_thr_up)
        self.root.bind_all("<KeyPress-Down>", _on_thr_down)
        logger.info("Keys bound (app-wide): N R L E → ← Space ↑ ↓")


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
    parser.add_argument(
        "--viz", action="store_true",
        help="特徴量 (ライブ vs 参照 chroma)・融合コスト時系列・探索バンド内"
             "コスト曲線をリアルタイム表示する可視化ウィンドウを別途開く。"
             "追随本体には影響しない診断/デモ用。",
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
            viz=opts.viz,
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
