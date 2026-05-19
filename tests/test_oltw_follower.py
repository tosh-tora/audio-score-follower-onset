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
    """Self-alignment should yield confidence close to 1."""
    cfg = FeatureConfig()
    ref = _make_chroma_sequence(80)
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
