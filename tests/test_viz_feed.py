"""Unit tests for VizFeed (headless — no Tk)."""

from __future__ import annotations

import math
import threading

import numpy as np

from audio_score_follower.core.oltw_follower import FollowResult
from audio_score_follower.core.viz_feed import VizFeed, VizThresholds, _HISTORY_LEN


def _thresholds() -> VizThresholds:
    return VizThresholds(
        display_conf_cost_lo=0.05,
        display_conf_cost_hi=0.22,
        mismatch_cost=0.18,
    )


def _result(cost=0.1, band=None, live=None, ref=None, band_lo=0,
            dp=0, mismatch=False) -> FollowResult:
    return FollowResult(
        ref_frame=dp,
        ref_time_sec=0.0,
        confidence=0.8,
        raw_local_cost=cost,
        band_lo=band_lo,
        band_hi=band_lo + (0 if band is None else len(band)),
        dist_chroma=0.05,
        dist_onset=0.02,
        dp_ref_frame=dp,
        is_mismatched=mismatch,
        band_costs=band,
        live_chroma=live,
        ref_chroma=ref,
    )


def test_empty_snapshot():
    feed = VizFeed(_thresholds())
    snap = feed.snapshot()
    assert snap["frame_count"] == 0
    assert snap["cost"] == []
    assert snap["live_chroma"] is None
    assert snap["band_costs"] is None


def test_push_records_scalars():
    feed = VizFeed(_thresholds())
    feed.push(_result(cost=0.12, mismatch=True), measure=7, display_confidence=0.6)
    snap = feed.snapshot()
    assert snap["frame_count"] == 1
    assert snap["cost"] == [0.12]
    assert snap["measure"] == 7
    assert snap["display_confidence"] == 0.6
    assert snap["is_mismatched"] is True
    assert snap["mismatch"] == [True]


def test_push_records_arrays_as_copies():
    feed = VizFeed(_thresholds())
    band = np.array([0.3, 0.1, 0.2], dtype=np.float32)
    live = np.arange(12, dtype=np.float32)
    ref = np.arange(12, dtype=np.float32) * 2
    feed.push(
        _result(band=band, live=live, ref=ref, band_lo=5, dp=6),
        measure=1, display_confidence=0.9,
    )
    snap = feed.snapshot()
    assert snap["band_lo"] == 5
    assert snap["dp_ref_frame"] == 6
    np.testing.assert_array_equal(snap["band_costs"], band)
    # Mutating the source array after push must not change the snapshot.
    band[:] = -1.0
    snap2 = feed.snapshot()
    assert snap2["band_costs"][0] == 0.3


def test_band_measures_recorded():
    feed = VizFeed(_thresholds())
    band = np.array([0.3, 0.1, 0.2], dtype=np.float32)
    feed.push(
        _result(band=band, live=np.zeros(12, np.float32),
                ref=np.zeros(12, np.float32), band_lo=5, dp=6),
        measure=37, display_confidence=0.9,
        band_lo_measure=33, band_hi_measure=41, peak_measure=37,
    )
    snap = feed.snapshot()
    assert snap["band_lo_measure"] == 33
    assert snap["band_hi_measure"] == 41
    assert snap["peak_measure"] == 37


def test_band_measures_default_none():
    """Omitting band measures (edge/frozen frames) degrades to None."""
    feed = VizFeed(_thresholds())
    feed.push(
        _result(band=np.array([0.2, 0.1], np.float32),
                live=np.zeros(12, np.float32), ref=np.zeros(12, np.float32)),
        measure=1, display_confidence=0.5,
    )
    snap = feed.snapshot()
    assert snap["band_lo_measure"] is None
    assert snap["peak_measure"] is None


def test_history_bounded():
    feed = VizFeed(_thresholds())
    for i in range(_HISTORY_LEN + 50):
        feed.push(_result(cost=float(i)), measure=i, display_confidence=0.5)
    snap = feed.snapshot()
    assert len(snap["cost"]) == _HISTORY_LEN
    # Oldest entries dropped; newest retained.
    assert snap["cost"][-1] == float(_HISTORY_LEN + 49)
    assert snap["frame_count"] == _HISTORY_LEN + 50


def test_nan_cost_kept_in_history():
    """Frozen frames report NaN cost — kept so the strip shows the gap."""
    feed = VizFeed(_thresholds())
    feed.push(_result(cost=float("nan")), measure=1, display_confidence=0.0)
    snap = feed.snapshot()
    assert math.isnan(snap["cost"][0])


def test_arrays_persist_when_next_frame_omits_them():
    """A frame without arrays (e.g. frozen) leaves the last arrays intact."""
    feed = VizFeed(_thresholds())
    band = np.array([0.2, 0.1], dtype=np.float32)
    feed.push(_result(band=band, live=np.zeros(12, np.float32),
                      ref=np.zeros(12, np.float32)),
              measure=1, display_confidence=0.9)
    feed.push(_result(cost=float("nan")), measure=2, display_confidence=0.0)
    snap = feed.snapshot()
    assert snap["band_costs"] is not None
    np.testing.assert_array_equal(snap["band_costs"], band)


def test_thread_safety_smoke():
    """Concurrent producers + a consumer must not raise or corrupt counts."""
    feed = VizFeed(_thresholds())
    n_per = 500

    def producer():
        for i in range(n_per):
            feed.push(_result(cost=0.1), measure=i, display_confidence=0.5)

    threads = [threading.Thread(target=producer) for _ in range(3)]
    for t in threads:
        t.start()
    for _ in range(200):
        feed.snapshot()
    for t in threads:
        t.join()
    assert feed.snapshot()["frame_count"] == 3 * n_per
