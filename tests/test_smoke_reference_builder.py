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


def _make_note_sequence(sr: int, note_durations_sec: list[float]) -> np.ndarray:
    """Ascending diatonic note sequence (one sine per note).

    Chroma varies over time, so DTW can localize — a single sustained
    tone has constant chroma and any warp path is equally cheap.
    """
    # C4, D4, E4, F4, G4, A4, B4, C5 — well inside synctoolbox's
    # MIDI 21-108 pitch-feature band.
    freqs = [261.63, 293.66, 329.63, 349.23, 392.00, 440.00, 493.88, 523.25]
    assert len(note_durations_sec) == len(freqs)
    parts = []
    for freq, dur in zip(freqs, note_durations_sec):
        parts.append(_make_synth_audio(sr, dur, freq))
    return np.concatenate(parts)


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


def test_build_reference_recovers_known_tempo_warp(tmp_path):
    """Issue #32 regression guard: the warp path must capture tempo変化.

    旧実装はランタイム CENS (10.77 Hz, 重平滑) を sync_via_mrmsdtw に
    渡しており、multiscale smoothing が全レベルで過平滑になって warp が
    「平均テンポの対角線」に退化していた (幻想4 実測: 2947 ステップ中
    2937 が完全対角)。既知の区分的タイムストレッチを復元できることを
    確認する — 退化した対角パスはここで必ず落ちる。
    """
    pytest.importorskip("librosa")
    pytest.importorskip("synctoolbox")
    from scipy.io import wavfile  # type: ignore
    from audio_score_follower.core.feature_extractor import FeatureConfig
    from audio_score_follower.core.reference_builder import build_reference

    sr = 22050
    cfg = FeatureConfig(sample_rate=sr, hop_length=2048)

    # Score: 8 notes × 0.5 s = 4.0 s at "constant tempo".
    score_durs = [0.5] * 8
    score_audio = _make_note_sequence(sr, score_durs)
    score_path = tmp_path / "score.wav"
    wavfile.write(str(score_path), sr, (score_audio * 32000).astype(np.int16))

    # Reference: same notes, first half slower (0.75 s/note), second
    # half faster (0.375 s/note) — total 4.5 s, known piecewise warp.
    ref_durs = [0.75] * 4 + [0.375] * 4
    ref_audio = _make_note_sequence(sr, ref_durs)
    ref_path = tmp_path / "ref.wav"
    wavfile.write(str(ref_path), sr, (ref_audio * 32000).astype(np.int16))

    out_dir = tmp_path / "built_warp"
    result = build_reference(
        score_wav=score_path,
        reference_wav=ref_path,
        output_dir=out_dir,
        score_bpm=120.0,
        feature_config=cfg,
        plot=False,
    )

    # Analytic ground truth from the note boundaries: ref_t → score_t.
    score_bounds = np.cumsum([0.0] + score_durs)
    ref_bounds = np.cumsum([0.0] + ref_durs)

    def gt_score_time(ref_t: float) -> float:
        return float(np.interp(ref_t, ref_bounds, score_bounds))

    # Probe at mid-note reference positions (skip first/last notes:
    # endpoint snapping makes them trivially correct even when degenerate).
    probes = [(ref_bounds[i] + ref_bounds[i + 1]) / 2 for i in range(1, 7)]
    for ref_t in probes:
        got = float(np.interp(ref_t, result.ref_times, result.score_times))
        expect = gt_score_time(ref_t)
        assert abs(got - expect) < 0.3, (
            f"warp path inaccurate at ref_t={ref_t:.3f}s: "
            f"got score_t={got:.3f}, expected {expect:.3f}"
        )

    # Anti-degeneracy: a diagonal-at-global-ratio path must NOT fit.
    ratio = result.score_times[-1] / max(result.ref_times[-1], 1e-9)
    max_dev = float(np.max(np.abs(
        result.score_times - result.ref_times * ratio
    )))
    assert max_dev > 0.3, (
        f"warp path is (near-)degenerate diagonal: max deviation "
        f"{max_dev:.3f}s from the global-ratio line"
    )


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


def test_detect_start_offset_sec_clean_start_returns_zero(tmp_path):
    """A reference whose head already matches the score must not be trimmed.

    detect_start_offset_sec must not manufacture a trim when there is no
    confident junk→music ramp (contrast below _HEAD_DETECT_CONTRAST_MIN).
    """
    pytest.importorskip("librosa")
    from scipy.io import wavfile  # type: ignore
    from audio_score_follower.core.reference_builder import detect_start_offset_sec

    sr = 22050
    notes = _make_note_sequence(sr, [0.5] * 8)  # 4s ascending sequence
    score_path = tmp_path / "score.wav"
    wavfile.write(str(score_path), sr, (notes * 32000).astype(np.int16))
    ref_path = tmp_path / "ref_clean.wav"
    wavfile.write(str(ref_path), sr, (notes * 32000).astype(np.int16))

    offset = detect_start_offset_sec(score_path, ref_path, sample_rate=sr)
    assert offset == pytest.approx(0.0, abs=0.05)


def test_detect_start_offset_sec_tonal_head_noise_detected_conservatively(tmp_path):
    """A sustained tuning-A-like tone before the music must be caught.

    Energy-only silence detection is blind to this (the tone is not
    quiet); comparing against the score synthesis separates it. The
    detector must never OVER-trim into the true music start — prototype
    validation (see reference_builder.detect_start_offset_sec docstring)
    showed every tonal-junk scenario resolves to an under-trim, never a
    cut into the music, so we only assert offset stays within
    [0, true_offset] with a small safety margin.
    """
    pytest.importorskip("librosa")
    from scipy.io import wavfile  # type: ignore
    from audio_score_follower.core.reference_builder import detect_start_offset_sec

    sr = 22050
    notes = _make_note_sequence(sr, [0.5] * 8)
    score_path = tmp_path / "score.wav"
    wavfile.write(str(score_path), sr, (notes * 32000).astype(np.int16))

    true_offset_sec = 1.2
    rng = np.random.default_rng(0)
    t = np.arange(int(true_offset_sec * sr)) / sr
    tuning_a = (0.12 * np.sin(2 * np.pi * 440.0 * t)).astype(np.float32)
    tuning_a += (0.01 * rng.standard_normal(len(t))).astype(np.float32)
    ref_audio = np.concatenate([tuning_a, notes])
    ref_path = tmp_path / "ref_noisy_head.wav"
    wavfile.write(str(ref_path), sr, (ref_audio * 32000).astype(np.int16))

    offset = detect_start_offset_sec(score_path, ref_path, sample_rate=sr)
    assert offset > 0.3, (
        f"expected the tonal head noise to be detected (got {offset:.2f}s)"
    )
    assert offset <= true_offset_sec + 0.1, (
        f"must never trim past the true music start: "
        f"offset={offset:.2f}s true={true_offset_sec:.2f}s"
    )
