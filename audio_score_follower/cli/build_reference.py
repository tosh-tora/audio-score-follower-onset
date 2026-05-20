#!/usr/bin/env python3
"""
asf-build — offline reference builder CLI.

Synthesises the score MusicXML via MuseScore 4 CLI (see
tasks/generate_score_wav.py), then runs MrMsDTW to align the synthesis
against a real performance recording. Output is a directory containing
``warping_path.npz``, ``reference_cens.npy``, and a JSON metadata
sidecar. Runs entirely on Windows native — no WSL2 needed.

Usage:

    asf-build \\
        --score data/scores/beethoven5.xml \\
        --reference data/reference_audio/karajan_1977.wav \\
        --output data/built/beethoven5_karajan \\
        [--score-bpm 120] \\
        [--start-offset 0.5] \\
        [--plot]

If ``--score-wav`` is given, it is used directly instead of synthesising
from the XML. Useful for re-runs with different DTW parameters when the
synth is unchanged.
"""

from __future__ import annotations

import argparse
import logging
import subprocess
import sys
import tempfile
from pathlib import Path

# Windows console defaults to cp932 / cp1252, which cannot encode the
# em-dash and Japanese characters we use in argparse help text. Forcing
# UTF-8 here is harmless on POSIX and necessary on Windows.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from audio_score_follower.core.feature_extractor import FeatureConfig
from audio_score_follower.core.reference_builder import build_reference

logger = logging.getLogger(__name__)


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
    logger.info("Synthesising score: %s", " ".join(cmd))
    completed = subprocess.run(
        cmd, capture_output=True, text=True, encoding="utf-8", errors="replace"
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"generate_score_wav.py failed (exit {completed.returncode}):\n"
            f"  stdout: {completed.stdout}\n"
            f"  stderr: {completed.stderr}\n"
            f"  NOTE: requires MuseScore 4 installed. Set MSCORE_EXE env "
            f"var or pass --mscore-exe if auto-detection fails."
        )
    logger.info("Score synth complete: %s", tmp_path)
    return tmp_path


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--score", required=True, type=Path,
                        help="MusicXML / MXL file")
    parser.add_argument("--reference", required=True, type=Path,
                        help="Reference recording (WAV / FLAC / OGG / MP3 / M4A)")
    parser.add_argument("--output", required=True, type=Path,
                        help="Output directory")
    parser.add_argument(
        "--score-wav", type=Path, default=None,
        help="Pre-synthesised score WAV. If omitted, generate_score_wav.py "
             "is invoked.",
    )
    parser.add_argument(
        "--score-bpm", type=float, default=120.0,
        help="BPM used by the score synth. Must match --score-wav if "
             "given. Default 120.",
    )
    parser.add_argument(
        "--start-offset", type=float, default=0.0,
        help="Seconds to trim from the head of --reference (e.g. drop "
             "conductor breath). Default 0.",
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

    score_wav = args.score_wav
    cleanup_tmp = False
    if score_wav is None:
        try:
            score_wav = _synth_score_wav(args.score, args.score_bpm, args.sample_rate)
            cleanup_tmp = True
        except Exception as exc:
            logger.error("Failed to synthesise score WAV: %s", exc)
            return 1
    elif not score_wav.exists():
        logger.error("--score-wav not found: %s", score_wav)
        return 1

    try:
        result = build_reference(
            score_wav=score_wav,
            reference_wav=args.reference,
            output_dir=args.output,
            score_bpm=args.score_bpm,
            feature_config=cfg,
            reference_start_offset_sec=args.start_offset,
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
    finally:
        if cleanup_tmp and score_wav is not None and score_wav.exists():
            try:
                score_wav.unlink()
            except OSError:
                logger.debug("Could not remove temp synth WAV: %s", score_wav)

    return 0


if __name__ == "__main__":
    sys.exit(main())
