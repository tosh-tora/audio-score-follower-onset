#!/usr/bin/env python3
"""
state_manager.py - Thread-Safe Central State Store

Manages all application state with proper locking.
The GUI reads state via a 100ms Tk-timer poll of get_all() (see
ui/gui_tkinter.py FollowerGUI._poll_state) rather than an event/wait
mechanism.
"""

import logging
import threading
from typing import Optional, List

logger = logging.getLogger(__name__)


class AppState:
    """
    Central state repository for the Sequential Live Follower application.

    All state reads/writes are protected by a threading.Lock to prevent race conditions.
    """

    def __init__(self):
        """Initialize state with defaults."""
        self._lock = threading.Lock()

        # Current movement context
        self.current_movement_id: Optional[int] = None
        self.current_xml_file: Optional[str] = None
        self.current_triggers: List[dict] = []
        self.current_movement_number: int = 1   # 1-indexed display number
        self.total_movements: int = 1
        self.total_measures: int = 0            # 0 = unknown (score not loaded)
        # Non-None when the last movement load failed — shown in the GUI so
        # the operator knows exactly where to put the missing file.
        self.load_error: Optional[str] = None

        # Playback state
        self.current_beat: float = 0.0
        self.current_measure: int = 1
        # Beat position within the current measure, 1-indexed for display
        # ("1拍目" = 1.0, halfway through 2拍目 = 2.5).  Computed by
        # ScoreMapper.get_beat_in_measure(continuous_beat) + 1.0 in the
        # state-sync loop, so time-signature changes are honored.
        self.current_beat_in_measure: float = 1.0
        self.confidence: float = 0.0
        # Operator-facing confidence derived from the ABSOLUTE fused local
        # cost (match quality), not from the OLTW's band-relative formula.
        # The internal ``confidence`` stays high (~0.6-0.8) even on
        # unrelated audio because non-negative chroma cosine has a high
        # floor; this display value collapses to ~0 there. Internal gates
        # (lock-in, trigger floor, resync) keep using ``confidence``.
        self.display_confidence: float = 0.0
        # Drift-suspicion flag from the OLTW mismatch detector: True while
        # smoothed cost has stayed above the calibrated junk threshold.
        # Triggers are suppressed and the GUI shows a warning while set.
        self.is_mismatched: bool = False

        # Trigger/cooldown state
        self.next_trigger_measure: Optional[int] = None
        self.cooldown_active: bool = False

        # OLTW lock-in / inertia state — mirrored from OnlineDTWFollower
        # by the result callback so the GUI tracking panel can render
        # the current follower mode (waiting / tracking / inertia /
        # inertia capped) without taking a lock on the OLTW itself.
        self.is_locked_in: bool = False
        self.is_in_inertia: bool = False
        self.inertia_elapsed_sec: float = 0.0
        self.inertia_cap_sec: float = 10.0

        # Live microphone level (dBFS) and whether the silence gate is
        # currently suppressing matcher confidence.  Surfaced to the GUI
        # so the operator can tell whether the mic is actually being heard.
        # ``mic_monitor_available`` is False when AudioLevelMonitor failed
        # to open its stream — in that case dBFS is meaningless and the
        # silence gate is bypassed.
        self.mic_level_db: float = -120.0
        self.silence_gate_active: bool = False
        self.mic_monitor_available: bool = False
        # Configured silence-gate threshold (dBFS), shown next to the
        # live level so the operator can see at a glance why the gate
        # is (or is not) engaging. None = not set (wav/loopback modes).
        self.silence_threshold_db: Optional[float] = None

        # Mic-mode manual start: True while the follower is parked
        # waiting for the operator to press 「▶ 演奏開始」 (or L).
        # Always False in wav/loopback modes (auto-start).
        self.waiting_for_start: bool = False

        # Operator pressed 「■ 演奏終了」 (or E): the follower worker has
        # been stopped so tracking no longer advances, and triggers are
        # suppressed. Terminal for the current movement — reset to False
        # on the next movement (re)load. Applies to every input mode.
        self.performance_ended: bool = False

        # Mic-mode start pressed but the performance is not confirmed
        # yet: the silence gate is waiting for the first sustained
        # sound, or the start-gate timeout (見切りスタート, Issue #41)
        # will fire. ``start_gate_timeout_sec`` mirrors the configured
        # timeout so the GUI can tell the operator what will happen
        # (0 = timeout disabled).
        self.awaiting_first_sound: bool = False
        self.start_gate_timeout_sec: float = 0.0

        # One-shot startup warning from mic_effects_probe (mic mode only):
        # non-None while the selected mic's OS-level noise suppression
        # was detected active (or could not be confirmed absent). None =
        # no warning to show. Set once at startup, not polled live.
        self.mic_effects_warning: Optional[str] = None

        # One-shot startup warning when SlideController failed to become ready
        # (Playwright not installed, browser launch/goto failure, or 30s
        # timeout). None = no warning to show. Set once at startup.
        self.slide_controller_warning: Optional[str] = None

    def get_all(self) -> dict:
        """
        Atomically get snapshot of all state.

        Returns:
            Dict with all current state values
        """
        with self._lock:
            return {
                'movement_id': self.current_movement_id,
                'xml_file': self.current_xml_file,
                'movement_number': self.current_movement_number,
                'total_movements': self.total_movements,
                'total_measures': self.total_measures,
                'load_error': self.load_error,
                'beat': self.current_beat,
                'measure': self.current_measure,
                'beat_in_measure': self.current_beat_in_measure,
                'confidence': self.confidence,
                'display_confidence': self.display_confidence,
                'is_mismatched': self.is_mismatched,
                'cooldown_active': self.cooldown_active,
                'next_trigger_measure': self.next_trigger_measure,
                'mic_level_db': self.mic_level_db,
                'silence_gate_active': self.silence_gate_active,
                'mic_monitor_available': self.mic_monitor_available,
                'silence_threshold_db': self.silence_threshold_db,
                'waiting_for_start': self.waiting_for_start,
                'performance_ended': self.performance_ended,
                'awaiting_first_sound': self.awaiting_first_sound,
                'start_gate_timeout_sec': self.start_gate_timeout_sec,
                'mic_effects_warning': self.mic_effects_warning,
                'slide_controller_warning': self.slide_controller_warning,
                'is_locked_in': self.is_locked_in,
                'is_in_inertia': self.is_in_inertia,
                'inertia_elapsed_sec': self.inertia_elapsed_sec,
                'inertia_cap_sec': self.inertia_cap_sec,
            }

    def update_beat_measure(
        self,
        beat: float,
        measure: int,
        beat_in_measure: float = 1.0,
    ):
        """
        Update beat and measure atomically.

        Args:
            beat: Continuous beat position
            measure: Measure number
            beat_in_measure: 1-indexed beat offset inside the current measure
                (e.g. 1.0 = downbeat, 2.5 = midway through the second beat).
                Caller is responsible for converting from the score's
                0-indexed offset to 1-indexed for display.
        """
        with self._lock:
            self.current_beat = beat
            self.current_measure = measure
            self.current_beat_in_measure = beat_in_measure

    def set_confidence(self, confidence: float):
        """
        Update confidence score.

        Args:
            confidence: [0.0, 1.0]
        """
        with self._lock:
            self.confidence = max(0.0, min(1.0, confidence))

    def set_display_confidence(self, confidence: float):
        """
        Update the operator-facing (cost-based) confidence.

        Args:
            confidence: [0.0, 1.0]
        """
        with self._lock:
            self.display_confidence = max(0.0, min(1.0, confidence))

    def set_mismatch(self, mismatched: bool):
        """
        Update the drift-suspicion flag from the OLTW mismatch detector.

        Args:
            mismatched: True while the follower suspects the count has
                drifted from the performance (triggers are suppressed
                and the GUI shows a warning).
        """
        with self._lock:
            self.is_mismatched = bool(mismatched)

    def set_movement(
        self,
        movement_id: int,
        xml_file: str,
        triggers: List[dict],
        movement_number: int = 1,
        total_movements: int = 1,
        total_measures: int = 0,
    ):
        """
        Set current movement context.

        Args:
            movement_id: Movement ID from config
            xml_file: Path to MusicXML file
            triggers: List of trigger dicts
        """
        with self._lock:
            self.current_movement_id = movement_id
            self.current_xml_file = xml_file
            self.current_triggers = triggers
            self.current_movement_number = movement_number
            self.total_movements = total_movements
            self.total_measures = total_measures
            self.load_error = None  # clear any previous error on successful load
            self.current_beat = 0.0
            self.current_measure = 1
            self.current_beat_in_measure = 1.0
            self.confidence = 0.0
            self.display_confidence = 0.0
            self.is_mismatched = False
            self.cooldown_active = False
            self.next_trigger_measure = None
            self.performance_ended = False

    def set_mic_level(
        self,
        level_db: float,
        gate_active: bool,
        monitor_available: bool = True,
    ):
        """Update mic level (dBFS), silence-gate state and monitor health.

        Args:
            level_db: Current mic RMS level in dBFS.
            gate_active: True if the silence gate is currently forcing
                matcher confidence to 0 (i.e. level_db <= threshold).
            monitor_available: False if AudioLevelMonitor failed to open
                its input stream — dBFS is then meaningless and the gate
                is bypassed.
        """
        with self._lock:
            self.mic_level_db = level_db
            self.silence_gate_active = gate_active
            self.mic_monitor_available = monitor_available

    def set_silence_threshold(self, threshold_db: Optional[float]):
        """Record the configured silence-gate threshold for GUI display."""
        with self._lock:
            self.silence_threshold_db = threshold_db

    def set_waiting_for_start(self, waiting: bool):
        """Update the mic-mode manual-start waiting flag."""
        with self._lock:
            self.waiting_for_start = waiting

    def set_performance_ended(self, ended: bool):
        """Update the operator-ended flag (「■ 演奏終了」 / E key).

        True after the operator stops the follower; suppresses trigger
        firing and drives the GUI 'ended' mode. Reset by set_movement()
        on the next (re)load.
        """
        with self._lock:
            self.performance_ended = bool(ended)

    def set_awaiting_first_sound(
        self, awaiting: bool, timeout_sec: float = 0.0
    ):
        """Update the post-press / pre-confirmation flag (Issue #41).

        Args:
            awaiting: True from the operator's start press until the
                performance is confirmed (first sustained sound or the
                start-gate timeout).
            timeout_sec: configured ``start_gate_timeout_sec`` for GUI
                display (0 = timeout disabled).
        """
        with self._lock:
            self.awaiting_first_sound = awaiting
            self.start_gate_timeout_sec = timeout_sec

    def set_mic_effects_warning(self, message: Optional[str]) -> None:
        """Record (or clear) the startup mic-noise-suppression warning."""
        with self._lock:
            self.mic_effects_warning = message

    def set_slide_controller_warning(self, message: Optional[str]) -> None:
        """Record (or clear) the startup SlideController failure warning."""
        with self._lock:
            self.slide_controller_warning = message

    def set_follower_mode(
        self,
        *,
        is_locked_in: bool,
        is_in_inertia: bool,
        inertia_elapsed_sec: float,
        inertia_cap_sec: float,
    ) -> None:
        """Update OLTW follower mode fields atomically.

        Called from the result callback so the GUI can render the
        current mode (waiting / tracking / inertia / inertia capped).
        """
        with self._lock:
            self.is_locked_in = is_locked_in
            self.is_in_inertia = is_in_inertia
            self.inertia_elapsed_sec = inertia_elapsed_sec
            self.inertia_cap_sec = inertia_cap_sec

    def set_load_error(self, message: str) -> None:
        """Record a movement-load failure message for GUI display."""
        with self._lock:
            self.load_error = message

    def set_next_trigger(self, measure_num: Optional[int]):
        """
        Set next trigger measure for display.

        Args:
            measure_num: Measure number, or None if no upcoming trigger
        """
        with self._lock:
            self.next_trigger_measure = measure_num

    def activate_cooldown(self, duration_sec: float):
        """
        Activate cooldown timer.

        Args:
            duration_sec: Cooldown duration in seconds

        Spawns background timer thread to auto-clear.
        """
        with self._lock:
            self.cooldown_active = True

        # Timer to auto-clear after duration_sec.
        threading.Timer(duration_sec, self.deactivate_cooldown).start()

    def deactivate_cooldown(self):
        """Deactivate cooldown."""
        with self._lock:
            self.cooldown_active = False

    def __repr__(self) -> str:
        state = self.get_all()
        return (
            f"AppState(measure={state['measure']}, beat={state['beat']:.1f}, "
            f"confidence={state['confidence']:.2f})"
        )
