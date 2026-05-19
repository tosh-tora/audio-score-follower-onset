"""Smoke test: build a tiny reference end-to-end from synthetic audio.

Verifies that the offline pipeline runs without errors and produces a
warp path whose endpoints make sense. Skipped if synctoolbox isn't
installed (since the algorithm is the actual unit under test).
"""

from __future__ import annotations

import numpy as np
import pytest


def _make_synth_audio(sr: int, duration_sec: float, freq: float = 440.0) -> np.ndarray:
    """A simple sinusoid + a touch of noise — enough for CENS to produce
    a single-pitch-class chroma sequence."""
    t = np.linspace(0, duration_sec, int(sr * duration_sec), endpoint=False, dtype=np.float32)
    rng = np.random.default_rng(0)
    return (0.5 * np.sin(2 * np.pi * freq * t) + 0.01 * rng.standard_normal(len(t))).astype(np.float32)


def test_build_reference_smoke(tmp_path):
    pytest.importorskip("librosa")
    pytest.importorskip("synctoolbox")
    from scipy.io import wavfile  # type: ignore
    from audio_score_follower.core.feature_extractor import FeatureConfig
    from audio_score_follower.core.reference_builder import build_reference

    sr = 22050
    cfg = FeatureConfig(sample_rate=sr, hop_length=2048)

    # Score "synth": 4 seconds of 440 Hz.
    score_audio = _make_synth_audio(sr, 4.0, 440.0)
    score_path = tmp_path / "score.wav"
    wavfile.write(str(score_path), sr, (score_audio * 32000).astype(np.int16))

    # "Reference recording": same content, but with 1 second of silence
    # before and after — to simulate a real recording with some intro.
    silence = np.zeros(sr, dtype=np.float32)
    ref_audio = np.concatenate([silence, score_audio, silence])
    ref_path = tmp_path / "ref.wav"
    wavfile.write(str(ref_path), sr, (ref_audio * 32000).astype(np.int16))

    out_dir = tmp_path / "built"
    result = build_reference(
        score_wav=score_path,
        reference_wav=ref_path,
        output_dir=out_dir,
        score_bpm=120.0,
        feature_config=cfg,
        reference_start_offset_sec=0.0,
        plot=False,
    )

    # Artifacts exist
    assert (out_dir / "warping_path.npz").exists()
    assert (out_dir / "reference_cens.npy").exists()
    assert (out_dir / "build_meta.json").exists()

    # Warp path covers (roughly) the full reference duration.
    assert result.ref_times[-1] >= 4.0
    assert result.score_times[-1] >= 3.0
