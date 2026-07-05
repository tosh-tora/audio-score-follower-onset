"""posterior_follower.py — global-observation Bayesian score follower.

An alternative to :class:`OnlineDTWFollower` that replaces the local
greedy band-DP with a full **HMM forward filter** over the whole
reference. Every frame it:

1. **observes** the fused local cost against the *entire* reference
   (one matmul, N≈3000 columns — negligible next to the 10.8 Hz frame
   rate), turning it into a likelihood ``L = exp(-cost / T)``;
2. **predicts** by convolving the posterior with a forward transition
   kernel whose centre adapts to the tracked tempo (so tempo wobble is
   absorbed by the kernel width, not by drift);
3. injects a small **escape mass** — a distance-decaying kernel around
   the committed position plus a much smaller uniform floor — so the
   filter can *always* rebuild probability at the true position after a
   drift, preferring nearby locations over far ones; and
4. **updates** ``posterior ∝ prior · likelihood`` and renormalises.

Why this fixes the two failures the band-DP could not:

* **Detection is intrinsic.** The reported confidence is the posterior
  mass concentrated around the committed position. On junk audio / a
  wrong piece the likelihood is flat, the posterior diffuses, and
  confidence genuinely collapses — unlike the band-DP's confidence,
  which measured only in-band decisiveness and stayed 0.6–0.8 on noise.
* **Correction is intrinsic.** Because the observation spans the whole
  reference, a mistracked position keeps accruing likelihood at the
  *true* position and the posterior mass migrates back — no "stuck
  → reset" hatch is needed, and drift while still advancing is caught.

Self-similarity (the project's dominant failure mode) is handled by a
three-layer **near-position bias**: the forward-propagated prior favours
the current neighbourhood, the escape kernel decays with distance, and
a **distance-scaled commit hysteresis** demands overwhelming, sustained
evidence before teleporting the output to a far repeat.

The public surface mirrors :class:`OnlineDTWFollower` exactly
(``process_frame`` / ``freeze`` / ``unfreeze`` / ``seek`` / ``reset`` /
``force_lock_in`` and the ``is_*`` / ``*_ref_frame`` properties) so the
worker, GUI and eval harness can drive either interchangeably.
"""

from __future__ import annotations

import logging
import math
import threading
from typing import Optional

import numpy as np

from audio_score_follower.core.feature_extractor import (
    FeatureConfig,
    fused_local_cost,
)
from audio_score_follower.core.oltw_follower import FollowResult

logger = logging.getLogger(__name__)


class PosteriorFollower:
    """Streaming HMM forward filter against a fixed reference CENS matrix.

    Thread safety mirrors :class:`OnlineDTWFollower`: ``process_frame``
    is not reentrant (callers serialise it); ``freeze`` / ``unfreeze`` /
    ``seek`` and the getters take ``_state_lock`` and are safe from the
    GUI thread.
    """

    def __init__(
        self,
        reference_cens: np.ndarray,
        feature_config: FeatureConfig,
        *,
        reference_onset: Optional[np.ndarray] = None,
        chroma_weight: float = 1.0,
        onset_weight: float = 0.0,
        # --- likelihood ---
        likelihood_temperature: float = 0.08,
        # --- transition (tempo) prior ---
        min_rate: float = 0.3,
        max_rate: float = 2.0,
        rate_smoothing: float = 0.1,
        # --- band confinement (near-position prior) ---
        band_forward_seconds: float = 9.0,
        band_back_seconds: float = 3.0,
        escape_near: float = 0.02,
        escape_near_seconds: float = 4.0,
        # --- confidence-gated bounded recovery ---
        recovery_confidence: float = 0.30,
        recovery_patience_seconds: float = 1.5,
        recovery_hold_seconds: float = 1.0,
        recovery_min_mass: float = 0.40,
        recovery_forward_seconds: float = 35.0,
        recovery_back_seconds: float = 10.0,
        recovery_cost_margin: float = 0.05,
        # --- output smoothing ---
        output_forward_seconds: float = 2.0,
        output_back_seconds: float = 1.0,
        # --- confidence = concentration × match-quality ---
        commit_window_seconds: float = 2.0,
        match_cost_lo: float = 0.05,
        match_cost_hi: float = 0.22,
        # --- lock-in / confidence ---
        confidence_smoothing: int = 5,
        lock_in_frames: int = 30,
        lock_in_confidence: float = 0.50,
        inertia_confidence: float = 0.35,
        max_inertia_seconds: float = 10.0,
        init_search_width: int | None = None,
        # accepted-and-ignored (band-DP kwargs) for drop-in construction
        **_ignored_oltw_kwargs: object,
    ) -> None:
        """
        Args:
            reference_cens: (12, N) float32, L2-normalised per column.
            feature_config: must match the offline build.
            reference_onset / chroma_weight / onset_weight: fusion inputs,
                same semantics as the band-DP follower — passed straight
                to ``fused_local_cost``.
            likelihood_temperature: T in ``L = exp(-cost / T)``. Smaller
                = sharper likelihood (faster to commit, less forgiving of
                a single bad frame). Calibrated so matched cost ≈0.08 and
                junk cost ≈0.19 give a per-frame likelihood ratio ≈3×.
            min_rate / max_rate: clamp on the tracked ref-frames-per-live
                -frame tempo used to centre the transition kernel.
            rate_smoothing: EMA factor for the tracked tempo.
            band_forward_seconds / band_back_seconds: the posterior mass
                is CONFINED to this window around the committed position
                every frame. This is the near-position prior that stops
                pervasive orchestral self-similarity (whole repeated
                passages) from stealing mass to a far repeat — the naive
                whole-reference filter's fatal flaw. Forward is generous
                (tempo tolerance); backward is tight (self-similar earlier
                statements), mirroring the band-DP's asymmetric band.
            escape_near / escape_near_seconds: per-frame mass diverted to a
                distance-decaying kernel around the committed position, for
                *within-band* drift recovery. No uniform global floor — far
                jumps go exclusively through the gated recovery below.
            recovery_confidence: when smoothed confidence sits below this,
                the follower is considered possibly lost and the far
                recovery filter is engaged.
            recovery_patience_seconds: how long confidence must stay below
                ``recovery_confidence`` before far recovery starts.
            recovery_hold_seconds / recovery_min_mass: the recovery filter
                must concentrate ≥ ``recovery_min_mass`` around a candidate
                for this long before the follower jumps there.
            recovery_forward_seconds / recovery_back_seconds: recovery
                searches ONLY this bounded window around the committed
                position — never the whole reference. Drift is small
                (operator's observation), and a whole-reference search
                would find far self-similar repeats and teleport to them.
                Forward is generous (catch a run-away after a stall), back
                is tight (self-similar earlier statements).
            recovery_cost_margin: recovery only jumps if the candidate's
                absolute cost beats the current position's by this margin —
                so a legitimately-tracking position whose confidence merely
                dipped (low concentration, still low cost) is not disturbed.
            output_forward_seconds / output_back_seconds: the output
                position is the posterior peak searched only within this
                window of the previous output, rate-limiting per-frame
                motion so the count advances smoothly instead of flipping
                between competing in-band peaks. Larger in-band drift is
                absorbed gradually over frames; genuine far jumps go
                through far recovery.
            commit_window_seconds: half-width (± seconds) of the mass
                integral that gives the *concentration* factor of
                confidence.
            match_cost_lo / match_cost_hi: the *match-quality* factor of
                confidence ramps 1→0 as the smoothed local cost rises from
                ``lo`` to ``hi``. Confidence = concentration × match
                quality, so a diffuse posterior OR a poor absolute match
                (wrong piece / noise) both drive confidence down — the
                band-DP's confidence measured only in-band decisiveness and
                so stayed high on unrelated audio. Calibrated from measured
                cost: matched ≈0.08, wrong-piece ≈0.12.
            confidence_smoothing: EMA window (frames) for the reported
                confidence.
            lock_in_frames / lock_in_confidence: smoothed confidence must
                hold ≥ threshold this many consecutive frames to latch
                lock-in (monotonic; only ``reset`` clears it).
            inertia_confidence: below this smoothed confidence while
                locked in, the follower is reported as "coasting"
                (``is_in_inertia``) for GUI parity — position still
                advances via the tempo prior.
            max_inertia_seconds: reported to the GUI as the coast cap.
            init_search_width: reference frames considered for the very
                first frame's prior (defaults to whole reference).
        """
        ref = np.ascontiguousarray(reference_cens, dtype=np.float32)
        if ref.ndim != 2 or ref.shape[0] != 12:
            raise ValueError(f"reference_cens must be (12, N); got {ref.shape}")
        self._ref = ref
        self._N = ref.shape[1]
        self._cfg = feature_config
        self._fr = float(feature_config.effective_frame_rate())

        if reference_onset is not None:
            onset_arr = np.ascontiguousarray(reference_onset, dtype=np.float32)
            if onset_arr.ndim != 1 or onset_arr.shape[0] != self._N:
                raise ValueError(
                    f"reference_onset must be shape ({self._N},); "
                    f"got {onset_arr.shape}"
                )
            self._ref_onset: Optional[np.ndarray] = onset_arr
        else:
            self._ref_onset = None
        if chroma_weight < 0 or onset_weight < 0:
            raise ValueError("chroma_weight / onset_weight must be >= 0")
        self._chroma_w = float(chroma_weight)
        self._onset_w = float(onset_weight)
        self._fusion_enabled = self._ref_onset is not None and self._onset_w > 0.0

        if likelihood_temperature <= 0:
            raise ValueError("likelihood_temperature must be > 0")
        self._T = float(likelihood_temperature)
        if not 0.0 < min_rate <= max_rate:
            raise ValueError("require 0 < min_rate <= max_rate")
        self._min_rate = float(min_rate)
        self._max_rate = float(max_rate)
        self._rate_smoothing = float(rate_smoothing)
        if not 0.0 <= escape_near < 1.0:
            raise ValueError("escape_near must be in [0, 1)")
        self._escape_near = float(escape_near)
        self._escape_near_frames = max(1.0, escape_near_seconds * self._fr)

        self._band_fwd = max(1, int(round(band_forward_seconds * self._fr)))
        self._band_back = max(1, int(round(band_back_seconds * self._fr)))

        self._recovery_conf = float(recovery_confidence)
        self._recovery_patience = max(1, int(round(recovery_patience_seconds * self._fr)))
        self._recovery_hold = max(1, int(round(recovery_hold_seconds * self._fr)))
        if not 0.0 < recovery_min_mass <= 1.0:
            raise ValueError("recovery_min_mass must be in (0, 1]")
        self._recovery_min_mass = float(recovery_min_mass)
        self._recovery_fwd = max(1, int(round(recovery_forward_seconds * self._fr)))
        self._recovery_back = max(1, int(round(recovery_back_seconds * self._fr)))
        self._recovery_cost_margin = float(recovery_cost_margin)

        self._out_fwd = max(1, int(round(output_forward_seconds * self._fr)))
        self._out_back = max(1, int(round(output_back_seconds * self._fr)))
        self._commit_w = max(1, int(round(commit_window_seconds * self._fr)))
        if not 0.0 <= match_cost_lo < match_cost_hi:
            raise ValueError("require 0 <= match_cost_lo < match_cost_hi")
        self._match_cost_lo = float(match_cost_lo)
        self._match_cost_hi = float(match_cost_hi)

        self._conf_smoothing = max(1, int(confidence_smoothing))
        self._lock_in_frames = int(lock_in_frames)
        if not 0.0 <= lock_in_confidence <= 1.0:
            raise ValueError("lock_in_confidence must be in [0, 1]")
        self._lock_in_confidence = float(lock_in_confidence)
        self._inertia_confidence = float(inertia_confidence)
        self._max_inertia_seconds = float(max_inertia_seconds)
        self._init_search_width = (
            int(init_search_width) if init_search_width else self._N
        )
        if self._init_search_width <= 0:
            raise ValueError("init_search_width must be > 0")

        # Precomputed reference-frame index grid for the escape kernel.
        self._idx = np.arange(self._N, dtype=np.float32)

        self._state_lock = threading.Lock()
        self._reset_state()

        logger.info(
            "PosteriorFollower initialised: N_ref=%d, T=%.3f, "
            "band=[-%.1f,+%.1f]s, escape_near=%.3g@%.1fs, "
            "recovery<%.2f, commit_window=%.1fs, lock_in=%d@%.2f, "
            "fusion=%s (chroma=%.2f, onset=%.2f), frame_rate=%.2f Hz",
            self._N, self._T, band_back_seconds, band_forward_seconds,
            self._escape_near, escape_near_seconds, self._recovery_conf,
            commit_window_seconds, self._lock_in_frames,
            self._lock_in_confidence,
            "on" if self._fusion_enabled else "off",
            self._chroma_w, self._onset_w, self._fr,
        )

    # --------------------------------------------------------------- state
    def _reset_state(self) -> None:
        """Initialise all mutable state (called from __init__ and reset)."""
        self._alpha = np.full(self._N, 1.0 / self._N, dtype=np.float64)
        self._committed = 0
        self._live_frame_idx = 0
        self._frozen = False
        self._live_onset_frame: Optional[float] = None

        self._rate = 1.0
        self._conf_history: list[float] = []
        self._cost_history: list[float] = []
        self._smoothed_conf = 0.0

        self._locked_in = False
        self._high_conf_streak = 0
        # Confidence-gated far recovery: a secondary whole-reference
        # forward filter (gamma) that only runs while the main track is
        # lost, then teleports to the nearest concentrated candidate.
        self._lost_frames = 0
        self._gamma: Optional[np.ndarray] = None
        self._gamma_frames = 0
        # Coasting (low-evidence) bookkeeping for GUI parity.
        self._coast_frames = 0

    # ------------------------------------------------------------- runtime
    def process_frame(
        self,
        live_cens_frame: np.ndarray,
        live_onset_frame: Optional[float] = None,
    ) -> FollowResult:
        """Advance one live frame and return the new alignment estimate."""
        if self._frozen:
            return self._frozen_result()

        live = live_cens_frame.astype(np.float32, copy=False).reshape(-1)
        if live.shape[0] != 12:
            raise ValueError(
                f"live_cens_frame must be (12,); got {live_cens_frame.shape}"
            )
        self._live_onset_frame = (
            float(live_onset_frame) if live_onset_frame is not None else None
        )
        self._last_live = live

        likelihood = self._likelihood(live)

        with self._state_lock:
            if self._live_frame_idx == 0:
                self._init_posterior(likelihood)
            else:
                self._predict()
                self._apply_escape()
                self._mask_to_band()
                self._observe(likelihood)
            self._live_frame_idx += 1
            result = self._update_tracker(likelihood)
        return result

    def _likelihood(self, live: np.ndarray) -> np.ndarray:
        """Fused local cost over the *whole* reference → likelihood."""
        costs = fused_local_cost(
            self._ref,
            live,
            self._ref_onset,
            self._live_onset_frame,
            self._chroma_w,
            self._onset_w,
        ).astype(np.float64, copy=False)
        # exp(-cost/T); shift by min for numerical headroom (cancels in
        # the posterior normalisation, so it changes nothing statistically).
        return np.exp(-(costs - costs.min()) / self._T)

    def _init_posterior(self, likelihood: np.ndarray) -> None:
        """First frame: prior over the init window × likelihood."""
        prior = np.zeros(self._N, dtype=np.float64)
        hi = min(self._N, self._init_search_width)
        prior[:hi] = 1.0
        alpha = prior * likelihood
        s = alpha.sum()
        self._alpha = alpha / s if s > 0 else prior / prior.sum()
        self._committed = int(np.argmax(self._alpha))

    def _predict(self) -> None:
        """Convolve posterior with the forward tempo kernel (Δ ≥ 0)."""
        kernel = self._transition_kernel()
        out = np.zeros_like(self._alpha)
        for d, w in enumerate(kernel):
            if w == 0.0:
                continue
            if d == 0:
                out += w * self._alpha
            else:
                out[d:] += w * self._alpha[:-d]
        self._alpha = out

    def _transition_kernel(self) -> np.ndarray:
        """Forward step distribution centred on the tracked tempo."""
        v = self._rate
        dmax = int(math.ceil(v)) + 3
        ds = np.arange(dmax + 1, dtype=np.float64)
        sigma = max(0.7, 0.35 * v)
        k = np.exp(-0.5 * ((ds - v) / sigma) ** 2)
        return k / k.sum()

    def _apply_escape(self) -> None:
        """Divert a little mass to a near-decay kernel around committed.

        Within-band recovery only: after a small drift, the true position
        (still inside the band) keeps receiving seed mass the likelihood
        can amplify. There is deliberately NO uniform global floor — that
        is what let a single frame's mass flee to a far self-similar
        repeat. Far jumps go exclusively through the gated recovery.
        """
        near = np.exp(-np.abs(self._idx - self._committed) / self._escape_near_frames)
        near /= near.sum()
        self._alpha = (1.0 - self._escape_near) * self._alpha + self._escape_near * near

    def _mask_to_band(self) -> None:
        """Zero posterior mass outside the band around committed.

        The near-position prior, enforced structurally: normal dynamics
        can never place mass at a far self-similar passage. Forward extent
        is generous (tempo tolerance), backward tight (earlier statements
        of the same theme), mirroring the band-DP's asymmetric band.
        """
        lo = max(0, self._committed - self._band_back)
        hi = min(self._N, self._committed + self._band_fwd + 1)
        if lo > 0:
            self._alpha[:lo] = 0.0
        if hi < self._N:
            self._alpha[hi:] = 0.0
        s = self._alpha.sum()
        if s > 0:
            self._alpha /= s
        else:
            # Band emptied out (should not happen post-escape) — reseed flat.
            self._alpha[lo:hi] = 1.0 / (hi - lo)

    def _observe(self, likelihood: np.ndarray) -> None:
        """Bayesian update: posterior ∝ prior · likelihood."""
        alpha = self._alpha * likelihood
        s = alpha.sum()
        if s > 0:
            self._alpha = alpha / s
        # else: keep the predicted+escape prior (all-flat likelihood, e.g.
        # a silent frame) — no information to fold in this step.

    def _mass_around(self, pos: int, arr: Optional[np.ndarray] = None) -> float:
        a = self._alpha if arr is None else arr
        lo = max(0, pos - self._commit_w)
        hi = min(self._N, pos + self._commit_w + 1)
        return float(a[lo:hi].sum())

    def _cost_at(self, pos: int) -> float:
        """Fused local cost at a single reference position (for the live
        frame currently being processed)."""
        cens_cost = 1.0 - float(self._ref[:, pos] @ self._last_live)
        if self._fusion_enabled and self._live_onset_frame is not None:
            onset_cost = abs(float(self._ref_onset[pos]) - self._live_onset_frame)
            return self._chroma_w * cens_cost + self._onset_w * onset_cost
        return cens_cost

    def _score_confidence(self, committed: int) -> None:
        """confidence = concentration × match-quality, both smoothed.

        Concentration alone stayed high on unrelated audio (there is
        always *some* in-band peak); folding in the absolute match quality
        makes confidence collapse when the input does not actually match
        the score at the tracked position.
        """
        self._cost_history.append(self._cost_at(committed))
        if len(self._cost_history) > self._conf_smoothing:
            self._cost_history.pop(0)
        smoothed_cost = float(np.mean(self._cost_history))
        match_quality = float(np.clip(
            (self._match_cost_hi - smoothed_cost)
            / (self._match_cost_hi - self._match_cost_lo),
            0.0, 1.0,
        ))
        concentration = self._mass_around(committed)
        self._conf_history.append(concentration * match_quality)
        if len(self._conf_history) > self._conf_smoothing:
            self._conf_history.pop(0)
        self._smoothed_conf = float(np.mean(self._conf_history))

    def _update_tracker(self, likelihood: np.ndarray) -> FollowResult:
        """Move the committed output within the band; score confidence;
        run the confidence-gated far recovery when the track is lost."""
        prev = self._committed

        # 1. Smooth tracking: output is the posterior peak searched only
        #    within a small window of the previous output, rate-limiting
        #    per-frame motion so the count advances smoothly instead of
        #    flipping between competing in-band peaks. Larger in-band drift
        #    is absorbed gradually; far jumps go through far recovery.
        lo = max(0, prev - self._out_back)
        hi = min(self._N, prev + self._out_fwd + 1)
        committed = lo + int(np.argmax(self._alpha[lo:hi]))

        # 2. Tempo EMA — from smooth forward steps only.
        step = committed - prev
        if 0 <= step <= self._max_rate + 2:
            self._rate += self._rate_smoothing * (step - self._rate)
            self._rate = float(np.clip(self._rate, self._min_rate, self._max_rate))

        # 3. Confidence = concentration × match-quality.
        self._score_confidence(committed)

        # 4. Far recovery: while confidence is low the track may be lost
        #    (drifted beyond the band). Run a secondary whole-reference
        #    filter and teleport to the nearest concentrated candidate.
        self._committed = committed
        teleported = self._maybe_far_recovery(likelihood, prev)
        committed = self._committed
        if teleported:
            # Intentional jump: rescore fresh so a stale low confidence
            # doesn't suppress the newly-acquired track, and don't feed
            # the jump to the tempo EMA.
            self._cost_history = []
            self._conf_history = []
            self._score_confidence(committed)

        # 5. Lock-in latch + coast (inertia-parity) bookkeeping.
        if self._smoothed_conf >= self._lock_in_confidence:
            self._high_conf_streak += 1
        else:
            self._high_conf_streak = 0
        if not self._locked_in and self._high_conf_streak >= self._lock_in_frames:
            self._locked_in = True
            logger.info(
                "PosteriorFollower locked in at live_frame=%d ref_pos=%d "
                "(conf streak=%d)",
                self._live_frame_idx, committed, self._high_conf_streak,
            )
        if self._locked_in and self._smoothed_conf < self._inertia_confidence:
            self._coast_frames += 1
        else:
            self._coast_frames = 0

        cens_cost = 1.0 - float(self._ref[:, committed] @ self._last_live)
        cost_at = self._cost_at(committed)
        return FollowResult(
            ref_frame=committed,
            ref_time_sec=committed / self._fr,
            confidence=self._smoothed_conf,
            raw_local_cost=cost_at,
            band_lo=max(0, committed - self._commit_w),
            band_hi=min(self._N, committed + self._commit_w + 1),
            dist_chroma=cens_cost,
            dist_onset=0.0,
            dp_ref_frame=(
                int(np.argmax(self._gamma)) if self._gamma is not None else committed
            ),
        )

    def _maybe_far_recovery(self, likelihood: np.ndarray, prev: int) -> bool:
        """Bounded recovery, engaged only while the track is lost.

        When the committed position stalls, the true performance can run
        past the ±band forward edge and the band mask then pins the track
        forever. Recovery searches a BOUNDED window around the committed
        position (never the whole reference — drift is small, and a global
        search would find far self-similar repeats and jump to them) with
        a secondary forward filter (``gamma``). Because gamma integrates
        likelihood over time, incidental self-similar hits wash out and
        mass concentrates only where a coherent forward run exists.

        A jump requires all of: sustained low confidence, gamma
        concentrated (``recovery_min_mass``) and held (``recovery_hold``),
        and the candidate's absolute cost beating the current position by
        ``recovery_cost_margin`` — so a legitimately-tracking position
        whose confidence merely dipped is never disturbed.

        Returns True if a jump happened.
        """
        if self._smoothed_conf >= self._recovery_conf:
            # Healthy track — abandon any in-progress recovery.
            self._lost_frames = 0
            self._gamma = None
            return False

        self._lost_frames += 1
        if self._lost_frames < self._recovery_patience:
            return False

        lo = max(0, prev - self._recovery_back)
        hi = min(self._N, prev + self._recovery_fwd + 1)

        # Engage / advance the secondary bounded forward filter.
        if self._gamma is None:
            self._gamma = self._bounded_normalise(likelihood, lo, hi)
            self._gamma_frames = 0
            return False
        kernel = self._transition_kernel()
        out = np.zeros_like(self._gamma)
        for d, w in enumerate(kernel):
            if w == 0.0:
                continue
            if d == 0:
                out += w * self._gamma
            else:
                out[d:] += w * self._gamma[:-d]
        g = self._bounded_normalise(out * likelihood, lo, hi)
        if g is None:
            return False
        self._gamma = g
        self._gamma_frames += 1

        cand = lo + int(np.argmax(self._gamma[lo:hi]))
        cand_mass = self._mass_around(cand, self._gamma)
        cost_cur = self._cost_at(prev)
        cost_cand = self._cost_at(cand)
        if (
            self._gamma_frames >= self._recovery_hold
            and cand_mass >= self._recovery_min_mass
            and cost_cur - cost_cand >= self._recovery_cost_margin
        ):
            logger.info(
                "PosteriorFollower recovery: ref_frame %d→%d "
                "(dist=%.1fs, gamma mass=%.2f, cost %.3f→%.3f, held %d fr)",
                prev, cand, (cand - prev) / self._fr,
                cand_mass, cost_cur, cost_cand, self._gamma_frames,
            )
            self._seed_bump(cand, forward_biased=False)
            self._committed = cand
            self._gamma = None
            self._lost_frames = 0
            return True
        return False

    def _bounded_normalise(
        self, arr: np.ndarray, lo: int, hi: int
    ) -> Optional[np.ndarray]:
        """Zero ``arr`` outside [lo, hi) and renormalise (None if empty)."""
        out = np.zeros_like(arr)
        seg = arr[lo:hi]
        s = seg.sum()
        if s <= 0:
            return None
        out[lo:hi] = seg / s
        return out

    def _frozen_result(self) -> FollowResult:
        """Held position while frozen (silence gate). Confidence 0 so no
        trigger fires on a guessed spot — same contract as the band-DP."""
        c = self._committed
        return FollowResult(
            ref_frame=c,
            ref_time_sec=c / self._fr,
            confidence=0.0,
            raw_local_cost=float("nan"),
            band_lo=max(0, c - self._commit_w),
            band_hi=min(self._N, c + self._commit_w + 1),
            dp_ref_frame=c,
        )

    # ------------------------------------------------------------- control
    def force_lock_in(self) -> None:
        """Externally force the lock-in latch ON. Idempotent."""
        with self._state_lock:
            if self._locked_in:
                return
            self._locked_in = True
        logger.info(
            "PosteriorFollower lock-in forced (manual) at ref_pos=%d",
            self._committed,
        )

    def freeze(self) -> None:
        """Suspend the filter (silence gate). Posterior is retained; no
        predict/update runs until ``unfreeze``. No rewind/reseed needed —
        there is no cumulative DP table to go stale."""
        with self._state_lock:
            if self._frozen:
                return
            self._frozen = True
        logger.info("PosteriorFollower frozen at ref_frame=%d", self._committed)

    def unfreeze(self) -> None:
        """Resume the filter. The retained posterior means the next
        observed frames immediately re-concentrate mass at the true
        position (the escape channel guarantees recovery if it moved)."""
        with self._state_lock:
            if not self._frozen:
                return
            self._frozen = False
        logger.info("PosteriorFollower unfrozen at ref_frame=%d", self._committed)

    def seek(self, ref_frame: int, *, allow_catchup: bool = True) -> None:
        """Re-initialise the posterior as a bump around ``ref_frame``.

        ``allow_catchup`` (forward → override) widens the bump forward so
        the likelihood can settle onto a performance that is *past* the
        seeked target; a backward (← ) override keeps it symmetric.
        """
        ref_frame = max(0, min(int(ref_frame), self._N - 1))
        with self._state_lock:
            self._seed_bump(ref_frame, forward_biased=allow_catchup)
            self._committed = ref_frame
            self._lost_frames = 0
            self._gamma = None
            self._conf_history.clear()
            self._cost_history.clear()
            self._smoothed_conf = 0.0
            self._coast_frames = 0
            if self._live_frame_idx == 0:
                self._live_frame_idx = 1
        logger.info(
            "PosteriorFollower seek: ref_frame=%d (%.2fs)%s",
            ref_frame, ref_frame / self._fr,
            " [catchup armed]" if allow_catchup else "",
        )

    def _seed_bump(self, center: int, *, forward_biased: bool) -> None:
        """Concentrate the posterior around ``center`` (± ~commit window)."""
        sigma = float(self._commit_w)
        d = self._idx - center
        bump = np.exp(-0.5 * (d / sigma) ** 2)
        if forward_biased:
            # Fatten the forward tail so a → override can catch up to a
            # performance already past the target.
            fwd = (d > 0) & (d < self._band_fwd)
            bump[fwd] = np.maximum(bump[fwd], 0.5)
        self._alpha = (bump / bump.sum()).astype(np.float64)

    def reset(self) -> None:
        """Wipe all state — used when loading a new movement."""
        with self._state_lock:
            self._reset_state()
        logger.info("PosteriorFollower reset")

    # ------------------------------------------------------------- getters
    @property
    def current_ref_frame(self) -> int:
        return self._committed

    @property
    def dp_ref_frame(self) -> int:
        """Raw global-argmax position (pre commit-hysteresis). Debug."""
        return int(np.argmax(self._alpha))

    @property
    def current_ref_time_sec(self) -> float:
        return self._committed / self._fr

    @property
    def n_ref_frames(self) -> int:
        return self._N

    @property
    def is_frozen(self) -> bool:
        return self._frozen

    @property
    def is_locked_in(self) -> bool:
        return self._locked_in

    @property
    def is_in_inertia(self) -> bool:
        """Locked in but coasting on low evidence (GUI parity)."""
        return self._locked_in and self._coast_frames > 0

    @property
    def inertia_elapsed_sec(self) -> float:
        if self._coast_frames == 0:
            return 0.0
        return self._coast_frames / self._fr

    @property
    def max_inertia_seconds(self) -> float:
        return self._max_inertia_seconds
