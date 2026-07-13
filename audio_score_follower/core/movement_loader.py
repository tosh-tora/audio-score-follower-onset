#!/usr/bin/env python3
"""
core/movement_loader.py - Pure movement-artifact loading.

Extracted from ``main.AudioScoreFollowerApp._load_movement``. Resolves
the score / built-reference paths, loads the ScoreMapper, WarpLookup and
reference features, validates the warp path, and constructs the OLTW
follower — returning them bundled in a ``LoadedMovement``.

App-side orchestration stays in main: stopping the previous worker (via
the injected ``stop_previous`` callback, invoked at the exact original
point — after the existence checks, before artifact load, so a reload
onto a movement with missing files leaves the current worker running),
mic-mode parking, AppState updates, and worker construction.

Failures raise ``MovementLoadError`` carrying the GUI-facing message the
app passes to ``AppState.set_load_error`` (``state_message`` is None for
the missing-keys case, which historically only logged).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Optional

from audio_score_follower.core.oltw_follower import OnlineDTWFollower
from audio_score_follower.core.score_mapper import ScoreMapper
from audio_score_follower.core.warp_lookup import (
    WarpLookup,
    load_reference_cens,
    load_reference_onset,
)

if TYPE_CHECKING:
    from audio_score_follower.config.loader import ConfigLoader

logger = logging.getLogger(__name__)


class MovementLoadError(Exception):
    """A movement failed to load. ``state_message`` is the GUI-facing
    error (None → log only, no AppState error, as with missing keys)."""

    def __init__(self, state_message: Optional[str]) -> None:
        super().__init__(state_message or "movement load failed")
        self.state_message = state_message


@dataclass
class LoadedMovement:
    """Everything the app needs to wire up a freshly loaded movement."""

    xml_file: Path
    score_mapper: ScoreMapper
    warp_lookup: WarpLookup
    oltw: OnlineDTWFollower
    onset_enabled: bool
    triggers: list
    total_measures: int


def load_movement(
    config: "ConfigLoader",
    movement: dict,
    *,
    mic_mode: bool,
    viz: bool,
    stop_previous: Callable[[], None],
) -> LoadedMovement:
    """Load and construct one movement's follower, or raise MovementLoadError."""
    xml_raw = movement.get("xml_file")
    built_raw = movement.get("built_dir")
    if not xml_raw or not built_raw:
        logger.error("Movement missing xml_file or built_dir: %s", movement)
        raise MovementLoadError(None)

    xml_file = Path(config.resolve_path(xml_raw))
    built_dir = Path(config.resolve_path(built_raw))

    if not xml_file.exists():
        msg = f"楽譜ファイルが見つかりません。\n  → {xml_file}\n  に置いてください"
        logger.error(msg)
        raise MovementLoadError(f"ファイルが見つかりません\n{xml_file}")
    if not built_dir.exists():
        msg = (
            f"ビルド済みリファレンスが見つかりません: {built_dir}\n"
            f"  asf-build を実行してから再起動してください"
        )
        logger.error(msg)
        raise MovementLoadError(f"asf-build 出力なし\n{built_dir}")

    logger.info("Loading movement: xml=%s built=%s", xml_file, built_dir)

    # Stop the previous worker here — after the existence checks (so a bad
    # reload keeps the current worker), before loading the new artifacts.
    stop_previous()

    try:
        score_mapper = ScoreMapper(str(xml_file))
        warp_lookup = WarpLookup.load(built_dir)
        reference_cens = load_reference_cens(built_dir)
        reference_onset = load_reference_onset(built_dir)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Failed to load movement artifacts: %s", exc)
        raise MovementLoadError(f"読込失敗: {exc}") from exc

    logger.info(
        "Loaded: %s, %s, reference_cens=(%d,%d)",
        score_mapper, warp_lookup,
        reference_cens.shape[0], reference_cens.shape[1],
    )

    chroma_weight, onset_weight = config.get_feature_fusion()
    onset_enabled = reference_onset is not None and onset_weight > 0.0
    logger.info(
        "Feature fusion: %s (chroma=%.2f onset=%.2f)%s",
        "enabled" if onset_enabled else "disabled",
        chroma_weight, onset_weight,
        "" if onset_enabled else
        " — rebuild with asf-build to generate reference_onset.npy",
    )

    # Validate warp path consistency before starting OLTW.
    try:
        warp_lookup.validate(score_mapper)
    except ValueError as exc:
        logger.error("Warp path validation failed: %s", exc)
        raise MovementLoadError(
            f"warp path 検証エラー:\n{exc}\nasf-build をやり直してください。"
        ) from exc
    except Exception as exc:  # noqa: BLE001
        logger.exception("Warp path validation error: %s", exc)
        raise MovementLoadError(f"warp path 検証中にエラー: {exc}") from exc

    oltw_kwargs = config.get_oltw_kwargs()
    if mic_mode:
        # Manual-start correction for a LATE press: widen the
        # first-frame search window so tracking can land several
        # seconds into the piece. (Most late-press recovery goes
        # through the armed post-unfreeze catchup, but this covers
        # the corner where no freeze preceded the first frame.)
        fr = warp_lookup.feature_config.effective_frame_rate()
        start_width = int(round(config.get_start_search_seconds() * fr))
        oltw_kwargs["init_search_width"] = max(
            int(oltw_kwargs.get("init_search_width") or 0), start_width
        )
    try:
        oltw = OnlineDTWFollower(
            reference_cens=reference_cens,
            feature_config=warp_lookup.feature_config,
            reference_onset=reference_onset,
            chroma_weight=chroma_weight,
            onset_weight=onset_weight,
            capture_viz=viz,
            **oltw_kwargs,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("Failed to construct OLTW: %s", exc)
        raise MovementLoadError(f"OLTW 初期化失敗: {exc}") from exc

    return LoadedMovement(
        xml_file=xml_file,
        score_mapper=score_mapper,
        warp_lookup=warp_lookup,
        oltw=oltw,
        onset_enabled=onset_enabled,
        triggers=movement.get("triggers", []),
        total_measures=score_mapper.get_total_measures(),
    )
