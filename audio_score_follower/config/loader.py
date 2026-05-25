#!/usr/bin/env python3
"""
config/loader.py - Configuration File Parser

Loads config.json and provides access to movement definitions, trigger
settings, and global parameters. Forked from live-score-sync; extended
with ``built_dir`` per movement so the OLTW follower knows where to load
the offline-built warping_path + reference_cens artifacts.
"""

import json
import logging
from pathlib import Path
from typing import Optional, List, Dict

logger = logging.getLogger(__name__)

_VALID_ACTIONS = {"right", "left"}


class ConfigError(ValueError):
    """Raised when config.json has a syntax error or invalid structure."""


class ConfigLoader:
    """Parses and manages config.json.

    Schema (audio-score-follower variant)::

        {
          "settings": {
            "cooldown_seconds": 3.0,
            "silence_threshold_db": -55.0,
            "mic_device": null,
            "oltw_search_width": 240,
            "oltw_step_size": 1
          },
          "movements": [
            {
              "id": 1,
              "xml_file": "beethoven5.xml",
              "built_dir": "../data/built/beethoven5_karajan",
              "triggers": [
                {"measure": 1, "action": "right", "note": "開始"},
                {"measure": 45, "action": "right", "note": "テーマA"}
              ]
            }
          ]
        }
    """

    def __init__(self, config_path: str):
        path = Path(config_path).resolve()
        if not path.exists():
            raise FileNotFoundError(f"Config file not found: {config_path}")

        self.config_dir: Path = path.parent

        try:
            with open(path, "r", encoding="utf-8") as f:
                self.config = json.load(f)
        except json.JSONDecodeError as exc:
            raise ConfigError(
                f"config.json の JSON 構文エラー: {exc.msg} "
                f"(行 {exc.lineno}, 列 {exc.colno})\n"
                f"  ヒント: カンマ忘れ・括弧の対応ミスが多い原因です"
            ) from exc

        self.settings = self.config.get("settings", {})
        self.movements = self.config.get("movements", [])
        self.current_movement_idx = 0

        self._validate()

        logger.info(
            "Config loaded: %d movements, cooldown=%.1fs, silence_threshold=%.1f dBFS",
            len(self.movements),
            self.get_cooldown_seconds(),
            self.get_silence_threshold_db(),
        )

    def resolve_path(self, relative_or_absolute: str) -> str:
        """Resolve a path that may be relative to the config file's directory."""
        p = Path(relative_or_absolute)
        if p.is_absolute():
            return str(p)
        resolved = (self.config_dir / p).resolve()
        return str(resolved)

    def _auto_discover_mxl(self) -> Optional[Path]:
        mxl_files = sorted(self.config_dir.glob("*.mxl"))
        return mxl_files[0] if mxl_files else None

    def _validate(self) -> None:
        if not isinstance(self.movements, list) or not self.movements:
            raise ConfigError(
                "'movements' が空または存在しません。"
                "少なくとも1楽章（movement）を定義してください"
            )

        for mv_idx, movement in enumerate(self.movements):
            mv = f"movements[{mv_idx}]"

            if not movement.get("xml_file"):
                discovered = self._auto_discover_mxl()
                if discovered is None:
                    raise ConfigError(
                        f"{mv}: 'xml_file' が指定されておらず、"
                        f"{self.config_dir} に .mxl ファイルも見つかりません"
                    )
                movement["xml_file"] = discovered.name
                logger.info("%s: xml_file 未指定 → '%s' を自動検出", mv, discovered.name)

            if not movement.get("built_dir"):
                raise ConfigError(
                    f"{mv}: 'built_dir' フィールドがありません。"
                    f"asf-build で生成した出力ディレクトリ (warping_path.npz と "
                    f"reference_cens.npy を含むもの) を指定してください"
                )

            triggers = movement.get("triggers", [])
            if not isinstance(triggers, list):
                raise ConfigError(f"{mv}.triggers: リスト形式が必要です")

            for t_idx, trig in enumerate(triggers):
                tp = f"{mv}.triggers[{t_idx}]"

                if "measure" not in trig:
                    raise ConfigError(f"{tp}: 'measure' フィールドがありません")
                try:
                    m = int(trig["measure"])
                    if m < 1:
                        raise ValueError
                except (TypeError, ValueError):
                    raise ConfigError(
                        f"{tp}.measure: 1 以上の整数が必要です "
                        f"(got {trig['measure']!r})"
                    )

                action = trig.get("action")
                if action not in _VALID_ACTIONS:
                    raise ConfigError(
                        f"{tp}.action: 'right' または 'left' が必要です "
                        f"(got {action!r})"
                    )

        logger.debug("Config validation passed (%d movements)", len(self.movements))

    def get_current_movement(self) -> Optional[Dict]:
        if self.current_movement_idx < len(self.movements):
            return self.movements[self.current_movement_idx]
        return None

    def next_movement(self) -> bool:
        if self.current_movement_idx < len(self.movements) - 1:
            self.current_movement_idx += 1
            return True
        logger.warning("Already at last movement")
        return False

    def previous_movement(self) -> bool:
        if self.current_movement_idx > 0:
            self.current_movement_idx -= 1
            return True
        logger.warning("Already at first movement")
        return False

    def get_cooldown_seconds(self) -> float:
        return self.settings.get("cooldown_seconds", 3.0)

    def get_silence_threshold_db(self) -> float:
        return self.settings.get("silence_threshold_db", -55.0)

    def get_mic_device(self):
        if "mic_device" in self.settings:
            return self.settings["mic_device"]
        # In live-score-sync the WSL2 default was "pulse"; on Windows hosts
        # we usually want None (= OS default) instead.
        import sys
        if sys.platform.startswith("linux"):
            return "pulse"
        return None

    def get_loopback_device(self):
        """Output device to capture in loopback mode.

        Returns the value of ``settings.loopback_device`` when present.
        None means "use the system default output device", which is
        resolved at stream-open time by FollowerWorker.
        """
        return self.settings.get("loopback_device", None)

    def get_oltw_kwargs(self) -> dict:
        """Return kwargs forwarded to OnlineDTWFollower.

        Keys:
            search_width: forward extent of the search band on the
                reference axis (frames). Wider = more tolerant to tempo
                deviation but more drift-prone. Default 240 ≈ 22s at
                hop=2048/sr=22050 — enough to absorb the 5–10% tempo
                differences typical between recordings of the same work.
            back_inhibit_frames: backward extent of the search band.
                Asymmetric with ``search_width``: forward is wide for
                tempo flexibility, back is narrow so the DP can't latch
                onto an earlier repetition of the same theme. Default
                30 ≈ 2.8s.
            step_size: max reference frames advanced per live frame.
                Default 1 (no skipping).
            step_penalty: extra cost on horizontal/vertical DP transitions
                to bias the alignment toward "1 ref ↔ 1 live" diagonal
                motion. Default 0.06 — picked from a sweep over six
                recordings of the same work: lifts same-recording
                confidence from 0.58 to 0.97 (near-perfect) and cuts
                early stuck plateaus on different recordings from 4-7s
                down to <2s in most cases, with no coverage regressions.
                Going to 0.10 yields marginal further gains but risks
                "race ahead" behaviour on tempo holds.
        """
        defaults = {
            "search_width": 240,
            "back_inhibit_frames": 30,
            "init_search_width": 30,
            "step_size": 1,
            "step_penalty": 0.06,
            # Hard cap on per-live-frame ref advance: prevents the DP's
            # vert chain from racing 10+ measures forward across a
            # similar passage (e.g. theme recapitulation). 50 frames ≈
            # 4.6s ≈ 2-3 measures of marche material — wide enough that
            # the system can still catch up after a brief misalignment,
            # narrow enough that a "sudden 20-measure leap" is
            # structurally impossible. Tested to give 100% coverage on
            # Karajan and 96.5–100% on 5 alternate recordings.
            "max_advance_per_frame": 50,
            # stuck_dp_reset wipes accumulated backward cost when the
            # DP has been trying to step backward for an extended
            # period without making real forward progress — the
            # "cumulative cost lock-in" failure mode that mic input
            # often triggers. Default 12s (loose enough that natural
            # band-DP recovery isn't preempted; tight enough that
            # user-perceptible "the slide is frozen" stalls get an
            # escape after ~12s). Set 0 to disable.
            "stuck_dp_reset_seconds": 12.0,
            # stuck_rematch_seconds is 0 (disabled) by default. The
            # mechanism reliably recovers from "script started after
            # music began" mislocks in headless file-input testing,
            # but produces false forward jumps on mic captures whose
            # chroma is too ambiguous for a global-min position to be
            # trusted. Live mic users should leave this off; file-input
            # workflows that need lead-time recovery can opt in with
            # e.g. {"oltw_kwargs": {"stuck_rematch_seconds": 4.0}} in
            # config.json.
            "stuck_rematch_seconds": 0.0,
            "stuck_rematch_min_advance": 3,
            "stuck_rematch_cost_margin": 0.08,
            "stuck_rematch_min_jump_frames": 60,
            "stuck_rematch_max_jump_frames": 480,
            "stuck_rematch_min_discriminability_ratio": 0.75,
            # Lock-in latch: confidence>=lock_in_confidence sustained
            # for lock_in_frames consecutive frames after the init
            # window establishes that "the piece has been caught".
            # After lock-in, silence-gate freezes switch from
            # position-fixed to inertia progression. Defaults pick
            # ~3 seconds of confident tracking (30 frames at 10.77 Hz).
            "lock_in_frames": 30,
            "lock_in_confidence": 0.45,
            # Inertia mode: entered ONLY via silence-gate freeze()
            # (post-lock-in). Low DP confidence alone does NOT enter
            # inertia — orchestral pp passages can produce low conf
            # while DP is still finding the right position, and
            # overriding with inertia would degrade alt-recording
            # coverage. Exited via _maybe_resync_from_dp when DP
            # confidence recovers within inertia_resync_max_gap_frames.
            # inertia_enter_frames is reserved for future use.
            "inertia_enter_frames": 5,
            "inertia_exit_frames": 3,
            # Position-history window for inertia-rate estimation.
            # 40 frames ≈ 3.7s — long enough to smooth out beat-to-beat
            # jitter, short enough to react to gradual tempo changes.
            "inertia_history_frames": 40,
            # Hard cap on how long inertia may progress before falling
            # back to position-fixed mode. Past this horizon the
            # accumulated rate-estimate error can't be trusted; the
            # operator must manually resync via → or L. 10s covers
            # typical fermatas and tutti rests; for Bruckner-scale long
            # rests, raise this to 20–30s in user config. Set 0.0 to
            # disable inertia entirely (legacy "freeze locks position").
            "max_inertia_seconds": 10.0,
            # Maximum gap (ref frames) between DP estimate and inertia
            # position for _maybe_resync_from_dp to accept the DP and
            # exit inertia. None → use search_width (≈22s).
            "inertia_resync_max_gap_frames": None,
        }
        user_kwargs = self.settings.get("oltw_kwargs", {})
        if not isinstance(user_kwargs, dict):
            logger.warning(
                "settings.oltw_kwargs must be a dict; got %r — ignoring", user_kwargs
            )
            user_kwargs = {}
        return {**defaults, **user_kwargs}

    def get_movement_triggers(self, movement_id: Optional[int] = None) -> List[Dict]:
        if movement_id is None:
            movement = self.get_current_movement()
        else:
            movement = next(
                (m for m in self.movements if m.get("id") == movement_id), None
            )
        if movement:
            return sorted(movement.get("triggers", []), key=lambda t: t.get("measure", 0))
        return []

    def get_xml_file_for_movement(
        self, movement_idx: Optional[int] = None
    ) -> Optional[str]:
        if movement_idx is None:
            movement = self.get_current_movement()
        else:
            if 0 <= movement_idx < len(self.movements):
                movement = self.movements[movement_idx]
            else:
                return None
        return movement.get("xml_file") if movement else None

    def get_built_dir_for_movement(
        self, movement_idx: Optional[int] = None
    ) -> Optional[str]:
        if movement_idx is None:
            movement = self.get_current_movement()
        else:
            if 0 <= movement_idx < len(self.movements):
                movement = self.movements[movement_idx]
            else:
                return None
        return movement.get("built_dir") if movement else None

    def total_movements(self) -> int:
        return len(self.movements)

    def current_movement_number(self) -> int:
        return self.current_movement_idx + 1

    def __repr__(self) -> str:
        return (
            f"ConfigLoader(movements={len(self.movements)}, "
            f"current={self.current_movement_number()}/{self.total_movements()})"
        )
