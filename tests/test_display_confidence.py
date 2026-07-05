"""Tests for the operator-facing (cost-based) display confidence.

The OLTW's internal confidence is band-relative and floors at ~0.6-0.8
even on unrelated audio (non-negative chroma cosine floor), which misled
operators ("unrelated piano BGM reads 70%"). The display confidence maps
the smoothed ABSOLUTE fused cost onto [0, 1] using thresholds calibrated
from real measurements (see main.py constants).
"""

from __future__ import annotations

import math

from audio_score_follower.main import (
    _DISPLAY_CONF_COST_HI,
    _DISPLAY_CONF_COST_LO,
    display_confidence_from_cost,
)


def test_perfect_match_reads_full():
    # Same-recording smoothed cost (measured p50 = 0.014).
    assert display_confidence_from_cost(0.014) == 1.0
    assert display_confidence_from_cost(_DISPLAY_CONF_COST_LO) == 1.0


def test_alt_performance_reads_high():
    # Different performance of the correct piece (measured p50 = 0.082).
    assert 0.7 < display_confidence_from_cost(0.082) < 0.9


def test_unrelated_piano_reads_zero():
    # Unrelated piano BGM (measured p50 = 0.300) — the operator complaint
    # this feature fixes: the internal confidence showed 60-80% here.
    assert display_confidence_from_cost(0.300) == 0.0
    assert display_confidence_from_cost(_DISPLAY_CONF_COST_HI) == 0.0


def test_frozen_nan_reads_zero():
    assert display_confidence_from_cost(float("nan")) == 0.0


def test_monotonic_ramp():
    prev = 1.1
    for cost in (0.0, 0.05, 0.10, 0.15, 0.20, 0.25):
        cur = display_confidence_from_cost(cost)
        assert cur <= prev
        assert 0.0 <= cur <= 1.0
        prev = cur
