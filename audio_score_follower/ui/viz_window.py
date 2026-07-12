#!/usr/bin/env python3
"""ui/viz_window.py - Realtime feature / confidence visualiser.

A separate Toplevel window that shows, in real time, how the follower's
confidence is formed. Designed around an intuitive visual grammar —
**up = good, green = matching, big = loud** — so a non-expert can read the
state at a glance:

  0. Header       — current measure + a large colour-coded 「一致度」gauge.
  1. いまの音 と 楽譜の音 — live input (cyan) and reference (amber) side by
     side per pitch class, growing from a shared baseline. When the harmony
     matches, each pair has equal height; a 「この瞬間の一致」 percentage
     (cosine similarity) summarises it.
  2. 一致度の推移 — the display confidence (0-100%) scrolling left, drawn
     as a filled area over green/yellow/red zones. Rising = locking on.
     Deliberately the SAME word (一致度) as the header gauge: it is that
     gauge's time history, not a different metric.
  3. 演奏位置さがし — the search band's cost curve flipped into a mountain:
     the peak marks where the follower thinks the performance is right
     now. Framed as position search, not another similarity metric. A
     sharp lone peak = confident; a flat or twin-peaked ridge = ambiguous.

The window is a pure consumer of ``VizFeed`` (core/viz_feed.py) and knows
nothing about the follower or the app, so a future audience-facing screen
can be added as a parallel renderer over the same feed.

Drawing follows the project's existing perf rule (gui_tkinter pitfall #6):
persistent canvas items updated via ``coords`` / ``itemconfigure`` rather
than delete/recreate every tick.
"""

from __future__ import annotations

import logging
import tkinter as tk
from typing import Optional

import numpy as np

from audio_score_follower.core.viz_feed import VizFeed
from audio_score_follower.ui.gui_tkinter import _pick_font_family

logger = logging.getLogger(__name__)

_POLL_MS = 100  # matches the main GUI poll cadence

_PITCH_CLASSES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]

# Dark palette — high contrast, reads well in a dim pit.
_BG = "#151920"
_PANEL = "#1f242e"
_GRID = "#39404f"
_TEXT = "#e6e9ef"
_MUTED = "#8b93a3"
_LIVE_COLOR = "#4fc3f7"     # cyan — live input
_REF_COLOR = "#ffb74d"      # amber — reference
_TREND_LINE = "#d7dee9"
_TERRAIN_LINE = "#c3e88d"   # green ridge line
_TERRAIN_FILL = "#2c4232"   # dim green under the ridge
_NEEDLE = "#ffffff"

# Confidence colour steps — same 0.6 / 0.4 breakpoints as the main GUI's
# confidence label so both screens tell the same story.
_CONF_GOOD = "#43a047"
_CONF_MID = "#ef9a1a"
_CONF_BAD = "#e53935"
# Subtle zone tints behind the trend curve.
_ZONE_GOOD = "#1f3122"
_ZONE_MID = "#332d1c"
_ZONE_BAD = "#33201f"

_WINDOW_GEOMETRY = "980x920+40+40"


def _conf_color(value: float) -> str:
    if value >= 0.6:
        return _CONF_GOOD
    if value >= 0.4:
        return _CONF_MID
    return _CONF_BAD


class VizWindow:
    """Toplevel realtime visualiser driven by a VizFeed."""

    def __init__(self, root: tk.Tk, feed: VizFeed) -> None:
        self.feed = feed
        self.top = tk.Toplevel(root)
        self.top.title("特徴量・確信度モニタ")
        self.top.geometry(_WINDOW_GEOMETRY)
        self.top.configure(bg=_BG)
        # Raise above the (larger) main GUI window so it isn't hidden on
        # open. Brief topmost pulse then release so it doesn't permanently
        # obscure the operator's main display.
        self.top.lift()
        self.top.attributes("-topmost", True)
        self.top.after(400, lambda: self.top.attributes("-topmost", False))
        self._font = _pick_font_family(root)

        self.canvas = tk.Canvas(self.top, bg=_BG, highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)

        # Item id caches so we update rather than recreate each tick.
        self._measure_id: Optional[int] = None
        self._status_id: Optional[int] = None
        self._gauge_fill: Optional[int] = None
        self._gauge_text: Optional[int] = None
        self._live_bars: list[int] = []
        self._ref_bars: list[int] = []
        self._sim_text: Optional[int] = None
        self._trend_fill: Optional[int] = None
        self._trend_line: Optional[int] = None
        self._terrain_fill: Optional[int] = None
        self._terrain_line: Optional[int] = None
        self._needle_id: Optional[int] = None
        self._needle_dot: Optional[int] = None
        self._static_built = False
        self._built_w = 0
        self._built_h = 0

        self.top.after(_POLL_MS, self._poll)

    # ------------------------------------------------------------ geometry
    def _layout(self) -> dict:
        w = max(self.canvas.winfo_width(), 200)
        h = max(self.canvas.winfo_height(), 200)
        pad = 16
        header_h = 96
        body_top = header_h
        body_h = h - header_h - pad
        chroma_h = int(body_h * 0.34)
        trend_h = int(body_h * 0.30)
        terrain_h = body_h - chroma_h - trend_h - 2 * pad
        return {
            "w": w, "h": h, "pad": pad,
            "chroma": (pad, body_top, w - pad, body_top + chroma_h),
            "trend": (pad, body_top + chroma_h + pad,
                      w - pad, body_top + chroma_h + pad + trend_h),
            "terrain": (pad, body_top + chroma_h + trend_h + 2 * pad,
                        w - pad,
                        body_top + chroma_h + trend_h + 2 * pad + terrain_h),
        }

    # ------------------------------------------------------------ static art
    def _build_static(self, lay: dict) -> None:
        c = self.canvas
        c.delete("all")
        pad = lay["pad"]
        w = lay["w"]

        # --- header: measure (big) + status + match gauge ---
        self._measure_id = c.create_text(
            pad, 28, anchor="w", fill=_TEXT,
            font=(self._font, 26, "bold"), text="小節 --",
        )
        self._status_id = c.create_text(
            w - pad, 28, anchor="e", fill=_MUTED,
            font=(self._font, 13), text="",
        )
        gauge_y0, gauge_y1 = 56, 82
        c.create_rectangle(pad, gauge_y0, w - pad, gauge_y1,
                           fill=_PANEL, outline=_GRID)
        self._gauge_fill = c.create_rectangle(
            pad, gauge_y0, pad, gauge_y1, fill=_CONF_BAD, outline="",
        )
        self._gauge_text = c.create_text(
            (pad + w - pad) / 2, (gauge_y0 + gauge_y1) / 2,
            fill=_TEXT, font=(self._font, 13, "bold"), text="一致度 --%",
        )
        self._gauge_span = (pad, gauge_y0, w - pad, gauge_y1)

        # --- chroma panel ---
        cx0, cy0, cx1, cy1 = lay["chroma"]
        c.create_rectangle(cx0, cy0, cx1, cy1, fill=_PANEL, outline=_GRID)
        c.create_text(cx0 + 8, cy0 + 14, anchor="w", fill=_TEXT,
                     font=(self._font, 12, "bold"),
                     text="いまの音 と 楽譜の音 — 高さが揃えば一致")
        # Legend.
        lx = cx0 + 8
        ly = cy0 + 36
        c.create_rectangle(lx, ly - 6, lx + 14, ly + 6, fill=_LIVE_COLOR, outline="")
        c.create_text(lx + 20, ly, anchor="w", fill=_MUTED,
                     font=(self._font, 10), text="いま聴こえている音（マイク）")
        c.create_rectangle(lx + 210, ly - 6, lx + 224, ly + 6,
                           fill=_REF_COLOR, outline="")
        c.create_text(lx + 230, ly, anchor="w", fill=_MUTED,
                     font=(self._font, 10), text="楽譜の音（参照演奏）")
        self._sim_text = c.create_text(
            cx1 - 10, cy0 + 24, anchor="e", fill=_MUTED,
            font=(self._font, 15, "bold"), text="この瞬間の一致 --%",
        )
        # Baseline + pitch labels.
        base_y = cy1 - 24
        c.create_line(cx0 + 8, base_y, cx1 - 8, base_y, fill=_GRID)
        n = len(_PITCH_CLASSES)
        span = (cx1 - cx0 - 16)
        slot = span / n
        for i, name in enumerate(_PITCH_CLASSES):
            xc = cx0 + 8 + slot * (i + 0.5)
            c.create_text(xc, base_y + 12, fill=_MUTED,
                         font=(self._font, 10), text=name)
        self._chroma_geom = (cx0 + 8, cy0 + 52, cx1 - 8, base_y)

        # Paired bars: live on the left half of the slot, ref on the right.
        bar_w = slot * 0.34
        self._live_bars = []
        self._ref_bars = []
        for i in range(n):
            xc = cx0 + 8 + slot * (i + 0.5)
            self._live_bars.append(c.create_rectangle(
                xc - bar_w - 1, base_y, xc - 1, base_y,
                fill=_LIVE_COLOR, outline="",
            ))
            self._ref_bars.append(c.create_rectangle(
                xc + 1, base_y, xc + bar_w + 1, base_y,
                fill=_REF_COLOR, outline="",
            ))

        # --- trend panel ---
        tx0, ty0, tx1, ty1 = lay["trend"]
        c.create_rectangle(tx0, ty0, tx1, ty1, fill=_PANEL, outline=_GRID)
        plot_y0 = ty0 + 30
        plot_y1 = ty1 - 8

        def trend_y(conf: float) -> float:
            conf = max(0.0, min(1.0, conf))
            return plot_y1 - conf * (plot_y1 - plot_y0)

        self._trend_geom = (tx0 + 8, plot_y0, tx1 - 8, plot_y1)
        self._trend_y = trend_y
        # Colour zones behind the curve (same breakpoints as the gauge).
        for lo, hi, col in ((0.6, 1.0, _ZONE_GOOD), (0.4, 0.6, _ZONE_MID),
                            (0.0, 0.4, _ZONE_BAD)):
            c.create_rectangle(tx0 + 8, trend_y(hi), tx1 - 8, trend_y(lo),
                               fill=col, outline="")
        for v in (0.4, 0.6):
            c.create_line(tx0 + 8, trend_y(v), tx1 - 8, trend_y(v),
                         fill=_GRID, dash=(3, 4))
        c.create_text(tx0 + 8, ty0 + 14, anchor="w", fill=_TEXT,
                     font=(self._font, 12, "bold"),
                     text="一致度の推移 — 上にいるほど自信あり")
        self._trend_fill = c.create_polygon(
            0, 0, 0, 0, fill="#31435a", outline="",
        )
        self._trend_line = c.create_line(0, 0, 0, 0, fill=_TREND_LINE, width=2)
        # Zone labels AFTER the fill/line items so they stay readable on
        # top of the area chart.
        c.create_text(tx1 - 10, trend_y(0.8), anchor="e", fill=_CONF_GOOD,
                     font=(self._font, 10, "bold"), text="好調")
        c.create_text(tx1 - 10, trend_y(0.5), anchor="e", fill=_CONF_MID,
                     font=(self._font, 10, "bold"), text="様子見")
        c.create_text(tx1 - 10, trend_y(0.2), anchor="e", fill=_CONF_BAD,
                     font=(self._font, 10, "bold"), text="迷子ぎみ")

        # --- terrain panel ---
        bx0, by0, bx1, by1 = lay["terrain"]
        c.create_rectangle(bx0, by0, bx1, by1, fill=_PANEL, outline=_GRID)
        c.create_text(bx0 + 8, by0 + 14, anchor="w", fill=_TEXT,
                     font=(self._font, 12, "bold"),
                     text="演奏位置さがし — 近くの小節を聴きくらべて、"
                          "いちばん似ている小節（山の頂上）が「いまここ」")
        # Vertical meaning of the mountain height (top-only; the mountain
        # metaphor already implies low = less similar, and a bottom label
        # collides with the measure axis).
        c.create_text(bx0 + 8, by0 + 34, anchor="w", fill=_MUTED,
                     font=(self._font, 9), text="↑ 高いほど よく似ている")
        # Axis-end labels get the actual band-edge measures at draw time.
        self._terrain_left_label = c.create_text(
            bx0 + 8, by1 - 10, anchor="w", fill=_MUTED,
            font=(self._font, 10), text="◀ 手前の小節",
        )
        self._terrain_right_label = c.create_text(
            bx1 - 8, by1 - 10, anchor="e", fill=_MUTED,
            font=(self._font, 10), text="先の小節 ▶",
        )
        self._terrain_geom = (bx0 + 8, by0 + 46, bx1 - 8, by1 - 26)
        self._terrain_fill = c.create_polygon(
            0, 0, 0, 0, fill=_TERRAIN_FILL, outline="",
        )
        self._terrain_line = c.create_line(0, 0, 0, 0, fill=_TERRAIN_LINE,
                                           width=2)
        self._needle_id = c.create_line(0, 0, 0, 0, fill=_NEEDLE, width=1,
                                        dash=(4, 3))
        self._needle_dot = c.create_oval(0, 0, 0, 0, fill=_NEEDLE, outline="")
        # "いまここ 小節N" callout that rides above the peak.
        self._peak_label = c.create_text(
            0, 0, anchor="s", fill=_NEEDLE,
            font=(self._font, 11, "bold"), text="",
        )

        self._static_built = True
        self._built_w = lay["w"]
        self._built_h = lay["h"]

    # ------------------------------------------------------------ poll/redraw
    def _poll(self) -> None:
        try:
            self._redraw()
        except Exception:  # noqa: BLE001 — never let the viz kill the app
            logger.exception("viz redraw failed")
        if self.top.winfo_exists():
            self.top.after(_POLL_MS, self._poll)

    def _redraw(self) -> None:
        lay = self._layout()
        if (not self._static_built or lay["w"] != self._built_w
                or lay["h"] != self._built_h):
            self._build_static(lay)

        snap = self.feed.snapshot()
        if snap["frame_count"] == 0:
            self.canvas.itemconfigure(
                self._status_id, text="待機中 — 入力を待っています…")
            return

        self._draw_header(snap)
        self._draw_chroma(snap)
        self._draw_trend(snap)
        self._draw_terrain(snap)

    def _draw_header(self, snap: dict) -> None:
        c = self.canvas
        c.itemconfigure(self._measure_id, text=f"小節 {snap['measure']}")
        if snap["is_mismatched"]:
            c.itemconfigure(self._status_id, text="⚠ 追随ずれ疑い",
                            fill=_CONF_BAD)
        else:
            c.itemconfigure(self._status_id, text="追跡中", fill=_MUTED)

        conf = snap["display_confidence"]
        gx0, gy0, gx1, gy1 = self._gauge_span
        fill_x = gx0 + (gx1 - gx0) * max(0.0, min(1.0, conf))
        c.coords(self._gauge_fill, gx0, gy0, fill_x, gy1)
        c.itemconfigure(self._gauge_fill, fill=_conf_color(conf))
        c.itemconfigure(self._gauge_text, text=f"一致度 {conf * 100:.0f}%")

    def _draw_chroma(self, snap: dict) -> None:
        live = snap["live_chroma"]
        ref = snap["ref_chroma"]
        x0, y_top, x1, base_y = self._chroma_geom
        n = len(_PITCH_CLASSES)
        slot = (x1 - x0) / n
        bar_w = slot * 0.34
        max_h = base_y - y_top

        def _bars(items, vec, dx):
            if vec is None:
                return
            peak = float(vec.max()) if vec.size else 0.0
            scale = peak if peak > 1e-6 else 1.0
            for i in range(n):
                xc = x0 + slot * (i + 0.5)
                height = (float(vec[i]) / scale) * max_h
                self.canvas.coords(
                    items[i],
                    xc + dx - bar_w if dx <= 0 else xc + dx,
                    base_y - height,
                    xc + dx if dx <= 0 else xc + dx + bar_w,
                    base_y,
                )

        _bars(self._live_bars, live, dx=-1)
        _bars(self._ref_bars, ref, dx=1)

        if live is not None and ref is not None:
            sim = float(np.dot(live, ref))
            sim = max(0.0, min(1.0, sim))
            self.canvas.itemconfigure(
                self._sim_text,
                text=f"この瞬間の一致 {sim * 100:.0f}%",
                fill=_conf_color(sim),
            )

    def _draw_trend(self, snap: dict) -> None:
        hist = snap["display_confidence_hist"]
        if len(hist) < 2:
            return
        x0, _, x1, y1 = self._trend_geom
        m = len(hist)
        span = x1 - x0
        pts: list[float] = []
        for i, v in enumerate(hist):
            x = x0 + span * (i / (m - 1))
            pts.extend((x, self._trend_y(v)))
        self.canvas.coords(self._trend_line, *pts)
        # Filled area under the curve down to the 0% baseline.
        self.canvas.coords(
            self._trend_fill, x0, y1, *pts, x1, y1,
        )

    def _draw_terrain(self, snap: dict) -> None:
        band = snap["band_costs"]
        if band is None or band.size < 2:
            return
        x0, y0, x1, y1 = self._terrain_geom
        m = band.size
        span = x1 - x0
        vmax = float(band.max())
        vmin = float(band.min())
        rng = (vmax - vmin) if (vmax - vmin) > 1e-6 else 1.0
        pts: list[float] = []
        for i in range(m):
            x = x0 + span * (i / (m - 1))
            # Similarity = flipped cost: the best match becomes the PEAK.
            goodness = (vmax - float(band[i])) / rng
            pts.extend((x, y1 - goodness * (y1 - y0)))
        self.canvas.coords(self._terrain_line, *pts)
        self.canvas.coords(self._terrain_fill, x0, y1, *pts, x1, y1)

        # Ground the axis in real measure numbers when available.
        lo_m = snap["band_lo_measure"]
        hi_m = snap["band_hi_measure"]
        self.canvas.itemconfigure(
            self._terrain_left_label,
            text=f"◀ 手前（小節 {lo_m}）" if lo_m is not None else "◀ 手前の小節",
        )
        self.canvas.itemconfigure(
            self._terrain_right_label,
            text=f"（小節 {hi_m}）先 ▶" if hi_m is not None else "先の小節 ▶",
        )

        # Needle at the DP-chosen position — where the follower says
        # "the performance is HERE" — with an "いまここ 小節N" callout.
        argmin_idx = snap["dp_ref_frame"] - snap["band_lo"]
        if 0 <= argmin_idx < m:
            nx = x0 + span * (argmin_idx / (m - 1))
            ny = pts[2 * argmin_idx + 1]
            self.canvas.coords(self._needle_id, nx, y0, nx, y1)
            self.canvas.coords(self._needle_dot, nx - 4, ny - 4, nx + 4, ny + 4)
            peak_m = snap["peak_measure"]
            label = f"いまここ 小節 {peak_m}" if peak_m is not None else "いまここ"
            # Keep the callout inside the panel horizontally.
            lx = min(max(nx, x0 + 48), x1 - 48)
            self.canvas.coords(self._peak_label, lx, ny - 8)
            self.canvas.itemconfigure(self._peak_label, text=label)
