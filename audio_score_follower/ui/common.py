#!/usr/bin/env python3
"""
ui/common.py - Shared UI utilities (fonts, base style, shared thresholds)

Single home for the small bits every Tk window in this project needs:

- CJK-capable font family detection (previously a private helper in
  gui_tkinter that three other modules imported).
- The ttk base-style bootstrap that launcher / build_window duplicated.
- The confidence colour breakpoints that the operator GUI and the viz
  window must keep in sync.
"""

from __future__ import annotations

import logging
import tkinter as tk
from tkinter import font, ttk
from typing import Optional

logger = logging.getLogger(__name__)

# Font families preferred for rendering Japanese filenames / labels.  We pick
# the first one that the local Tk installation actually has — falling back to
# the generic "TkDefaultFont" so the GUI still works (with tofu glyphs) when
# no CJK font is installed.  On WSL2/Ubuntu, `sudo apt install fonts-noto-cjk`
# makes "Noto Sans CJK JP" available.
PREFERRED_FONT_FAMILIES = (
    "Noto Sans CJK JP",
    "Noto Sans JP",
    "Yu Gothic UI",
    "Yu Gothic",
    "Meiryo",
    "MS Gothic",
    "TakaoPGothic",
    "TakaoGothic",
    "IPAexGothic",
    "IPAPGothic",
    "Hiragino Sans",
    "DejaVu Sans",
)

# Confidence colour breakpoints shared by the operator GUI's confidence
# label (gui_tkinter.update_display) and the viz window (_conf_color) so
# both screens tell the same story. The palettes differ per screen (Tk
# colour names on the light GUI, dark-pit hex on the viz window) and the
# boundary comparison is historical per-site (> vs >=) — only the
# breakpoints themselves are shared here.
CONFIDENCE_GOOD_THRESHOLD = 0.6
CONFIDENCE_MID_THRESHOLD = 0.4


def pick_font_family(root: tk.Tk) -> str:
    """Return the first available CJK-capable font family for this Tk root."""
    try:
        available = set(font.families(root=root))
    except Exception:  # noqa: BLE001 — Tk could be in a weird state
        available = set()
    for family in PREFERRED_FONT_FAMILIES:
        if family in available:
            logger.info("GUI font family: %s", family)
            return family
    logger.warning(
        "No CJK-capable font found among %s — Japanese text may render as tofu. "
        "Install fonts-noto-cjk (Ubuntu) or equivalent.",
        PREFERRED_FONT_FAMILIES,
    )
    return "TkDefaultFont"


def apply_base_style(
    target: tk.Misc, font_source: Optional[tk.Tk] = None
) -> tuple[tuple[str, int], tuple[str, int]]:
    """Apply the shared 12pt base font to ``target`` and return the fonts.

    ``target`` is the window whose ttk style / option database gets the
    font (the Tk root for the launcher, the Toplevel for the build
    window). ``font_source`` is the root used for font-family detection;
    defaults to ``target``.

    Returns ``(font, font_small)`` — the (family, 12) / (family, 10)
    tuples the callers keep for per-widget overrides.
    """
    family = pick_font_family(font_source if font_source is not None else target)
    base_font = (family, 12)
    small_font = (family, 10)
    ttk.Style(target).configure(".", font=base_font)
    target.option_add("*Font", base_font)
    return base_font, small_font
