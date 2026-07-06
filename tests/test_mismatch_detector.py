"""Tests for the OLTW mismatch (drift) detector + bounded forward recovery.

Detection watches the ABSOLUTE smoothed fused cost: sustained excess over
``mismatch_cost_threshold`` raises ``is_mismatched`` (triggers suppressed,
GUI warning); a triple-guarded bounded forward probe then attempts an
automatic re-anchor once per probe interval.

Test conventions follow CLAUDE.md: ``np.zeros(12)`` chroma guarantees a
high cosine cost (1.0); self-alignment on random unit chroma guarantees a
near-zero cost.
"""

from __future__ import annotations

import numpy as np

from audio_score_follower.core.feature_extractor import FeatureConfig
from audio_score_follower.core.oltw_follower import OnlineDTWFollower


def _random_reference(n_frames: int, seed: int = 0) -> np.ndarray:
    rng = np.random.default_rng(seed)
    ref = np.abs(rng.standard_normal((12, n_frames))).astype(np.float32)
    return ref / np.linalg.norm(ref, axis=0, keepdims=True)


def _make_follower(ref: np.ndarray, **overrides) -> OnlineDTWFollower:
    kwargs = dict(
        search_width=30,
        # Short windows so tests run in tens of frames, not hundreds.
        mismatch_seconds=1.0,          # ~11 frames
        mismatch_probe_interval_seconds=0.5,
    )
    kwargs.update(overrides)
    return OnlineDTWFollower(ref, FeatureConfig(), **kwargs)


def _track(follower, ref, n, start=0):
    for k in range(start, start + n):
        result = follower.process_frame(ref[:, k])
    return result


JUNK = np.zeros(12, dtype=np.float32)


def test_sustained_high_cost_raises_flag():
    ref = _random_reference(300)
    f = _make_follower(ref)
    f.force_lock_in()
    _track(f, ref, 30)
    assert not f.is_mismatched

    for _ in range(30):  # >> mismatch_seconds
        result = f.process_frame(JUNK)
    assert f.is_mismatched
    assert result.is_mismatched


def test_no_flag_on_good_tracking():
    ref = _random_reference(300)
    f = _make_follower(ref)
    f.force_lock_in()
    result = _track(f, ref, 200)
    assert not f.is_mismatched
    assert not result.is_mismatched


def test_transient_high_cost_does_not_raise():
    ref = _random_reference(300)
    f = _make_follower(ref, mismatch_seconds=2.0)  # ~21 frames needed
    f.force_lock_in()
    _track(f, ref, 30)
    # 8 junk frames (< threshold duration), then back to good tracking.
    for _ in range(8):
        f.process_frame(JUNK)
    assert not f.is_mismatched
    _track(f, ref, 10, start=40)
    assert not f.is_mismatched


def test_not_counted_before_lock_in():
    ref = _random_reference(300)
    f = _make_follower(ref)
    # No lock-in: junk from the start must never raise the flag.
    for _ in range(40):
        f.process_frame(JUNK)
    assert not f.is_mismatched


def test_flag_clears_when_cost_returns_to_matched_band():
    ref = _random_reference(300)
    # Disable recovery jumps so only hysteresis can clear the flag.
    f = _make_follower(ref, mismatch_recovery_cost_ceiling=1e-9)
    f.force_lock_in()
    _track(f, ref, 30)
    for _ in range(30):
        f.process_frame(JUNK)
    assert f.is_mismatched
    # Resume matching audio at the follower's own position: cost drops
    # into the matched band and the ~1s hysteresis clears the flag.
    for _ in range(30):
        pos = f.dp_ref_frame
        result = f.process_frame(ref[:, min(pos + 1, ref.shape[1] - 1)])
    assert not f.is_mismatched
    assert not result.is_mismatched


def test_recovery_jumps_to_decisive_forward_match():
    """While mismatched, the bounded probe re-anchors onto a decisively
    better forward position and clears the flag."""
    ref = _random_reference(300)
    f = _make_follower(ref)
    f.force_lock_in()
    _track(f, ref, 20)
    for _ in range(30):
        f.process_frame(JUNK)
    assert f.is_mismatched
    before = f.dp_ref_frame

    # The performance is actually ~80 frames ahead of the anchor. Feed
    # those frames; the probe (interval 0.5s ≈ 5 frames) finds the
    # decisive forward match and jumps.
    jumped_result = None
    for k in range(before + 80, before + 110):
        result = f.process_frame(ref[:, k])
        if result.ref_frame >= before + 60 and jumped_result is None:
            jumped_result = result
    assert jumped_result is not None, "recovery never jumped forward"
    assert not f.is_mismatched


def test_recovery_absolute_ceiling_blocks_junk_jumps():
    """Even when a forward position is RELATIVELY best (the probe's two
    relative guards pass), recovery must not jump unless its absolute
    cost is inside the matched band. Isolated by stubbing the probe:
    real junk audio makes all costs equal, so the relative guards alone
    would already reject and the ceiling would never be exercised."""
    ref = _random_reference(300)
    f = _make_follower(ref, mismatch_recovery_cost_ceiling=0.10)
    f.force_lock_in()
    _track(f, ref, 20)
    anchor = f.dp_ref_frame

    # Candidate passes both relative guards but sits ABOVE the ceiling
    # (0.30 = "least-bad junk", not a real match) → must be rejected.
    # DP still crawls forward on junk (known marching behaviour), so the
    # assertion is "no TELEPORT" (no large single-frame jump), not "no
    # movement".
    target = anchor + 200
    f._probe_decisive_forward_match = lambda *a, **k: (target, 0.30, 0.9, 0.8)
    prev = f.dp_ref_frame
    for _ in range(30):
        r = f.process_frame(JUNK)
        assert r.ref_frame - prev < 60, "teleported despite absolute guard"
        prev = r.ref_frame
    assert f.is_mismatched

    # Same candidate INSIDE the matched band → jump allowed.
    f._probe_decisive_forward_match = lambda *a, **k: (target, 0.05, 0.9, 0.8)
    for _ in range(10):
        f.process_frame(JUNK)
    assert f.dp_ref_frame >= target
    assert not f.is_mismatched


def test_seek_clears_flag_and_rearms():
    ref = _random_reference(300)
    f = _make_follower(ref)
    f.force_lock_in()
    _track(f, ref, 20)
    for _ in range(30):
        f.process_frame(JUNK)
    assert f.is_mismatched

    # Manual correction (coarse, trigger-level): flag clears immediately.
    f.seek(150, allow_catchup=False)
    assert not f.is_mismatched

    # Residual drift persists (still junk) → detector re-raises after
    # mismatch_seconds, giving the retrying-probe behaviour.
    for _ in range(30):
        f.process_frame(JUNK)
    assert f.is_mismatched


def test_freeze_and_reset_clear_flag():
    # max_inertia_seconds=0: post-lock-in freeze parks the position
    # instead of entering inertia, so post-unfreeze frames count again
    # (during inertia the detector is intentionally suspended — inertia
    # frames don't observe the performance at the reported position).
    ref = _random_reference(300)
    f = _make_follower(ref, max_inertia_seconds=0.0)
    f.force_lock_in()
    _track(f, ref, 20)
    for _ in range(30):
        f.process_frame(JUNK)
    assert f.is_mismatched
    f.freeze()
    assert not f.is_mismatched
    f.unfreeze()

    for _ in range(30):
        f.process_frame(JUNK)
    assert f.is_mismatched
    f.reset()
    assert not f.is_mismatched


def test_threshold_zero_disables_detector():
    ref = _random_reference(300)
    f = _make_follower(ref, mismatch_cost_threshold=0.0)
    f.force_lock_in()
    _track(f, ref, 20)
    for _ in range(60):
        result = f.process_frame(JUNK)
    assert not f.is_mismatched
    assert not result.is_mismatched
