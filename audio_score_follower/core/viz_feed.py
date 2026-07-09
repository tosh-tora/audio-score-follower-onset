#!/usr/bin/env python3
"""viz_feed.py - Thread-safe data channel for the realtime visualiser.

Decouples the OLTW worker thread (producer) from the Tk drawing thread
(consumer). Holds a rolling history of scalar diagnostics plus the latest
per-frame arrays (band cost curve, live/reference chroma). Deliberately
imports neither tkinter nor sounddevice so it can be unit-tested headlessly
and reused by a future audience-facing screen: any renderer is just another
``snapshot()`` consumer.

Only ``VizFeed`` knows how to marshal a ``FollowResult`` into displayable
state; the renderers (ui/viz_window.py, and later an AudienceWindow) never
touch the follower directly.
"""

from __future__ import annotations

import math
import threading
from collections import deque
from dataclasses import dataclass
from typing import Optional

import numpy as np

# History length for the scalar time-series strip. ~28s at the 10.77 Hz
# feature rate — long enough to show a musical phrase, short enough to stay
# cheap to redraw every 100ms.
_HISTORY_LEN = 300


@dataclass(frozen=True)
class VizThresholds:
    """Calibration lines the cost strip draws. Sourced from main.py /
    OLTW so their tuned values stay the single source of truth here."""

    display_conf_cost_lo: float
    display_conf_cost_hi: float
    mismatch_cost: float


class VizFeed:
    """Thread-safe producer/consumer buffer for visualisation data.

    ``push`` is called from the OLTW worker thread once per frame;
    ``snapshot`` is called from the Tk main thread on its poll timer. All
    access is guarded by a single lock; snapshots return copies so the
    consumer never races the producer over shared arrays.
    """

    def __init__(self, thresholds: VizThresholds) -> None:
        self.thresholds = thresholds
        self._lock = threading.Lock()

        # Scalar time-series (oldest → newest).
        self._cost_hist: deque[float] = deque(maxlen=_HISTORY_LEN)
        self._dist_chroma_hist: deque[float] = deque(maxlen=_HISTORY_LEN)
        self._dist_onset_hist: deque[float] = deque(maxlen=_HISTORY_LEN)
        self._confidence_hist: deque[float] = deque(maxlen=_HISTORY_LEN)
        self._display_conf_hist: deque[float] = deque(maxlen=_HISTORY_LEN)
        self._mismatch_hist: deque[bool] = deque(maxlen=_HISTORY_LEN)

        # Latest per-frame arrays (None until the first viz-enabled frame).
        self._live_chroma: Optional[np.ndarray] = None
        self._ref_chroma: Optional[np.ndarray] = None
        self._band_costs: Optional[np.ndarray] = None
        self._band_lo: int = 0
        self._dp_ref_frame: int = 0
        # Measure numbers at the band edges and at the DP-chosen peak, so
        # the "演奏位置さがし" panel can label its axis with real measures
        # instead of an abstract "a bit before/after". None when the
        # frame→measure lookup is unavailable (frozen / edge frames).
        self._band_lo_measure: Optional[int] = None
        self._band_hi_measure: Optional[int] = None
        self._peak_measure: Optional[int] = None

        # Latest scalars mirrored for the header readout.
        self._measure: int = 0
        self._display_confidence: float = 0.0
        self._is_mismatched: bool = False
        self._frame_count: int = 0

    def push(
        self,
        result,
        *,
        measure: int,
        display_confidence: float,
        band_lo_measure: Optional[int] = None,
        band_hi_measure: Optional[int] = None,
        peak_measure: Optional[int] = None,
    ) -> None:
        """Ingest one FollowResult from the worker thread.

        ``result`` is an oltw_follower.FollowResult. ``measure`` and
        ``display_confidence`` are passed in because main._on_oltw_result
        already computed them; recomputing here would duplicate logic.
        ``band_lo_measure`` / ``band_hi_measure`` / ``peak_measure`` are the
        score measures at the band edges and the DP peak (also computed by
        main, which owns the warp lookup); None when unavailable. A frozen
        frame reports raw_local_cost=NaN and no arrays — handled gracefully
        (NaN kept in history so the strip shows the gap; arrays left at
        their previous value).
        """
        cost = float(result.raw_local_cost)
        with self._lock:
            self._cost_hist.append(cost)
            self._dist_chroma_hist.append(float(result.dist_chroma))
            self._dist_onset_hist.append(float(result.dist_onset))
            self._confidence_hist.append(float(result.confidence))
            self._display_conf_hist.append(float(display_confidence))
            self._mismatch_hist.append(bool(result.is_mismatched))

            if result.live_chroma is not None:
                self._live_chroma = np.array(result.live_chroma, dtype=np.float32)
            if result.ref_chroma is not None:
                self._ref_chroma = np.array(result.ref_chroma, dtype=np.float32)
            if result.band_costs is not None:
                self._band_costs = np.array(result.band_costs, dtype=np.float32)
                self._band_lo = int(result.band_lo)
                self._dp_ref_frame = int(result.dp_ref_frame)
                self._band_lo_measure = band_lo_measure
                self._band_hi_measure = band_hi_measure
                self._peak_measure = peak_measure

            self._measure = int(measure)
            self._display_confidence = float(display_confidence)
            self._is_mismatched = bool(result.is_mismatched)
            self._frame_count += 1

    def snapshot(self) -> dict:
        """Return an atomic copy of the current state for a renderer.

        Lists/arrays are fresh copies so the caller may hold them across the
        next ``push`` without a race.
        """
        with self._lock:
            return {
                "cost": list(self._cost_hist),
                "dist_chroma": list(self._dist_chroma_hist),
                "dist_onset": list(self._dist_onset_hist),
                "confidence": list(self._confidence_hist),
                "display_confidence_hist": list(self._display_conf_hist),
                "mismatch": list(self._mismatch_hist),
                "live_chroma": (
                    None if self._live_chroma is None
                    else self._live_chroma.copy()
                ),
                "ref_chroma": (
                    None if self._ref_chroma is None
                    else self._ref_chroma.copy()
                ),
                "band_costs": (
                    None if self._band_costs is None
                    else self._band_costs.copy()
                ),
                "band_lo": self._band_lo,
                "dp_ref_frame": self._dp_ref_frame,
                "band_lo_measure": self._band_lo_measure,
                "band_hi_measure": self._band_hi_measure,
                "peak_measure": self._peak_measure,
                "measure": self._measure,
                "display_confidence": self._display_confidence,
                "is_mismatched": self._is_mismatched,
                "frame_count": self._frame_count,
            }
