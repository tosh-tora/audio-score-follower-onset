#!/usr/bin/env python3
"""
warp_lookup.py - reference_time → score_time → beat → measure.

Loads ``warping_path.npz`` produced by reference_builder.build_reference
and provides O(log n) lookups for the runtime follower.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Optional

import numpy as np

from audio_score_follower.core.feature_extractor import FeatureConfig

if TYPE_CHECKING:
    from audio_score_follower.core.score_mapper import ScoreMapper

logger = logging.getLogger(__name__)


class WarpLookup:
    """Lookup table for reference_time → score_time and downstream.

    ``ref_times`` and ``score_times`` are parallel arrays sorted by
    ``ref_times`` (build_reference enforces this). We interpolate
    linearly between samples; outside the recorded range we clamp to
    the boundary value rather than extrapolating, because extrapolation
    past the start of the recording is meaningless and the follower
    should rely on its silence gate before tracking begins.
    """

    def __init__(
        self,
        ref_times: np.ndarray,
        score_times: np.ndarray,
        score_bpm: float,
        feature_config: FeatureConfig,
    ) -> None:
        if ref_times.shape != score_times.shape:
            raise ValueError(
                f"ref_times {ref_times.shape} != score_times {score_times.shape}"
            )
        if ref_times.ndim != 1:
            raise ValueError("warp arrays must be 1D")
        if score_bpm <= 0:
            raise ValueError(f"score_bpm must be positive, got {score_bpm}")

        # Cast to float64 — numpy.interp is faster on float64 and we want
        # sub-millisecond accuracy after compounding.
        self.ref_times = ref_times.astype(np.float64)
        self.score_times = score_times.astype(np.float64)
        self.score_bpm = float(score_bpm)
        self.feature_config = feature_config

    @classmethod
    def load(cls, built_dir: Path) -> "WarpLookup":
        """Load a previously built warp path from a directory."""
        npz_path = built_dir / "warping_path.npz"
        if not npz_path.exists():
            raise FileNotFoundError(
                f"warping_path.npz not found in {built_dir}.\n"
                f"  Did you run asf-build for this movement?"
            )
        data = np.load(npz_path)
        cfg = FeatureConfig(
            sample_rate=int(data["feature_config"][0]),
            hop_length=int(data["feature_config"][1]),
            cens_win=int(data["feature_config"][2]),
            norm=float(data["feature_config"][3]),
            quant_steps=tuple(int(x) for x in data["feature_config_quant_steps"]),
        )
        return cls(
            ref_times=data["ref_times"],
            score_times=data["score_times"],
            score_bpm=float(data["score_bpm"]),
            feature_config=cfg,
        )

    # ---------------------------------------------------------- core lookups
    def ref_to_score_time(self, ref_time_sec: float) -> float:
        """Map a reference time (sec) to the corresponding score time (sec)."""
        return float(np.interp(ref_time_sec, self.ref_times, self.score_times))

    def score_time_to_beat(self, score_time_sec: float) -> float:
        """Constant tempo synth: beat = score_time * bpm / 60."""
        return score_time_sec * self.score_bpm / 60.0

    def ref_to_beat(self, ref_time_sec: float) -> float:
        """Convenience: reference time → beat (combining the two above)."""
        return self.score_time_to_beat(self.ref_to_score_time(ref_time_sec))

    def ref_to_measure(
        self, ref_time_sec: float, score_mapper: "ScoreMapper"
    ) -> int:
        """Reference time → measure number via ``ScoreMapper``."""
        beat = self.ref_to_beat(ref_time_sec)
        return score_mapper.beat_to_measure(beat)

    def ref_to_measure_and_beat(
        self, ref_time_sec: float, score_mapper: "ScoreMapper"
    ) -> tuple[int, float, float]:
        """Reference time → (measure, beat_in_measure, continuous_beat).

        ``beat_in_measure`` is 0-indexed (downbeat = 0.0).
        """
        continuous_beat = self.ref_to_beat(ref_time_sec)
        measure = score_mapper.beat_to_measure(continuous_beat)
        beat_in_measure = score_mapper.get_beat_in_measure(continuous_beat)
        return measure, beat_in_measure, continuous_beat

    # ---------------------------------------------------------- diagnostics
    def reference_duration_sec(self) -> float:
        """Last reference time covered by the warp."""
        if len(self.ref_times) == 0:
            return 0.0
        return float(self.ref_times[-1])

    def score_duration_sec(self) -> float:
        """Last score time covered by the warp."""
        if len(self.score_times) == 0:
            return 0.0
        return float(self.score_times[-1])

    def __repr__(self) -> str:
        return (
            f"WarpLookup(K={len(self.ref_times)}, "
            f"ref_dur={self.reference_duration_sec():.1f}s, "
            f"score_dur={self.score_duration_sec():.1f}s, "
            f"bpm={self.score_bpm:.1f})"
        )


def load_reference_cens(built_dir: Path) -> np.ndarray:
    """Convenience loader for reference_cens.npy."""
    cens_path = built_dir / "reference_cens.npy"
    if not cens_path.exists():
        raise FileNotFoundError(
            f"reference_cens.npy not found in {built_dir}"
        )
    return np.load(cens_path).astype(np.float32, copy=False)
