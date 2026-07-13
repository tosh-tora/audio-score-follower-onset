#!/usr/bin/env python3
"""
feature_extractor.py - Multi-feature audio extraction (single source of truth).

Both the offline reference builder and the online OLTW follower must call
``compute_cens()`` / ``compute_onset()`` with the *same* parameters; otherwise
the cost matrix between live frames and pre-computed reference frames becomes
meaningless. ``FeatureConfig`` carries those parameters and is serialized into
the build output so the follower can verify match at load time.

CENS (Chroma Energy Normalized Statistics, Müller & Ewert 2011) extends
plain chroma with three robustness layers:

    1. log-compression of the raw chroma to suppress sustained-tone dominance
       (orchestral string sections especially benefit).
    2. per-frame L2 normalisation to remove level differences (mic distance,
       part-balance variation between rehearsal and concert).
    3. short-time downsampled smoothing (1–2 s window) so micro-tempo jitter
       in the live performance doesn't ping-pong through the DTW grid.

Onset (spectral-flux, median-aggregated): complements CENS with a *temporal*
event signature that is largely orthogonal to harmonic content. Repeated
themes / same-key recapitulations share chroma but rarely share the exact
attack envelope, so onset disambiguates self-similar passages that CENS
alone treats as nearly equal. Frame-aligned with CENS by sharing
``hop_length`` and ``sample_rate``.

Library: librosa. We chose its built-in CENS / onset implementations rather
than re-implementing because the upstream code has been tuned for years on
MIREX benchmarks; deviating would require independently validating sync
quality.
"""

from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass, asdict
from typing import Any, Optional

import numpy as np

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class FeatureConfig:
    """Parameters that define the CENS feature space.

    Frozen because changing any field invalidates pre-built reference
    artifacts. Keep this immutable so the dataclass can be hashed / used
    as a dict key when caching.
    """

    sample_rate: int = 22050
    # Hop in *samples* for the underlying chroma STFT. 2048 @ 22050 ≈ 93ms.
    # Smaller hops give finer time resolution but cost more CPU at runtime.
    hop_length: int = 2048
    # CENS internally re-samples chroma by quant_steps before quantising,
    # then smooths with a window of cens_win frames. We expose librosa's
    # defaults verbatim; tuning is a Phase-2 concern once the pipeline runs.
    quant_steps: tuple = (40, 20, 10, 5)
    cens_win: int = 41
    # After CENS smoothing, the effective frame rate is reduced. We keep
    # track of it so callers can convert frames ↔ seconds without
    # re-deriving from hop_length.
    norm: float = 2.0

    def effective_frame_rate(self) -> float:
        """Hz of the CENS output (after smoothing, NOT raw chroma rate)."""
        return self.sample_rate / self.hop_length

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["quant_steps"] = list(self.quant_steps)
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "FeatureConfig":
        return cls(
            sample_rate=int(d.get("sample_rate", 22050)),
            hop_length=int(d.get("hop_length", 2048)),
            quant_steps=tuple(d.get("quant_steps", (40, 20, 10, 5))),
            cens_win=int(d.get("cens_win", 41)),
            norm=float(d.get("norm", 2.0)),
        )

    def to_npz_arrays(self) -> dict[str, np.ndarray]:
        """Serialise to the array pair stored in ``warping_path.npz``.

        The layout is positional and MUST stay byte-identical — existing
        built artifacts are reloaded via :meth:`from_npz_arrays`. This is
        the single owner of that on-disk schema (writer in
        reference_builder, reader in warp_lookup both route through here).
        """
        return {
            "feature_config": np.array(
                [self.sample_rate, self.hop_length, self.cens_win, self.norm],
                dtype=np.float32,
            ),
            "feature_config_quant_steps": np.array(self.quant_steps, dtype=np.int32),
        }

    @classmethod
    def from_npz_arrays(
        cls, feature_config: np.ndarray, feature_config_quant_steps: np.ndarray
    ) -> "FeatureConfig":
        """Reconstruct from the :meth:`to_npz_arrays` array pair."""
        return cls(
            sample_rate=int(feature_config[0]),
            hop_length=int(feature_config[1]),
            cens_win=int(feature_config[2]),
            norm=float(feature_config[3]),
            quant_steps=tuple(int(x) for x in feature_config_quant_steps),
        )


def align_onset_to_cens(
    cens: np.ndarray, onset: np.ndarray
) -> tuple[np.ndarray, np.ndarray]:
    """Truncate ``cens`` (12, N) and ``onset`` (M,) to their common frame count.

    Onset and CENS extraction can disagree by a frame due to STFT edge
    handling; the fused DP cost indexes both by frame, so callers clip to
    ``min(N, M)``. Callers that want a warning log it themselves (the
    wording differs per call site), then pass the pair through here.
    """
    n = min(cens.shape[1], onset.shape[0])
    return cens[:, :n], onset[:n]


def compute_cens(
    audio: np.ndarray,
    cfg: FeatureConfig,
) -> np.ndarray:
    """Compute CENS features from a mono float32 audio array.

    Args:
        audio: 1D float32 audio at ``cfg.sample_rate``. Multi-channel input
            must be mixed down by the caller (this function does NOT do
            that, on purpose — caller knows the mic geometry).
        cfg: Feature configuration. Must match between offline build and
            online follower.

    Returns:
        ndarray of shape (12, n_frames), L2-normalised per frame.

    Raises:
        ValueError: if audio is not 1D float.
    """
    import librosa  # type: ignore — heavyweight import, defer

    if audio.ndim != 1:
        raise ValueError(
            f"compute_cens expects 1D mono audio, got shape {audio.shape}"
        )
    if audio.dtype != np.float32:
        audio = audio.astype(np.float32)

    cens = librosa.feature.chroma_cens(
        y=audio,
        sr=cfg.sample_rate,
        hop_length=cfg.hop_length,
        norm=cfg.norm,
        win_len_smooth=cfg.cens_win,
    )
    # Defensive re-normalisation: librosa normalises pre-smoothing already,
    # but the smoothing pass can leave frames with tiny residual norms in
    # silent regions. Renormalise so the cosine cost is well-defined.
    norms = np.linalg.norm(cens, axis=0, keepdims=True)
    norms = np.where(norms > 1e-8, norms, 1.0)
    return (cens / norms).astype(np.float32)


# ============================================================================
# Onset (spectral-flux) feature — complementary to CENS for fusion.
# ============================================================================

def compute_onset(
    audio: np.ndarray,
    cfg: FeatureConfig,
) -> np.ndarray:
    """Compute median-aggregated spectral-flux onset strength.

    Frame-aligned with ``compute_cens`` by sharing ``cfg.sample_rate`` and
    ``cfg.hop_length``. The output length matches ``compute_cens(audio, cfg)``
    in practice; callers should still defensively trim to ``min(len)`` since
    librosa's framing edges can occasionally differ by 1 frame.

    Args:
        audio: 1D float32 mono audio at ``cfg.sample_rate``.
        cfg: Feature configuration shared with ``compute_cens``.

    Returns:
        ndarray of shape (n_frames,) — unnormalised. Apply
        ``normalize_onset_global`` (offline) or feed through
        ``OnsetNormalizer`` (online) before fusing into DP cost.

    Raises:
        ValueError: if audio is not 1D.
    """
    import librosa  # type: ignore — heavyweight import, defer

    if audio.ndim != 1:
        raise ValueError(
            f"compute_onset expects 1D mono audio, got shape {audio.shape}"
        )
    if audio.dtype != np.float32:
        audio = audio.astype(np.float32)

    onset = librosa.onset.onset_strength(
        y=audio,
        sr=cfg.sample_rate,
        hop_length=cfg.hop_length,
        aggregate=np.median,
    )
    # librosa returns float32 by default but it's not contractually
    # guaranteed across versions — cast defensively. Replace any
    # spurious NaN (can appear on all-silence inputs in some librosa
    # builds) with 0 so downstream normalisation stays well-defined.
    onset = np.asarray(onset, dtype=np.float32).reshape(-1)
    if not np.all(np.isfinite(onset)):
        onset = np.nan_to_num(onset, nan=0.0, posinf=0.0, neginf=0.0)
    return onset


def normalize_onset_global(onset: np.ndarray) -> np.ndarray:
    """Divide by global max (with epsilon) — for offline reference.

    Maps the onset envelope into roughly [0, 1] so it can be combined
    with cosine distance (also in [0, 2]) at comparable scale. The
    epsilon prevents division by zero on silent / synthetic inputs.

    Returns a float32 copy; safe on empty input (returns empty array).
    """
    if onset.size == 0:
        return onset.astype(np.float32, copy=False)
    peak = float(np.max(onset))
    return (onset / (peak + 1e-8)).astype(np.float32)


# Rolling-max window for live onset normalisation. Must be identical
# everywhere live onset is produced (mic worker, file worker, headless
# eval), or the onset term of the fused DP cost changes scale between
# them — the same trap as CENS parameter drift between offline build
# and online follower.
LIVE_ONSET_WINDOW_SEC = 5.0


class OnsetNormalizer:
    """Rolling-max normaliser for the live onset stream.

    The offline reference is normalised once against its global max.
    Live audio cannot do that (the future is unknown), so we maintain a
    deque of the most recent onset values and divide by their max. This
    is mic-gain invariant after the buffer fills, and the rolling
    window (~5s by default) is short enough to react to dynamics
    changes but long enough to span at least a few beats so the max
    estimate isn't dominated by a single frame.

    Not thread-safe — meant to be called from a single worker thread.
    """

    def __init__(self, window_frames: int) -> None:
        if window_frames < 1:
            raise ValueError(f"window_frames must be >= 1, got {window_frames}")
        self._buf: deque[float] = deque(maxlen=int(window_frames))

    @classmethod
    def for_config(cls, cfg: FeatureConfig) -> "OnsetNormalizer":
        """Construct with the shared ``LIVE_ONSET_WINDOW_SEC`` rolling window."""
        return cls(max(1, int(LIVE_ONSET_WINDOW_SEC * cfg.effective_frame_rate())))

    def normalize(self, value: float) -> float:
        """Push ``value`` into the rolling buffer and return ``value / max``.

        Returns 0.0 for the very first call where the buffer is otherwise
        empty AND the value itself is zero (silence boot-up). In all other
        cases the result is in [0, 1].
        """
        v = float(value)
        self._buf.append(v)
        peak = max(self._buf)  # max() over a deque is O(n); n is small (~50)
        return v / (peak + 1e-8)

    def reset(self) -> None:
        self._buf.clear()


def fused_local_cost(
    cens_ref_block: np.ndarray,
    live_cens: np.ndarray,
    onset_ref_block: Optional[np.ndarray],
    live_onset: Optional[float],
    chroma_weight: float,
    onset_weight: float,
) -> np.ndarray:
    """Compute the fused DP local cost over a block of reference frames.

    Single point of distance computation — all OLTW cost-calculation
    sites route through here so adding a new feature (HPCP, Mel, …)
    means editing this function alone.

    Cost formula (when fusion is active):

        cost[k] = chroma_weight * (1 - <cens_ref[:, k], live_cens>)
                + onset_weight  * |onset_ref[k] - live_onset|

    Args:
        cens_ref_block: (12, K) L2-normalised reference chroma block.
        live_cens: (12,) L2-normalised live chroma frame.
        onset_ref_block: (K,) normalised reference onset block, or None.
        live_onset: scalar normalised live onset, or None.
        chroma_weight: weight on the cosine distance term.
        onset_weight: weight on the absolute-difference onset term.

    Returns:
        (K,) float32 cost array.

    Fusion is silently disabled (CENS-only path) when any of:
        - ``onset_ref_block`` is None
        - ``live_onset`` is None
        - ``onset_weight`` is <= 0
        - reference and block sizes mismatch (logged once per call)
    """
    cens_cost = 1.0 - cens_ref_block.T @ live_cens

    fusion_active = (
        onset_ref_block is not None
        and live_onset is not None
        and onset_weight > 0.0
    )
    if not fusion_active:
        # When onset is disabled, preserve historical scale: the OLTW
        # was tuned for cost values in [0, 2] (raw cosine distance).
        # Multiplying by chroma_weight here would silently rescale
        # step_penalty / lock_in_confidence thresholds. Pass through.
        return cens_cost.astype(np.float32, copy=False)

    assert onset_ref_block is not None  # type narrowing for mypy
    assert live_onset is not None
    if onset_ref_block.shape[0] != cens_ref_block.shape[1]:
        logger.warning(
            "fused_local_cost: size mismatch (cens=%d, onset=%d); "
            "falling back to CENS-only for this call",
            cens_ref_block.shape[1], onset_ref_block.shape[0],
        )
        return cens_cost.astype(np.float32, copy=False)

    onset_cost = np.abs(onset_ref_block - float(live_onset)).astype(np.float32)
    fused = chroma_weight * cens_cost + onset_weight * onset_cost
    return fused.astype(np.float32, copy=False)
