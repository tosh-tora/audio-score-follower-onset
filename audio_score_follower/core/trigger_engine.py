#!/usr/bin/env python3
"""
core/trigger_engine.py - Slide-trigger firing + manual overrides.

Extracted from ``main.AudioScoreFollowerApp``. Owns the trigger-executor
daemon thread (polls the current measure from AppState and fires slide
presses when a trigger measure is reached), plus the manual →/← override
helpers that fire a slide and re-anchor the follower.

The follower, warp lookup and score mapper are recreated on every
movement load, so this engine reads them through getter callables rather
than capturing (soon-stale) references. A ``notify_seek`` callback lets
the app record the wall-clock time of forward re-anchors so its jump
detector can suppress the expected post-seek jump.

Firing conditions (all AND):
  1. current measure == a trigger measure
  2. smoothed confidence >= _TRIGGER_CONFIDENCE_FLOOR
  3. not flagged as mismatched
  4. CooldownTimer.should_trigger() allows it
  5. that measure hasn't fired yet (_fired_trigger_measures)
  6. the operator hasn't ended the performance (state.performance_ended,
     Issue #44 — 「■ 演奏終了」 / E stops the worker + suppresses triggers)
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Dict, Optional

from audio_score_follower.core.cooldown_timer import CooldownTimer
from audio_score_follower.core.oltw_follower import OnlineDTWFollower
from audio_score_follower.core.score_mapper import ScoreMapper
from audio_score_follower.core.state_manager import AppState
from audio_score_follower.core.warp_lookup import WarpLookup

logger = logging.getLogger(__name__)

# How often the trigger executor checks for measure hits.
_TRIGGER_POLL_HZ = 20
# Minimum smoothed OLTW confidence before triggers are allowed to fire.
# Acts as the "lock-in" condition that InertiaEngine provided in
# live-score-sync; below this, alignment hasn't stabilised yet and
# firing the measure-1 trigger at startup would be spurious.
_TRIGGER_CONFIDENCE_FLOOR = 0.30


class TriggerEngine:
    """Trigger-executor thread + manual slide overrides."""

    def __init__(
        self,
        *,
        state: AppState,
        cooldown: CooldownTimer,
        slide_controller,
        stop_event: threading.Event,
        get_oltw: Callable[[], Optional[OnlineDTWFollower]],
        get_warp_lookup: Callable[[], Optional[WarpLookup]],
        get_score_mapper: Callable[[], Optional[ScoreMapper]],
        get_cooldown_seconds: Callable[[], float],
        notify_seek: Callable[[], None],
    ) -> None:
        self.state = state
        self.cooldown = cooldown
        self.slide_controller = slide_controller
        self._stop_event = stop_event
        self._get_oltw = get_oltw
        self._get_warp_lookup = get_warp_lookup
        self._get_score_mapper = get_score_mapper
        self._get_cooldown_seconds = get_cooldown_seconds
        self._notify_seek = notify_seek

        self._fired_trigger_measures: set[int] = set()
        self._thread: Optional[threading.Thread] = None

    # -------------------------------------------------- lifecycle
    def start(self) -> None:
        """Spawn the trigger-executor daemon thread."""
        self._thread = threading.Thread(
            target=self._run_loop, name="trigger-executor", daemon=True
        )
        self._thread.start()

    def reset_for_movement(self) -> None:
        """Clear the fired-trigger set on movement (re)load."""
        self._fired_trigger_measures.clear()

    # -------------------------------------------------- trigger loop
    def _run_loop(self) -> None:
        logger.info("Trigger loop started (%.0f Hz)", _TRIGGER_POLL_HZ)
        interval = 1.0 / _TRIGGER_POLL_HZ
        while not self._stop_event.is_set():
            try:
                snapshot = self.state.get_all()
                triggers = self.state.current_triggers
                current_measure = snapshot["measure"]

                if not triggers:
                    time.sleep(interval)
                    continue

                # Operator pressed 「■ 演奏終了」 (Issue #44): the follower
                # worker is stopped, so the measure count is stale. Suppress
                # firing and clear the next-trigger display.
                if snapshot.get("performance_ended"):
                    self.state.set_next_trigger(None)
                    time.sleep(interval)
                    continue

                upcoming = [
                    t["measure"] for t in triggers
                    if t["measure"] > current_measure
                    and t["measure"] not in self._fired_trigger_measures
                ]
                self.state.set_next_trigger(min(upcoming) if upcoming else None)

                # Don't fire until OLTW has locked in.
                if snapshot["confidence"] < _TRIGGER_CONFIDENCE_FLOOR:
                    time.sleep(interval)
                    continue

                # Don't fire while the drift detector suspects the count
                # has lost the performance — advancing slides on a
                # mistracked position is worse than a late slide the
                # operator fixes manually.
                if snapshot.get("is_mismatched"):
                    time.sleep(interval)
                    continue

                if snapshot["cooldown_active"]:
                    time.sleep(interval)
                    continue

                for trig in triggers:
                    if trig["measure"] != current_measure:
                        continue
                    if current_measure in self._fired_trigger_measures:
                        continue
                    if not self.cooldown.should_trigger(current_measure):
                        continue

                    action = trig.get("action", "right")
                    self.execute_action(action, source="auto", trigger=trig)
                    self.cooldown.mark_triggered(current_measure)
                    self.state.activate_cooldown(self._get_cooldown_seconds())
                    self._fired_trigger_measures.add(current_measure)
                    break
            except Exception as exc:  # noqa: BLE001
                logger.error("Trigger loop error: %s", exc, exc_info=True)
            time.sleep(interval)
        logger.info("Trigger loop exiting")

    def execute_action(
        self,
        action: str,
        *,
        source: str = "auto",
        trigger: Optional[Dict] = None,
    ) -> None:
        """Send a single slide keypress and log it with provenance.

        Args:
            action: "right" or "left".
            source: "auto" (fired by trigger loop from OLTW position) or
                "manual" (user pressed ← / → / Space). Goes into the
                log line so post-hoc review can tell which advances
                were the human compensating for tracking drift.
            trigger: the trigger dict (with measure, note) when known;
                included in the log for context.
        """
        try:
            self.slide_controller.press(action)
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "Slide action %s failed [%s]: %s", action, source, exc, exc_info=True
            )
            return
        if trigger is not None:
            logger.info(
                "Slide %s [%s] measure=%d note=%s",
                action, source, trigger.get("measure"),
                trigger.get("note", ""),
            )
        else:
            logger.info("Slide %s [%s]", action, source)

    # ----------------------- manual override helpers -----------------
    def _find_next_pending_trigger(self) -> Optional[Dict]:
        """Lowest-measure trigger that hasn't been fired yet.

        Returns None when every trigger in the current movement has
        already fired.
        """
        triggers = self.state.current_triggers
        pending = [
            t for t in triggers
            if t["measure"] not in self._fired_trigger_measures
        ]
        if not pending:
            return None
        return min(pending, key=lambda t: t["measure"])

    def _find_last_fired_trigger(self) -> Optional[Dict]:
        """Highest-measure trigger that has been fired so far.

        We use "highest measure" rather than "most-recently-fired by
        wall-clock time" because triggers naturally fire in score order
        during live performance; if the user has been pressing manual
        overrides out of order they probably want to undo the latest
        position in the score, not the latest in time.
        """
        if not self._fired_trigger_measures:
            return None
        last_measure = max(self._fired_trigger_measures)
        triggers = self.state.current_triggers
        for t in triggers:
            if t["measure"] == last_measure:
                return t
        return None

    def _seek_oltw_to_ref_time(
        self, ref_time_sec: float, *, allow_catchup: bool = True
    ) -> None:
        oltw = self._get_oltw()
        warp_lookup = self._get_warp_lookup()
        if oltw is None or warp_lookup is None:
            return
        fr = warp_lookup.feature_config.effective_frame_rate()
        target_frame = int(round(ref_time_sec * fr))
        oltw.seek(target_frame, allow_catchup=allow_catchup)
        self._notify_seek()

    def advance_to_next_trigger(self) -> None:
        """User pressed →: send slide right, then re-sync OLTW.

        The user is saying "the performance is at or past the next
        trigger measure" — so we (1) send the press, (2) mark that
        trigger as fired so the auto loop doesn't double-fire, and
        (3) seek OLTW to that measure's reference time so future
        auto-triggers fire from the correct downstream context.
        """
        nxt = self._find_next_pending_trigger()
        warp_lookup = self._get_warp_lookup()
        score_mapper = self._get_score_mapper()
        if nxt is None or warp_lookup is None or score_mapper is None:
            # No trigger to consume — fall back to bare slide press.
            self.execute_action("right", source="manual")
            return

        measure = int(nxt["measure"])
        try:
            ref_t = warp_lookup.measure_to_ref_time(measure, score_mapper)
        except Exception as exc:  # noqa: BLE001
            logger.error("measure_to_ref_time failed for m=%d: %s", measure, exc)
            self.execute_action("right", source="manual")
            return

        self.execute_action(nxt.get("action", "right"), source="manual", trigger=nxt)
        self.cooldown.mark_triggered(measure)
        self.state.activate_cooldown(self._get_cooldown_seconds())
        self._fired_trigger_measures.add(measure)
        self._seek_oltw_to_ref_time(ref_t)
        logger.info(
            "Manual sync: OLTW re-anchored to measure %d (ref_t=%.2fs)",
            measure, ref_t,
        )

    def back_to_prev_trigger(self) -> None:
        """User pressed ←: send slide left, then re-sync OLTW backwards.

        The user is saying "the performance is BEFORE the most-recent
        slide change". We (1) send the left press, (2) un-fire the
        most-recently fired trigger so it can fire again when the
        music re-enters that region, and (3) seek OLTW back to just
        BEFORE that measure so we won't immediately re-trigger it
        on the next live frame.
        """
        last = self._find_last_fired_trigger()
        warp_lookup = self._get_warp_lookup()
        score_mapper = self._get_score_mapper()
        if last is None or warp_lookup is None or score_mapper is None:
            self.execute_action("left", source="manual")
            return

        measure = int(last["measure"])
        try:
            ref_t = warp_lookup.measure_to_ref_time(measure, score_mapper)
        except Exception as exc:  # noqa: BLE001
            logger.error("measure_to_ref_time failed for m=%d: %s", measure, exc)
            self.execute_action("left", source="manual")
            return

        self.execute_action("left", source="manual", trigger=last)
        self.cooldown.unmark_triggered(measure)
        self._fired_trigger_measures.discard(measure)
        # Seek to a frame slightly BEFORE the measure's start so the
        # auto loop won't immediately re-fire on the next OLTW tick.
        # No post-seek catchup: the operator says the music is BEFORE
        # this point, so an automatic forward scan would re-defeat
        # the back-step. This backward re-anchor deliberately does NOT
        # notify_seek — the jump detector's grace period is only for
        # forward catchup jumps.
        fr = warp_lookup.feature_config.effective_frame_rate()
        pre_frame = max(0, int(round(ref_t * fr)) - max(1, int(round(0.2 * fr))))
        oltw = self._get_oltw()
        if oltw is not None:
            oltw.seek(pre_frame, allow_catchup=False)
        logger.info(
            "Manual sync: OLTW re-anchored before measure %d "
            "(ref_frame=%d, ~%.2fs)",
            measure, pre_frame, pre_frame / fr,
        )
