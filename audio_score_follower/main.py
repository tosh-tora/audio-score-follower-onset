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

Keys (on the operator GUI window):
    N            : load next movement
    R            : reload current movement
    → / Space    : manual slide advance
    ←            : manual slide back
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

from audio_score_follower.config.loader import ConfigError, ConfigLoader
from audio_score_follower.core.audio_level import AudioLevelMonitor
from audio_score_follower.core.cooldown_timer import CooldownTimer
from audio_score_follower.core.follower_worker import FollowerWorker
from audio_score_follower.core.oltw_follower import (
    FollowResult,
    OnlineDTWFollower,
)
from audio_score_follower.core.score_mapper import ScoreMapper
from audio_score_follower.core.slide_controller import SlideController
from audio_score_follower.core.state_manager import AppState
from audio_score_follower.core.warp_lookup import WarpLookup, load_reference_cens
from audio_score_follower.ui.gui_tkinter import FollowerGUI

logger = logging.getLogger(__name__)

# How often the trigger executor checks for measure hits.
_TRIGGER_POLL_HZ = 20
# How often we poll the silence gate from the Tk main loop.
_GATE_POLL_MS = 50
# Minimum smoothed OLTW confidence before triggers are allowed to fire.
# Acts as the "lock-in" condition that InertiaEngine provided in
# live-score-sync; below this, alignment hasn't stabilised yet and
# firing the measure-1 trigger at startup would be spurious.
_TRIGGER_CONFIDENCE_FLOOR = 0.30


class AudioScoreFollowerApp:
    """Top-level orchestrator."""

    def __init__(self, config_path: str, slide_url: str) -> None:
        logger.info("Initialising AudioScoreFollowerApp (config=%s)", config_path)

        self.config = ConfigLoader(config_path)
        self.slide_url = slide_url

        self.state = AppState()
        self.cooldown = CooldownTimer(self.config.get_cooldown_seconds())
        self.audio_monitor = AudioLevelMonitor(
            threshold_db=self.config.get_silence_threshold_db(),
            device=self.config.get_mic_device(),
        )

        # Per-movement objects (recreated each load)
        self.score_mapper: ScoreMapper | None = None
        self.warp_lookup: WarpLookup | None = None
        self.oltw: OnlineDTWFollower | None = None
        self.worker: FollowerWorker | None = None

        self._fired_trigger_measures: set[int] = set()

        self.slide_controller = SlideController(slide_url=slide_url)

        # Tk root + GUI (built before worker so update callbacks have
        # something to push into).
        self.root = tk.Tk()
        self.gui = FollowerGUI(self.root, self.state)

        self._workers_stop = threading.Event()
        self._trigger_thread: threading.Thread | None = None
        self._prev_gate_active = False

        logger.info("Initialisation complete")

    # ---------------------------------------------------- lifecycle
    def run(self) -> None:
        logger.info("Launching AudioLevelMonitor …")
        try:
            self.audio_monitor.start()
        except BaseException as exc:  # noqa: BLE001
            logger.warning(
                "AudioLevelMonitor.start raised (%s: %s); continuing "
                "without silence gate",
                type(exc).__name__, exc,
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

        self.root.after(_GATE_POLL_MS, self._check_silence_gate)

        logger.info("Ready. N=next movement, R=reload, →/Space=manual next slide.")
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
        except Exception as exc:  # noqa: BLE001
            logger.exception("Failed to load movement artifacts: %s", exc)
            self.state.set_load_error(f"読込失敗: {exc}")
            return

        logger.info(
            "Loaded: %s, %s, reference_cens=(%d,%d)",
            self.score_mapper, self.warp_lookup,
            reference_cens.shape[0], reference_cens.shape[1],
        )

        try:
            self.oltw = OnlineDTWFollower(
                reference_cens=reference_cens,
                feature_config=self.warp_lookup.feature_config,
                **self.config.get_oltw_kwargs(),
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("Failed to construct OLTW: %s", exc)
            self.state.set_load_error(f"OLTW 初期化失敗: {exc}")
            return

        self.cooldown.cleanup_old()
        self._fired_trigger_measures.clear()

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

        self.worker = FollowerWorker(
            oltw_follower=self.oltw,
            feature_config=self.warp_lookup.feature_config,
            mic_device=self.config.get_mic_device(),
            on_result=self._on_oltw_result,
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

    # ---------------------------------------------------- silence gate
    def _check_silence_gate(self) -> None:
        try:
            mic_available = self.audio_monitor.is_available()
            mic_db = self.audio_monitor.get_level_db()
            gate_active = mic_available and not self.audio_monitor.is_active()

            if gate_active != self._prev_gate_active and self.oltw is not None:
                if gate_active:
                    self.oltw.freeze()
                else:
                    self.oltw.unfreeze()
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
                    note = trig.get("note", "")
                    self._execute_action(action)
                    logger.info(
                        "Trigger fired at measure %d: action=%s note=%s",
                        current_measure, action, note,
                    )
                    self.cooldown.mark_triggered(current_measure)
                    self.state.activate_cooldown(self.config.get_cooldown_seconds())
                    self._fired_trigger_measures.add(current_measure)
                    break
            except Exception as exc:  # noqa: BLE001
                logger.error("Trigger loop error: %s", exc, exc_info=True)
            time.sleep(interval)
        logger.info("Trigger loop exiting")

    def _execute_action(self, action: str) -> None:
        try:
            self.slide_controller.press(action)
        except Exception as exc:  # noqa: BLE001
            logger.error("Slide action %s failed: %s", action, exc, exc_info=True)

    # ---------------------------------------------------- key bindings
    def _bind_keys(self) -> None:
        def _on_n(_e: tk.Event) -> None:
            self._load_next_movement()

        def _on_r(_e: tk.Event) -> None:
            self._load_current_movement()

        def _on_next(_e: tk.Event) -> None:
            self._execute_action("right")

        def _on_prev(_e: tk.Event) -> None:
            self._execute_action("left")

        self.root.bind("<KeyPress-n>", _on_n)
        self.root.bind("<KeyPress-N>", _on_n)
        self.root.bind("<KeyPress-r>", _on_r)
        self.root.bind("<KeyPress-R>", _on_r)
        self.root.bind("<KeyPress-Right>", _on_next)
        self.root.bind("<KeyPress-space>", _on_next)
        self.root.bind("<KeyPress-Left>", _on_prev)
        logger.info("Keys bound: N R → ← Space")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="audio-score-follower — orchestral audio-to-audio score following"
    )
    parser.add_argument("config", help="Path to config.json")
    parser.add_argument(
        "--slide-url", required=True,
        help="Google Slides /present URL",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    for noisy in ("numba", "matplotlib", "asyncio", "PIL", "fontTools", "librosa"):
        logging.getLogger(noisy).setLevel(logging.INFO)

    config_path = Path(args.config)
    if not config_path.exists():
        logger.error("Config not found: %s", config_path)
        return 1

    try:
        app = AudioScoreFollowerApp(str(config_path), slide_url=args.slide_url)
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
