"""Unit tests for PosteriorFollower (global-observation Bayesian follower)."""

from __future__ import annotations

import numpy as np
import pytest

from audio_score_follower.core.feature_extractor import FeatureConfig
from audio_score_follower.core.posterior_follower import PosteriorFollower


def _distinct_reference(n_frames: int, seed: int = 0) -> np.ndarray:
    """A (12, N) reference where every frame has a distinct sparse chroma.

    Two random pitch classes active per frame, L2-normalised. Distinct
    per frame (unlike the cyclic one-hot used for OLTW ambiguity tests)
    so the true position has an unambiguous global likelihood peak.
    """
    rng = np.random.default_rng(seed)
    ref = np.zeros((12, n_frames), dtype=np.float32)
    for k in range(n_frames):
        pcs = rng.choice(12, size=2, replace=False)
        ref[pcs, k] = 1.0
    ref /= np.linalg.norm(ref, axis=0, keepdims=True)
    return ref


def test_self_alignment_tracks_diagonal_and_locks_in():
    cfg = FeatureConfig()
    ref = _distinct_reference(300)
    f = PosteriorFollower(ref, cfg)

    positions = []
    for k in range(300):
        r = f.process_frame(ref[:, k])
        positions.append(r.ref_frame)

    positions = np.array(positions)
    # Exact diagonal after the first frame.
    assert np.abs(positions[10:] - np.arange(10, 300)).max() <= 1
    assert f.is_locked_in
    assert r.confidence > 0.9


def test_junk_input_collapses_confidence():
    """A wrong piece / environmental sound must drive confidence down,
    not hold it high — the core defect being fixed (band-DP reported
    0.6–0.8 on unrelated audio).

    NOTE: unlike OLTW, zeros-chroma is NOT a valid junk model here. A
    zero (or uniformly dense) live frame yields a *flat* likelihood,
    which the filter correctly treats as "no evidence" and coasts on the
    tempo prior (see ``test_flat_likelihood_coasts_forward_on_tempo_prior``).
    Real junk — a different piece, noise — produces *sparse but
    incoherent* feature vectors: each frame peaks somewhere unrelated to
    the last, so the posterior cannot hold a concentrated bump and
    confidence collapses. That is what we model here.
    """
    cfg = FeatureConfig()
    ref = _distinct_reference(300)
    rng = np.random.default_rng(99)
    f = PosteriorFollower(ref, cfg)
    for k in range(60):
        r = f.process_frame(ref[:, k])
    assert r.confidence > 0.9

    # Sustained confidence over the junk stretch is the operationally
    # meaningful signal (single frames spike transiently when random junk
    # momentarily overlaps the committed neighbourhood). It must fall well
    # below the lock-in threshold (0.50) so lock-in cannot hold and
    # coasting is flagged.
    tail = []
    for _ in range(80):
        v = np.zeros(12, dtype=np.float32)
        v[rng.choice(12, size=2, replace=False)] = 1.0
        v /= np.linalg.norm(v)
        r = f.process_frame(v)
        tail.append(r.confidence)
    tail_mean = float(np.mean(tail[-50:]))
    assert tail_mean < 0.45, f"junk confidence stayed high: mean={tail_mean}"


def test_recovers_from_unexpected_forward_drift():
    """After the performance jumps ahead without a seek, the posterior
    mass migrates to the true position — the correction the band-DP
    could not do while still 'advancing'."""
    cfg = FeatureConfig()
    ref = _distinct_reference(400)
    f = PosteriorFollower(ref, cfg)
    f.force_lock_in()
    for k in range(20):
        f.process_frame(ref[:, k])

    # Performance is really at frame 150 now (a 130-frame jump).
    recovered_at = None
    for k in range(150, 280):
        r = f.process_frame(ref[:, k])
        if recovered_at is None and abs(r.ref_frame - k) <= 5:
            recovered_at = k
    assert recovered_at is not None, "never recovered from the drift"
    # Recovery within a few seconds of the jump.
    assert recovered_at - 150 < int(round(5.0 * cfg.effective_frame_rate()))
    assert abs(r.ref_frame - 279) <= 2 and r.confidence > 0.8


def test_nearby_candidate_wins_over_identical_far_candidate():
    """When a far frame has an IDENTICAL chroma to the current position,
    the near-position prior must keep the follow local — no teleport to
    the self-similar repeat. This is the project's dominant failure mode.
    """
    cfg = FeatureConfig()
    ref = _distinct_reference(400)
    # Make frames [250:300] an exact copy of [50:100].
    ref[:, 250:300] = ref[:, 50:100]
    ref /= np.linalg.norm(ref, axis=0, keepdims=True)
    f = PosteriorFollower(ref, cfg)

    outs = []
    for k in range(400):
        r = f.process_frame(ref[:, k])
        outs.append(r.ref_frame)
    outs = np.array(outs)

    # While playing the FIRST theme (50..100), output must stay there,
    # never jumping to the identical copy at 250.
    assert outs[50:100].max() < 150, (
        f"teleported to self-similar repeat: {outs[50:100].max()}"
    )
    # Monotonic to the end.
    assert outs[-1] >= 395
    assert (np.diff(outs) >= -5).all()


def test_far_teleport_requires_sustained_evidence():
    """A brief spurious far peak must not move the output; only sustained,
    dominant far evidence (distance-scaled hysteresis) may teleport."""
    cfg = FeatureConfig()
    ref = _distinct_reference(400)
    f = PosteriorFollower(ref, cfg)
    for k in range(60):
        f.process_frame(ref[:, k])
    committed_before = f.current_ref_frame

    # Two isolated frames matching a far position (frame 320), then back
    # to the true track. Too brief to satisfy the commit hysteresis.
    f.process_frame(ref[:, 320])
    f.process_frame(ref[:, 320])
    r = f.process_frame(ref[:, 62])
    assert abs(r.ref_frame - committed_before) < 30, (
        f"teleported on a 2-frame far blip: {r.ref_frame}"
    )


def test_freeze_holds_position_and_zero_confidence():
    cfg = FeatureConfig()
    ref = _distinct_reference(200)
    f = PosteriorFollower(ref, cfg)
    for k in range(40):
        f.process_frame(ref[:, k])
    held = f.current_ref_frame

    f.freeze()
    assert f.is_frozen
    for _ in range(10):
        r = f.process_frame(ref[:, 120])  # frames arrive but must be ignored
    assert r.ref_frame == held, "frozen position moved"
    assert r.confidence == 0.0

    f.unfreeze()
    assert not f.is_frozen
    # Resumes normal tracking from the retained posterior.
    r = f.process_frame(ref[:, held + 1])
    assert abs(r.ref_frame - held) <= 3


def test_seek_reinitialises_around_target():
    cfg = FeatureConfig()
    ref = _distinct_reference(300)
    f = PosteriorFollower(ref, cfg)
    for k in range(40):
        f.process_frame(ref[:, k])

    f.seek(200, allow_catchup=False)
    assert f.current_ref_frame == 200
    # Continue the real performance at 200 — stays put, re-concentrates.
    for k in range(200, 230):
        r = f.process_frame(ref[:, k])
    assert abs(r.ref_frame - 229) <= 2


def test_reset_clears_lock_in_and_state():
    cfg = FeatureConfig()
    ref = _distinct_reference(200)
    f = PosteriorFollower(ref, cfg)
    for k in range(60):
        f.process_frame(ref[:, k])
    assert f.is_locked_in

    f.reset()
    assert not f.is_locked_in
    assert not f.is_frozen
    assert f.current_ref_frame == 0
    # First frame after reset re-initialises cleanly.
    r = f.process_frame(ref[:, 0])
    assert r.ref_frame <= 2


def test_force_lock_in_is_idempotent():
    cfg = FeatureConfig()
    ref = _distinct_reference(100)
    f = PosteriorFollower(ref, cfg)
    assert not f.is_locked_in
    f.force_lock_in()
    assert f.is_locked_in
    f.force_lock_in()  # no-op, no raise
    assert f.is_locked_in


def test_flat_likelihood_coasts_forward_on_tempo_prior():
    """A pp / silent passage (flat likelihood) should keep advancing via
    the transition prior rather than stalling — inertia by construction."""
    cfg = FeatureConfig()
    ref = _distinct_reference(300)
    f = PosteriorFollower(ref, cfg)
    for k in range(60):
        f.process_frame(ref[:, k])
    before = f.current_ref_frame

    # Feed a flat (uninformative) frame repeatedly: uniform chroma.
    flat = np.full(12, 1.0 / np.sqrt(12), dtype=np.float32)
    for _ in range(10):
        r = f.process_frame(flat)
    assert r.ref_frame > before, "position stalled instead of coasting forward"


def test_accepts_and_ignores_oltw_kwargs():
    """Drop-in construction: band-DP-only kwargs must not raise."""
    cfg = FeatureConfig()
    ref = _distinct_reference(50)
    f = PosteriorFollower(
        ref, cfg,
        search_width=100, step_penalty=0.02, back_inhibit_frames=30,
        stuck_dp_reset_seconds=6.0, display_slew_factor=3.0,
    )
    r = f.process_frame(ref[:, 0])
    assert r.ref_frame >= 0
