#!/usr/bin/env python3
"""
oltw_follower.py - Online DTW alignment of live audio against a fixed
reference CENS sequence.

Algorithm: forward-running DTW with a constrained search band that
slides with the current best alignment position. Compared to Dixon's
canonical OLTW (which can advance on either axis), this variant always
advances one live frame per call — which fits cleanly with a streaming
mic capture loop. Reference position is free to advance 0, 1, or several
frames per call, dictated by the DP minimum.

Memory: we keep only the previous DP column (size N_ref). For an 8-minute
orchestral piece at hop_length=512 / sr=22050 (~43 Hz), N_ref ≈ 20k and
the column is ~80 KB. Negligible.

CPU: ~O(band_width) per frame. With band_width=480, at 43 Hz that's
~20k operations per second of audio — comfortably real-time even in pure
Python+numpy.

Confidence: 1 - cosine_distance(live_frame, ref[:, current_ref_pos]),
clamped to [0, 1]. Smoothed over the last few frames so a single
poorly-matched frame doesn't drop confidence to 0 spuriously.
"""

from __future__ import annotations

import logging
import threading
from collections import deque
from dataclasses import dataclass
from typing import Optional

import numpy as np

from audio_score_follower.core.feature_extractor import FeatureConfig

logger = logging.getLogger(__name__)


@dataclass
class FollowResult:
    """Per-frame result returned by ``process_frame``."""

    ref_frame: int          # best-aligned reference frame
    ref_time_sec: float     # ref_frame / feature_rate
    confidence: float       # smoothed, in [0, 1]
    raw_local_cost: float   # cosine distance at the current frame (diagnostic)
    band_lo: int            # left edge of search band (diagnostic)
    band_hi: int            # right edge (exclusive)


class OnlineDTWFollower:
    """Streaming forward DTW against a fixed reference CENS matrix.

    Thread safety: ``process_frame`` is NOT thread-safe; callers must
    serialise calls. ``freeze()`` / ``unfreeze()`` and ``current_*``
    getters ARE safe to call from another thread.
    """

    def __init__(
        self,
        reference_cens: np.ndarray,
        feature_config: FeatureConfig,
        *,
        search_width: int = 240,
        step_size: int = 1,
        confidence_smoothing: int = 5,
    ) -> None:
        """
        Args:
            reference_cens: (12, N) float32, L2-normalised per column.
            feature_config: must match the offline build.
            search_width: half-width of the band (frames). The band
                spans [current - search_width, current + search_width].
                Wider = more tolerant to tempo deviation but allows
                more drift. ``ConfigLoader.get_oltw_kwargs`` exposes this.
            step_size: maximum number of reference frames the alignment
                can advance per live frame. Always 1 in the current
                recurrence; reserved for future tuning.
            confidence_smoothing: window (frames) for confidence EMA.
        """
        if reference_cens.ndim != 2 or reference_cens.shape[0] != 12:
            raise ValueError(
                f"reference_cens must be (12, N); got {reference_cens.shape}"
            )
        if search_width <= 0:
            raise ValueError("search_width must be > 0")
        if step_size != 1:
            # Wider step sizes give pymatchmaker-style "race ahead" failures
            # we explicitly want to avoid in this project. Reject loudly.
            raise ValueError("step_size > 1 not implemented (intentionally)")

        self._ref = np.ascontiguousarray(reference_cens, dtype=np.float32)
        self._N = reference_cens.shape[1]
        self._cfg = feature_config
        self._search_width = int(search_width)
        self._step_size = int(step_size)

        # DP previous column, size N_ref. Initialised to +inf except the
        # initial seed at frame 0 = 0.0 (we let the first live frame "land"
        # at any reference frame within the start window).
        self._D_prev = np.full(self._N, np.inf, dtype=np.float32)
        # Track which indices were valid in the previous column so we know
        # whether D_prev[i] is a real cost or a stale inf — important
        # when the band slides forward.
        self._prev_band_lo = 0
        self._prev_band_hi = 0

        self._current_ref_pos = 0
        self._live_frame_idx = 0
        self._frozen = False
        self._frozen_pos: Optional[int] = None

        self._cost_history: deque[float] = deque(maxlen=int(confidence_smoothing))

        self._state_lock = threading.Lock()

        logger.info(
            "OnlineDTWFollower initialised: N_ref=%d, search_width=%d, "
            "feature_rate=%.2f Hz",
            self._N, self._search_width, self._cfg.effective_frame_rate(),
        )

    # ------------------------------------------------------------ runtime
    def process_frame(self, live_cens_frame: np.ndarray) -> FollowResult:
        """Advance one live frame and return the new alignment estimate.

        Args:
            live_cens_frame: (12,) float32, L2-normalised. Pass a single
                column from ``compute_cens_streaming`` output.

        Returns:
            FollowResult with the new reference position and confidence.
        """
        if self._frozen:
            # While frozen, keep returning the frozen position. We still
            # update _live_frame_idx so resumes don't re-trigger the
            # initial-frame branch.
            pos = self._frozen_pos if self._frozen_pos is not None else self._current_ref_pos
            self._live_frame_idx += 1
            return FollowResult(
                ref_frame=pos,
                ref_time_sec=pos / self._cfg.effective_frame_rate(),
                confidence=0.0,
                raw_local_cost=float("nan"),
                band_lo=pos,
                band_hi=pos + 1,
            )

        live = live_cens_frame.astype(np.float32, copy=False).reshape(-1)
        if live.shape[0] != 12:
            raise ValueError(
                f"live_cens_frame must be (12,); got {live_cens_frame.shape}"
            )

        if self._live_frame_idx == 0:
            return self._process_first_frame(live)
        return self._process_subsequent_frame(live)

    def _process_first_frame(self, live: np.ndarray) -> FollowResult:
        """Initial alignment: search the first ``search_width`` reference frames."""
        hi = min(self._N, self._search_width)
        local_costs = 1.0 - self._ref[:, :hi].T @ live  # (hi,)

        D_curr = np.full(self._N, np.inf, dtype=np.float32)
        D_curr[:hi] = local_costs

        best = int(np.argmin(local_costs))
        local_cost = float(local_costs[best])

        self._D_prev = D_curr
        self._prev_band_lo = 0
        self._prev_band_hi = hi
        self._current_ref_pos = best
        self._live_frame_idx = 1

        with self._state_lock:
            self._cost_history.append(local_cost)

        return FollowResult(
            ref_frame=best,
            ref_time_sec=best / self._cfg.effective_frame_rate(),
            confidence=max(0.0, 1.0 - local_cost),
            raw_local_cost=local_cost,
            band_lo=0,
            band_hi=hi,
        )

    def _process_subsequent_frame(self, live: np.ndarray) -> FollowResult:
        """Standard DP update for live frames > 0."""
        lo = max(0, self._current_ref_pos - self._search_width)
        hi = min(self._N, self._current_ref_pos + self._search_width + 1)

        # Vectorised local cost — single matmul over the band.
        local_costs = 1.0 - self._ref[:, lo:hi].T @ live  # (band_width,)

        # DP recurrence: D[i, j] = local_cost[i] + min(D[i-1, j-1],
        #                                              D[i, j-1],
        #                                              D[i-1, j]).
        # D[i-1, j-1] and D[i, j-1] come from D_prev; D[i-1, j] comes from
        # the cell we computed in this same pass (so we go left → right).
        #
        # For indices i with D_prev[i] == inf (i was outside the previous
        # band), the diagonal/horizontal options are inf, so D_curr[i]
        # depends entirely on the vertical (left neighbour in this band).
        # That makes a brand-new "rightward extension" of the band slow to
        # build up, which is correct behaviour: the algorithm should not
        # jump forward unless evidence accumulates.

        D_curr = np.full(self._N, np.inf, dtype=np.float32)
        D_prev = self._D_prev

        # Vectorised diagonal + horizontal options.
        # diag[i]   = D_prev[i - 1]   for i in [lo, hi)
        # horiz[i]  = D_prev[i]       for i in [lo, hi)
        # Combine first, then loop over the vertical option (which has
        # data dependence on D_curr[i-1]).
        band_width = hi - lo
        diag = np.full(band_width, np.inf, dtype=np.float32)
        if lo > 0:
            diag[:] = D_prev[lo - 1:hi - 1]
        elif lo == 0 and hi > 1:
            # i = 0 has no i-1; leave diag[0] = inf, copy the rest.
            diag[1:] = D_prev[lo:hi - 1]
        horiz = D_prev[lo:hi]
        partial = local_costs + np.minimum(diag, horiz)

        # Serial pass for vertical (left-in-band) — Python loop on a small
        # array is fast enough at our frame rates.
        partial_list = partial.tolist()
        local_list = local_costs.tolist()
        D_curr_band = [partial_list[0]]
        for k in range(1, band_width):
            vert = D_curr_band[k - 1] + local_list[k]
            D_curr_band.append(min(partial_list[k], vert))
        D_curr_band_arr = np.asarray(D_curr_band, dtype=np.float32)
        D_curr[lo:hi] = D_curr_band_arr

        # Find new best.
        best_in_band = int(np.argmin(D_curr_band_arr))
        new_pos = lo + best_in_band
        local_cost_at_best = float(local_costs[best_in_band])

        # Enforce monotonic non-decreasing reference position. The DP
        # recurrence already strongly biases this (horizontal/diagonal
        # come from j-1 column), but a momentary chroma collision could
        # in principle drag us backward. Clamping protects the warp_lookup
        # path which assumes monotonic ref_time.
        if new_pos < self._current_ref_pos:
            logger.debug(
                "OLTW would step backward (%d → %d); clamping",
                self._current_ref_pos, new_pos,
            )
            new_pos = self._current_ref_pos

        self._D_prev = D_curr
        self._prev_band_lo = lo
        self._prev_band_hi = hi
        self._current_ref_pos = new_pos
        self._live_frame_idx += 1

        with self._state_lock:
            self._cost_history.append(local_cost_at_best)
            smoothed_cost = float(np.mean(self._cost_history))
        confidence = max(0.0, min(1.0, 1.0 - smoothed_cost))

        return FollowResult(
            ref_frame=new_pos,
            ref_time_sec=new_pos / self._cfg.effective_frame_rate(),
            confidence=confidence,
            raw_local_cost=local_cost_at_best,
            band_lo=lo,
            band_hi=hi,
        )

    # ------------------------------------------------------------ control
    def freeze(self) -> None:
        """Stop advancing the alignment (e.g. during a silence gate).

        Called from a different thread than process_frame, typically. The
        next process_frame call will return the frozen position with
        confidence=0.
        """
        with self._state_lock:
            if not self._frozen:
                self._frozen = True
                self._frozen_pos = self._current_ref_pos
                logger.info(
                    "OLTW frozen at ref_frame=%d (%.2fs)",
                    self._frozen_pos,
                    self._frozen_pos / self._cfg.effective_frame_rate(),
                )

    def unfreeze(self) -> None:
        """Resume advancing. The DP state was preserved during freeze."""
        with self._state_lock:
            if self._frozen:
                self._frozen = False
                self._frozen_pos = None
                logger.info("OLTW unfrozen")

    def reset(self) -> None:
        """Wipe DP state — used when loading a new movement."""
        with self._state_lock:
            self._D_prev[:] = np.inf
            self._prev_band_lo = 0
            self._prev_band_hi = 0
            self._current_ref_pos = 0
            self._live_frame_idx = 0
            self._frozen = False
            self._frozen_pos = None
            self._cost_history.clear()
        logger.info("OLTW reset")

    # ------------------------------------------------------------ getters
    @property
    def current_ref_frame(self) -> int:
        return self._current_ref_pos

    @property
    def current_ref_time_sec(self) -> float:
        return self._current_ref_pos / self._cfg.effective_frame_rate()

    @property
    def n_ref_frames(self) -> int:
        return self._N

    @property
    def is_frozen(self) -> bool:
        return self._frozen
