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
    assert (out_dir / "reference_onset.npy").exists()
    assert (out_dir / "build_meta.json").exists()

    # Warp path covers (roughly) the full reference duration.
    assert result.ref_times[-1] >= 4.0
    assert result.score_times[-1] >= 3.0

    # Onset artifact: correct shape, values in [0, 1].
    assert result.reference_onset is not None
    assert result.reference_onset.ndim == 1
    assert result.reference_onset.shape[0] == result.reference_cens.shape[1]
    assert float(result.reference_onset.max()) <= 1.0 + 1e-5
    assert float(result.reference_onset.min()) >= 0.0

    # build_meta.json has onset flag.
    import json
    meta = json.loads((out_dir / "build_meta.json").read_text(encoding="utf-8"))
    assert meta.get("has_onset") is True


def test_build_reference_end_trim(tmp_path):
    """reference_end_trim_sec cuts the reference tail before alignment.

    末尾無音を残すと warp が最終小節を無音尾部にマップして runtime が
    最後まで到達できない (実測: 幻想4 で m=173/178 頭打ち)。トリム後の
    warp path は参照の実音楽区間内で終わることを確認する。
    """
    pytest.importorskip("librosa")
    pytest.importorskip("synctoolbox")
    from scipy.io import wavfile  # type: ignore
    from audio_score_follower.core.feature_extractor import FeatureConfig
    from audio_score_follower.core.reference_builder import build_reference

    sr = 22050
    cfg = FeatureConfig(sample_rate=sr, hop_length=2048)

    score_audio = _make_synth_audio(sr, 4.0, 440.0)
    score_path = tmp_path / "score.wav"
    wavfile.write(str(score_path), sr, (score_audio * 32000).astype(np.int16))

    # Reference: music followed by 3 seconds of trailing silence.
    silence = np.zeros(3 * sr, dtype=np.float32)
    ref_audio = np.concatenate([score_audio, silence])
    ref_path = tmp_path / "ref.wav"
    wavfile.write(str(ref_path), sr, (ref_audio * 32000).astype(np.int16))

    out_dir = tmp_path / "built_trim"
    result = build_reference(
        score_wav=score_path,
        reference_wav=ref_path,
        output_dir=out_dir,
        score_bpm=120.0,
        feature_config=cfg,
        reference_end_trim_sec=3.0,
        plot=False,
    )

    # Warp path must end within the trimmed (4s) reference, not the
    # original 7s file.
    assert result.ref_times[-1] <= 4.5, (
        f"warp extends into the trimmed tail: {result.ref_times[-1]:.2f}s"
    )

    import json
    meta = json.loads((out_dir / "build_meta.json").read_text(encoding="utf-8"))
    assert meta.get("reference_end_trim_sec") == pytest.approx(3.0)

    # Trim longer than the file must raise.
    with pytest.raises(ValueError, match="end_trim"):
        build_reference(
            score_wav=score_path,
            reference_wav=ref_path,
            output_dir=tmp_path / "built_bad",
            score_bpm=120.0,
            feature_config=cfg,
            reference_end_trim_sec=100.0,
            plot=False,
        )
