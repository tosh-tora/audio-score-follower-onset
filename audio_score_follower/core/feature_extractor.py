#!/usr/bin/env python3
"""
feature_extractor.py - CENS chroma feature extraction (single source of truth).

Both the offline reference builder and the online OLTW follower must call
``compute_cens()`` with the *same* parameters; otherwise the cost matrix
between live frames and pre-computed reference frames becomes meaningless.
``FeatureConfig`` carries those parameters and is serialized into the build
output so the follower can verify match at load time.

CENS (Chroma Energy Normalized Statistics, Müller & Ewert 2011) extends
plain chroma with three robustness layers:

    1. log-compression of the raw chroma to suppress sustained-tone dominance
       (orchestral string sections especially benefit).
    2. per-frame L2 normalisation to remove level differences (mic distance,
       part-balance variation between rehearsal and concert).
    3. short-time downsampled smoothing (1–2 s window) so micro-tempo jitter
       in the live performance doesn't ping-pong through the DTW grid.

Library: librosa. We chose its built-in CENS implementation rather than
re-implementing because the upstream code has been tuned for years on MIREX
benchmarks; deviating would require independently validating sync quality.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, asdict
from typing import Any

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


def compute_cens_streaming(
    audio_block: np.ndarray,
    cfg: FeatureConfig,
) -> np.ndarray:
    """Compute CENS for a single live audio block.

    Identical to ``compute_cens`` for now — librosa's chroma_cens accepts
    arbitrary length input and computes as many frames as it can. We
    expose a separate name so the online code documents its intent
    (and so we can swap in a true streaming implementation later
    without changing call sites).

    Note: for very short blocks (<cens_win * hop_length samples) the
    CENS smoothing has insufficient context and the first few frames
    are unreliable. The OLTW follower compensates with its
    confidence/cost gate.
    """
    return compute_cens(audio_block, cfg)


def cosine_cost_matrix(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """Pairwise cosine distance between two CENS sequences.

    Args:
        a: (12, n) reference CENS
        b: (12, m) live CENS

    Returns:
        (n, m) cost matrix in [0, 2], where 0 = identical direction.

    Implementation note: both inputs are assumed already L2-normalised
    (which compute_cens guarantees). So cosine distance reduces to
    ``1 - a.T @ b`` with no division.
    """
    if a.shape[0] != 12 or b.shape[0] != 12:
        raise ValueError(
            f"Expected 12-d chroma, got {a.shape} and {b.shape}"
        )
    return 1.0 - a.T @ b
