"""Unit tests for feature_extractor."""

from __future__ import annotations

import numpy as np
import pytest

from audio_score_follower.core.feature_extractor import (
    FeatureConfig,
    compute_cens,
    cosine_cost_matrix,
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
