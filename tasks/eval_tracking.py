#!/usr/bin/env python3
"""
eval_tracking.py - headless OLTW tracking evaluation against a built reference.

Drives OnlineDTWFollower.process_frame() directly over an input WAV
(no threads, no realtime pacing, no Tk) and reports tracking-quality
metrics: coverage, measure-jump statistics, stall statistics, and
per-frame advance smoothness. Optionally dumps a per-frame CSV for
before/after comparison of parameter changes.

Usage:
    python tasks/eval_tracking.py --built-dir data/built/幻想4 \\
        --score data/scores/幻想4_リピート削除.mxl \\
        --input-wav data/recordings/alt_performance.wav \\
        [--csv out.csv] [--start-offset 3.0] \\
        [--oltw-kwargs '{"display_slew_factor": 0}']

The --oltw-kwargs JSON overrides the production defaults
(ConfigLoader.default_oltw_kwargs), enabling A/B sweeps without
editing any config file.
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from audio_score_follower.config.loader import ConfigLoader  # noqa: E402
from audio_score_follower.core.feature_extractor import (  # noqa: E402
    OnsetNormalizer,
    compute_cens,
    compute_onset,
)
from audio_score_follower.core.oltw_follower import OnlineDTWFollower  # noqa: E402
from audio_score_follower.core.result_handler import (  # noqa: E402
    _MAX_FRAME_MEASURE_JUMP as _JUMP_ANOMALY_MEASURES,
)
from audio_score_follower.core.score_mapper import ScoreMapper  # noqa: E402
from audio_score_follower.core.trigger_engine import (  # noqa: E402
    _TRIGGER_CONFIDENCE_FLOOR,
)
from audio_score_follower.core.warp_lookup import (  # noqa: E402
    WarpLookup,
    load_reference_cens,
    load_reference_onset,
)

logger = logging.getLogger("eval_tracking")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    p.add_argument("--built-dir", required=True, type=Path,
                   help="asf-build output directory (warping_path.npz etc.)")
    p.add_argument("--score", required=True, type=Path,
                   help="MusicXML/MXL file (same one used for the build)")
    p.add_argument("--input-wav", required=True, type=Path,
                   help="audio file to track (same or alternate performance)")
    p.add_argument("--csv", type=Path, default=None,
                   help="write per-frame CSV to this path")
    p.add_argument("--start-offset", type=float, default=0.0,
                   help="skip this many seconds at the start of the input")
    p.add_argument("--oltw-kwargs", type=str, default=None,
                   help="JSON dict merged over the default OLTW kwargs")
    p.add_argument("--chroma-weight", type=float, default=0.7)
    p.add_argument("--onset-weight", type=float, default=0.3)
    p.add_argument("--follower", choices=("oltw", "posterior"), default="oltw",
                   help="tracking core to evaluate (default: oltw)")
    p.add_argument("-v", "--verbose", action="store_true")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    for path, label in ((args.built_dir, "--built-dir"),
                        (args.score, "--score"),
                        (args.input_wav, "--input-wav")):
        if not path.exists():
            logger.error("%s not found: %s", label, path)
            return 1

    oltw_kwargs = ConfigLoader.default_oltw_kwargs()
    if args.oltw_kwargs:
        try:
            overrides = json.loads(args.oltw_kwargs)
        except json.JSONDecodeError as exc:
            logger.error("--oltw-kwargs is not valid JSON: %s", exc)
            return 1
        if not isinstance(overrides, dict):
            logger.error("--oltw-kwargs must be a JSON object")
            return 1
        oltw_kwargs.update(overrides)
        logger.info("OLTW overrides: %s", overrides)

    # ---- load artifacts (same path main._load_movement uses) ----------
    mapper = ScoreMapper(str(args.score))
    warp = WarpLookup.load(args.built_dir)
    reference_cens = load_reference_cens(args.built_dir)
    reference_onset = load_reference_onset(args.built_dir)
    warp.validate(mapper)
    cfg = warp.feature_config
    frame_rate = cfg.effective_frame_rate()
    total_measures = mapper.get_total_measures()

    onset_enabled = reference_onset is not None and args.onset_weight > 0.0
    logger.info(
        "Loaded: ref_cens=%s, fusion=%s, frame_rate=%.2f Hz, total_measures=%d",
        reference_cens.shape, "on" if onset_enabled else "off",
        frame_rate, total_measures,
    )

    if args.follower == "posterior":
        from audio_score_follower.core.posterior_follower import (  # noqa: E402
            PosteriorFollower,
        )
        # PosteriorFollower accepts-and-ignores band-DP kwargs, so the
        # same default dict drives both cores unchanged.
        oltw = PosteriorFollower(
            reference_cens=reference_cens,
            feature_config=cfg,
            reference_onset=reference_onset if onset_enabled else None,
            chroma_weight=args.chroma_weight,
            onset_weight=args.onset_weight if onset_enabled else 0.0,
            **oltw_kwargs,
        )
    else:
        oltw = OnlineDTWFollower(
            reference_cens=reference_cens,
            feature_config=cfg,
            reference_onset=reference_onset if onset_enabled else None,
            chroma_weight=args.chroma_weight,
            onset_weight=args.onset_weight if onset_enabled else 0.0,
            **oltw_kwargs,
        )
    logger.info("Follower: %s", args.follower)

    # ---- live features -------------------------------------------------
    import librosa  # heavy import, deferred

    logger.info("Loading audio: %s", args.input_wav)
    audio, _ = librosa.load(str(args.input_wav), sr=cfg.sample_rate, mono=True)
    if args.start_offset > 0:
        audio = audio[int(args.start_offset * cfg.sample_rate):]
    logger.info("Computing CENS (%.1fs of audio) …", len(audio) / cfg.sample_rate)
    cens = compute_cens(audio, cfg)

    onset_all = None
    normalizer = None
    if onset_enabled:
        onset_raw = compute_onset(audio, cfg)
        n_common = min(cens.shape[1], onset_raw.shape[0])
        cens = cens[:, :n_common]
        onset_all = onset_raw[:n_common]
        normalizer = OnsetNormalizer.for_config(cfg)

    n_frames = cens.shape[1]
    logger.info("Feeding %d frames (%.1fs) at max speed …",
                n_frames, n_frames / frame_rate)

    # ---- drive the follower --------------------------------------------
    rows: list[dict] = []
    t0 = time.monotonic()
    for j in range(n_frames):
        live_onset = None
        if onset_all is not None and normalizer is not None:
            live_onset = normalizer.normalize(float(onset_all[j]))
        result = oltw.process_frame(cens[:, j], live_onset)
        measure, _beat_in_measure, _cont_beat = warp.ref_to_measure_and_beat(
            result.ref_time_sec, mapper
        )
        rows.append({
            "live_time_sec": j / frame_rate,
            "ref_frame": result.ref_frame,
            "dp_ref_frame": result.dp_ref_frame,
            "ref_time_sec": result.ref_time_sec,
            "measure": measure,
            "confidence": result.confidence,
            "raw_local_cost": result.raw_local_cost,
            "is_inertia": int(oltw.is_in_inertia),
            "is_locked_in": int(oltw.is_locked_in),
            "is_mismatched": int(result.is_mismatched),
        })
    elapsed = time.monotonic() - t0
    logger.info("Processed %d frames in %.1fs (%.0fx realtime)",
                n_frames, elapsed, (n_frames / frame_rate) / max(elapsed, 1e-9))

    if args.csv:
        with open(args.csv, "w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)
        logger.info("Per-frame CSV written: %s", args.csv)

    # ---- summary statistics ---------------------------------------------
    measures = np.array([r["measure"] for r in rows], dtype=np.int64)
    ref_frames = np.array([r["ref_frame"] for r in rows], dtype=np.int64)
    confs = np.array([r["confidence"] for r in rows], dtype=np.float64)

    coverage = 100.0 * measures[-1] / total_measures if total_measures else 0.0
    m_delta = np.diff(measures)
    f_delta = np.diff(ref_frames)

    # Stall: longest run of frames with zero ref advance.
    max_stall = 0
    total_stalled = 0
    run = 0
    for d in f_delta:
        if d == 0:
            run += 1
            total_stalled += 1
            max_stall = max(max_stall, run)
        else:
            run = 0

    print()
    print("=== eval_tracking summary ===")
    print(f"input           : {args.input_wav.name}")
    print(f"frames          : {n_frames} ({n_frames / frame_rate:.1f}s)")
    print(f"final measure   : {measures[-1]} / {total_measures} "
          f"(coverage {coverage:.1f}%)")
    print(f"mean confidence : {confs.mean():.3f} (p10 {np.percentile(confs, 10):.3f})")
    # Fraction of frames a trigger could fire (confidence >= floor). On a
    # wrong piece / noise this should be near 0 for a follower that
    # actually detects the mismatch. Uses the production trigger floor.
    trig_floor = _TRIGGER_CONFIDENCE_FLOOR
    print(f"trigger-eligible: {100.0 * (confs >= trig_floor).mean():.1f}% "
          f"of frames (conf >= {trig_floor})")
    # Mismatch detector: percent of frames flagged + time of first raise.
    # A matched performance must show 0.0% (false-positive gate); junk /
    # offset inputs should raise within tens of seconds.
    mismatch = np.array([r.get("is_mismatched", 0) for r in rows], dtype=bool)
    if mismatch.any():
        first_sec = float(np.argmax(mismatch)) / frame_rate
        print(f"mismatch flagged: {100.0 * mismatch.mean():.1f}% of frames "
              f"(first at {first_sec:.1f}s)")
    else:
        print("mismatch flagged: 0.0% of frames")
    print(f"measure jumps >1: {int((m_delta > 1).sum())}")
    print(f"measure jumps >{_JUMP_ANOMALY_MEASURES}: {int((m_delta > _JUMP_ANOMALY_MEASURES).sum())} "
          f"(max jump {int(m_delta.max()) if m_delta.size else 0})")
    print(f"max stall       : {max_stall} frames ({max_stall / frame_rate:.1f}s); "
          f"total stalled {total_stalled} frames ({total_stalled / frame_rate:.1f}s)")
    print(f"advance stddev  : {f_delta.std():.2f} frames/frame "
          f"(mean {f_delta.mean():.2f})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
