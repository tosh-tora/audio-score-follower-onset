"""End-performance tests (main.end_performance + gate/trigger interaction).

Issue #44: 演奏が終わっても follower は追随を続ける（拍手・環境音で小節が
進みトリガーが出る）。操作者が「■ 演奏終了」/ E を押すと OLTW ワーカーを
止めてフレーム供給を絶ち、トリガーを抑止する。

test_silence_gate.py と同じく Tk を起動せず ``object.__new__`` で
AudioScoreFollowerApp の骨組みだけを作り、メソッドを直接叩く。
"""

from __future__ import annotations

import threading
import time

from audio_score_follower.core.cooldown_timer import CooldownTimer
from audio_score_follower.core.state_manager import AppState
from audio_score_follower.core.trigger_engine import TriggerEngine
from audio_score_follower.main import AudioScoreFollowerApp


class _FakeMonitor:
    def __init__(self) -> None:
        self.available = True
        self.active = True  # sustained sound
        self.level_db = -40.0

    def is_available(self) -> bool:
        return self.available

    def is_active(self) -> bool:
        return self.active

    def get_level_db(self) -> float:
        return self.level_db


class _FakeOltw:
    def __init__(self) -> None:
        self.frozen = False
        self.freeze_calls = 0
        self.unfreeze_calls = 0

    @property
    def is_frozen(self) -> bool:
        return self.frozen

    def freeze(self) -> None:
        self.frozen = True
        self.freeze_calls += 1

    def unfreeze(self) -> None:
        self.frozen = False
        self.unfreeze_calls += 1


class _FakeWorker:
    def __init__(self) -> None:
        self.stop_calls = 0

    def stop(self) -> None:
        self.stop_calls += 1


def _make_app() -> AudioScoreFollowerApp:
    """Bare app skeleton: only the attributes the tested methods touch."""
    app = object.__new__(AudioScoreFollowerApp)
    app.audio_monitor = _FakeMonitor()
    app.oltw = _FakeOltw()
    app.worker = _FakeWorker()
    app.state = AppState()
    app._performance_started = True
    app._performance_confirmed = True
    app._performance_ended = False
    app._mic_mode = True
    app._prev_gate_active = False
    app._start_press_time = None
    app._start_gate_timeout_sec = 3.0
    app._workers_stop = threading.Event()
    app._workers_stop.set()  # suppress root.after rescheduling
    return app


def test_end_performance_stops_worker_and_sets_flags():
    app = _make_app()
    worker = app.worker

    app.end_performance()

    assert worker.stop_calls == 1
    assert app.worker is None
    assert app._performance_ended is True
    snap = app.state.get_all()
    assert snap["performance_ended"] is True
    assert snap["waiting_for_start"] is False
    assert snap["awaiting_first_sound"] is False
    assert snap["next_trigger_measure"] is None


def test_end_performance_idempotent():
    app = _make_app()
    app.end_performance()
    # Second press: already ended, no worker — must be a clean no-op.
    app.end_performance()
    assert app._performance_ended is True


def test_end_performance_no_worker_is_noop():
    app = _make_app()
    app.worker = None
    app.end_performance()
    assert app._performance_ended is False
    assert app.state.get_all()["performance_ended"] is False


def test_gate_poll_leaves_oltw_alone_after_end():
    """After 演奏終了 the gate poll must not freeze/unfreeze the OLTW."""
    app = _make_app()
    app.end_performance()
    # Re-attach an OLTW so we can watch it (end_performance dropped the
    # worker but the OLTW object stays; the gate poll still runs).
    app.oltw = _FakeOltw()

    app.audio_monitor.active = True   # sound present
    app._check_silence_gate()
    app.audio_monitor.active = False  # then silence
    app._check_silence_gate()

    assert app.oltw.freeze_calls == 0
    assert app.oltw.unfreeze_calls == 0
    # Level display still refreshed.
    assert app.state.get_all()["mic_level_db"] == app.audio_monitor.level_db


def test_manual_start_after_end_is_noop():
    """A start press after 演奏終了 must not touch the (stopped) OLTW."""
    app = _make_app()
    app.end_performance()
    app.oltw = _FakeOltw()  # would raise if force_lock_in were called

    app.manual_start()  # _FakeOltw has no force_lock_in

    assert app._performance_ended is True


def test_trigger_loop_suppressed_when_ended():
    """TriggerEngine must not fire while performance_ended is set."""
    state = AppState()
    state.set_movement(
        movement_id=1,
        xml_file="x.mxl",
        triggers=[{"measure": 5, "action": "right"}],
        total_measures=10,
    )
    state.update_beat_measure(beat=0.0, measure=5)
    state.set_confidence(0.9)  # above the trigger floor
    state.set_performance_ended(True)

    presses: list[str] = []

    class _FakeSlides:
        def press(self, action: str) -> None:
            presses.append(action)

    stop = threading.Event()
    engine = TriggerEngine(
        state=state,
        cooldown=CooldownTimer(3.0),
        slide_controller=_FakeSlides(),
        stop_event=stop,
        get_oltw=lambda: None,
        get_warp_lookup=lambda: None,
        get_score_mapper=lambda: None,
        get_cooldown_seconds=lambda: 3.0,
        notify_seek=lambda: None,
    )
    engine.start()
    time.sleep(0.2)  # a few 20Hz poll ticks
    stop.set()

    assert presses == []
    assert state.get_all()["next_trigger_measure"] is None


def test_set_movement_clears_ended_flag():
    state = AppState()
    state.set_performance_ended(True)
    assert state.get_all()["performance_ended"] is True
    state.set_movement(movement_id=2, xml_file="y.mxl", triggers=[])
    assert state.get_all()["performance_ended"] is False
