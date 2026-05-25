"""Unit tests for OnlineDTWFollower."""

from __future__ import annotations

import numpy as np
import pytest

from audio_score_follower.core.feature_extractor import FeatureConfig
from audio_score_follower.core.oltw_follower import OnlineDTWFollower


def _make_chroma_sequence(n_frames: int, period: int = 4, seed: int = 0) -> np.ndarray:
    """Synthesise a (12, n_frames) chroma-like sequence.

    Each frame is a one-hot vector rotating through 12 pitch classes
    with a period of ``period`` frames. The result is already L2-
    normalised (each column is a unit-length basis vector), matching
    what ``compute_cens`` guarantees.
    """
    rng = np.random.default_rng(seed)
    cens = np.zeros((12, n_frames), dtype=np.float32)
    for j in range(n_frames):
        pc = (j // period) % 12
        cens[pc, j] = 1.0
    # Add a tiny noise floor so cosine distances aren't pathological zeros.
    cens = cens + 0.01 * rng.standard_normal(cens.shape).astype(np.float32)
    norms = np.linalg.norm(cens, axis=0, keepdims=True)
    return (cens / norms).astype(np.float32)


def test_self_alignment_returns_monotonic_positions():
    """When live == reference, OLTW should track the diagonal."""
    cfg = FeatureConfig()
    ref = _make_chroma_sequence(120)
    follower = OnlineDTWFollower(ref, cfg, search_width=30)

    positions = []
    for j in range(120):
        result = follower.process_frame(ref[:, j])
        positions.append(result.ref_frame)

    # Position must be monotonic non-decreasing.
    diffs = np.diff(positions)
    assert (diffs >= 0).all(), f"non-monotonic: diffs={diffs[:10]}"

    # And by the end we should be near the end of the reference.
    assert positions[-1] >= 100, (
        f"expected to be near end, got {positions[-1]} (last 5: {positions[-5:]})"
    )


def test_self_alignment_high_confidence():
    """Self-alignment with non-periodic data should yield confidence close to 1.

    Uses random unit chroma vectors (not one-hot cyclic) so each ref
    frame has a unique signature within the search band. With the
    margin-based confidence formula this requires both a good local
    match AND a sharply-peaked DP minimum — the periodic one-hot
    sequence used elsewhere is too ambiguous (12 pitch classes recur)
    and legitimately gives ~0.5 confidence; not a regression but a
    correctly-pessimistic reading.
    """
    cfg = FeatureConfig()
    rng = np.random.default_rng(123)
    ref = rng.standard_normal((12, 80)).astype(np.float32)
    ref = np.abs(ref)  # chroma is non-negative
    norms = np.linalg.norm(ref, axis=0, keepdims=True)
    ref = (ref / norms).astype(np.float32)
    follower = OnlineDTWFollower(ref, cfg, search_width=20)

    confidences = []
    for j in range(80):
        result = follower.process_frame(ref[:, j])
        confidences.append(result.confidence)

    # After the smoothing window settles, confidence should be high.
    settled = confidences[20:]
    assert min(settled) > 0.7, f"low confidence: min={min(settled):.2f}"


def test_freeze_holds_position_before_lockin():
    """lock-in 前の freeze は位置固定 (legacy 挙動)。

    冒頭ノイズで誤発火しないようにするため、曲開始を捉えるまで
    (lock-in 未確立の間) は silence gate による freeze で位置を固定する。
    """
    cfg = FeatureConfig()
    ref = _make_chroma_sequence(60)
    follower = OnlineDTWFollower(ref, cfg, search_width=20)

    for j in range(10):
        follower.process_frame(ref[:, j])

    assert not follower.is_locked_in, (
        "10 frames of cyclic chroma should not have established lock-in"
    )

    frozen_pos = follower.current_ref_frame
    follower.freeze()
    for j in range(10, 20):
        r = follower.process_frame(ref[:, j])
        assert r.ref_frame == frozen_pos, (
            f"pre-lock-in freeze should hold position, got {r.ref_frame}"
        )
        assert r.confidence == 0.0

    follower.unfreeze()
    for j in range(20, 40):
        follower.process_frame(ref[:, j])
    assert follower.current_ref_frame >= frozen_pos


def test_freeze_continues_inertia_after_lockin():
    """lock-in 後の freeze は慣性進行: 位置が止まらず動き続ける。

    シナリオ: random unit chroma (test_self_alignment_high_confidence
    と同じ前提) で 高 confidence を 30 frame 維持 → 自動 lock-in。
    その後 freeze → confidence=0 のまま位置が前進し続ける。
    """
    cfg = FeatureConfig()
    rng = np.random.default_rng(7)
    ref = rng.standard_normal((12, 200)).astype(np.float32)
    ref = np.abs(ref)
    norms = np.linalg.norm(ref, axis=0, keepdims=True)
    ref = (ref / norms).astype(np.float32)

    # Use the natural defaults: lock_in_frames=30, lock_in_confidence=0.45
    follower = OnlineDTWFollower(
        ref, cfg, search_width=20, init_search_width=10,
    )

    # Drive 80 self-aligned frames so lock-in latches and history fills.
    for j in range(80):
        follower.process_frame(ref[:, j])

    assert follower.is_locked_in, (
        f"expected lock-in after 80 high-conf frames; "
        f"current_ref_frame={follower.current_ref_frame}"
    )

    frozen_pos = follower.current_ref_frame
    follower.freeze()
    assert follower.is_in_inertia, "freeze() post-lock-in must enter inertia"

    # 20 frozen frames → inertia should advance position.
    for _ in range(20):
        r = follower.process_frame(np.zeros(12, dtype=np.float32))  # input ignored while frozen
        assert r.confidence == 0.0, "inertia must report confidence=0"

    new_pos = follower.current_ref_frame
    advance = new_pos - frozen_pos
    # At rate ~1.0 (self-aligned), 20 frames → ~20 ref frames forward.
    # Allow generous tolerance because the rate is estimated from finite history.
    assert advance > 10, (
        f"inertia did not advance: frozen_pos={frozen_pos} → {new_pos} "
        f"(advance={advance}, expected > 10)"
    )
    assert advance <= 25, (
        f"inertia advanced too far: advance={advance} (expected ~20, max ~25)"
    )


def test_inertia_capped_by_max_seconds():
    """慣性は max_inertia_seconds で打ち止め、それ以降は位置固定。"""
    cfg = FeatureConfig()
    rng = np.random.default_rng(11)
    ref = rng.standard_normal((12, 500)).astype(np.float32)
    ref = np.abs(ref)
    ref = (ref / np.linalg.norm(ref, axis=0, keepdims=True)).astype(np.float32)

    # Tight cap so test runs quickly: 1 second = ~10 frames at default rate.
    follower = OnlineDTWFollower(
        ref, cfg, search_width=20, init_search_width=10,
        max_inertia_seconds=1.0,
    )

    # Establish lock-in.
    for j in range(80):
        follower.process_frame(ref[:, j])
    assert follower.is_locked_in

    frozen_pos = follower.current_ref_frame
    follower.freeze()

    # Drive 60 frames during freeze. Cap is ~10 frames; rest should be
    # held at the capped position.
    positions = []
    for _ in range(60):
        r = follower.process_frame(np.zeros(12, dtype=np.float32))
        positions.append(r.ref_frame)

    # The last position should be stationary for many frames (cap engaged).
    last_n = positions[-20:]
    assert max(last_n) - min(last_n) == 0, (
        f"position should be stationary after cap; last 20={last_n}"
    )
    # The capped position should not have advanced more than the cap allows.
    cap_frames = int(round(1.0 * cfg.effective_frame_rate()))
    advance = positions[-1] - frozen_pos
    assert advance <= cap_frames + 1, (
        f"advance {advance} exceeds cap {cap_frames} (frozen at {frozen_pos}, "
        f"capped at {positions[-1]})"
    )


def test_inertia_persists_after_unfreeze_until_dp_resync():
    """unfreeze() 直後は inertia を即座に解除せず、DP-resync を待つ。

    シナリオ:
      1. 自己整列で lock-in 確立
      2. freeze() で inertia 開始
      3. unfreeze() — _frozen=False だが _inertia_active=True のまま
      4. 正しい chroma を流すと _process_subsequent_frame の DP が
         走り、_maybe_resync_from_dp が発火して inertia を抜ける
    """
    cfg = FeatureConfig()
    rng = np.random.default_rng(41)
    ref = rng.standard_normal((12, 200)).astype(np.float32)
    ref = np.abs(ref)
    ref = (ref / np.linalg.norm(ref, axis=0, keepdims=True)).astype(np.float32)

    follower = OnlineDTWFollower(
        ref, cfg, search_width=30, init_search_width=10,
        inertia_exit_frames=3,
    )
    for j in range(80):
        follower.process_frame(ref[:, j])
    assert follower.is_locked_in

    follower.freeze()
    assert follower.is_in_inertia
    # Run inertia for some frames (input ignored while frozen).
    for _ in range(15):
        follower.process_frame(np.zeros(12, dtype=np.float32))

    follower.unfreeze()
    # Inertia must still be active immediately after unfreeze.
    assert follower.is_in_inertia, (
        "unfreeze() should not immediately exit inertia — DP resync handles it"
    )

    # Feed correct chroma. DP runs and _maybe_resync_from_dp fires
    # once high_conf_streak >= inertia_exit_frames.
    dp_pos = follower._current_ref_pos
    for j in range(dp_pos, min(dp_pos + 30, ref.shape[1])):
        follower.process_frame(ref[:, j])

    assert not follower.is_in_inertia, (
        "DP recovery did not exit inertia after correct chroma resumed"
    )


def test_inertia_does_not_global_rematch(monkeypatch):
    """慣性中は _try_global_rematch が呼ばれないことを spy で検証。

    回帰防止: 慣性中に global rematch が走ると、distant self-similar
    位置へのテレポートで慣性追従が破壊される。本テストは freeze() →
    unfreeze() 後の慣性継続中に DP が走るシナリオで rematch 抑制を
    確認する。

    実テストでは confidence smoothing の影響で resync が早期発火し
    inertia がすぐ抜けてしまうため、_maybe_resync_from_dp を mock
    して常に False を返すように固定し、inertia 状態を maintainal。
    """
    cfg = FeatureConfig()
    rng = np.random.default_rng(53)
    ref = rng.standard_normal((12, 200)).astype(np.float32)
    ref = np.abs(ref)
    ref = (ref / np.linalg.norm(ref, axis=0, keepdims=True)).astype(np.float32)

    follower = OnlineDTWFollower(
        ref, cfg, search_width=30, init_search_width=10,
        stuck_rematch_seconds=0.5,  # 5 frames — would normally fire
        stuck_rematch_min_advance=3,
        stuck_rematch_cost_margin=0.01,
        stuck_rematch_min_discriminability_ratio=0.0,
    )
    for j in range(80):
        follower.process_frame(ref[:, j])
    assert follower.is_locked_in

    follower.freeze()
    follower.unfreeze()
    assert follower.is_in_inertia

    # Force resync to never fire so inertia stays active throughout.
    monkeypatch.setattr(follower, "_maybe_resync_from_dp", lambda *_a, **_kw: False)

    # Spy on _try_global_rematch.
    calls = []
    original = follower._try_global_rematch

    def spy(live, cost):
        calls.append(cost)
        return original(live, cost)
    monkeypatch.setattr(follower, "_try_global_rematch", spy)

    # Feed mixed input — some self-aligned (would normally let DP
    # advance) and some zeros (stuck). Either way rematch must NOT
    # fire during inertia.
    for j in range(30):
        # Feed the same frame repeatedly to make DP appear stuck —
        # the exact stuck_rematch trigger condition.
        follower.process_frame(ref[:, 50])

    assert follower.is_in_inertia, "inertia state should persist throughout"
    assert not calls, (
        f"_try_global_rematch called {len(calls)} times during inertia"
    )


def test_force_lock_in_immediate():
    """force_lock_in() で即座に lock-in 状態になる (GUI ボタン用 API)。"""
    cfg = FeatureConfig()
    ref = _make_chroma_sequence(60)
    follower = OnlineDTWFollower(ref, cfg, search_width=20)

    assert not follower.is_locked_in
    follower.force_lock_in()
    assert follower.is_locked_in

    # Idempotent: second call is a no-op
    follower.force_lock_in()
    assert follower.is_locked_in


def test_force_lock_in_enables_inertia_on_freeze():
    """force_lock_in() 後の freeze は inertia mode に入る。

    指揮者の振り出しに合わせてオペレータが「楽章開始」ボタンを
    押した直後、まだ自動 lock-in は確立していなくても、強制 lock-in
    で慣性経路が即座に有効化されることを確認。
    """
    cfg = FeatureConfig()
    rng = np.random.default_rng(23)
    ref = rng.standard_normal((12, 100)).astype(np.float32)
    ref = np.abs(ref)
    ref = (ref / np.linalg.norm(ref, axis=0, keepdims=True)).astype(np.float32)

    follower = OnlineDTWFollower(
        ref, cfg, search_width=20, init_search_width=10,
    )

    # Drive a few frames so history begins to fill.
    for j in range(20):
        follower.process_frame(ref[:, j])

    # Force lock-in (operator action).
    follower.force_lock_in()
    assert follower.is_locked_in

    follower.freeze()
    assert follower.is_in_inertia, (
        "force_lock_in() + freeze() should enter inertia mode"
    )


def test_seek_jumps_to_target_and_resumes_dp():
    """seek(ref_frame) は位置を指定値に飛ばし、DP 状態をリセットする。

    Used by manual ← / → keyboard overrides in main.py to tell OLTW
    "the music is here now, restart tracking from this frame".
    """
    cfg = FeatureConfig()
    ref = _make_chroma_sequence(80)
    follower = OnlineDTWFollower(ref, cfg, search_width=20, init_search_width=10)
    # advance a bit so cumulative state builds up
    for j in range(10):
        follower.process_frame(ref[:, j])
    pos_before = follower.current_ref_frame
    assert pos_before > 0

    # jump forward to frame 50
    follower.seek(50)
    assert follower.current_ref_frame == 50
    # cumulative cost should be wiped except at the target
    finite_count = int(np.isfinite(follower._D_prev).sum())
    assert finite_count == 1, (
        f"seek did not wipe D_prev: {finite_count} finite cells"
    )

    # next process_frame should stay near 50 (live still matches frame 50
    # which is the same chroma class we just seeded).
    r = follower.process_frame(ref[:, 50])
    assert r.ref_frame >= 50, (
        f"after seek to 50, expected position >= 50; got {r.ref_frame}"
    )


def test_seek_with_catchup_finds_real_position_ahead():
    """seek + post-seek catchup: 操作者が早すぎる位置に seek した場合、
    次の live frame で実際の演奏位置に飛び直す。

    シナリオ: 演奏は ref frame 50 にいるが、操作者は trigger m=17
    相当の frame 30 にしか seek できない (trigger 配置の制約)。
    catchup ありなら、次フレームで chroma が一致する frame 50 を
    band 内検索して再 anchor する。
    """
    cfg = FeatureConfig()
    rng = np.random.default_rng(23)
    ref = _make_chroma_sequence(120)
    # Build a discriminative ref: each 10-frame block uses a distinct
    # pitch class so live-vs-ref chroma at any other position is high.
    ref = np.zeros((12, 120), dtype=np.float32)
    for j in range(120):
        ref[(j // 10) % 12, j] = 1.0
    ref += 0.02 * rng.standard_normal(ref.shape).astype(np.float32)
    ref = (ref / np.linalg.norm(ref, axis=0, keepdims=True)).astype(np.float32)

    follower = OnlineDTWFollower(
        ref, cfg,
        search_width=60, back_inhibit_frames=20, init_search_width=10,
        step_penalty=0.06, max_advance_per_frame=50,
        stuck_dp_reset_seconds=0.0, stuck_rematch_seconds=0.0,
    )
    # Step the follower a few frames so it's "warm"
    for j in range(5):
        follower.process_frame(ref[:, j])

    # seek too-early to frame 30 (with catchup armed)
    follower.seek(30, allow_catchup=True)
    assert follower.current_ref_frame == 30

    # Feed a live frame whose chroma matches frame 50, NOT frame 30
    r = follower.process_frame(ref[:, 50])

    # post-seek catchup should have jumped us forward toward 50
    assert r.ref_frame >= 45, (
        f"post-seek catchup did not advance: ref_frame={r.ref_frame} "
        f"(expected ≥45, seeked to 30, live matches 50)"
    )


def test_seek_without_catchup_stays_put():
    """allow_catchup=False で post-seek catchup が走らないことを確認。

    ← (back-step) 用途: 操作者が「演奏はもっと手前」と言っているので、
    自動 forward scan は本末転倒。
    """
    cfg = FeatureConfig()
    ref = _make_chroma_sequence(120)
    rng = np.random.default_rng(29)
    ref = np.zeros((12, 120), dtype=np.float32)
    for j in range(120):
        ref[(j // 10) % 12, j] = 1.0
    ref += 0.02 * rng.standard_normal(ref.shape).astype(np.float32)
    ref = (ref / np.linalg.norm(ref, axis=0, keepdims=True)).astype(np.float32)

    follower = OnlineDTWFollower(
        ref, cfg, search_width=60, back_inhibit_frames=20,
        init_search_width=10, step_penalty=0.06,
        stuck_dp_reset_seconds=0.0, stuck_rematch_seconds=0.0,
    )
    for j in range(5):
        follower.process_frame(ref[:, j])
    follower.seek(30, allow_catchup=False)
    # Live frame matches frame 50 but we don't want to jump there.
    r = follower.process_frame(ref[:, 50])
    # No catchup → DP should advance at most 1 frame (or stay).
    assert r.ref_frame <= 35, (
        f"allow_catchup=False but position jumped forward to {r.ref_frame}"
    )


def test_seek_clamps_out_of_range():
    """seek to a negative or past-end frame clamps to the valid range."""
    cfg = FeatureConfig()
    ref = _make_chroma_sequence(40)
    follower = OnlineDTWFollower(ref, cfg, search_width=10)
    follower.seek(-5)
    assert follower.current_ref_frame == 0
    follower.seek(10**9)
    assert follower.current_ref_frame == 39  # N - 1


def test_reset_clears_state():
    cfg = FeatureConfig()
    ref = _make_chroma_sequence(40)
    follower = OnlineDTWFollower(ref, cfg, search_width=10)
    for j in range(20):
        follower.process_frame(ref[:, j])
    assert follower.current_ref_frame > 0
    follower.reset()
    assert follower.current_ref_frame == 0


def test_rejects_wrong_shape_reference():
    cfg = FeatureConfig()
    with pytest.raises(ValueError, match="must be"):
        OnlineDTWFollower(np.zeros((13, 50), dtype=np.float32), cfg)
    with pytest.raises(ValueError, match="must be"):
        OnlineDTWFollower(np.zeros((12,), dtype=np.float32), cfg)


def test_rejects_step_size_greater_than_one():
    cfg = FeatureConfig()
    ref = _make_chroma_sequence(40)
    with pytest.raises(ValueError, match="step_size"):
        OnlineDTWFollower(ref, cfg, step_size=2)


def test_rejects_bad_live_frame_shape():
    cfg = FeatureConfig()
    ref = _make_chroma_sequence(40)
    follower = OnlineDTWFollower(ref, cfg, search_width=10)
    with pytest.raises(ValueError, match="must be"):
        follower.process_frame(np.zeros(13, dtype=np.float32))


def test_advances_with_smoothed_similar_frames():
    """連続 live frame が極めて似ている場合でも DP は前進する。

    実機バグの回帰防止: CENS の cens_win=41 平滑化により live frame の
    chroma が緩慢にしか変化しない状況で、step_penalty + 前進 tie-break
    が無いと DP は現位置に貼り付く。50 frame chunks で chroma class を
    切り替え、chunk 内は微小ノイズのみという平滑化済みチロマの簡易
    モデルで、1 live ≒ 1 ref の進行が維持されることを確認する。
    """
    cfg = FeatureConfig()
    rng = np.random.default_rng(42)
    n = 400
    ref = np.zeros((12, n), dtype=np.float32)
    for j in range(n):
        pc = (j // 50) % 12
        ref[pc, j] = 1.0
        ref[:, j] += 0.05 * rng.standard_normal(12).astype(np.float32)
    norms = np.linalg.norm(ref, axis=0, keepdims=True)
    ref = (ref / norms).astype(np.float32)

    follower = OnlineDTWFollower(ref, cfg, search_width=100)
    positions = []
    for j in range(n):
        result = follower.process_frame(ref[:, j])
        positions.append(result.ref_frame)

    advancement = positions[-1] - positions[0]
    assert advancement >= 200, (
        f"position stuck: {positions[0]} → {positions[-1]} "
        f"(expected advancement >= 200, got {advancement}). "
        f"last 10 positions: {positions[-10:]}"
    )


def test_step_penalty_zero_disables_forward_bias():
    """step_penalty=0.0 で旧挙動 (前進バイアスなし) に戻ることを確認。

    後方互換テスト: step_penalty を 0 にすると tie-break が前進方向に
    残るが、DP のペナルティ自体は消える。ambiguous なシナリオでは
    旧挙動と同程度の動きになるはず。
    """
    cfg = FeatureConfig()
    ref = _make_chroma_sequence(60, period=4)
    follower = OnlineDTWFollower(ref, cfg, search_width=15, step_penalty=0.0)

    # Should not raise; should produce monotonic positions.
    positions = []
    for j in range(60):
        result = follower.process_frame(ref[:, j])
        positions.append(result.ref_frame)
    diffs = np.diff(positions)
    assert (diffs >= 0).all(), f"non-monotonic with step_penalty=0: diffs={diffs}"


def test_stuck_dp_reset_fires_on_backward_lockin(caplog):
    """累積コストが後退を強く欲したとき stuck_dp_reset がログ発火する。

    DP reset の正しい挙動: 後退試行が連発しつつ前進ゼロが続いたら、
    INFO ログ "OLTW DP reset" が出る。位置移動の保証はしない (それは
    後続フレームの DP 任せ)。
    """
    import logging
    cfg = FeatureConfig()
    ref = _make_chroma_sequence(80)
    follower = OnlineDTWFollower(
        ref, cfg,
        search_width=40, back_inhibit_frames=20, init_search_width=10,
        step_penalty=0.06,
        stuck_dp_reset_seconds=1.0,  # ~10 frames at default rate
        stuck_rematch_seconds=0.0,
    )
    # Advance a few frames legitimately.
    for j in range(15):
        follower.process_frame(ref[:, j])
    # Now feed an earlier frame repeatedly — DP will want to step
    # backward every frame, satisfying both stuck conditions.
    with caplog.at_level(logging.INFO, logger="audio_score_follower.core.oltw_follower"):
        for _ in range(40):
            follower.process_frame(ref[:, 5])
    assert any("DP reset" in rec.message for rec in caplog.records), (
        f"DP reset never fired despite sustained backward lock-in. "
        f"log records: {[r.message for r in caplog.records[-10:]]}"
    )


def test_rapid_dp_reset_fires_before_full_window(caplog):
    """Rapid reset triggers within 10 consecutive backward frames, well
    before the full stuck_dp_reset_seconds window elapses.

    回帰防止: 後退アトラクタが 10 フレーム連続で続いた時点でフルウィンドウ
    （~54 フレーム）より早く "rapid DP reset" が発火すること。

    実際の障害パターン再現: ref の中間地点で chroma が A→B に急変し、
    直後に A クロマを投入すると「1 つ前の位置 (A) が今の位置 (B) より
    低コスト」という後退アトラクタが発生する。
    これは m=117 の raw_cost 0.06→0.20 急騰と同じ構造。
    """
    import logging
    cfg = FeatureConfig()

    # Build reference: positions 0-19 = pitch class 0 (chroma A),
    # positions 20-59 = pitch class 6 (chroma B, maximally different).
    n = 60
    ref = np.zeros((12, n), dtype=np.float32)
    ref[0, :20] = 1.0   # A
    ref[6, 20:] = 1.0   # B
    norms = np.linalg.norm(ref, axis=0, keepdims=True)
    norms[norms == 0] = 1.0
    ref = (ref / norms).astype(np.float32)

    follower = OnlineDTWFollower(
        ref, cfg,
        search_width=40, back_inhibit_frames=20, init_search_width=10,
        step_penalty=0.06,
        stuck_dp_reset_seconds=5.0,   # full window ≈ 54 frames at 10.77 Hz
        stuck_rematch_seconds=0.0,
    )
    # Track through the A region (perfect match, low local costs).
    for j in range(20):
        follower.process_frame(ref[:, j])
    # Feed one B frame — DP advances into position 20 (ref[:,20]=B, low cost).
    follower.process_frame(ref[:, 20])
    pos_before = follower._current_ref_pos  # should be ≥ 20

    # Now feed chroma A repeatedly. Local cost at pos 20 (=B) is ~1.0
    # (high), while local cost at pos 19 (=A) is ~0. D_prev[19] is fresh
    # (set just two frames ago), so the unclamped argmin prefers 19 →
    # backward attempt every frame → _consecutive_backward_frames grows.
    chroma_A = ref[:, 0].copy()
    rapid_reset_frame: int | None = None
    with caplog.at_level(logging.INFO, logger="audio_score_follower.core.oltw_follower"):
        for k in range(15):
            follower.process_frame(chroma_A)
            if any("rapid DP reset" in rec.message for rec in caplog.records):
                rapid_reset_frame = k + 1
                break

    assert rapid_reset_frame is not None, (
        "Rapid DP reset never fired despite every-frame backward lock-in. "
        f"log: {[r.message for r in caplog.records[-10:]]}"
    )
    # Must fire well before the full 54-frame window.
    assert rapid_reset_frame <= 12, (
        f"Rapid reset took {rapid_reset_frame} backward frames "
        f"(expected ≤ 12, i.e. _RAPID_RESET_FRAMES=10 plus margin)"
    )
    # After rapid reset, D_prev[<current] = inf, so DP can only advance
    # forward. Feeding a B frame should not retreat below pos_before.
    result_after = follower.process_frame(ref[:, min(pos_before + 1, n - 1)])
    assert result_after.ref_frame >= pos_before, (
        f"Position retreated after rapid reset: "
        f"before={pos_before}, after={result_after.ref_frame}"
    )


def test_max_advance_per_frame_caps_dp_race_ahead():
    """band-DP の vert chain による前方暴走を物理的に防ぐ。

    回帰防止: 自己類似テーマがある曲で、band 内の遠い前方位置に低コスト
    マッチがあると、vert chain が累積して argmin が遠い前方を選んでしまう
    "1 live frame で N 小節先にジャンプ" の失敗モード。
    max_advance_per_frame は argmin の探索範囲そのものを縮めることでこの
    挙動を構造的に不可能にする (band は広いままで前後文脈は保持)。
    """
    cfg = FeatureConfig()
    rng = np.random.default_rng(13)

    # ref: pc=0 (50) + pc=4 (50) + pc=0 (50, テーマA再現)
    def _pc(n, pc):
        x = np.zeros((12, n), dtype=np.float32); x[pc, :] = 1.0; return x
    ref = np.concatenate([_pc(50, 0), _pc(50, 4), _pc(50, 0)], axis=1)
    ref += 0.02 * rng.standard_normal(ref.shape).astype(np.float32)
    ref = (ref / np.linalg.norm(ref, axis=0, keepdims=True)).astype(np.float32)

    # live: pc=0 を継続(=テーマAを continually 再生) → 旧挙動なら frame 100+
    # (テーマA再現の位置) に DP がジャンプしうる。cap が効けば前進量制限される。
    live = ref[:, :30].copy() + 0.05 * rng.standard_normal((12, 30)).astype(np.float32)
    live = (live / np.linalg.norm(live, axis=0, keepdims=True)).astype(np.float32)

    cap = 5
    follower = OnlineDTWFollower(
        ref, cfg,
        search_width=120, back_inhibit_frames=10, init_search_width=10,
        step_penalty=0.06, max_advance_per_frame=cap,
        stuck_rematch_seconds=0.0,  # rematch off: isolate band-DP behavior
    )
    positions = [0]
    for j in range(30):
        r = follower.process_frame(live[:, j])
        positions.append(r.ref_frame)
    diffs = np.diff(positions)
    assert diffs.max() <= cap, (
        f"max_advance_per_frame={cap} violated: max diff={diffs.max()}, diffs={diffs.tolist()}"
    )


def test_stuck_rematch_escapes_wrong_initial_lock():
    """初期フレームで誤った位置にロックされても、stuck-rematch で前方ジャンプ。

    シナリオ:
      - ref[0..50]   = テーマA (pitch class 0)
      - ref[50..150] = 別の部分 (pitch class 4)
      - ref[150..200]= テーマC (pitch class 7)
      - live は ref[150..200] と一致する chroma を継続的に送る
      - init_search_width=30 では live の初フレームは [0,30) しか見られず、
        誤って pitch class 0 (テーマA) の位置にロック
      - 通常 DP では累積コスト障壁を越えられず stuck
      - stuck-rematch が前方の真の位置 (frame 150 付近) を見つけて jump
    """
    cfg = FeatureConfig()
    rng = np.random.default_rng(11)

    def _pc(n, pc):
        x = np.zeros((12, n), dtype=np.float32)
        x[pc, :] = 1.0
        return x

    ref = np.concatenate([_pc(50, 0), _pc(100, 4), _pc(50, 7)], axis=1)
    ref += 0.02 * rng.standard_normal(ref.shape).astype(np.float32)
    ref = (ref / np.linalg.norm(ref, axis=0, keepdims=True)).astype(np.float32)

    # live は ref[150:200] (pitch class 7) と同じ
    live_template = ref[:, 150:200].copy()
    live = np.tile(live_template, (1, 4))  # 200 frames

    # init_search_width=10 にして、live の初 chroma (pc=7) を ref[0:10] と
    # マッチさせる → 誤ロック発生
    follower = OnlineDTWFollower(
        ref, cfg,
        search_width=120, back_inhibit_frames=30, init_search_width=10,
        stuck_rematch_seconds=1.0,  # 早めに発火させる
        stuck_rematch_min_advance=3,
        stuck_rematch_cost_margin=0.05,
        stuck_rematch_min_jump_frames=30,
    )
    positions = []
    for j in range(live.shape[1]):
        r = follower.process_frame(live[:, j])
        positions.append(r.ref_frame)

    # 末尾までに stuck-rematch で frame 150 付近に到達しているはず
    final_pos = positions[-1]
    assert final_pos >= 130, (
        f"stuck-rematch failed to escape: final_pos={final_pos}, "
        f"last 10 positions={positions[-10:]}"
    )


def test_stuck_rematch_disabled_when_seconds_zero():
    """stuck_rematch_seconds=0 で再 match 機構が無効化されることを確認。"""
    cfg = FeatureConfig()
    ref = _make_chroma_sequence(60)
    follower = OnlineDTWFollower(
        ref, cfg, search_width=20, stuck_rematch_seconds=0.0
    )
    for j in range(60):
        follower.process_frame(ref[:, j])
    # No assertion needed beyond "doesn't crash"; the disabled path is
    # exercised every frame.


def test_back_inhibit_prevents_self_similar_capture():
    """自己類似テーマがあっても、過去テーマに引き戻されないことを確認。

    回帰防止: Marche au supplice 別演奏の追従テストで、行進曲テーマの
    2 回目に DP が 1 回目の位置に飛び戻る (`would step backward (102→68)`
    連発) → stuck になる現象の最小再現。

    シナリオ:
      - ref[0..50]   = テーマ A (pitch class 0)
      - ref[50..100] = 別の部分 (pitch class 6)
      - ref[100..150] = テーマ A の再現 (pitch class 0)
      - live は ref[100..150] と「ほぼ同じだが微妙に違う」chroma
    back_inhibit=10 なら、live が ref[100..150] を流れたとき、DP は
    ref[0..50] (テーマ A の 1 回目) に戻れない → 前進し続ける。
    """
    cfg = FeatureConfig()
    rng = np.random.default_rng(7)

    def _theme_a(n):
        x = np.zeros((12, n), dtype=np.float32)
        x[0, :] = 1.0
        return x

    def _theme_b(n):
        x = np.zeros((12, n), dtype=np.float32)
        x[6, :] = 1.0
        return x

    ref = np.concatenate([_theme_a(50), _theme_b(50), _theme_a(50)], axis=1)
    ref += 0.02 * rng.standard_normal(ref.shape).astype(np.float32)
    ref = (ref / np.linalg.norm(ref, axis=0, keepdims=True)).astype(np.float32)

    # live = ref[100:150] with a small perturbation (a "different recording"
    # of the second theme-A statement). We seed the follower at frame 100
    # by feeding the first live frame and letting the wide search_width
    # initialise there; then the back-inhibit must keep it from collapsing
    # back to frames 0..50.
    live = ref[:, 100:150].copy()
    live += 0.05 * rng.standard_normal(live.shape).astype(np.float32)
    live = (live / np.linalg.norm(live, axis=0, keepdims=True)).astype(np.float32)

    # Wide search_width so symmetric band could reach back to frame 0,
    # but tight back_inhibit keeps it forward.
    follower = OnlineDTWFollower(
        ref, cfg, search_width=120, back_inhibit_frames=10
    )

    positions = []
    for j in range(50):
        result = follower.process_frame(live[:, j])
        positions.append(result.ref_frame)

    # After the first few frames we should have locked somewhere past
    # frame 80 (i.e. inside the second theme-A statement, not the first).
    assert min(positions[5:]) >= 80, (
        f"back-inhibit failed: positions dropped into first theme. "
        f"min(positions[5:])={min(positions[5:])}, last 10={positions[-10:]}"
    )


def test_back_inhibit_zero_forbids_all_backward_motion():
    """back_inhibit_frames=0 で band の左端が前フレーム位置に張り付く。

    band_lo はフレーム開始時点の current_ref_pos から計算され、ref_frame
    は DP 後の新位置 (>= 開始位置) なので、不変条件は band_lo <= ref_frame
    かつ band_lo の単調非減少。
    """
    cfg = FeatureConfig()
    ref = _make_chroma_sequence(60)
    follower = OnlineDTWFollower(
        ref, cfg, search_width=30, back_inhibit_frames=0
    )

    prev_lo = 0
    for j in range(60):
        result = follower.process_frame(ref[:, j])
        assert result.band_lo <= result.ref_frame, (
            f"frame {j}: band_lo={result.band_lo} > ref_frame={result.ref_frame}"
        )
        assert result.band_lo >= prev_lo, (
            f"frame {j}: band_lo={result.band_lo} < prev_lo={prev_lo}"
        )
        prev_lo = result.band_lo


def test_compute_inertia_rate_returns_fallback_when_history_short():
    """履歴が 5 件未満の時は fallback 1.0 を返す。"""
    cfg = FeatureConfig()
    ref = _make_chroma_sequence(60)
    f = OnlineDTWFollower(ref, cfg, search_width=20)
    # No history at all
    assert f._compute_inertia_rate() == 1.0
    # Seed 4 entries (under the 5-required threshold)
    for j in range(4):
        f._pos_history.append((j, j))
    assert f._compute_inertia_rate() == 1.0


def test_compute_inertia_rate_diagonal_history_yields_unit_rate():
    """live/ref が 1 対 1 で進んだ履歴は rate=1.0 になる。"""
    cfg = FeatureConfig()
    ref = _make_chroma_sequence(60)
    f = OnlineDTWFollower(ref, cfg, search_width=20)
    for j in range(20):
        f._pos_history.append((j, j))
    assert abs(f._compute_inertia_rate() - 1.0) < 1e-6


def test_compute_inertia_rate_estimates_half_speed():
    """ref が live の半分の速度で進む履歴は rate≈0.5。"""
    cfg = FeatureConfig()
    ref = _make_chroma_sequence(60)
    f = OnlineDTWFollower(ref, cfg, search_width=20)
    for j in range(20):
        # live frame j → ref pos j // 2 (half speed)
        f._pos_history.append((j, j // 2))
    rate = f._compute_inertia_rate()
    # 19 live frames span (0→9) ref → rate = 9/19 ≈ 0.474
    assert 0.4 < rate < 0.55, f"expected ~0.5, got {rate}"


def test_compute_inertia_rate_clamps_extreme_slow():
    """異常に遅い rate (<0.3) はクランプされる。"""
    cfg = FeatureConfig()
    ref = _make_chroma_sequence(60)
    f = OnlineDTWFollower(ref, cfg, search_width=20)
    # live advances 20 frames, ref barely moves (1 frame)
    for j in range(20):
        f._pos_history.append((j, j // 20))
    assert f._compute_inertia_rate() == 0.3


def test_compute_inertia_rate_clamps_extreme_fast():
    """異常に速い rate (>2.0) はクランプされる。"""
    cfg = FeatureConfig()
    ref = _make_chroma_sequence(200)
    f = OnlineDTWFollower(ref, cfg, search_width=20)
    # live advances 10 frames, ref advances 100 (10x rate)
    for j in range(10):
        f._pos_history.append((j, j * 10))
    assert f._compute_inertia_rate() == 2.0


def test_offset_input_tracks_with_lag():
    """If live starts at reference frame K, the follower should land near K."""
    cfg = FeatureConfig()
    ref = _make_chroma_sequence(120)
    follower = OnlineDTWFollower(ref, cfg, search_width=50)

    # Feed live = ref[:, 30:90] (an aligned excerpt starting mid-piece)
    for j in range(30, 90):
        result = follower.process_frame(ref[:, j])

    # First call to process_frame starts the search at frame 0 with
    # search_width=50, so the initial frame can lock onto ref[30].
    # By the end we should be near ref[89].
    assert result.ref_frame >= 80, (
        f"expected near 89, got {result.ref_frame}"
    )
