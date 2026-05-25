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

    # ---------------------------------------------------------- inverse lookups
    def beat_to_score_time(self, beat: float) -> float:
        """Inverse of ``score_time_to_beat``: beat → score time (sec)."""
        return float(beat) * 60.0 / self.score_bpm

    def score_time_to_ref_time(self, score_time_sec: float) -> float:
        """Inverse of ``ref_to_score_time`` (sec → sec).

        The forward direction uses np.interp on (ref_times → score_times).
        For the inverse we interpolate on (score_times → ref_times). Both
        arrays are monotonic non-decreasing because the warp path is, but
        ``score_times`` may contain repeated values where the reference
        recording paused on the same score position; np.interp handles
        this by returning the first matching index, which is fine for
        our "seek to position" use case (we just want some valid ref_t).
        """
        return float(np.interp(score_time_sec, self.score_times, self.ref_times))

    def beat_to_ref_time(self, beat: float) -> float:
        """Convenience: beat → reference time (sec)."""
        return self.score_time_to_ref_time(self.beat_to_score_time(beat))

    def measure_to_ref_time(
        self, measure: int, score_mapper: "ScoreMapper"
    ) -> float:
        """Measure number → reference time (sec) of that measure's downbeat.

        Used by manual ← / → slide overrides to re-anchor the OLTW
        follower's position to where the user says the music actually is.
        """
        start_beat, _end_beat = score_mapper.beat_range_for_measure(measure)
        return self.beat_to_ref_time(start_beat)

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

    # ---------------------------------------------------------- validation
    def validate(
        self,
        score_mapper: "ScoreMapper",
        *,
        max_slope: float = 4.0,
        max_coverage_diff_measures: int = 5,
    ) -> None:
        """Raise ValueError if the warp path is inconsistent with the score.

        Two checks are performed:

        1. **Slope**: sample the path at 1-second intervals; if any 1-second
           window of reference time maps to more than ``max_slope`` seconds of
           score time, the DTW has produced a physically impossible alignment
           (e.g. the reference recording skips a structural section that the
           score has linearly).  Default max_slope=4.0 allows a 4× tempo ratio
           between the synth BPM and the reference performance, which is already
           extremely generous.

        2. **Coverage**: the last score_time in the path should correspond to
           approximately the last measure of the score.  If the difference
           exceeds ``max_coverage_diff_measures``, the reference recording
           likely has different structure (repeats, cuts) from the score.

        Args:
            score_mapper: Loaded ScoreMapper for the same score XML.
            max_slope: Maximum allowed score_time / ref_time ratio in any
                1-second reference window.
            max_coverage_diff_measures: Maximum allowed difference (in
                measures) between the warp path's coverage and the score's
                total measure count.

        Raises:
            ValueError: with a descriptive message on the first failed check.
        """
        if len(self.ref_times) < 2:
            raise ValueError("Warp path has fewer than 2 points; rebuild with asf-build.")

        # --- Check 1: slope over 1-second windows ---
        # Sample the interpolated score_time at 1-second ref_time steps.
        sample_ref = np.arange(
            float(self.ref_times[0]),
            float(self.ref_times[-1]),
            1.0,
        )
        if len(sample_ref) >= 2:
            sample_score = np.interp(sample_ref, self.ref_times, self.score_times)
            slope_per_sec = np.diff(sample_score)  # score_seconds per 1 ref_second
            bad = slope_per_sec > max_slope
            if bad.any():
                idx = int(np.argmax(bad))
                r0, r1 = sample_ref[idx], sample_ref[idx + 1]
                s0, s1 = sample_score[idx], sample_score[idx + 1]
                # Convert to measures for the error message
                b0 = s0 * self.score_bpm / 60.0
                b1 = s1 * self.score_bpm / 60.0
                m0 = score_mapper.beat_to_measure(b0)
                m1 = score_mapper.beat_to_measure(b1)
                raise ValueError(
                    f"warp path に異常な勾配があります: "
                    f"参照音源の {r0:.1f}s-{r1:.1f}s ({r1 - r0:.1f}s) が "
                    f"スコアの {s1 - s0:.1f}s 分 (小節 {m0}-{m1}) に対応しています "
                    f"(slope={slope_per_sec[idx]:.1f}x, 上限={max_slope:.1f}x)。\n"
                    f"参照音源とスコアの繰り返し構造（リピート・カット）が一致していない可能性があります。"
                )

        # --- Check 2: measure coverage ---
        score_total_beats = float(self.score_times[-1]) * self.score_bpm / 60.0
        warp_last_measure = score_mapper.beat_to_measure(score_total_beats)
        xml_total_measures = score_mapper.get_total_measures()
        diff = abs(warp_last_measure - xml_total_measures)
        if diff > max_coverage_diff_measures:
            raise ValueError(
                f"warp path の小節数が一致しません: "
                f"warp path の末尾は小節 {warp_last_measure} ですが、"
                f"スコアの総小節数は {xml_total_measures} です (差={diff} 小節)。\n"
                f"参照音源がスコアと同じ繰り返し構造を持っているか確認してください。"
            )

        logger.info(
            "Warp path validation OK: slope_max=%.2f×, "
            "coverage=%d/%d measures",
            float(np.max(np.diff(
                np.interp(sample_ref, self.ref_times, self.score_times)
            ))) if len(sample_ref) >= 2 else 0.0,
            warp_last_measure, xml_total_measures,
        )

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
