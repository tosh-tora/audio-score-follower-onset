#!/usr/bin/env python3
"""
cooldown_timer.py - Trigger Rate Limiting

Prevents rapid re-triggering of the same measure.
Grace period between consecutive triggers.
"""

import logging
import time

logger = logging.getLogger(__name__)


class CooldownTimer:
    """
    Manages per-measure trigger cooldown.

    Once a measure triggers, it enters a grace period (default 3 seconds).
    Repeated triggers on the same measure are blocked until grace period expires.
    """

    def __init__(self, duration_sec: float = 3.0):
        """
        Initialize cooldown timer.

        Args:
            duration_sec: Grace period in seconds
        """
        self.duration = duration_sec
        self.triggered_measures = {}  # measure_num → timestamp of last trigger

    def should_trigger(self, measure: int) -> bool:
        """
        Check if a measure can be triggered right now.

        Args:
            measure: Measure number

        Returns:
            True if trigger is allowed (not in cooldown), False otherwise
        """
        if measure not in self.triggered_measures:
            return True

        elapsed = time.time() - self.triggered_measures[measure]
        can_trigger = elapsed >= self.duration

        if can_trigger:
            logger.debug(f"Measure {measure}: cooldown expired, can retrigger")
        else:
            logger.debug(
                f"Measure {measure}: in cooldown ({self.duration - elapsed:.1f}s remaining)"
            )

        return can_trigger

    def mark_triggered(self, measure: int):
        """
        Record that a measure was triggered.

        Args:
            measure: Measure number
        """
        self.triggered_measures[measure] = time.time()
        logger.debug(f"Measure {measure}: marked triggered, cooldown {self.duration}s")

    def cleanup_old(self, max_age_sec: float = 10.0):
        """
        Remove old entries to prevent memory leak.

        Args:
            max_age_sec: Remove entries older than this
        """
        now = time.time()
        before_count = len(self.triggered_measures)

        self.triggered_measures = {
            m: t for m, t in self.triggered_measures.items()
            if now - t < max_age_sec
        }

        after_count = len(self.triggered_measures)
        if after_count < before_count:
            logger.debug(f"Cleaned {before_count - after_count} old cooldown entries")

    def __repr__(self) -> str:
        return f"CooldownTimer(duration={self.duration}s, tracked={len(self.triggered_measures)})"
