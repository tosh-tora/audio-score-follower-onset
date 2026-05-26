"""Unit tests for feature_extractor."""

from __future__ import annotations

import numpy as np
import pytest

from audio_score_follower.core.feature_extractor import (
    AudioFeatures,
    FeatureConfig,
    OnsetNormalizer,
    compute_cens,
    compute_onset,
    cosine_cost_matrix,
    fused_local_cost,
    normalize_onset_global,
)


def test_config_roundtrip():
    cfg = FeatureConfig(sample_rate=44100, hop_length=1024, cens_win=21, norm=1.5)
    d = cfg.to_dict()
    cfg2 = FeatureConfig.from_dict(d)
    assert cfg2.sample_rate == 44100
    assert cfg2.hop_length == 1024
    assert cfg2.cens_win == 21
    assert cfg2.norm == 1.5


def test_config_default_frame_rate():
    cfg = FeatureConfig(sample_rate=22050, hop_length=2048)
    assert cfg.effective_frame_rate() == pytest.approx(22050 / 2048)


def test_cens_shape_and_normalisation():
    """compute_cens must return (12, n_frames) with per-column L2 = 1."""
    pytest.importorskip("librosa")
    cfg = FeatureConfig()
    # 2 seconds of a 440 Hz tone — chroma should peak on the 'A' pitch class.
    sr = cfg.sample_rate
    t = np.linspace(0, 2.0, 2 * sr, endpoint=False, dtype=np.float32)
    audio = 0.5 * np.sin(2 * np.pi * 440.0 * t).astype(np.float32)
    cens = compute_cens(audio, cfg)
    assert cens.shape[0] == 12
    assert cens.shape[1] > 0
    # Norms should be ~1.0 per column (small numerical drift OK)
    norms = np.linalg.norm(cens, axis=0)
    assert np.all(np.abs(norms - 1.0) < 1e-3) or np.all(norms == 0.0), \
        f"unexpected norms: min={norms.min()}, max={norms.max()}"


def test_cens_rejects_multichannel():
    cfg = FeatureConfig()
    bad = np.zeros((2, 1000), dtype=np.float32)
    with pytest.raises(ValueError, match="1D mono"):
        compute_cens(bad, cfg)


def test_cosine_cost_matrix_self_zero():
    """A sequence aligned with itself has zero cost on the diagonal."""
    a = np.zeros((12, 5), dtype=np.float32)
    for j in range(5):
        a[j, j] = 1.0  # one-hot, distinct per column
    cost = cosine_cost_matrix(a, a)
    assert cost.shape == (5, 5)
    np.testing.assert_allclose(np.diag(cost), 0.0, atol=1e-6)
    # Off-diagonal must be 1.0 (orthogonal one-hots).
    assert cost[0, 1] == pytest.approx(1.0, abs=1e-6)


def test_cosine_cost_matrix_dimension_check():
    a = np.zeros((11, 3), dtype=np.float32)
    b = np.zeros((12, 3), dtype=np.float32)
    with pytest.raises(ValueError, match="12-d"):
        cosine_cost_matrix(a, b)


# ================================================================ onset tests

def test_onset_shape_matches_cens():
    """compute_onset must return the same number of frames as compute_cens."""
    pytest.importorskip("librosa")
    cfg = FeatureConfig()
    sr = cfg.sample_rate
    # 3 seconds of audio — long enough for reliable frame alignment.
    t = np.linspace(0, 3.0, 3 * sr, endpoint=False, dtype=np.float32)
    audio = 0.5 * np.sin(2 * np.pi * 440.0 * t).astype(np.float32)
    cens = compute_cens(audio, cfg)
    onset = compute_onset(audio, cfg)
    assert onset.ndim == 1
    # Allow ±1 frame difference (librosa STFT framing edge).
    assert abs(onset.shape[0] - cens.shape[1]) <= 1, (
        f"onset={onset.shape[0]}, cens={cens.shape[1]}"
    )


def test_onset_silence_no_nan():
    """compute_onset on a silent signal must not produce NaN."""
    pytest.importorskip("librosa")
    cfg = FeatureConfig()
    audio = np.zeros(cfg.sample_rate * 2, dtype=np.float32)
    onset = compute_onset(audio, cfg)
    assert np.all(np.isfinite(onset)), "NaN/Inf in onset for silent audio"


def test_onset_short_audio_no_crash():
    """compute_onset on very short audio (< 1 hop) must not crash."""
    pytest.importorskip("librosa")
    cfg = FeatureConfig()
    audio = np.zeros(cfg.hop_length // 2, dtype=np.float32)
    onset = compute_onset(audio, cfg)  # should return array (possibly empty)
    assert onset.ndim == 1
    assert np.all(np.isfinite(onset))


def test_normalize_onset_global():
    onset = np.array([0.0, 0.5, 1.0, 0.25], dtype=np.float32)
    normed = normalize_onset_global(onset)
    assert float(normed.max()) == pytest.approx(1.0, abs=1e-5)
    assert float(normed.min()) >= 0.0


def test_normalize_onset_global_empty():
    onset = np.array([], dtype=np.float32)
    normed = normalize_onset_global(onset)
    assert normed.shape == (0,)


def test_onset_normalizer_rolling_max():
    norm = OnsetNormalizer(window_frames=4)
    vals = [0.1, 0.4, 0.2, 0.8, 0.5]
    results = [norm.normalize(v) for v in vals]
    # All outputs must be in [0, 1].
    for r in results:
        assert 0.0 <= r <= 1.0 + 1e-6, f"out of range: {r}"
    # After the peak (0.8), the following 0.5 should give ~0.5/0.8.
    assert results[4] == pytest.approx(0.5 / (0.8 + 1e-8), abs=1e-5)


def test_onset_normalizer_reset():
    norm = OnsetNormalizer(window_frames=10)
    norm.normalize(100.0)  # seed with large value
    norm.reset()
    result = norm.normalize(1.0)
    assert result == pytest.approx(1.0 / (1.0 + 1e-8), abs=1e-5)


# ================================================================ fused_local_cost tests

def test_fused_local_cost_cens_only_no_onset():
    """With onset=None, fused_local_cost == cosine cost (raw, no weight applied)."""
    rng = np.random.default_rng(42)
    ref_block = rng.standard_normal((12, 8)).astype(np.float32)
    norms = np.linalg.norm(ref_block, axis=0, keepdims=True)
    ref_block = (ref_block / (norms + 1e-8)).astype(np.float32)
    live = rng.standard_normal(12).astype(np.float32)
    live = (live / (np.linalg.norm(live) + 1e-8)).astype(np.float32)

    expected = (1.0 - ref_block.T @ live).astype(np.float32)
    got = fused_local_cost(ref_block, live, None, None, 0.7, 0.3)
    np.testing.assert_allclose(got, expected, atol=1e-5)


def test_fused_local_cost_fusion_active():
    """With fusion active, cost is weighted sum of chroma + onset."""
    rng = np.random.default_rng(7)
    ref_block = rng.standard_normal((12, 5)).astype(np.float32)
    norms = np.linalg.norm(ref_block, axis=0, keepdims=True)
    ref_block = (ref_block / (norms + 1e-8)).astype(np.float32)
    live = rng.standard_normal(12).astype(np.float32)
    live = (live / (np.linalg.norm(live) + 1e-8)).astype(np.float32)

    ref_onset = np.array([0.1, 0.5, 0.9, 0.3, 0.7], dtype=np.float32)
    live_onset = 0.4

    chroma_cost = 1.0 - ref_block.T @ live
    onset_cost = np.abs(ref_onset - live_onset)
    expected = (0.7 * chroma_cost + 0.3 * onset_cost).astype(np.float32)

    got = fused_local_cost(ref_block, live, ref_onset, live_onset, 0.7, 0.3)
    np.testing.assert_allclose(got, expected, atol=1e-5)


def test_audio_features_aligned_truncate():
    cens = np.zeros((12, 10), dtype=np.float32)
    onset = np.zeros(11, dtype=np.float32)  # one extra frame
    af = AudioFeatures(cens=cens, onset=onset).aligned_truncate()
    assert af.cens.shape[1] == 10
    assert af.onset is not None and af.onset.shape[0] == 10
