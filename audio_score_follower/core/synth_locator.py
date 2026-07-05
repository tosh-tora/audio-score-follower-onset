#!/usr/bin/env python3
"""synth_locator.py - FluidSynth 実行ファイルと SoundFont の検出（唯一の実装）。

以前は tasks/generate_score_wav.py と cli/build_reference.py の両方に
ほぼ同一の検出ロジックが重複していた。検出順序は実運用でのトラブル履歴に
基づく（CLAUDE.md 参照）: vendor/ が最優先なのは、LocalAppData に置いた
実体を Windows Defender / Controlled Folder Access が削除した実例が
あるため。
"""

from __future__ import annotations

import logging
import os
import shutil
import sys
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parents[2]

_FLUIDSYNTH_FIXED_CANDIDATES = [
    Path(r"C:\Program Files\FluidSynth\bin\fluidsynth.exe"),
    Path(r"C:\ProgramData\chocolatey\bin\fluidsynth.exe"),
    Path("/usr/bin/fluidsynth"),
    Path("/usr/local/bin/fluidsynth"),
    Path("/opt/homebrew/bin/fluidsynth"),
]
_SF_CANDIDATES = [
    Path(r"C:\Program Files\MuseScore 4\sound\MS Basic.sf3"),
    Path(r"C:\Program Files\MuseScore 3\sound\MuseScore_General.sf3"),
    Path(r"C:\Program Files\MuseScore 3\sound\MuseScore_General.sf2"),
    Path("/usr/share/sounds/sf2/default.sf2"),
    Path("/usr/share/soundfonts/default.sf2"),
    Path("/opt/homebrew/share/soundfonts/default.sf2"),
]


def find_fluidsynth(explicit: Optional[Path] = None) -> Optional[Path]:
    """FluidSynth 実行ファイルを発見する。

    探索順: explicit → vendor/ → 環境変数 FLUIDSYNTH_EXE → PATH →
    LocalAppData → 既知の固定パス。見つからなければ None を返す
    （raise しない — エラーメッセージの組み立ては呼び出し側の責務）。
    ``explicit`` が指定されているがそのパスが存在しない場合も None。
    """
    if explicit is not None:
        return explicit if explicit.exists() else None

    # Project-local vendor directory (most reliable — no sandbox/virtualization issues)
    vendor_dir = _REPO_ROOT / "vendor" / "FluidSynth"
    if vendor_dir.exists():
        for p in sorted(vendor_dir.glob("*/bin/fluidsynth.exe")):
            if p.exists():
                return p

    env = os.environ.get("FLUIDSYNTH_EXE")
    if env:
        p = Path(env)
        if p.exists():
            return p
        logger.warning("FLUIDSYNTH_EXE=%s が見つからない — 他の候補を探す", env)

    which = shutil.which("fluidsynth")
    if which:
        return Path(which)

    # Search LocalAppData via multiple methods — env vars can be unreliable
    # depending on how the process was spawned.
    local_appdata_candidates: list[Path] = []
    local_appdata_candidates.append(Path.home() / "AppData" / "Local")
    userprofile = os.environ.get("USERPROFILE", "")
    if userprofile:
        local_appdata_candidates.append(Path(userprofile) / "AppData" / "Local")
    # Walk up from sys.executable to find AppData\Local (e.g. user-installed Python)
    for parent in Path(sys.executable).resolve().parents:
        if parent.name.lower() == "local" and parent.parent.name.lower() == "appdata":
            local_appdata_candidates.append(parent)
            break

    for local_appdata in local_appdata_candidates:
        fluidsynth_dir = local_appdata / "FluidSynth"
        logger.debug("FluidSynth 検索: %s (exists=%s)", fluidsynth_dir, fluidsynth_dir.exists())
        for p in sorted(fluidsynth_dir.glob("*/bin/fluidsynth.exe")):
            if p.exists():
                return p

    for cand in _FLUIDSYNTH_FIXED_CANDIDATES:
        if cand.exists():
            return cand

    return None


def find_soundfont(explicit: Optional[Path] = None) -> Optional[Path]:
    """SF2/SF3 サウンドフォントファイルを発見する。

    探索順: explicit → 環境変数 SF_FILE → 既知のインストール先。
    見つからなければ None を返す（raise しない）。
    """
    if explicit is not None:
        return explicit if explicit.exists() else None

    env = os.environ.get("SF_FILE")
    if env:
        p = Path(env)
        if p.exists():
            return p
        logger.warning("SF_FILE=%s が見つからない — 他の候補を探す", env)

    for cand in _SF_CANDIDATES:
        if cand.exists():
            return cand

    return None
