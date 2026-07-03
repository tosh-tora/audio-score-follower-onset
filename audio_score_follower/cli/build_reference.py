#!/usr/bin/env python3
"""
asf-build — offline reference builder CLI.

Synthesises the score MusicXML via MuseScore 4 CLI (see
tasks/generate_score_wav.py), then runs MrMsDTW to align the synthesis
against a real performance recording. Output is a directory containing
``warping_path.npz``, ``reference_cens.npy``, and a JSON metadata
sidecar. Runs entirely on Windows native — no WSL2 needed.

Usage (filename-only shorthand — paths are resolved automatically):

    asf-build \\
        --score beethoven5.xml \\
        --reference karajan_1977.wav \\
        --output beethoven5_karajan \\
        [--score-bpm 120] \\
        [--start-offset 0.5] \\
        [--plot]

When a plain filename (no directory component) is given, each argument
resolves to a fixed location relative to the current working directory:

    --score     →  <cwd>/data/scores/<filename>
    --reference →  <cwd>/data/reference_audio/<filename>
    --output    →  <cwd>/data/built/<name>

If a path with a directory component or an absolute path is given it is
used as-is, so existing invocations are unchanged.

If ``--score-wav`` is given, it is used directly instead of synthesising
from the XML. Useful for re-runs with different DTW parameters when the
synth is unchanged.
"""

from __future__ import annotations

import argparse
import logging
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Optional

# Windows console defaults to cp932 / cp1252, which cannot encode the
# em-dash and Japanese characters we use in argparse help text. Forcing
# UTF-8 here is harmless on POSIX and necessary on Windows.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from audio_score_follower.core.feature_extractor import FeatureConfig
from audio_score_follower.core.reference_builder import build_reference
from audio_score_follower.core.score_mapper import ScoreMapper
from audio_score_follower.core.warp_lookup import WarpLookup

logger = logging.getLogger(__name__)

# Default data directories, resolved relative to the current working directory.
# When a plain filename (no directory component) is passed to --score,
# --reference, or --output, the corresponding default dir is prepended so
# users can just type the filename. Paths that already have a directory
# component (e.g. "my_dir/score.mxl" or an absolute path) are not modified.
_DEFAULT_SCORE_DIR = Path("data") / "scores"
_DEFAULT_REFERENCE_DIR = Path("data") / "reference_audio"
_DEFAULT_OUTPUT_DIR = Path("data") / "built"


def _resolve_data_path(given: Path, default_dir: Path) -> Path:
    """Return the resolved path for a CLI argument.

    If ``given`` is absolute or contains a directory component (i.e. its
    ``.parent`` is not the current directory sentinel ``Path(".")``), return
    it unchanged.  Otherwise, prepend ``default_dir`` so the caller can pass
    a bare filename such as ``"score.mxl"`` instead of the full
    ``"data/scores/score.mxl"``.
    """
    if given.is_absolute() or given.parent != Path("."):
        return given
    return default_dir / given


def _find_fluidsynth() -> Optional[Path]:
    """Locate fluidsynth.exe in the parent process where env vars are reliable."""
    # Project-local vendor directory (most reliable — no sandbox/virtualization issues)
    repo_root = Path(__file__).resolve().parents[2]
    vendor_dir = repo_root / "vendor" / "FluidSynth"
    if vendor_dir.exists():
        for p in sorted(vendor_dir.glob("*/bin/fluidsynth.exe")):
            if p.exists():
                return p

    env = os.environ.get("FLUIDSYNTH_EXE")
    if env:
        p = Path(env)
        if p.exists():
            return p
        logger.warning("FLUIDSYNTH_EXE=%s が見つからない", env)

    which = shutil.which("fluidsynth")
    if which:
        return Path(which)

    # Search LocalAppData via multiple methods — env vars can be unreliable
    # depending on how the process was spawned.
    local_appdata_candidates: list[Path] = []
    local_appdata_candidates.append(Path.home() / "AppData" / "Local")
    userprofile = os.environ.get("USERPROFILE", "")
    if userprofile:
        local_appdata_candidates.append(Path(userprofile) / "AppData" / "Local")
    # Walk up from sys.executable to find AppData\Local (e.g. user-installed Python)
    for parent in Path(sys.executable).resolve().parents:
        if parent.name.lower() == "local" and parent.parent.name.lower() == "appdata":
            local_appdata_candidates.append(parent)
            break

    for local_appdata in local_appdata_candidates:
        fluidsynth_dir = local_appdata / "FluidSynth"
        logger.debug("FluidSynth 検索: %s (exists=%s)", fluidsynth_dir, fluidsynth_dir.exists())
        for p in sorted(fluidsynth_dir.glob("*/bin/fluidsynth.exe")):
            if p.exists():
                return p

    for cand in [
        Path(r"C:\Program Files\FluidSynth\bin\fluidsynth.exe"),
        Path(r"C:\ProgramData\chocolatey\bin\fluidsynth.exe"),
    ]:
        if cand.exists():
            return cand

    return None


def _find_sf_file() -> Optional[Path]:
    """Locate a SF2/SF3 soundfont in the parent process where env vars are reliable."""
    env = os.environ.get("SF_FILE")
    if env and Path(env).exists():
        return Path(env)
    for cand in [
        Path(r"C:\Program Files\MuseScore 4\sound\MS Basic.sf3"),
        Path(r"C:\Program Files\MuseScore 3\sound\MuseScore_General.sf3"),
        Path(r"C:\Program Files\MuseScore 3\sound\MuseScore_General.sf2"),
    ]:
        if cand.exists():
            return cand
    return None


def _synth_score_wav(score_xml: Path, bpm: float, sample_rate: int) -> Path:
    """Invoke tasks/generate_score_wav.py to produce a temporary synth WAV.

    The script lives in this project root; we call it via the same
    Python interpreter the CLI is running under.
    """
    repo_root = Path(__file__).resolve().parents[2]
    script = repo_root / "tasks" / "generate_score_wav.py"
    if not script.exists():
        raise FileNotFoundError(
            f"generate_score_wav.py not found at {script}. "
            "Did you change the project layout?"
        )

    tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
    tmp.close()
    tmp_path = Path(tmp.name)

    cmd = [
        sys.executable, str(script),
        str(score_xml),
        "-o", str(tmp_path),
        "--bpm", str(bpm),
        "--samplerate", str(sample_rate),
    ]
    # Detect FluidSynth and SF in the parent process (env vars are reliable here)
    # and pass them explicitly so the subprocess doesn't need to re-detect.
    fluidsynth = _find_fluidsynth()
    if fluidsynth:
        cmd += ["--fluidsynth-exe", str(fluidsynth)]
        logger.info("FluidSynth: %s", fluidsynth)
    sf_file = _find_sf_file()
    if sf_file:
        cmd += ["--sf-file", str(sf_file)]
        logger.info("SoundFont: %s", sf_file)

    logger.info("Synthesising score: %s", " ".join(cmd))
    completed = subprocess.run(
        cmd, capture_output=True, text=True, encoding="utf-8", errors="replace"
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"generate_score_wav.py failed (exit {completed.returncode}):\n"
            f"  stdout: {completed.stdout}\n"
            f"  stderr: {completed.stderr}\n"
            f"  NOTE: requires FluidSynth. Set FLUIDSYNTH_EXE env var "
            f"to the fluidsynth.exe path, and SF_FILE to a SF2/SF3 soundfont."
        )
    logger.info("Score synth complete: %s", tmp_path)
    return tmp_path


_MIN_REF_DURATION_SEC = 5.0
_BPM_SANITY_RANGE = (20.0, 400.0)
# Tail-silence auto-detection: trailing audio quieter than the file's
# peak minus this margin is considered "after the music". Only trims
# when the detected tail exceeds the minimum, so normal release/reverb
# tails are left alone.
_TAIL_SILENCE_TOP_DB = 45.0
_TAIL_SILENCE_MIN_SEC = 1.5


def _probe_reference_duration(path: Path) -> float:
    """Return the reference recording's duration in seconds without full decode.

    Uses librosa.get_duration(path=...), which dispatches to soundfile for
    PCM formats and audioread for MP3/M4A — same code path the rest of the
    builder uses, so any format the builder can actually load will probe
    here too.
    """
    import librosa  # type: ignore

    return float(librosa.get_duration(path=str(path)))


def _detect_tail_silence_sec(path: Path, sample_rate: int) -> float:
    """Return the duration (sec) of trailing silence in the recording.

    Rationale: YouTube rips and live recordings often carry several
    seconds of near-silence (or hall noise) after the final chord. If
    that tail is kept, two things go wrong: (1) the auto BPM estimate
    divides the score's beats by an inflated duration, and (2) MrMsDTW
    maps the score's final measures onto the silent tail, so the
    runtime follower tops out a few measures short of the end — the
    reference CENS there matches nothing (実測: 幻想4 で末尾 8s の無音
    により m=173/178 で頭打ち).

    Uses librosa.effects.trim's dB-below-peak criterion on the tail
    only (the head is governed by --start-offset).
    """
    import librosa  # type: ignore

    audio, _ = librosa.load(str(path), sr=sample_rate, mono=True)
    _trimmed, index = librosa.effects.trim(audio, top_db=_TAIL_SILENCE_TOP_DB)
    return max(0.0, (len(audio) - int(index[1])) / float(sample_rate))


def _estimate_score_bpm(total_beats: float, ref_duration_sec: float) -> float:
    """Estimate the quarter-note BPM that makes the score synth align with
    the reference recording's duration.

    Formula: ``bpm = total_beats * 60 / ref_duration_sec``. Assumes
    ``total_beats`` is in quarter-note units, which is what
    ``ScoreMapper.get_total_beats()`` returns.
    """
    if total_beats <= 0:
        raise ValueError(
            f"Score reports total_beats={total_beats}; cannot estimate BPM."
        )
    if ref_duration_sec < _MIN_REF_DURATION_SEC:
        raise ValueError(
            f"Reference duration {ref_duration_sec:.2f}s is below the "
            f"minimum {_MIN_REF_DURATION_SEC}s for tempo estimation."
        )
    return total_beats * 60.0 / ref_duration_sec


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--score", required=True, type=Path,
        help="MusicXML / MXL file. Plain filename (no directory) is resolved "
             "under data/scores/ in the current working directory.",
    )
    parser.add_argument(
        "--reference", required=True, type=Path,
        help="Reference recording (WAV / FLAC / OGG / MP3 / M4A). "
             "Plain filename is resolved under data/reference_audio/.",
    )
    parser.add_argument(
        "--output", required=True, type=Path,
        help="Output directory. Plain name (no directory) is resolved under "
             "data/built/.",
    )
    parser.add_argument(
        "--score-wav", type=Path, default=None,
        help="Pre-synthesised score WAV. If omitted, generate_score_wav.py "
             "is invoked.",
    )
    parser.add_argument(
        "--score-bpm", type=float, default=None,
        help="Quarter-note BPM used by the score synth. If omitted, "
             "estimated automatically from the reference recording's "
             "duration and the score's total beat count "
             "(total_beats * 60 / ref_duration). REQUIRED when "
             "--score-wav is given, since synth tempo cannot be "
             "inferred from a pre-made WAV.",
    )
    parser.add_argument(
        "--start-offset", type=float, default=0.0,
        help="Seconds to trim from the head of --reference (e.g. drop "
             "conductor breath). Default 0.",
    )
    parser.add_argument(
        "--end-trim", type=float, default=None,
        help="Seconds to trim from the TAIL of --reference (trailing "
             "silence / applause after the music). If omitted, trailing "
             "silence is auto-detected and trimmed when longer than "
             f"{_TAIL_SILENCE_MIN_SEC}s. Pass 0 to disable trimming.",
    )
    parser.add_argument(
        "--sample-rate", type=int, default=22050,
        help="Sample rate for feature extraction (must match runtime). "
             "Default 22050.",
    )
    parser.add_argument(
        "--hop-length", type=int, default=2048,
        help="Hop length in samples for CENS. Default 2048 (~93ms@22050).",
    )
    parser.add_argument(
        "--plot", action="store_true",
        help="Write warp_path.png into --output (requires matplotlib).",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true",
        help="DEBUG-level logging",
    )

    args = parser.parse_args()

    # Resolve plain filenames to their default data directories so the user
    # can type just the filename instead of the full path.
    args.score = _resolve_data_path(args.score, _DEFAULT_SCORE_DIR)
    args.reference = _resolve_data_path(args.reference, _DEFAULT_REFERENCE_DIR)
    args.output = _resolve_data_path(args.output, _DEFAULT_OUTPUT_DIR)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    # synctoolbox / librosa drag in numba which floods -v output with
    # JIT bytecode dumps. Pin those loggers to INFO so we can read our
    # own DEBUG messages.
    for noisy in ("numba", "matplotlib", "PIL", "fontTools",
                  "music21", "libfmp"):
        logging.getLogger(noisy).setLevel(logging.INFO)

    if not args.score.exists():
        logger.error("Score file not found: %s", args.score)
        return 1
    if not args.reference.exists():
        logger.error("Reference recording not found: %s", args.reference)
        return 1

    cfg = FeatureConfig(
        sample_rate=args.sample_rate,
        hop_length=args.hop_length,
    )

    # Parse the score once up front: we need total_beats for BPM
    # estimation and the same ScoreMapper instance for validation
    # below.
    try:
        score_mapper = ScoreMapper(str(args.score))
    except Exception as exc:  # noqa: BLE001
        logger.exception("Failed to load score: %s", exc)
        return 1

    # --- Resolve reference tail trim ---
    # Trailing silence/applause must be excluded BEFORE BPM estimation:
    # it both inflates the estimated duration (slower synth tempo) and
    # makes MrMsDTW map the final measures onto silence, capping the
    # runtime follower a few measures short of the end.
    if args.end_trim is not None:
        end_trim = max(0.0, float(args.end_trim))
    else:
        try:
            tail = _detect_tail_silence_sec(args.reference, args.sample_rate)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Tail-silence detection failed (%s); building without "
                "end trim. Pass --end-trim explicitly if needed.", exc
            )
            tail = 0.0
        end_trim = tail if tail >= _TAIL_SILENCE_MIN_SEC else 0.0
        if end_trim > 0:
            logger.info(
                "Auto-detected %.2fs of trailing silence in reference — "
                "trimming (override with --end-trim).", end_trim,
            )

    # --- Resolve synth BPM ---
    score_wav = args.score_wav
    cleanup_tmp = False
    resolved_bpm: float

    if score_wav is not None:
        if not score_wav.exists():
            logger.error("--score-wav not found: %s", score_wav)
            return 1
        if args.score_bpm is None:
            logger.error(
                "--score-bpm is required when --score-wav is given "
                "(synth tempo cannot be inferred from a pre-made WAV)."
            )
            return 1
        resolved_bpm = float(args.score_bpm)
    else:
        if args.score_bpm is None:
            try:
                ref_dur = _probe_reference_duration(args.reference)
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "Could not probe reference duration: %s "
                    "Pass --score-bpm explicitly to skip estimation.", exc
                )
                return 1
            effective_ref_dur = ref_dur - max(args.start_offset, 0.0) - end_trim
            total_beats = score_mapper.get_total_beats()
            try:
                resolved_bpm = _estimate_score_bpm(total_beats, effective_ref_dur)
            except ValueError as exc:
                logger.error("Cannot estimate BPM: %s "
                             "Pass --score-bpm explicitly.", exc)
                return 1
            lo, hi = _BPM_SANITY_RANGE
            if not (lo <= resolved_bpm <= hi):
                logger.error(
                    "Estimated BPM %.2f is outside the sanity range "
                    "[%.0f, %.0f] (total_beats=%.1f, ref_dur=%.2fs). "
                    "Check that the score and reference correspond to "
                    "the same movement, or pass --score-bpm explicitly.",
                    resolved_bpm, lo, hi, total_beats, effective_ref_dur,
                )
                return 1
            logger.info(
                "Estimated synth tempo: %.1f beats / %.2fs ref → BPM=%.2f "
                "(quarter-note). Override with --score-bpm if undesirable.",
                total_beats, effective_ref_dur, resolved_bpm,
            )
        else:
            resolved_bpm = float(args.score_bpm)

        try:
            score_wav = _synth_score_wav(args.score, resolved_bpm, args.sample_rate)
            cleanup_tmp = True
        except Exception as exc:
            logger.error("Failed to synthesise score WAV: %s", exc)
            return 1

    try:
        result = build_reference(
            score_wav=score_wav,
            reference_wav=args.reference,
            output_dir=args.output,
            score_bpm=resolved_bpm,
            feature_config=cfg,
            reference_start_offset_sec=args.start_offset,
            reference_end_trim_sec=end_trim,
            plot=args.plot,
        )
        logger.info(
            "Build complete: warp path length=%d, ref_dur=%.1fs, score_dur=%.1fs",
            len(result.ref_times),
            float(result.ref_times[-1]) if len(result.ref_times) else 0.0,
            float(result.score_times[-1]) if len(result.score_times) else 0.0,
        )
    except Exception as exc:
        logger.exception("Build failed: %s", exc)
        return 1

    # --- Validate warp path against score ---
    logger.info("Validating warp path against score …")
    try:
        warp = WarpLookup.load(args.output)
        warp.validate(score_mapper)
        logger.info("Warp path validation passed.")
    except ValueError as exc:
        logger.error(
            "Warp path validation FAILED:\n  %s\n"
            "ビルド成果物は保存されましたが、このまま使うと追随が外れます。\n"
            "参照音源とスコアの構造を確認して asf-build をやり直してください。",
            exc,
        )
        return 1
    except Exception as exc:  # noqa: BLE001
        logger.exception("Validation error (score parse / load failed): %s", exc)
        return 1
    finally:
        if cleanup_tmp and score_wav is not None and score_wav.exists():
            try:
                score_wav.unlink()
            except OSError:
                logger.debug("Could not remove temp synth WAV: %s", score_wav)

    return 0


if __name__ == "__main__":
    sys.exit(main())
