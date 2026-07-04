"""Silence-gate poll state machine tests (main._check_silence_gate).

Issue #13: 静かに始まる楽章（幻想交響曲 4 楽章冒頭など）は音量が閾値を
跨いで上下するため、gate close のたびに pre-lock-in rewind が前進を
巻き戻して lock-in が永遠に成立しなかった。修正後は「▶ 演奏開始」押下後の
**最初の持続音**で演奏進行中と確定し、以後 gate は freeze を発火しない
（one-shot release）。

Tk を起動せず ``object.__new__`` で AudioScoreFollowerApp の骨組みだけを
作り、``_check_silence_gate`` の分岐を直接叩く。
"""

from __future__ import annotations

import threading

from audio_score_follower.core.state_manager import AppState
from audio_score_follower.main import AudioScoreFollowerApp


class _FakeMonitor:
    """AudioLevelMonitor stand-in with a settable gate state."""

    def __init__(self) -> None:
        self.available = True
        self.active = False  # True = sustained sound (gate open)
        self.level_db = -60.0

    def is_available(self) -> bool:
        return self.available

    def is_active(self) -> bool:
        return self.active

    def get_level_db(self) -> float:
        return self.level_db


class _FakeOltw:
    """Records freeze/unfreeze calls."""

    def __init__(self) -> None:
        self.frozen = True
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


def _make_app() -> AudioScoreFollowerApp:
    """Bare app skeleton: only the attributes _check_silence_gate touches."""
    app = object.__new__(AudioScoreFollowerApp)
    app.audio_monitor = _FakeMonitor()
    app.oltw = _FakeOltw()
    app.state = AppState()
    app._performance_started = False
    app._performance_confirmed = False
    app._prev_gate_active = True
    app._workers_stop = threading.Event()
    app._workers_stop.set()  # suppress root.after rescheduling
    return app


def test_waiting_for_start_stays_frozen_despite_sound():
    """Before the start press, sound must NOT unfreeze the follower."""
    app = _make_app()
    app.audio_monitor.active = True  # sustained sound present
    app.oltw.frozen = False  # e.g. a stray unfreeze slipped through

    app._check_silence_gate()

    assert app.oltw.frozen is True
    assert app.oltw.unfreeze_calls == 0
    assert app._performance_confirmed is False
    assert app._prev_gate_active is True


def test_silence_after_start_press_keeps_frozen():
    """Start pressed but no sound yet: hold at the anchor (early press)."""
    app = _make_app()
    app._performance_started = True
    app.audio_monitor.active = False  # still silent

    app._check_silence_gate()

    assert app.oltw.frozen is True
    assert app.oltw.unfreeze_calls == 0
    assert app._performance_confirmed is False


def test_first_sustained_sound_unfreezes_and_confirms():
    """The first gate opening after the start press confirms the
    performance and releases the gate."""
    app = _make_app()
    app._performance_started = True
    app.audio_monitor.active = True

    app._check_silence_gate()

    assert app.oltw.frozen is False
    assert app.oltw.unfreeze_calls == 1
    assert app._performance_confirmed is True
    assert app._prev_gate_active is False


def test_gate_never_refreezes_after_confirmation():
    """Issue #13 core: post-confirmation threshold dips must not freeze
    (pre-lock-in rewind churn) — quiet passages straddle the threshold."""
    app = _make_app()
    app._performance_started = True

    # First sustained sound → confirmed.
    app.audio_monitor.active = True
    app._check_silence_gate()
    assert app._performance_confirmed is True

    # Level dips below the threshold again (quiet opening).
    app.audio_monitor.active = False
    app._check_silence_gate()
    assert app.oltw.freeze_calls == 0
    assert app.oltw.frozen is False

    # Oscillates back above — no extra unfreeze either (already running).
    app.audio_monitor.active = True
    app._check_silence_gate()
    assert app.oltw.unfreeze_calls == 1


def test_monitor_unavailable_confirms_on_first_poll_after_start():
    """No mic monitor → gate reads inactive → confirm immediately (the
    gate was already bypassed in this configuration)."""
    app = _make_app()
    app._performance_started = True
    app.audio_monitor.available = False

    app._check_silence_gate()

    assert app.oltw.frozen is False
    assert app._performance_confirmed is True
