#!/usr/bin/env python3
"""
core/result_handler.py - OLTW per-frame result handling.

Extracted from ``main.AudioScoreFollowerApp._on_oltw_result``. Runs on
the OLTW worker thread, once per CENS frame (~10 Hz). Maps the reference
time to a measure, mirrors state into AppState (measure/beat, internal +
display confidence, mismatch, follower mode), optionally feeds the
realtime visualiser, detects anomalous measure jumps, and emits a
throttled diagnostic log.

The follower, warp lookup and score mapper are recreated on every
movement load, so this handler reads them through getter callables.
``get_last_seek_time`` returns the wall-clock time of the app's most
recent forward re-anchor so the jump detector can suppress the expected
post-seek jump during the grace period.
"""

from __future__ import annotations

import logging
import math
import time
from collections import deque
from typing import Callable, Optional

from audio_score_follower.core.oltw_follower import FollowResult, OnlineDTWFollower
from audio_score_follower.core.score_mapper import ScoreMapper
from audio_score_follower.core.state_manager import AppState
from audio_score_follower.core.viz_feed import VizFeed
from audio_score_follower.core.warp_lookup import WarpLookup

logger = logging.getLogger(__name__)

# Maximum measure jump allowed between consecutive OLTW frames without a
# preceding user seek. At 200 BPM 4/4 with 4× warp slope (the build-time
# limit) the measure advances <1 per 0.093s frame — so jumps >3 are anomalous.
_MAX_FRAME_MEASURE_JUMP = 3
# Suppress the jump-anomaly alert for this long after a user-initiated seek
# (jumps right after a seek are expected, not a warp path anomaly).
_SEEK_GRACE_SEC = 2.0
# Operator-facing (display) confidence: match-quality ramp over the
# smoothed ABSOLUTE fused local cost. The OLTW's internal confidence is
# band-relative and floors at ~0.6-0.8 even on unrelated audio (the
# non-negative chroma cosine floor), which misleads the operator ("piano
# BGM reads 70%"). This ramp maps smoothed cost LO→HI onto 1→0.
# Calibrated on 幻想4 measurements (2026-07): same recording p50=0.014,
# alt performance p50=0.082/p90=0.159, wrong movement p50=0.189,
# unrelated piano p50=0.300 → same ≈100%, alt ≈40-90%, piano ≈0%.
# Display only — lock-in / trigger floor / resync keep the internal scale.
_DISPLAY_CONF_COST_LO = 0.05
_DISPLAY_CONF_COST_HI = 0.22
# Frames of cost smoothing for the display ramp (matches the OLTW's own
# confidence_smoothing default; ~0.46s at 10.77 Hz).
_DISPLAY_CONF_SMOOTHING = 5


def display_confidence_from_cost(smoothed_cost: float) -> float:
    """Map a smoothed fused local cost to the operator-facing confidence.

    Linear ramp: cost <= LO → 1.0, cost >= HI → 0.0. NaN (frozen frames,
    where OLTW reports no cost) → 0.0.
    """
    if math.isnan(smoothed_cost):
        return 0.0
    span = _DISPLAY_CONF_COST_HI - _DISPLAY_CONF_COST_LO
    return max(0.0, min(1.0, (_DISPLAY_CONF_COST_HI - smoothed_cost) / span))


class OltwResultHandler:
    """Per-frame OLTW result → AppState / viz / diagnostics."""

    def __init__(
        self,
        *,
        state: AppState,
        viz_feed: Optional[VizFeed],
        get_oltw: Callable[[], Optional[OnlineDTWFollower]],
        get_warp_lookup: Callable[[], Optional[WarpLookup]],
        get_score_mapper: Callable[[], Optional[ScoreMapper]],
        get_last_seek_time: Callable[[], float],
    ) -> None:
        self.state = state
        self.viz_feed = viz_feed
        self._get_oltw = get_oltw
        self._get_warp_lookup = get_warp_lookup
        self._get_score_mapper = get_score_mapper
        self._get_last_seek_time = get_last_seek_time

        # Runtime jump detection: previous measure seen. Deliberately NOT
        # reset on movement load (matches the original inline behaviour).
        self._prev_oltw_measure: int = 0
        # Diagnostic log throttle: emit one OLTW state log per wall-clock
        # second. on_result fires per CENS frame (~10 Hz) and would
        # otherwise flood the log.
        self._last_diag_log_sec = 0
        # Rolling window of fused local costs feeding the operator-facing
        # display confidence (see display_confidence_from_cost). Only
        # touched from the OLTW worker thread (on_result).
        self._display_cost_window: deque[float] = deque(
            maxlen=_DISPLAY_CONF_SMOOTHING
        )

    def reset_for_movement(self) -> None:
        """Clear the display-confidence smoothing window on movement load."""
        self._display_cost_window.clear()

    def on_result(self, result: FollowResult) -> None:
        """Called from the OLTW worker thread per CENS frame."""
        mapper = self._get_score_mapper()
        lookup = self._get_warp_lookup()
        if mapper is None or lookup is None:
            return
        try:
            measure, beat_in_measure, continuous_beat = lookup.ref_to_measure_and_beat(
                result.ref_time_sec, mapper
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("ref→measure failed: %s", exc)
            return

        # GUI displays 1-indexed beat-in-measure (downbeat = 1.0).
        beat_in_measure_display = beat_in_measure + 1.0
        self.state.update_beat_measure(
            continuous_beat, measure, beat_in_measure_display
        )
        self.state.set_confidence(result.confidence)
        self.state.set_mismatch(result.is_mismatched)
        # Operator-facing confidence: absolute match quality from the
        # smoothed fused cost. Frozen frames report NaN cost → display 0
        # without polluting the smoothing window.
        if math.isnan(result.raw_local_cost):
            display_conf = 0.0
        else:
            self._display_cost_window.append(result.raw_local_cost)
            smoothed = sum(self._display_cost_window) / len(self._display_cost_window)
            display_conf = display_confidence_from_cost(smoothed)
        self.state.set_display_confidence(display_conf)

        # Feed the realtime visualiser (--viz only). Reuses the measure and
        # display confidence already computed above; no-op when disabled.
        if self.viz_feed is not None:
            # Ground the "演奏位置さがし" panel in real measure numbers: map
            # the band edges and the DP peak (reference frames) to measures
            # via the same warp lookup. ~3 cheap lookups/frame at 10 Hz.
            frame_rate = lookup.feature_config.effective_frame_rate()

            def _frame_measure(frame: int):
                try:
                    return lookup.ref_to_measure_and_beat(
                        frame / frame_rate, mapper
                    )[0]
                except Exception:  # noqa: BLE001 — edge frames may fall off
                    return None

            self.viz_feed.push(
                result,
                measure=measure,
                display_confidence=display_conf,
                band_lo_measure=_frame_measure(result.band_lo),
                band_hi_measure=_frame_measure(max(result.band_lo, result.band_hi - 1)),
                peak_measure=_frame_measure(result.dp_ref_frame),
            )
        # Mirror OLTW follower mode into AppState so the GUI tracking
        # panel reflects lock-in / inertia transitions in real time.
        oltw = self._get_oltw()
        if oltw is not None:
            self.state.set_follower_mode(
                is_locked_in=oltw.is_locked_in,
                is_in_inertia=oltw.is_in_inertia,
                inertia_elapsed_sec=oltw.inertia_elapsed_sec,
                inertia_cap_sec=oltw.max_inertia_seconds,
            )

        # Runtime jump detection: large measure jumps between consecutive
        # frames (outside the seek grace period) indicate a warp path
        # anomaly that should have been caught by asf-build --validate.
        jump = abs(measure - self._prev_oltw_measure)
        if (
            jump > _MAX_FRAME_MEASURE_JUMP
            and self._prev_oltw_measure != 0  # skip first frame (initialisation)
            and (time.monotonic() - self._get_last_seek_time()) > _SEEK_GRACE_SEC
        ):
            logger.error(
                "異常な小節ジャンプを検出: %d → %d (+%d 小節) at ref_t=%.2fs。"
                "warp path の勾配が異常です。asf-build をやり直してください。",
                self._prev_oltw_measure, measure, jump, result.ref_time_sec,
            )
        self._prev_oltw_measure = measure

        # Throttled diagnostic log: emit once per wall-clock second so
        # `--verbose` doesn't drown in per-frame entries. Lets the
        # operator watch measure / confidence / cost / band live to
        # diagnose stuck or skipping behaviour. Includes mic dBFS in
        # live-mic mode so the user can spot "mic too quiet → noise
        # dominates chroma → OLTW stuck" failure modes.
        now_sec = int(time.time())
        if now_sec != self._last_diag_log_sec:
            self._last_diag_log_sec = now_sec
            try:
                snap = self.state.get_all()
                mic_db = snap.get("mic_level_db")
                mic_part = (
                    f" mic={mic_db:+.0f}dBFS" if mic_db is not None else ""
                )
            except Exception:  # noqa: BLE001
                mic_part = ""
            logger.info(
                "OLTW: m=%d β=%.2f conf=%.2f disp=%.2f raw_cost=%.3f "
                "ref_t=%.1fs band=[%d,%d)%s",
                measure, beat_in_measure_display, result.confidence,
                self.state.get_all().get("display_confidence", 0.0),
                result.raw_local_cost, result.ref_time_sec,
                result.band_lo, result.band_hi, mic_part,
            )
