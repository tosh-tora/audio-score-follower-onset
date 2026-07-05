#!/usr/bin/env python3
"""ConfigLoader.default_oltw_kwargs の特性テスト。"""
import numpy as np

from audio_score_follower.config.loader import ConfigLoader
from audio_score_follower.core.feature_extractor import FeatureConfig
from audio_score_follower.core.oltw_follower import OnlineDTWFollower


def test_default_oltw_kwargs_constructs_follower():
    """デフォルト kwargs が OnlineDTWFollower にそのまま渡せることを固定。
    （loader のキーと follower のシグネチャの同期ズレを検出する）"""
    kwargs = ConfigLoader.default_oltw_kwargs()
    ref = np.tile(np.eye(12, dtype=np.float32), 50)[:, :600]
    ref = ref / np.linalg.norm(ref, axis=0, keepdims=True).clip(1e-8)
    follower = OnlineDTWFollower(
        reference_cens=ref, feature_config=FeatureConfig(), **kwargs
    )
    assert follower.n_ref_frames == 600


def test_default_oltw_kwargs_key_values():
    kwargs = ConfigLoader.default_oltw_kwargs()
    assert kwargs["search_width"] == 240
    assert kwargs["max_advance_per_frame"] == 50
    assert kwargs["lock_in_confidence"] == 0.45
