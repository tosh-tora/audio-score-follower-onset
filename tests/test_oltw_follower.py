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


def test_freeze_stops_advancement():
    cfg = FeatureConfig()
    ref = _make_chroma_sequence(60)
    follower = OnlineDTWFollower(ref, cfg, search_width=20)

    for j in range(10):
        follower.process_frame(ref[:, j])

    frozen_pos = follower.current_ref_frame
    follower.freeze()
    for j in range(10, 20):
        r = follower.process_frame(ref[:, j])
        assert r.ref_frame == frozen_pos
        assert r.confidence == 0.0

    follower.unfreeze()
    for j in range(20, 40):
        follower.process_frame(ref[:, j])
    assert follower.current_ref_frame >= frozen_pos


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
