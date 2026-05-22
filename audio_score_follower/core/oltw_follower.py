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
        search_width: int = 100,
        step_size: int = 1,
        step_penalty: float = 0.02,
        back_inhibit_frames: int = 30,
        init_search_width: int | None = None,
        confidence_smoothing: int = 5,
    ) -> None:
        """
        Args:
            reference_cens: (12, N) float32, L2-normalised per column.
            feature_config: must match the offline build.
            search_width: half-width of the band (frames). The band
                spans [current - search_width, current + search_width].
                Wider = more tolerant to tempo deviation but allows
                more drift. Default 100 ≈ 9s at hop=2048/sr=22050;
                widening past ~150 risks self-similarity collapses in
                repetitive music. ``ConfigLoader.get_oltw_kwargs``
                exposes this.
            step_size: maximum number of reference frames the alignment
                can advance per live frame. Always 1 in the current
                recurrence; reserved for future tuning.
            step_penalty: extra cost added to "horizontal" (stay at the
                same reference frame) and "vertical" (advance multiple
                reference frames per live frame) DP transitions. The
                diagonal (1 ref ↔ 1 live) is the preferred path; when
                costs are nearly equal across the band, this penalty
                breaks the tie toward forward motion. Set to 0.0 to
                disable (pre-fix behaviour, useful for testing). Default
                0.02 — small enough to allow tempo flexibility, large
                enough to escape "stuck position" local minima within
                a few frames of accumulation.
            back_inhibit_frames: maximum number of reference frames the
                search band may look BACKWARD from the current position.
                Forward search is governed by ``search_width``; backward
                is capped by this (smaller) value to prevent the DP from
                being captured by self-similar material earlier in the
                score (e.g. an earlier statement of the same theme in
                marches/repeats). Default 30 ≈ 2.8s at hop=2048/sr=22050,
                enough room to settle from small overshoots but far less
                than a typical theme period. Set very large (>=search_width)
                to restore the symmetric pre-fix band.
            init_search_width: search range used ONLY for the very first
                live frame, before any DP history exists. Decoupled from
                ``search_width`` because a wide initial range lets the
                first frame land anywhere in the first N seconds of the
                reference — usually wrong. Default None = same as
                ``search_width`` (legacy behaviour). Recommended: 30
                (~2.8s) so the follower always starts near the score's
                beginning, even when ``search_width`` is large for
                tempo flexibility downstream.
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
        if step_penalty < 0:
            raise ValueError("step_penalty must be >= 0")
        if back_inhibit_frames < 0:
            raise ValueError("back_inhibit_frames must be >= 0")

        self._ref = np.ascontiguousarray(reference_cens, dtype=np.float32)
        self._N = reference_cens.shape[1]
        self._cfg = feature_config
        self._search_width = int(search_width)
        self._step_size = int(step_size)
        self._step_penalty = float(step_penalty)
        self._back_inhibit_frames = int(back_inhibit_frames)
        self._init_search_width = (
            int(init_search_width)
            if init_search_width is not None
            else int(search_width)
        )
        if self._init_search_width <= 0:
            raise ValueError("init_search_width must be > 0")

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
            "back_inhibit=%d, step_penalty=%.3f, feature_rate=%.2f Hz",
            self._N, self._search_width, self._back_inhibit_frames,
            self._step_penalty, self._cfg.effective_frame_rate(),
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
        """Initial alignment: search the first ``init_search_width`` reference frames.

        ``init_search_width`` (not ``search_width``) is used so a wide
        downstream search band — needed to accommodate tempo deviation
        as alignment progresses — doesn't cause the very first frame to
        land arbitrarily far into the score.
        """
        hi = min(self._N, self._init_search_width)
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
        """Standard DP update for live frames > 0.

        The search band is *asymmetric*: forward extent is
        ``search_width`` (tolerate tempo deviation / dropped frames),
        backward extent is capped at ``back_inhibit_frames`` (prevent
        the DP from latching onto self-similar material — e.g. an
        earlier statement of the same theme — which is the dominant
        failure mode on march/repeat-heavy orchestral material).
        """
        back = min(self._search_width, self._back_inhibit_frames)
        lo = max(0, self._current_ref_pos - back)
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
        # Add step_penalty to horizontal (stay) so diagonal (advance by 1)
        # is preferred when costs are similar across the band. Without
        # this, tightly-smoothed CENS features (cens_win=41 ≈ 3.8s) give
        # the DP a nearly-flat local cost field, and argmin picks the
        # leftmost minimum → the position gets stuck.
        horiz = D_prev[lo:hi] + self._step_penalty
        partial = local_costs + np.minimum(diag, horiz)

        # Serial pass for vertical (left-in-band, ref advancing faster
        # than live) — same penalty applied so the DP doesn't race
        # forward gratuitously either.
        partial_list = partial.tolist()
        local_list = local_costs.tolist()
        D_curr_band = [partial_list[0]]
        for k in range(1, band_width):
            vert = D_curr_band[k - 1] + local_list[k] + self._step_penalty
            D_curr_band.append(min(partial_list[k], vert))
        D_curr_band_arr = np.asarray(D_curr_band, dtype=np.float32)
        D_curr[lo:hi] = D_curr_band_arr

        # Find new best. When multiple positions tie at the minimum,
        # prefer the *rightmost* (most-advanced) — this is the second
        # half of the forward bias: even if step_penalty doesn't fully
        # separate them, the tie-break still moves us forward.
        min_cost = float(D_curr_band_arr.min())
        candidates = np.where(D_curr_band_arr <= min_cost + 1e-6)[0]
        best_in_band = int(candidates.max())
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
        match_score = max(0.0, 1.0 - smoothed_cost)

        # Decisiveness: how much better is the best position than the
        # second-best? If the DP cost is uniform across the band (the
        # "stuck position" pathology), margin ≈ 0 and confidence is
        # heavily suppressed — high confidence requires both a good
        # local match AND a sharply-peaked minimum.
        if D_curr_band_arr.size > 1:
            sorted_costs = np.sort(D_curr_band_arr)
            margin = float(sorted_costs[1] - sorted_costs[0])
        else:
            margin = 0.05  # degenerate band: treat as decisive
        margin_score = min(1.0, margin / 0.05)  # 0.05 of cost diff = full score
        confidence = max(0.0, min(1.0, match_score * (0.3 + 0.7 * margin_score)))

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
