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
        max_advance_per_frame: int | None = None,
        back_inhibit_frames: int = 30,
        init_search_width: int | None = None,
        stuck_dp_reset_seconds: float = 6.0,
        stuck_rematch_seconds: float = 4.0,
        stuck_rematch_min_advance: int = 3,
        stuck_rematch_cost_margin: float = 0.08,
        stuck_rematch_min_jump_frames: int = 60,
        stuck_rematch_max_jump_frames: int = 480,
        stuck_rematch_min_discriminability_ratio: float = 0.75,
        confidence_smoothing: int = 5,
        lock_in_frames: int = 30,
        lock_in_confidence: float = 0.45,
        inertia_enter_frames: int = 5,
        inertia_exit_frames: int = 3,
        inertia_history_frames: int = 40,
        max_inertia_seconds: float = 10.0,
        inertia_resync_max_gap_frames: int | None = None,
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
            step_size: legacy field, validated to be 1 only. Real
                control of "how many ref frames advanced per live
                frame" is via ``max_advance_per_frame`` below.
            max_advance_per_frame: hard upper bound on how far the new
                reference position may be from the previous one, per
                live frame. None (default) = no cap (legacy: band-DP
                may race ahead when the vertical chain in the band
                makes a far-forward cell look cheaper than nearby
                cells, e.g. across a similar passage 10+ measures
                ahead). Setting this to a small integer (recommended:
                ~5–10, corresponding to ~0.5–1s of reference) makes
                the follower advance at "approximately live rate"
                even when the chroma argmin lies far forward, which
                is what users normally expect for live performance.
                Note this is a SOFT cap on the *advance*, not the
                search band itself: the band may still be ``search_width``
                wide, so the DP can still see far ahead and adapt
                gradually over many frames, just not in a single jump.
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
            stuck_dp_reset_seconds: when the follower has failed to
                advance for this many seconds, wipe the accumulated DP
                cost field (set D_prev to inf everywhere except the
                current position, which is anchored at cost 0). The
                position itself is NOT changed — this is a "soft"
                escape that only erases the cost memory that's keeping
                the DP locked. After a reset the system can only move
                forward from the current position; any "go back to
                frame X" attractor is physically removed.
                This is the safe complement to ``stuck_rematch_*``:
                rematch can teleport to a (potentially wrong) faraway
                forward position, while this just unsticks in place.
                Set to 0 to disable. Default 6.0s.
            stuck_rematch_seconds: trigger a global rematch attempt
                after the position has failed to advance by at least
                ``stuck_rematch_min_advance`` frames for this many
                seconds. Set to 0 to disable. Default 4.0s.
                Escape hatch for the "cumulative cost barrier" failure
                mode: when OLTW locks onto a wrong reference position
                (e.g. mic input first-frame match against silence), the
                accumulated DP cost there is much lower than the inf
                seed elsewhere, and the DP cannot escape via local
                steps. This re-anchors on the strongest forward match
                in the whole reference.
            stuck_rematch_min_advance: number of frames per
                ``stuck_rematch_seconds`` window below which the
                follower is considered "stuck". Default 3 (~ 0.07x
                forward rate at feature_rate=10.77 Hz).
            stuck_rematch_cost_margin: minimum local-cost improvement
                required for a stuck-rematch jump to be accepted.
                Prevents oscillation when the current and global-best
                positions are nearly equivalent. Default 0.05 (matches
                the margin used in confidence scoring).
            stuck_rematch_min_jump_frames: minimum forward distance a
                stuck-rematch jump must cover. The escape hatch is for
                gross mislocks, not local jitter — small forward jumps
                should be handled by normal DP, not by this mechanism.
                Default 60 (~5.6s).
            stuck_rematch_max_jump_frames: maximum forward distance the
                rematch may jump. Caps catastrophic mis-jumps where a
                self-similar passage far ahead in the score (e.g. the
                end of a march/the recapitulation) happens to match the
                current live chroma. Default 720 (~67s).
            stuck_rematch_min_discriminability_ratio: how SHARPLY the
                global-best position must stand out from the bulk of
                forward positions to accept the jump. Defined as
                (median_forward_cost - best_cost) / median_forward_cost
                — i.e. the relative gap to the typical-position cost,
                normalised so the threshold is meaningful across both
                "everywhere matches well" (low absolute costs) and
                "everywhere matches poorly" (high absolute costs)
                cases. Default 0.75: the best must beat the median by
                at least 75% of the median's value. Empirically, mic
                captures of mid-piece live music produce ratios around
                0.60-0.70 (ambiguous) while a true large-jump escape
                from an initial mislock shows ratios > 0.95 (decisive
                peak in chroma space).
            confidence_smoothing: window (frames) for confidence EMA.
            lock_in_frames: number of consecutive frames with
                ``confidence >= lock_in_confidence`` required to consider
                "the piece has been caught" and switch from position-fixed
                freeze to inertia-progression behaviour. Default 30
                (≈3s at 10.77 Hz). Once lock-in is set, it stays set for
                the lifetime of this follower (monotonic).
            lock_in_confidence: confidence threshold for lock-in counter
                and for ``_maybe_resync_from_dp`` recovery. Default 0.45.
            inertia_enter_frames: number of consecutive low-confidence
                frames required to enter inertia mode automatically.
                Default 5. (silence-gate ``freeze()`` enters inertia
                immediately, bypassing this counter.)
            inertia_exit_frames: number of consecutive high-confidence
                DP frames required to exit inertia mode via
                ``_maybe_resync_from_dp``. Default 3.
            inertia_history_frames: window (frames) of position history
                kept for inertia-rate estimation. Default 40 (≈3.7s at
                10.77 Hz). Larger = smoother inertia rate but slower to
                react to tempo changes.
            max_inertia_seconds: hard cap on how long inertia
                progression may continue before falling back to
                position-fixed mode (operator-recoverable via manual
                seek). Default 10.0s. Prevents accumulated rate-estimate
                error from growing without bound during arbitrarily
                long silences. Set 0.0 to disable inertia entirely
                (=legacy "freeze locks position" behaviour).
            inertia_resync_max_gap_frames: maximum gap (in ref frames)
                between DP-estimated position and inertia position for
                ``_maybe_resync_from_dp`` to accept the DP and exit
                inertia. None (default) = use ``search_width`` (≈22s)
                so the resync follows the DP search band.
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
        self._max_advance_per_frame = (
            int(max_advance_per_frame) if max_advance_per_frame is not None else None
        )
        if self._max_advance_per_frame is not None and self._max_advance_per_frame < 0:
            raise ValueError("max_advance_per_frame must be >= 0 (or None to disable)")
        self._back_inhibit_frames = int(back_inhibit_frames)
        self._init_search_width = (
            int(init_search_width)
            if init_search_width is not None
            else int(search_width)
        )
        if self._init_search_width <= 0:
            raise ValueError("init_search_width must be > 0")
        if stuck_dp_reset_seconds < 0:
            raise ValueError("stuck_dp_reset_seconds must be >= 0")
        if stuck_rematch_seconds < 0:
            raise ValueError("stuck_rematch_seconds must be >= 0")
        self._stuck_dp_reset_seconds = float(stuck_dp_reset_seconds)
        self._stuck_dp_reset_frames = int(
            round(self._stuck_dp_reset_seconds * self._cfg.effective_frame_rate())
        )
        # Separate counter from rematch so they can fire on independent
        # cadences without interfering with each other.
        self._stuck_dp_counter = 0
        self._stuck_dp_window_start_pos = 0
        # Count how often the unclamped argmin wants to go BACKWARD
        # during the window. This is the signature of "DP locked by
        # backward-attractor in cumulative cost" — the failure mode
        # DP reset is meant to fix. Slow forward advance without
        # backward attempts is a *different* failure mode (degraded
        # chroma matching) that DP reset can only hurt, not help.
        self._backward_attempts_in_window = 0
        # One-shot flag set by seek(...allow_catchup=True). On the next
        # live frame, run a bounded forward local rematch before the
        # main DP so manual "→ catch up" overrides can close gaps
        # larger than the DP can traverse in one live frame.
        self._post_seek_catchup_pending = False
        # Count of consecutive frames where the unclamped DP argmin
        # pointed backward. Reset to 0 on any non-backward step.
        # Used by the rapid-reset path: sustained pure-backward
        # behaviour is the definitive signature of a backward-cost
        # attractor that the full stuck_dp_reset_seconds window would
        # otherwise leave unaddressed for 12+ seconds.
        self._consecutive_backward_frames: int = 0
        self._stuck_rematch_seconds = float(stuck_rematch_seconds)
        self._stuck_rematch_min_advance = int(stuck_rematch_min_advance)
        self._stuck_rematch_cost_margin = float(stuck_rematch_cost_margin)
        self._stuck_rematch_min_jump_frames = int(stuck_rematch_min_jump_frames)
        self._stuck_rematch_max_jump_frames = int(stuck_rematch_max_jump_frames)
        self._stuck_rematch_min_discriminability_ratio = float(
            stuck_rematch_min_discriminability_ratio
        )
        # Stuck tracking: how many frames since we last saw a "real"
        # forward advance, and where we were at the start of the window.
        self._stuck_window_frames = int(
            round(self._stuck_rematch_seconds * self._cfg.effective_frame_rate())
        )
        self._stuck_counter = 0
        self._stuck_window_start_pos = 0

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

        # Lock-in + inertia state ------------------------------------------
        # Lock-in is a monotonic latch: once the piece has been "caught"
        # (sustained high confidence after init phase, OR operator pressed
        # the GUI "楽章開始" button), freeze() / low-confidence no longer
        # parks the position; instead inertia progression advances ref_pos
        # at the most recently observed live-to-ref rate, while the DP
        # keeps running underneath and _maybe_resync_from_dp recovers when
        # the DP regains a confident match.
        if lock_in_frames < 0:
            raise ValueError("lock_in_frames must be >= 0")
        if not 0.0 <= lock_in_confidence <= 1.0:
            raise ValueError("lock_in_confidence must be in [0, 1]")
        if inertia_enter_frames < 1:
            raise ValueError("inertia_enter_frames must be >= 1")
        if inertia_exit_frames < 1:
            raise ValueError("inertia_exit_frames must be >= 1")
        if inertia_history_frames < 2:
            raise ValueError("inertia_history_frames must be >= 2")
        if max_inertia_seconds < 0:
            raise ValueError("max_inertia_seconds must be >= 0")

        self._lock_in_frames = int(lock_in_frames)
        self._lock_in_confidence = float(lock_in_confidence)
        self._inertia_enter_frames = int(inertia_enter_frames)
        self._inertia_exit_frames = int(inertia_exit_frames)
        self._inertia_history_frames = int(inertia_history_frames)
        self._max_inertia_seconds = float(max_inertia_seconds)
        self._max_inertia_frames = int(
            round(self._max_inertia_seconds * self._cfg.effective_frame_rate())
        )
        self._inertia_resync_max_gap_frames = (
            int(inertia_resync_max_gap_frames)
            if inertia_resync_max_gap_frames is not None
            else int(search_width)
        )

        self._locked_in: bool = False
        self._high_conf_streak: int = 0
        self._low_conf_streak: int = 0
        self._pos_history: deque[tuple[int, int]] = deque(
            maxlen=self._inertia_history_frames
        )

        # Inertia mode: when active, _current_ref_pos advances by
        # _compute_inertia_rate() per live frame instead of by DP argmin.
        # _inertia_ref_pos holds the fractional position (int truncation
        # only on output to _current_ref_pos) so accumulated drift stays
        # bounded across many frames at non-integer rates.
        self._inertia_active: bool = False
        self._inertia_ref_pos: float = 0.0
        self._inertia_frames_elapsed: int = 0
        # Cached last-good inertia rate so quick re-entries (where
        # _pos_history hasn't yet refilled past the 5-sample minimum)
        # don't degenerate to the 1.0 fallback. Updated whenever
        # _compute_inertia_rate produces a valid estimate.
        # 1.0 is the bootstrap fallback used at the very first inertia
        # entry, before any history has accumulated.
        self._last_good_rate: float = 1.0

        self._state_lock = threading.Lock()

        logger.info(
            "OnlineDTWFollower initialised: N_ref=%d, search_width=%d, "
            "back_inhibit=%d, step_penalty=%.3f, feature_rate=%.2f Hz, "
            "lock_in=%d frames @ conf>=%.2f, max_inertia=%.1fs",
            self._N, self._search_width, self._back_inhibit_frames,
            self._step_penalty, self._cfg.effective_frame_rate(),
            self._lock_in_frames, self._lock_in_confidence,
            self._max_inertia_seconds,
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
            return self._run_frozen_step()

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

    def _try_global_rematch(self, live: np.ndarray, current_local_cost: float) -> bool:
        """If a forward position has a much better local match, jump there.

        Searches a *bounded* forward window of the reference (capped by
        ``stuck_rematch_max_jump_frames``) for the best local cost, and
        only jumps if all three guards pass:

        1. The candidate beats the current position by at least
           ``stuck_rematch_cost_margin`` (do something only if it helps).
        2. The candidate beats the median forward cost by at least
           ``stuck_rematch_min_discriminability`` (don't trust the
           "best" when many candidates are essentially tied — that's
           the signature of a non-discriminative chroma profile, e.g.
           a different orchestration of the same theme, and the global
           argmin is then dominated by chance / self-similarity).
        3. The jump distance is within the configured window.

        Returns True if a jump was performed.
        """
        min_jump_pos = self._current_ref_pos + self._stuck_rematch_min_jump_frames
        if min_jump_pos >= self._N:
            return False
        max_jump_pos = min(
            self._N, self._current_ref_pos + self._stuck_rematch_max_jump_frames + 1
        )
        if max_jump_pos <= min_jump_pos:
            return False
        forward_block = self._ref[:, min_jump_pos:max_jump_pos]
        global_costs = 1.0 - forward_block.T @ live  # (block_size,)
        best_offset = int(np.argmin(global_costs))
        best_cost = float(global_costs[best_offset])

        # Guard 1: must beat the current position.
        if current_local_cost - best_cost < self._stuck_rematch_cost_margin:
            return False

        # Guard 2: the best must stand out RELATIVE to typical forward
        # positions, not just by an absolute margin. Use a ratio so the
        # threshold is meaningful whether absolute costs are tiny (perfect
        # match exists) or large (mic capture of a different performance).
        # An absolute margin alone misclassifies both ends: it rejects
        # clean perfect matches whose median is also low (lead-time
        # scenario), and accepts ambiguous mic matches whose median is
        # moderate (the false-jump scenario).
        median_cost = float(np.median(global_costs))
        if median_cost <= 0:
            return False  # degenerate; nothing to compare against
        discriminability_ratio = (median_cost - best_cost) / median_cost
        if discriminability_ratio < self._stuck_rematch_min_discriminability_ratio:
            logger.debug(
                "OLTW stuck-rematch: skipping jump, low discriminability "
                "ratio %.2f (best %.3f, median %.3f)",
                discriminability_ratio, best_cost, median_cost,
            )
            return False

        new_pos = min_jump_pos + best_offset
        logger.info(
            "OLTW stuck-rematch: jumping ref_frame %d→%d "
            "(local cost %.3f→%.3f, discrim_ratio %.2f, median %.3f)",
            self._current_ref_pos, new_pos,
            current_local_cost, best_cost,
            discriminability_ratio, median_cost,
        )
        # Reseed DP: clear cumulative history, anchor at the new position.
        self._D_prev[:] = np.inf
        self._D_prev[new_pos] = best_cost
        self._prev_band_lo = new_pos
        self._prev_band_hi = new_pos + 1
        self._current_ref_pos = new_pos
        return True

    def _process_subsequent_frame(self, live: np.ndarray) -> FollowResult:
        """Standard DP update for live frames > 0.

        The search band is *asymmetric*: forward extent is
        ``search_width`` (tolerate tempo deviation / dropped frames),
        backward extent is capped at ``back_inhibit_frames`` (prevent
        the DP from latching onto self-similar material — e.g. an
        earlier statement of the same theme — which is the dominant
        failure mode on march/repeat-heavy orchestral material).
        """
        # If the operator just pressed → (manual seek with catchup
        # armed), let the live frame find the actual performance
        # position within the band before normal DP starts from a
        # possibly-still-too-early anchor. One-shot.
        if self._post_seek_catchup_pending:
            self._post_seek_catchup_pending = False
            self._try_post_seek_catchup(live)

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
        #
        # Cap the argmin search range so a single live frame cannot
        # advance the reference position by more than
        # ``max_advance_per_frame``. Without this cap the vertical
        # chain D_curr[k] = D_curr[k-1] + local + penalty propagates
        # cheap forward seeds across the whole band, and a far-ahead
        # position whose chroma happens to match (e.g. an early
        # recurrence of the current theme, or a similarly-orchestrated
        # passage 15 measures later) can win the argmin — producing
        # the "the follower suddenly jumped 20 measures forward"
        # failure. The cap keeps the band wide (so the DP retains
        # context for tempo flexibility), but constrains the *acted-on*
        # advance to live rate.
        if self._max_advance_per_frame is not None:
            max_band_idx = (self._current_ref_pos - lo) + self._max_advance_per_frame
            max_band_idx = min(max_band_idx, band_width - 1)
        else:
            max_band_idx = band_width - 1
        # argmin within [0, max_band_idx]
        capped_band = D_curr_band_arr[: max_band_idx + 1]
        min_cost = float(capped_band.min())
        candidates = np.where(capped_band <= min_cost + 1e-6)[0]
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
            self._backward_attempts_in_window += 1
            self._consecutive_backward_frames += 1
        else:
            self._consecutive_backward_frames = 0

        self._D_prev = D_curr
        self._prev_band_lo = lo
        self._prev_band_hi = hi
        self._current_ref_pos = new_pos
        self._live_frame_idx += 1

        # ---- rapid DP reset: sustained pure-backward attractor ---------
        # When the unclamped argmin has pointed backward on EVERY frame
        # for _RAPID_RESET_FRAMES consecutive frames, the backward cost
        # attractor is definitive. No separate advance check is needed:
        # _consecutive_backward_frames already encodes zero progress for
        # that many frames (each backward attempt clamps position in
        # place). Wipe backward memory immediately rather than waiting
        # for the full stuck_dp_reset_seconds window (which can delay
        # recovery by 12–24 s in live performances).
        _RAPID_RESET_FRAMES = 10  # ≈0.93 s at default 10.77 Hz frame rate
        if (
            self._stuck_dp_reset_frames > 0
            and self._consecutive_backward_frames >= _RAPID_RESET_FRAMES
        ):
            logger.info(
                "OLTW rapid DP reset: %d consecutive backward attempts at "
                "ref_frame=%d; wiping backward cumulative cost",
                self._consecutive_backward_frames,
                self._current_ref_pos,
            )
            self._D_prev[: self._current_ref_pos] = np.inf
            self._D_prev[self._current_ref_pos] = 0.0
            self._prev_band_lo = self._current_ref_pos
            self._consecutive_backward_frames = 0
            self._stuck_dp_counter = 0
            self._stuck_dp_window_start_pos = self._current_ref_pos
            self._backward_attempts_in_window = 0

        # ---- stuck detection: DP reset (in-place unstick) ------------
        # If the position has barely advanced over the configured
        # window, wipe the cumulative DP cost. This breaks the
        # "frame X-back-from-here looks cheaper than current" trap
        # without changing position. After a reset the DP can only
        # advance forward from the current frame.
        self._stuck_dp_counter += 1
        if (
            self._stuck_dp_reset_frames > 0
            and self._stuck_dp_counter >= self._stuck_dp_reset_frames
        ):
            advance = self._current_ref_pos - self._stuck_dp_window_start_pos
            # Trigger the reset only when (a) we genuinely failed to
            # advance AND (b) the DP repeatedly tried to step backward
            # in the window — that's the cumulative-cost lock-in
            # signature. Without (b), the system is just tracking slow
            # forward material; wiping memory there would lose useful
            # forward seeds.
            min_backward_attempts = max(
                3, self._stuck_dp_reset_frames // 4
            )
            if (
                advance < self._stuck_rematch_min_advance
                and self._backward_attempts_in_window >= min_backward_attempts
            ):
                logger.info(
                    "OLTW DP reset: wiping backward cumulative cost at ref_frame=%d "
                    "(stuck %d frames, advance=%d, backward_attempts=%d)",
                    self._current_ref_pos, self._stuck_dp_counter,
                    advance, self._backward_attempts_in_window,
                )
                # Wipe ONLY backward memory — kills the "go back to
                # frame X" attractor — while keeping any forward
                # cumulative state intact, so DP can still pick up
                # legitimate forward matches that already accumulated.
                self._D_prev[: self._current_ref_pos] = np.inf
                # Re-anchor the current position low so the next frame
                # is guaranteed to have a finite seed at lo' = current.
                self._D_prev[self._current_ref_pos] = 0.0
                self._prev_band_lo = self._current_ref_pos
            self._stuck_dp_window_start_pos = self._current_ref_pos
            self._stuck_dp_counter = 0
            self._backward_attempts_in_window = 0
            self._consecutive_backward_frames = 0

        # ---- stuck detection + global rematch escape hatch -----------
        # Track advance over a sliding window. If the position fails to
        # advance by stuck_rematch_min_advance frames over the window,
        # search the whole reference for a strongly-better forward match
        # and re-anchor there.
        self._stuck_counter += 1
        if self._stuck_window_frames > 0 and self._stuck_counter >= self._stuck_window_frames:
            advance = self._current_ref_pos - self._stuck_window_start_pos
            # During inertia, the DP's _current_ref_pos is the band
            # anchor under the inertia overlay — its "stuck" reading
            # is meaningless for global rematch decisions, and a forward
            # teleport on top of inertia would defeat the whole point
            # of bounding recovery to the resync gap. Skip.
            if advance < self._stuck_rematch_min_advance and not self._inertia_active:
                jumped = self._try_global_rematch(live, local_cost_at_best)
                if jumped:
                    new_pos = self._current_ref_pos
                    local_cost_at_best = float(
                        1.0 - self._ref[:, new_pos] @ live
                    )
                    # After a jump the band is reset around the new pos;
                    # surface band info to caller so logs are honest.
                    lo = max(0, new_pos - min(self._search_width, self._back_inhibit_frames))
                    hi = min(self._N, new_pos + self._search_width + 1)
            # Reset the window regardless of outcome — otherwise a failed
            # rematch leaves us re-checking every single subsequent frame.
            self._stuck_window_start_pos = self._current_ref_pos
            self._stuck_counter = 0

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

        # ---- lock-in + inertia bookkeeping -----------------------------
        # Track high/low confidence streaks for lock-in latching and
        # inertia entry. Position history is only updated on
        # high-conf, locked-in, non-inertia frames so the rate
        # estimator isn't polluted by DP wandering during low-conf
        # passages or by stale samples during inertia.
        if confidence >= self._lock_in_confidence:
            self._high_conf_streak += 1
            self._low_conf_streak = 0
            if self._locked_in and not self._inertia_active:
                self._pos_history.append(
                    (self._live_frame_idx, self._current_ref_pos)
                )
        else:
            self._low_conf_streak += 1
            self._high_conf_streak = 0
        self._update_lock_in()

        # Inertia management: ONLY enter via the explicit silence-gate
        # freeze() path (handled in process_frame's frozen branch).
        # Low DP confidence alone is NOT enough — orchestral pp / pizz
        # passages can produce low conf for many frames while DP is
        # still finding the right position (just with reduced margin
        # because chroma is less discriminative). Overriding DP with
        # inertia in those cases caused a regression where alt-recording
        # coverage dropped from 100% to 34%.
        #
        # If we ARE in inertia (entered via freeze, then unfreeze called
        # while DP keeps running), try DP-based recovery on this frame
        # and either snap back or continue inertia.
        out_pos = new_pos
        out_conf = confidence
        if self._inertia_active:
            recovered = self._maybe_resync_from_dp(new_pos, confidence)
            if recovered:
                # seek() updated _current_ref_pos to dp_pos.
                out_pos = self._current_ref_pos
                out_conf = confidence
            else:
                # Inertia still active — advance the displayed inertia
                # position. DP keeps running underneath (its
                # _current_ref_pos was already updated and remains the
                # DP's anchor) so the next resync attempt has fresh data.
                self._advance_inertia()
                out_pos = int(self._inertia_ref_pos)
                out_conf = 0.0  # mute so triggers don't fire on a guess

        return FollowResult(
            ref_frame=out_pos,
            ref_time_sec=out_pos / self._cfg.effective_frame_rate(),
            confidence=out_conf,
            raw_local_cost=local_cost_at_best,
            band_lo=lo,
            band_hi=hi,
        )

    # ------------------------------------------------------------ frozen path
    def _run_frozen_step(self) -> FollowResult:
        """Per-frame advance while ``_frozen`` is set.

        Two modes depending on lock-in:

        1. **Pre-lock-in (legacy)**: position is fixed at ``_frozen_pos``
           with confidence=0. Same behaviour the silence gate has had
           since pymatchmaker days — prevents startup noise from
           establishing a spurious anchor.
        2. **Post-lock-in (inertia)**: position advances by
           ``_compute_inertia_rate()`` per live frame, capped at
           ``max_inertia_seconds``. Confidence still reported as 0 so
           triggers (which check ``confidence >= _TRIGGER_CONFIDENCE_FLOOR``)
           stay suppressed during inertia — the position is a guess,
           not a confirmed match.

        ``_live_frame_idx`` is still incremented so resumes don't
        re-trigger the initial-frame branch in ``process_frame``.
        """
        self._live_frame_idx += 1

        if self._inertia_active:
            self._advance_inertia()
            pos = int(self._inertia_ref_pos)
        else:
            pos = (
                self._frozen_pos
                if self._frozen_pos is not None
                else self._current_ref_pos
            )

        return FollowResult(
            ref_frame=pos,
            ref_time_sec=pos / self._cfg.effective_frame_rate(),
            confidence=0.0,
            raw_local_cost=float("nan"),
            band_lo=pos,
            band_hi=pos + 1,
        )

    def _advance_inertia(self) -> None:
        """Advance ``_inertia_ref_pos`` by one inertia step.

        Does NOT touch ``_current_ref_pos`` — that field is owned by
        the DP, which keeps tracking underneath inertia so that
        ``_maybe_resync_from_dp`` can detect recovery. The effective
        output position (what the GUI / score-mapper sees) is read
        through the ``current_ref_frame`` property and ``FollowResult.ref_frame``,
        both of which prefer ``_inertia_ref_pos`` when inertia is active.

        After ``_max_inertia_frames`` have elapsed, ``_inertia_ref_pos``
        is held constant (cap). Accumulated rate-estimate error can't
        be trusted past that horizon, so we fall back to "park here
        and wait for the operator to manually resync".
        """
        if self._inertia_frames_elapsed < self._max_inertia_frames:
            rate = self._compute_inertia_rate()
            self._inertia_ref_pos = min(
                float(self._N - 1),
                self._inertia_ref_pos + rate,
            )
        self._inertia_frames_elapsed += 1

    # ------------------------------------------------------------ lock-in
    def _update_lock_in(self) -> None:
        """Latch ``_locked_in`` once the piece has been confidently caught.

        Conditions (all must hold):
          1. Not already locked in (latch is monotonic — once True,
             never reverts within this follower's lifetime).
          2. Initialisation phase is over (``_live_frame_idx`` has
             advanced past ``init_search_width``). Before this the DP
             is still establishing its band and confidence can spike
             on coincidental matches.
          3. ``_high_conf_streak`` reached ``_lock_in_frames``.

        Setting ``_lock_in_frames=0`` would lock in on the very first
        high-confidence frame after init — the no-streak-required mode.
        """
        if self._locked_in:
            return
        if self._live_frame_idx <= self._init_search_width:
            return
        if self._high_conf_streak >= self._lock_in_frames:
            self._locked_in = True
            logger.info(
                "OLTW locked in at live_frame=%d ref_pos=%d "
                "(high_conf_streak=%d, threshold=%d)",
                self._live_frame_idx, self._current_ref_pos,
                self._high_conf_streak, self._lock_in_frames,
            )

    def _compute_inertia_rate(self) -> float:
        """Estimate ref-frames-per-live-frame from recent history.

        Reads ``_pos_history`` (high-confidence positions sampled in
        ``_process_subsequent_frame``) and returns ``Δref / Δlive``
        between the oldest and newest entries. Clamped to [0.3, 2.0] to
        reject outliers from extreme rubato.

        Falls back to ``_last_good_rate`` (cached from the previous
        successful estimate) when history is too short. This matters
        for fast-cycling inertia: when a resync clears history and the
        next inertia entry happens before 5 high-conf frames have
        accumulated, the cached rate (e.g. 0.95 from earlier) is
        vastly better than the bootstrap 1.0 — especially for slower
        live performances against a faster reference, where 1.0 would
        consistently overshoot and trigger more frequent inertia cycles.

        The clamp range matches the practical envelope of orchestral
        tempo deviation against a reference recording: at the slow end,
        a luftpause / extreme ritardando wouldn't credibly hold above
        0.3× live for sustained periods (faster than that and the
        operator would manually intervene); at the fast end, an accel
        beyond 2× would similarly be operator-triggered.
        """
        if len(self._pos_history) < 5:
            return self._last_good_rate
        live_old, ref_old = self._pos_history[0]
        live_new, ref_new = self._pos_history[-1]
        d_live = live_new - live_old
        if d_live <= 0:
            return self._last_good_rate
        rate = (ref_new - ref_old) / float(d_live)
        rate = max(0.3, min(2.0, rate))
        # Cache for future fast re-entries.
        self._last_good_rate = rate
        return rate

    def _maybe_resync_from_dp(self, dp_pos: int, dp_conf: float) -> bool:
        """Attempt to exit inertia mode by re-anchoring on the DP estimate.

        Called from the tail of ``_process_subsequent_frame`` whenever
        inertia is active. The DP keeps running under inertia (its
        anchor is its own ``_current_ref_pos``), so when the music
        comes back and DP confidence rises within range of the inertia
        position, we should accept the DP's truth and resume normal
        tracking.

        Conditions (all must hold):
          1. Confidence has been ``>= lock_in_confidence`` for
             ``_inertia_exit_frames`` consecutive frames.
          2. DP estimate lies within ``_inertia_resync_max_gap_frames``
             of the inertia position (so we're not snapping to a
             distant self-similar match).

        On accept: clear inertia state, ``seek(dp_pos, allow_catchup=True)``
        to re-anchor DP cumulative cost and arm the post-seek catchup
        for forward refinement on the next live frame. The
        ``_pos_history`` is cleared because it was accumulated near the
        old (pre-inertia) position and is no longer relevant.

        Returns True if a resync was performed.
        """
        if not self._inertia_active:
            return False
        if self._high_conf_streak < self._inertia_exit_frames:
            return False
        gap = abs(int(dp_pos) - int(self._inertia_ref_pos))
        if gap > self._inertia_resync_max_gap_frames:
            return False

        inertia_pos = int(self._inertia_ref_pos)
        elapsed = (
            self._inertia_frames_elapsed / self._cfg.effective_frame_rate()
        )
        logger.info(
            "OLTW inertia → DP resync: ran %.1fs, inertia_pos=%d → "
            "dp_pos=%d (gap=%d, conf=%.2f)",
            elapsed, inertia_pos, dp_pos, gap, dp_conf,
        )
        self._inertia_active = False
        self._inertia_frames_elapsed = 0
        self._pos_history.clear()
        # seek() acquires _state_lock; we're not holding it here.
        self.seek(int(dp_pos), allow_catchup=True)
        return True

    def force_lock_in(self) -> None:
        """Externally force the lock-in latch ON.

        Called from the GUI "楽章開始" button when the operator wants
        to manually arm inertia mode before the auto lock-in conditions
        are met — typically right at the conductor's downbeat so the
        first few seconds of music can use inertia recovery if needed.
        Idempotent; subsequent calls are no-ops.
        """
        if self._locked_in:
            return
        self._locked_in = True
        logger.info(
            "OLTW lock-in forced (manual) at live_frame=%d ref_pos=%d",
            self._live_frame_idx, self._current_ref_pos,
        )

    # ------------------------------------------------------------ control
    def freeze(self) -> None:
        """Suspend DP advance — entered by the silence gate.

        Semantics depend on whether the piece has been locked in:

        * **Pre-lock-in**: position is frozen at the current ref frame
          and held there until ``unfreeze()`` (legacy behaviour, prevents
          startup noise from anchoring on a spurious match).
        * **Post-lock-in**: enters **inertia mode** — position keeps
          advancing at the most recently observed live-to-ref rate,
          capped at ``max_inertia_seconds``. Confidence still reported
          as 0 so auto-triggers don't fire on a guessed position.
          The DP state (``_D_prev``) is preserved so an ``unfreeze()``
          can hand back to normal DP without restarting from scratch.

        Setting ``max_inertia_seconds=0.0`` in config disables the
        inertia behaviour entirely (=fully legacy: freeze always parks).

        Thread-safe relative to ``process_frame`` via ``_state_lock``.
        """
        with self._state_lock:
            if self._frozen:
                return
            self._frozen = True
            if self._locked_in and self._max_inertia_seconds > 0:
                self._inertia_active = True
                self._inertia_ref_pos = float(self._current_ref_pos)
                self._inertia_frames_elapsed = 0
                rate = self._compute_inertia_rate()
                logger.info(
                    "OLTW frozen → inertia at ref_frame=%d "
                    "(rate=%.2f, cap=%.1fs)",
                    self._current_ref_pos, rate,
                    self._max_inertia_seconds,
                )
            else:
                self._frozen_pos = self._current_ref_pos
                logger.info(
                    "OLTW frozen (position fixed, pre-lock-in) at "
                    "ref_frame=%d (%.2fs)",
                    self._frozen_pos,
                    self._frozen_pos / self._cfg.effective_frame_rate(),
                )

    def unfreeze(self) -> None:
        """Resume DP advance. Inertia (if active) keeps running until
        ``_maybe_resync_from_dp`` confirms DP recovery.

        The DP state was preserved during freeze, but DP was not
        running so ``_current_ref_pos`` is wherever it was before
        the freeze. ``_inertia_ref_pos`` advanced during the freeze.
        On the next live frames, DP runs normally and
        ``_maybe_resync_from_dp`` watches for sustained high conf
        within range of the inertia position — that's the "前後
        マッチングして復帰" path the user wanted. Until resync,
        the displayed position keeps being the inertia value (so
        the GUI doesn't jump backward to a stale DP position).
        """
        with self._state_lock:
            if not self._frozen:
                return
            self._frozen = False
            self._frozen_pos = None
            if self._inertia_active:
                elapsed = (
                    self._inertia_frames_elapsed
                    / self._cfg.effective_frame_rate()
                )
                logger.info(
                    "OLTW unfrozen, inertia continues at ref_pos=%d "
                    "(ran %.1fs; DP will take over via _maybe_resync_from_dp)",
                    int(self._inertia_ref_pos), elapsed,
                )
            else:
                logger.info("OLTW unfrozen at ref_frame=%d", self._current_ref_pos)

    def seek(self, ref_frame: int, *, allow_catchup: bool = True) -> None:
        """Jump the alignment to a specific reference frame.

        Used by manual slide-override controls (the human pressing
        ← / →) to tell the follower "the music is actually here now".
        Wipes the cumulative DP cost and re-anchors at ``ref_frame``
        with cost 0, so subsequent live frames start a fresh DP
        recurrence from there.

        Args:
            ref_frame: target reference frame index. Clamped to
                ``[0, N_ref - 1]``.
            allow_catchup: if True (default), the very next call to
                ``process_frame`` will scan the band forward of the
                seek target for a strongly-better local match, before
                running normal DP. This is what lets a "→ slide
                forward" override actually catch up when the real
                performance is *past* the trigger we seeked to:
                without it, the DP can only inch forward 1 frame per
                live frame via the vertical chain, never closing the
                gap. Pass False for ← (backward) overrides where the
                operator says the music is *before* the target —
                catching up would jump forward again, defeating the
                point of the manual back-step.
        """
        ref_frame = max(0, min(int(ref_frame), self._N - 1))
        with self._state_lock:
            self._D_prev[:] = np.inf
            self._D_prev[ref_frame] = 0.0
            self._prev_band_lo = ref_frame
            self._prev_band_hi = ref_frame + 1
            self._current_ref_pos = ref_frame
            # If we haven't processed any live frame yet, the next call
            # would otherwise go through the (init_search_width-limited)
            # first-frame path and overwrite our seek. Bumping the index
            # routes it through the subsequent-frame DP, which will use
            # the seeded D_prev we just set.
            if self._live_frame_idx == 0:
                self._live_frame_idx = 1
            self._cost_history.clear()
            self._stuck_counter = 0
            self._stuck_window_start_pos = ref_frame
            self._stuck_dp_counter = 0
            self._stuck_dp_window_start_pos = ref_frame
            self._backward_attempts_in_window = 0
            self._consecutive_backward_frames = 0
            self._post_seek_catchup_pending = bool(allow_catchup)
        logger.info(
            "OLTW seek: ref_frame=%d (%.2fs)%s",
            ref_frame, ref_frame / self._cfg.effective_frame_rate(),
            " [catchup armed]" if allow_catchup else "",
        )

    def _try_post_seek_catchup(self, live: np.ndarray) -> bool:
        """Bounded forward local rematch after a manual seek().

        The operator pressed → because the real performance is at or
        past the seeked trigger measure. After seek, current_ref_pos
        equals that trigger, but the live frame's chroma may match a
        position somewhere further forward in the search band — that's
        where the conductor actually is. Normal DP can't find that
        position in a single live frame because the vertical chain
        through D_curr accumulates ``local + step_penalty`` per cell,
        making far-forward cells arbitrarily expensive regardless of
        their local match quality.

        This routine bypasses the cumulative-cost barrier by directly
        comparing local cost at the seek target vs. the band forward
        of it, and jumping when the best forward position is decisive
        (same discriminability_ratio guard the stuck_rematch path uses).
        Bounded to ``[current, current + search_width]`` so it cannot
        teleport to a self-similar passage far ahead — the worst case
        is a small overshoot within the band.

        Returns True if a jump was performed.
        """
        min_jump_pos = self._current_ref_pos + 1
        max_jump_pos = min(self._N, self._current_ref_pos + self._search_width + 1)
        if max_jump_pos <= min_jump_pos:
            return False
        forward_block = self._ref[:, min_jump_pos:max_jump_pos]
        global_costs = 1.0 - forward_block.T @ live
        best_offset = int(np.argmin(global_costs))
        best_cost = float(global_costs[best_offset])
        current_cost = float(1.0 - self._ref[:, self._current_ref_pos] @ live)

        # Guard 1: must beat the current position.
        if current_cost - best_cost < self._stuck_rematch_cost_margin:
            return False

        # Guard 2: relative discriminability.
        median_cost = float(np.median(global_costs))
        if median_cost <= 0:
            return False
        ratio = (median_cost - best_cost) / median_cost
        if ratio < self._stuck_rematch_min_discriminability_ratio:
            logger.debug(
                "OLTW post-seek catchup: skipping, low discriminability "
                "ratio %.2f (best %.3f, median %.3f)",
                ratio, best_cost, median_cost,
            )
            return False

        new_pos = min_jump_pos + best_offset
        logger.info(
            "OLTW post-seek catchup: %d→%d (local cost %.3f→%.3f, "
            "discrim_ratio %.2f, +%.2fs forward)",
            self._current_ref_pos, new_pos, current_cost, best_cost, ratio,
            (new_pos - self._current_ref_pos) / self._cfg.effective_frame_rate(),
        )
        # Re-seed DP at the corrected position.
        self._D_prev[:] = np.inf
        self._D_prev[new_pos] = best_cost
        self._prev_band_lo = new_pos
        self._prev_band_hi = new_pos + 1
        self._current_ref_pos = new_pos
        return True

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
            self._stuck_counter = 0
            self._stuck_window_start_pos = 0
            self._stuck_dp_counter = 0
            self._stuck_dp_window_start_pos = 0
            self._backward_attempts_in_window = 0
            self._consecutive_backward_frames = 0
            self._post_seek_catchup_pending = False
            # Lock-in + inertia state — must reset on movement change
            # so the new movement starts from scratch (no carry-over).
            self._locked_in = False
            self._high_conf_streak = 0
            self._low_conf_streak = 0
            self._pos_history.clear()
            self._inertia_active = False
            self._inertia_ref_pos = 0.0
            self._inertia_frames_elapsed = 0
            self._last_good_rate = 1.0
        logger.info("OLTW reset")

    # ------------------------------------------------------------ getters
    @property
    def current_ref_frame(self) -> int:
        """Effective output position (inertia-aware).

        During inertia mode the inertia position is returned; otherwise
        the DP-tracked position. Callers (GUI, score-mapper, tests)
        should use this rather than ``_current_ref_pos`` directly so
        the inertia override is honored.
        """
        if self._inertia_active:
            return int(self._inertia_ref_pos)
        return self._current_ref_pos

    @property
    def current_ref_time_sec(self) -> float:
        return self.current_ref_frame / self._cfg.effective_frame_rate()

    @property
    def n_ref_frames(self) -> int:
        return self._N

    @property
    def is_frozen(self) -> bool:
        return self._frozen

    @property
    def is_locked_in(self) -> bool:
        """True once lock-in latch has fired (auto or manual)."""
        return self._locked_in

    @property
    def is_in_inertia(self) -> bool:
        """True iff inertia progression is currently advancing position."""
        return self._inertia_active

    @property
    def inertia_elapsed_sec(self) -> float:
        """Seconds elapsed in the current inertia run (0 if inactive)."""
        if not self._inertia_active:
            return 0.0
        return self._inertia_frames_elapsed / self._cfg.effective_frame_rate()

    @property
    def max_inertia_seconds(self) -> float:
        return self._max_inertia_seconds
