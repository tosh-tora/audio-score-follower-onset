#!/usr/bin/env python3
"""AppState の特性テスト（リファクタ前の挙動固定）。"""
import time

from audio_score_follower.core.state_manager import AppState


def test_get_all_returns_expected_keys():
    state = AppState()
    snap = state.get_all()
    # リファクタ後にキー集合が変わる場合はこのテストも同一コミットで更新する
    assert {"measure", "beat", "beat_in_measure", "confidence",
            "cooldown_active", "next_trigger_measure", "mic_level_db",
            "silence_gate_active", "mic_monitor_available",
            "silence_threshold_db", "waiting_for_start", "is_locked_in",
            "is_in_inertia", "inertia_elapsed_sec", "inertia_cap_sec",
            "movement_id", "xml_file", "movement_number", "total_movements",
            "total_measures", "load_error"} <= set(snap.keys())


def test_update_beat_measure_roundtrip():
    state = AppState()
    state.update_beat_measure(12.5, 4, 2.5)
    snap = state.get_all()
    assert snap["beat"] == 12.5
    assert snap["measure"] == 4
    assert snap["beat_in_measure"] == 2.5


def test_confidence_clamped():
    state = AppState()
    state.set_confidence(1.7)
    assert state.get_all()["confidence"] == 1.0
    state.set_confidence(-0.2)
    assert state.get_all()["confidence"] == 0.0


def test_set_movement_resets_playback_state():
    state = AppState()
    state.update_beat_measure(50.0, 20, 3.0)
    state.set_load_error("dummy")
    state.set_movement(movement_id=2, xml_file="x.mxl", triggers=[],
                       movement_number=2, total_movements=4, total_measures=100)
    snap = state.get_all()
    assert snap["measure"] == 1 and snap["beat"] == 0.0
    assert snap["load_error"] is None
    assert snap["total_measures"] == 100


def test_set_follower_mode():
    state = AppState()
    state.set_follower_mode(is_locked_in=True, is_in_inertia=True,
                            inertia_elapsed_sec=3.5, inertia_cap_sec=10.0)
    snap = state.get_all()
    assert snap["is_locked_in"] and snap["is_in_inertia"]
    assert snap["inertia_elapsed_sec"] == 3.5


def test_cooldown_activate_sets_flag_immediately():
    state = AppState()
    state.activate_cooldown(60.0)  # 長時間にして自動クリアがテスト中に走らないようにする
    assert state.get_all()["cooldown_active"] is True
    state.deactivate_cooldown()
    assert state.get_all()["cooldown_active"] is False


def test_cooldown_auto_clears_after_duration_not_double():
    """自動クリアが duration_sec で走ること（Issue #16: 旧実装は 2倍残った）。"""
    state = AppState()
    state.activate_cooldown(0.1)
    assert state.get_all()["cooldown_active"] is True
    # duration の 2 倍待つ。旧実装（time.sleep(duration) + Timer(duration)）なら
    # ここではまだ True のまま = 回帰を捕捉できる。
    time.sleep(0.2)
    assert state.get_all()["cooldown_active"] is False


def test_display_confidence_setter_and_reset():
    state = AppState()
    state.set_display_confidence(0.73)
    assert state.get_all()["display_confidence"] == 0.73
    # Clamped to [0, 1].
    state.set_display_confidence(1.5)
    assert state.get_all()["display_confidence"] == 1.0
    state.set_display_confidence(-0.2)
    assert state.get_all()["display_confidence"] == 0.0
    # Movement load resets it alongside the internal confidence.
    state.set_display_confidence(0.9)
    state.set_movement(movement_id=1, xml_file="x.mxl", triggers=[])
    assert state.get_all()["display_confidence"] == 0.0
