"""Unit tests for AudioLevelMonitor's debounced gate state.

The stream is never opened here — we drive ``_callback`` directly with
synthetic blocks and a mocked clock, which is exactly how the sounddevice
callback feeds it in production.
"""

from __future__ import annotations

import numpy as np
import pytest

from audio_score_follower.core.audio_level import AudioLevelMonitor


def _block(amplitude: float, n: int = 1024) -> np.ndarray:
    """A constant-amplitude block shaped like sounddevice input (n, 1)."""
    return np.full((n, 1), amplitude, dtype=np.float32)


LOUD = _block(0.5)      # ≈ -6 dBFS
QUIET = _block(1e-6)    # ≈ -120 dBFS


@pytest.fixture
def monitor(monkeypatch):
    m = AudioLevelMonitor(
        threshold_db=-40.0,
        activation_hold_sec=0.7,
        release_hold_sec=0.3,
    )
    m._available = True  # pretend the stream opened
    clock = {"t": 0.0}
    monkeypatch.setattr(
        "audio_score_follower.core.audio_level.time",
        type("T", (), {"monotonic": staticmethod(lambda: clock["t"])}),
    )
    return m, clock


def _feed(monitor, clock, block, duration, step=0.064):
    """Feed ``block`` repeatedly for ``duration`` seconds of fake time."""
    t_end = clock["t"] + duration
    while clock["t"] < t_end:
        monitor._callback(block, block.shape[0], None, None)
        clock["t"] += step


class TestGateDebounce:
    def test_starts_inactive(self, monitor):
        m, _clock = monitor
        assert not m.is_active(), "gate must start closed (silence assumed)"

    def test_momentary_noise_does_not_open_gate(self, monitor):
        # 咳・物音の再現: 0.3 秒だけ閾値超え → gate は開かない。
        # 開いてしまうと OLTW がノイズで前進し（単調非減少なので）戻れない。
        m, clock = monitor
        _feed(m, clock, QUIET, 1.0)
        _feed(m, clock, LOUD, 0.3)   # < activation_hold 0.7s
        assert not m.is_active(), "momentary noise opened the gate"
        _feed(m, clock, QUIET, 1.0)
        assert not m.is_active()

    def test_sustained_sound_opens_gate(self, monitor):
        m, clock = monitor
        _feed(m, clock, QUIET, 1.0)
        _feed(m, clock, LOUD, 1.0)   # > activation_hold 0.7s
        assert m.is_active(), "sustained sound must open the gate"

    def test_brief_dip_does_not_close_gate(self, monitor):
        # 音符間の短い切れ目 (0.1s) で freeze/unfreeze が暴れないこと。
        m, clock = monitor
        _feed(m, clock, LOUD, 1.0)
        assert m.is_active()
        _feed(m, clock, QUIET, 0.1)  # < release_hold 0.3s
        assert m.is_active(), "brief dip closed the gate"
        _feed(m, clock, LOUD, 0.2)
        assert m.is_active()

    def test_sustained_silence_closes_gate(self, monitor):
        m, clock = monitor
        _feed(m, clock, LOUD, 1.0)
        assert m.is_active()
        _feed(m, clock, QUIET, 0.5)  # > release_hold 0.3s
        assert not m.is_active(), "sustained silence must close the gate"

    def test_interrupted_noise_resets_activation_timer(self, monitor):
        # 断続ノイズ (0.4s 音 → 0.4s 静寂 → 0.4s 音) では activation の
        # 連続条件が満たされず gate は閉じたまま。
        m, clock = monitor
        _feed(m, clock, QUIET, 1.0)
        _feed(m, clock, LOUD, 0.4)
        _feed(m, clock, QUIET, 0.4)
        _feed(m, clock, LOUD, 0.4)
        assert not m.is_active(), (
            "intermittent noise must not accumulate toward activation"
        )

    def test_unavailable_monitor_reports_active(self, monitor):
        # Monitor 起動失敗時は True (gate バイパス) — 既存仕様の維持。
        m, _clock = monitor
        m._available = False
        assert m.is_active()

    def test_zero_hold_flips_instantly(self, monkeypatch):
        # activation/release 0 で旧挙動 (即時反転) に戻せる。
        m = AudioLevelMonitor(
            threshold_db=-40.0, activation_hold_sec=0.0, release_hold_sec=0.0
        )
        m._available = True
        m._callback(LOUD, LOUD.shape[0], None, None)
        assert m.is_active()
        m._callback(QUIET, QUIET.shape[0], None, None)
        assert not m.is_active()

    def test_set_threshold_db_updates_gate_decision(self, monitor):
        # 操作者の ↑/↓ 調整 (main.adjust_silence_threshold) が次のコールバック
        # から即座に反映されること。
        m, clock = monitor  # threshold_db=-40.0
        moderate = _block(10 ** (-45 / 20.0))  # ≈ -45 dBFS: below -40
        _feed(m, clock, QUIET, 1.0)
        _feed(m, clock, moderate, 1.0)
        assert not m.is_active(), "-45 dBFS is below the -40 threshold"

        m.set_threshold_db(-50.0)  # loosen: -45 dBFS is now above threshold
        _feed(m, clock, moderate, 1.0)
        assert m.is_active(), "gate must react to the newly-set threshold"
