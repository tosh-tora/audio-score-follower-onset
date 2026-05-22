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
