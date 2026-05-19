#!/usr/bin/env python3
"""
ui/gui_tkinter.py - Real-Time Display GUI

Tkinter-based GUI showing:
- Current file name
- Current measure (large)
- Confidence score (color-coded)
- Next trigger measure
- Inertia mode indicator
"""

import logging
import tkinter as tk
from tkinter import font

from audio_score_follower.core.state_manager import AppState

logger = logging.getLogger(__name__)

# Font families preferred for rendering Japanese filenames / labels.  We pick
# the first one that the local Tk installation actually has — falling back to
# the generic "TkDefaultFont" so the GUI still works (with tofu glyphs) when
# no CJK font is installed.  On WSL2/Ubuntu, `sudo apt install fonts-noto-cjk`
# makes "Noto Sans CJK JP" available.
_PREFERRED_FONT_FAMILIES = (
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

# Font sizes were originally tuned for a 900x500 window with ~12pt body text.
# Bumped to ~2x so the operator can read the GUI from across the pit.
_TITLE_FONT_SIZE = 28
_FILE_FONT_SIZE = 32
_MEASURE_FONT_SIZE = 140
_BEAT_FONT_SIZE = 42
_CONFIDENCE_FONT_SIZE = 24
_TRIGGER_FONT_SIZE = 28
_INERTIA_FONT_SIZE = 24
_COOLDOWN_FONT_SIZE = 22
_HINT_FONT_SIZE = 18

# Window + confidence bar geometry, also scaled ~2x to match the new fonts.
# Height bumped to 1000 so the mic-level / inertia / cooldown / hint rows
# below the confidence bar are not clipped by the bottom of the window.
_WINDOW_GEOMETRY = "1400x1000"
_BAR_WIDTH = 400
_BAR_HEIGHT = 36


def _pick_font_family(root: tk.Tk) -> str:
    """Return the first available CJK-capable font family for this Tk root."""
    try:
        available = set(font.families(root=root))
    except Exception:  # noqa: BLE001 — Tk could be in a weird state
        available = set()
    for family in _PREFERRED_FONT_FAMILIES:
        if family in available:
            logger.info("GUI font family: %s", family)
            return family
    logger.warning(
        "No CJK-capable font found among %s — Japanese text may render as tofu. "
        "Install fonts-noto-cjk (Ubuntu) or equivalent.",
        _PREFERRED_FONT_FAMILIES,
    )
    return "TkDefaultFont"


class FollowerGUI:
    """
    Tkinter GUI for Sequential Live Follower.

    Displays playback status in real-time without blocking.
    """

    def __init__(self, root: tk.Tk, state: AppState):
        """
        Initialize GUI.

        Args:
            root: tkinter root window
            state: Shared AppState object
        """
        self.root = root
        self.state = state

        self.root.title("Sequential Live Follower")
        self.root.geometry(_WINDOW_GEOMETRY)
        self.root.configure(bg="#f0f0f0")

        # Pick a font family that can actually render Japanese.  The previous
        # hard-coded "Arial" has no CJK glyphs, so Japanese filenames (e.g.
        # "運命_冒頭_guide.mxl") rendered as tofu boxes.
        self._font_family = _pick_font_family(self.root)

        # Create widgets
        self._create_widgets()

        # Start polling for state updates
        self._poll_state()

        logger.info("GUI initialized")

    def _create_widgets(self):
        """Create and layout tkinter widgets."""
        family = self._font_family

        # タイトル
        title_font = font.Font(family=family, size=_TITLE_FONT_SIZE, weight="bold")
        title_label = tk.Label(
            self.root, text="Sequential Live Follower", font=title_font, bg="#f0f0f0"
        )
        title_label.pack(pady=(10, 2))

        # 楽章表示（例: 第1楽章 / 全3楽章）
        movement_font = font.Font(family=family, size=_FILE_FONT_SIZE, weight="bold")
        self.label_movement = tk.Label(
            self.root, text="楽章読込中…", font=movement_font, bg="#f0f0f0", fg="#333"
        )
        self.label_movement.pack(pady=(2, 0))

        # ファイル名 or エラーメッセージ（小さめ）
        file_font = font.Font(family=family, size=_CONFIDENCE_FONT_SIZE)
        self.label_file = tk.Label(
            self.root, text="[ファイル未読込]", font=file_font, bg="#f0f0f0", fg="#888",
            wraplength=1300, justify="center",
        )
        self.label_file.pack(pady=(0, 4))

        # 現在小節（大きな数字）＋ n / m 小節目 表示
        measure_font = font.Font(family=family, size=_MEASURE_FONT_SIZE, weight="bold")
        self.label_measure = tk.Label(
            self.root, text="--", font=measure_font, bg="#f0f0f0", fg="blue"
        )
        self.label_measure.pack(pady=(10, 0))

        measure_sub_font = font.Font(family=family, size=_BEAT_FONT_SIZE)
        self.label_measure_sub = tk.Label(
            self.root, text="-- / -- 小節目", font=measure_sub_font, bg="#f0f0f0", fg="#246"
        )
        self.label_measure_sub.pack(pady=(0, 4))

        # 拍位置
        beat_font = font.Font(family=family, size=_BEAT_FONT_SIZE)
        self.label_beat = tk.Label(
            self.root, text="♩ --", font=beat_font, bg="#f0f0f0", fg="#468"
        )
        self.label_beat.pack(pady=(0, 10))

        # 確信度バー
        conf_frame = tk.Frame(self.root, bg="#f0f0f0")
        conf_frame.pack(pady=8)

        tk.Label(
            conf_frame, text="確信度:", font=(family, _CONFIDENCE_FONT_SIZE), bg="#f0f0f0"
        ).pack(side=tk.LEFT, padx=10)

        self.label_confidence = tk.Label(
            conf_frame, text="-- (--)", font=(family, _CONFIDENCE_FONT_SIZE), bg="#f0f0f0", fg="gray"
        )
        self.label_confidence.pack(side=tk.LEFT, padx=10)

        self.canvas_confidence = tk.Canvas(
            conf_frame, width=_BAR_WIDTH, height=_BAR_HEIGHT, bg="white", highlightthickness=1
        )
        self.canvas_confidence.pack(side=tk.LEFT, padx=10)

        # マイクレベル — 確信度バーの直下に置く。確信度はマイク入力に直結する
        # ので並べて確認できると運用しやすい。下にある要素（クールダウン等）が
        # ウィンドウ高さの関係で見切れても、入力レベルだけは見えるようにする。
        self.label_mic_level = tk.Label(
            self.root, text="マイク: -- dBFS", font=(family, _COOLDOWN_FONT_SIZE), bg="#f0f0f0", fg="#444"
        )
        self.label_mic_level.pack(pady=2)

        # 次のトリガー
        trigger_font = font.Font(family=family, size=_TRIGGER_FONT_SIZE)
        self.label_next_trigger = tk.Label(
            self.root, text="次のトリガー: --", font=trigger_font, bg="#f0f0f0", fg="#555"
        )
        self.label_next_trigger.pack(pady=4)

        # 慣性モード表示
        self.label_inertia = tk.Label(
            self.root, text="", font=(family, _INERTIA_FONT_SIZE, "bold"), bg="#f0f0f0", fg="red"
        )
        self.label_inertia.pack(pady=2)

        # クールダウン表示
        self.label_cooldown = tk.Label(
            self.root, text="", font=(family, _COOLDOWN_FONT_SIZE), bg="#f0f0f0", fg="orange"
        )
        self.label_cooldown.pack(pady=2)

        # キーヒント
        self.label_hints = tk.Label(
            self.root,
            text=(
                "→/Space: スライドを進める   ←: スライドを戻す   "
                "N: 次の楽章   R: 現在の楽章を再ロード"
            ),
            font=(family, _HINT_FONT_SIZE),
            bg="#f0f0f0",
            fg="#888",
        )
        self.label_hints.pack(side=tk.BOTTOM, pady=8)

    def update_display(self):
        """Update GUI with current state."""
        try:
            state = self.state.get_all()

            # 楽章表示（例: 第1楽章 / 全3楽章）
            mv_num = state.get('movement_number', 1)
            mv_total = state.get('total_movements', 1)
            self.label_movement.config(text=f"第{mv_num}楽章 / 全{mv_total}楽章")

            # ファイル名 or ロードエラーメッセージ
            load_error = state.get('load_error')
            if load_error:
                self.label_file.config(text=f"⚠ {load_error}", fg="red")
            else:
                filename = state['xml_file'] or "[ファイル未読込]"
                if isinstance(filename, str):
                    filename = filename.replace("\\", "/").rsplit("/", 1)[-1]
                self.label_file.config(text=filename, fg="#888")

            # 小節番号（大きな数字）
            measure = state['measure']
            self.label_measure.config(text=str(measure))

            # n / m 小節目
            total = state.get('total_measures', 0)
            if total > 0:
                self.label_measure_sub.config(text=f"{measure} / {total} 小節目")
            else:
                self.label_measure_sub.config(text=f"{measure} 小節目")

            # 拍位置
            beat_in_measure = state.get('beat_in_measure', 1.0)
            self.label_beat.config(text=f"♩ {beat_in_measure:.2f}")

            # 確信度（色分け）
            conf = state['confidence']
            if conf > 0.6:
                color = "green"
            elif conf > 0.4:
                color = "orange"
            else:
                color = "red"
            self.label_confidence.config(text=f"{conf:.2f} ({int(conf*100)}%)", fg=color)

            # 確信度バー
            self.canvas_confidence.delete("all")
            bar_width = _BAR_WIDTH * conf
            self.canvas_confidence.create_rectangle(
                0, 0, bar_width, _BAR_HEIGHT, fill=color, outline="black"
            )

            # 次のトリガー
            next_trig = state['next_trigger_measure']
            if next_trig:
                self.label_next_trigger.config(text=f"次のトリガー: {next_trig} 小節目")
            else:
                self.label_next_trigger.config(text="次のトリガー: --")

            # 慣性モード
            if state['inertia_mode']:
                self.label_inertia.config(text="⚠ 慣性モード（推定）")
            else:
                self.label_inertia.config(text="")

            # クールダウン
            if state['cooldown_active']:
                self.label_cooldown.config(text="🔒 クールダウン中")
            else:
                self.label_cooldown.config(text="")

            # マイクレベル
            mic_available = state.get('mic_monitor_available', False)
            mic_db = state.get('mic_level_db', -120.0)
            gate = state.get('silence_gate_active', False)
            if not mic_available:
                mic_text = "マイク: 監視無効（silence gate 無効）— ログを確認"
                mic_color = "#c60"
            elif gate:
                mic_text = f"マイク: {mic_db:.1f} dBFS  ⚠ 無音（閾値未満）"
                mic_color = "red"
            else:
                mic_text = f"マイク: {mic_db:.1f} dBFS  ✓ 入力検出"
                mic_color = "#2a7"
            self.label_mic_level.config(text=mic_text, fg=mic_color)

        except Exception as e:
            logger.error(f"GUI update error: {e}")

    def _poll_state(self):
        """Poll state for updates every 100ms."""
        try:
            self.update_display()
        except Exception as e:
            logger.error(f"Polling error: {e}")

        # Schedule next poll
        self.root.after(100, self._poll_state)

    def on_closing(self):
        """Handle window close event."""
        logger.info("GUI closing")
        self.root.destroy()
