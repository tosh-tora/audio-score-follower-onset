#!/usr/bin/env python3
"""
score_mapper.py - Partitura-based Beat ↔ Measure Conversion

Converts continuous beat counts (from pymatchmaker DTW) to measure numbers,
accounting for variable time signatures (3/4, 4/4, 5/8, etc.).

Also provides beat-within-measure calculations for detailed position tracking.
"""

import bisect
import logging
from typing import Tuple

import partitura

logger = logging.getLogger(__name__)


class ScoreMapper:
    """
    Maps between continuous beat positions and measure numbers.

    Handles:
    - Variable time signatures (anacrusis, tempo changes)
    - Accurate beat accumulation across measures
    - Binary search for fast beat→measure lookup

    Example:
        mapper = ScoreMapper("guide_mv1.xml")
        measure = mapper.beat_to_measure(45.5)  # Returns measure number
        beat_in_measure = mapper.get_beat_in_measure(45.5)  # Returns 0.5
    """

    def __init__(self, xml_path: str):
        """
        Load MusicXML and build cumulative beat map.

        Args:
            xml_path: Path to MusicXML file

        Raises:
            FileNotFoundError: If XML file doesn't exist
            Exception: If partitura fails to parse
        """
        logger.info(f"Loading score: {xml_path}")
        loaded = partitura.load_musicxml(xml_path)

        # partitura may return a list of parts or a Score object
        if isinstance(loaded, list):
            # Use first part
            self.part = loaded[0] if loaded else None
        else:
            # Score object - get first part
            parts = list(loaded.parts) if hasattr(loaded, 'parts') else [loaded]
            self.part = parts[0] if parts else loaded

        # Map: sorted list of beat positions → (measure_number, beats_in_measure)
        self.beat_thresholds = []      # Cumulative beats at start of each measure
        self.measure_info = {}         # beat_threshold → (measure_num, beats_per_measure)

        self._build_beat_map()
        logger.info(f"Built beat map with {len(self.beat_thresholds)} measures")

    def _get_measure_time_signature(self, measure) -> tuple[int, int]:
        """Return (numerator, denominator) for the time signature of ``measure``.

        partitura exposes time signatures in two different shapes depending on
        version / access path:

        - ``measure.time_signature`` may not exist at all, or may return an
          object with ``.beats`` and ``.beat_type`` attributes.
        - ``part.time_signature_map(t)`` (the path actually used in practice)
          returns a ``numpy.ndarray`` of shape ``(3,)`` like ``[beats,
          beat_type, beats_per_measure_in_unit]`` — the third element is the
          number of pulse-unit beats per measure in compound time and we
          ignore it; only the first two carry the time-signature numerator and
          denominator.

        If we cannot recognise the representation we warn loudly and fall back
        to 4/4.  Silent fallback was a long-standing bug: every score we ever
        loaded was secretly treated as 4/4 because the ``ndarray`` case was
        not handled, and triggers fired at half the expected rate on 2/4
        pieces (see issue #29).
        """
        ts = None
        if hasattr(measure, 'time_signature'):
            ts = measure.time_signature
        elif hasattr(self.part, 'time_signature_map'):
            start_t = measure.start.t if hasattr(measure, 'start') and hasattr(measure.start, 't') else 0
            ts_map = self.part.time_signature_map
            if ts_map:
                ts = ts_map(start_t) if callable(ts_map) else ts_map.get(start_t)

        if ts is None:
            logger.warning(
                "Time signature missing for measure %r; assuming 4/4",
                getattr(measure, 'name', '?'),
            )
            return 4, 4

        if hasattr(ts, 'beats') and hasattr(ts, 'beat_type'):
            return int(ts.beats), int(ts.beat_type)
        if isinstance(ts, tuple) and len(ts) >= 2:
            return int(ts[0]), int(ts[1])
        # numpy.ndarray and any other indexable [beats, beat_type, ...] form.
        if hasattr(ts, '__len__') and len(ts) >= 2:
            return int(ts[0]), int(ts[1])

        logger.warning(
            "Unrecognized time signature representation %r (type=%s) for "
            "measure %r; assuming 4/4 — measure/beat mapping will be wrong",
            ts, type(ts).__name__, getattr(measure, 'name', '?'),
        )
        return 4, 4

    def _build_beat_map(self):
        """
        Iterate through all measures, accumulate beats, build lookup map.

        Time signature interpretation:
        - (4, 4) = 4 quarter notes = 4.0 beats
        - (3, 4) = 3 quarter notes = 3.0 beats
        - (5, 8) = 5 eighth notes = 2.5 beats (5 * 0.5)
        - General: (num / denom) * 4 quarter notes per measure

        Partial-measure handling (anacrusis / pickup):
        The time signature gives the NOMINAL beat count for a full measure.
        An anacrusis (e.g. MusicXML measure number 0) has a shorter actual
        duration.  We compute ``divs_per_beat`` from the most common full
        measure and use ``measure.duration / divs_per_beat`` for each measure,
        so that a 1-beat anacrusis contributes exactly 1 beat to the cumulative
        counter — matching what pymatchmaker produces for the same score.
        """
        cumulative_beat = 0.0

        if self.part is None:
            logger.warning("No part available to build beat map")
            return

        # Get measures from part
        measures = list(self.part.iter_all(partitura.score.Measure)) if hasattr(self.part, 'iter_all') else []

        if not measures:
            # Fallback: try to access measures directly
            measures = getattr(self.part, 'measures', [])
            if callable(measures):
                measures = measures()

        # Sort measures by start time
        sorted_measures = sorted(
            measures,
            key=lambda m: m.start.t if hasattr(m, 'start') and hasattr(m.start, 't') else 0
        )

        # ------------------------------------------------------------------
        # Pass 1: compute divs_per_beat from the most common (duration, ts)
        # pairing.  This lets us convert measure.duration to beats correctly
        # for partial measures (anacrusis, final pickup, etc.) without relying
        # solely on the time signature.
        # ------------------------------------------------------------------
        from collections import Counter
        dur_ts_counter: Counter = Counter()
        for m in sorted_measures:
            d = getattr(m, 'duration', None)
            if not (d and d > 0):
                continue
            n, dn = self._get_measure_time_signature(m)
            beats = (n / dn) * 4.0
            if beats > 0:
                dur_ts_counter[(int(d), beats)] += 1

        divs_per_beat: float = 0.0
        if dur_ts_counter:
            (ref_dur, ref_beats), _ = dur_ts_counter.most_common(1)[0]
            divs_per_beat = ref_dur / ref_beats
            logger.debug("divs_per_beat=%.2f (from most common measure: dur=%d, beats=%.1f)",
                         divs_per_beat, ref_dur, ref_beats)

        # ------------------------------------------------------------------
        # Pass 2: build the beat map
        # ------------------------------------------------------------------
        for measure in sorted_measures:
            # Prefer the MusicXML measure name over partitura's internal
            # sequential number.  partitura renumbers from 1 even when the
            # score starts with an anacrusis (pickup) that the MusicXML marks
            # as measure 0.  Using `name` preserves the composer-intended
            # numbering (e.g. 0→anacrusis, 1–77 for the 77 full measures of
            # 別れの曲) so total_measures and trigger matching stay correct.
            name = getattr(measure, 'name', None)
            try:
                measure_num = int(name)
            except (TypeError, ValueError):
                measure_num = len(self.beat_thresholds) + 1

            num, denom = self._get_measure_time_signature(measure)

            # Nominal beat count from time signature (stored for beat_in_measure calc)
            beats_per_measure = (num / denom) * 4.0

            # Actual beats to advance the cumulative counter.
            # Use measure.duration when we have a valid divs_per_beat reference,
            # so that anacrusis / partial measures advance by their true length.
            actual_beats = beats_per_measure
            if divs_per_beat > 0:
                actual_dur = getattr(measure, 'duration', None)
                if actual_dur and actual_dur > 0:
                    actual_beats = actual_dur / divs_per_beat

            # Store mapping
            self.beat_thresholds.append(cumulative_beat)
            self.measure_info[cumulative_beat] = (measure_num, beats_per_measure)

            logger.debug(
                "Measure %s: beat %.1f→%.1f (ts=%d/%d, nominal=%.1f, actual=%.1f beats)",
                measure_num, cumulative_beat, cumulative_beat + actual_beats,
                num, denom, beats_per_measure, actual_beats,
            )

            cumulative_beat += actual_beats

    def beat_to_measure(self, beat_count: float) -> int:
        """
        Convert continuous beat count to measure number.

        Uses binary search for O(log n) lookup.

        Args:
            beat_count: Continuous position in beats (can be fractional)

        Returns:
            Measure number (1-indexed)
        """
        if not self.beat_thresholds:
            return 1

        # Find largest beat threshold ≤ beat_count
        idx = bisect.bisect_right(self.beat_thresholds, beat_count) - 1

        if idx < 0:
            # Before first measure (shouldn't happen in normal operation)
            return self.measure_info[self.beat_thresholds[0]][0]

        if idx >= len(self.beat_thresholds):
            # After last measure
            last_beat = self.beat_thresholds[-1]
            return self.measure_info[last_beat][0]

        beat_threshold = self.beat_thresholds[idx]
        measure_num, _ = self.measure_info[beat_threshold]
        return measure_num

    def get_beat_in_measure(self, beat_count: float) -> float:
        """
        Get beat offset within current measure (0.0 ≤ result < beats_per_measure).

        Args:
            beat_count: Continuous position in beats

        Returns:
            Beat within measure (0-indexed, e.g., 0.0, 1.5, 2.0, ...)
        """
        if not self.beat_thresholds:
            return beat_count

        idx = bisect.bisect_right(self.beat_thresholds, beat_count) - 1

        if idx < 0:
            return 0.0

        beat_threshold = self.beat_thresholds[idx]
        return beat_count - beat_threshold

    def get_total_measures(self) -> int:
        """Return the last measure number in the score (as written in the MusicXML).

        This equals the highest measure number stored in measure_info, which
        correctly excludes anacrusis pickup measures that MusicXML numbers as 0.
        """
        if not self.measure_info:
            return 0
        return max(mnum for mnum, _ in self.measure_info.values())

    def get_total_beats(self) -> float:
        """
        Get total duration of score in beats.

        Returns:
            Total beat count
        """
        if not self.beat_thresholds:
            return 0.0

        last_threshold = self.beat_thresholds[-1]
        if last_threshold in self.measure_info:
            _, beats_in_last = self.measure_info[last_threshold]
            return last_threshold + beats_in_last

        return last_threshold

    def beat_range_for_measure(self, measure_num: int) -> Tuple[float, float]:
        """
        Get beat range [start, end) for a given measure.

        Args:
            measure_num: Measure number (1-indexed)

        Returns:
            (start_beat, end_beat)
        """
        start_beat = None
        end_beat = None

        for beat_threshold, (mnum, beats_per_measure) in self.measure_info.items():
            if mnum == measure_num:
                start_beat = beat_threshold
                end_beat = beat_threshold + beats_per_measure
                break

        if start_beat is None:
            logger.warning(f"Measure {measure_num} not found in score")
            return (0.0, 0.0)

        return (start_beat, end_beat)

    def __repr__(self) -> str:
        total_beats = self.get_total_beats()
        return f"ScoreMapper(measures={self.get_total_measures()}, total_beats={total_beats:.1f})"
