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
from typing import Callable, Optional

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
_MODE_FONT_SIZE = 28
_BUTTON_FONT_SIZE = 22

# Colour palette for the follower-mode panel. Picked for high contrast
# from the operator's reading distance (~5m across the pit).
_MODE_COLOR_WAITING = ("#888888", "white")    # gray bg, white fg
_MODE_COLOR_TRACKING = ("#2a7", "white")      # green bg
_MODE_COLOR_INERTIA = ("#e87b00", "white")    # orange bg
_MODE_COLOR_CAPPED = ("#c00", "white")        # red bg
_MODE_COLOR_FLASH = ("#2255ff", "white")      # blue bg for force-lock-in flash

# Window geometry scaled ~2x to match the large pit-readable fonts.
_WINDOW_GEOMETRY = "1400x1000"


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

    def __init__(
        self,
        root: tk.Tk,
        state: AppState,
        *,
        on_force_lock_in: Optional[Callable[[], None]] = None,
    ):
        """
        Initialize GUI.

        Args:
            root: tkinter root window
            state: Shared AppState object
            on_force_lock_in: callback fired when the operator clicks
                the "▶ 楽章開始" button. Wired by ``main.py`` to
                ``AudioScoreFollowerApp.manual_force_lock_in``. Optional;
                passes a no-op if omitted so the GUI can run standalone
                in tests.
        """
        self.root = root
        self.state = state
        self._on_force_lock_in = on_force_lock_in or (lambda: None)

        self.root.title("Sequential Live Follower")
        self.root.geometry(_WINDOW_GEOMETRY)
        self.root.configure(bg="#f0f0f0")

        # Pick a font family that can actually render Japanese.  The previous
        # hard-coded "Arial" has no CJK glyphs, so Japanese filenames (e.g.
        # "運命_冒頭_guide.mxl") rendered as tofu boxes.
        self._font_family = _pick_font_family(self.root)

        # Force-lock-in button flash state (drives a 1s blue label
        # immediately after the button is pressed, then reverts to
        # the regular mode rendering).
        self._flash_until_ms: int = 0

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

        # 現在小節（大きな数字）＋ /全小節数 を横並びで表示
        measure_frame = tk.Frame(self.root, bg="#f0f0f0")
        measure_frame.pack(pady=(10, 4))

        measure_font = font.Font(family=family, size=_MEASURE_FONT_SIZE, weight="bold")
        self.label_measure = tk.Label(
            measure_frame, text="--", font=measure_font, bg="#f0f0f0", fg="blue"
        )
        self.label_measure.pack(side=tk.LEFT)

        measure_total_font = font.Font(family=family, size=_BEAT_FONT_SIZE)
        self.label_measure_total = tk.Label(
            measure_frame, text="/ --", font=measure_total_font, bg="#f0f0f0", fg="#246"
        )
        self.label_measure_total.pack(side=tk.LEFT, padx=(6, 0), anchor="s")

        # 拍位置
        beat_font = font.Font(family=family, size=_BEAT_FONT_SIZE)
        self.label_beat = tk.Label(
            self.root, text="♩ --", font=beat_font, bg="#f0f0f0", fg="#468"
        )
        self.label_beat.pack(pady=(0, 10))

        # 確信度
        conf_frame = tk.Frame(self.root, bg="#f0f0f0")
        conf_frame.pack(pady=8)

        tk.Label(
            conf_frame, text="確信度:", font=(family, _CONFIDENCE_FONT_SIZE), bg="#f0f0f0"
        ).pack(side=tk.LEFT, padx=10)

        self.label_confidence = tk.Label(
            conf_frame, text="--%", font=(family, _CONFIDENCE_FONT_SIZE), bg="#f0f0f0", fg="gray"
        )
        self.label_confidence.pack(side=tk.LEFT, padx=10)

        # マイクレベル — 確信度バーの直下に置く。確信度はマイク入力に直結する
        # ので並べて確認できると運用しやすい。下にある要素（クールダウン等）が
        # ウィンドウ高さの関係で見切れても、入力レベルだけは見えるようにする。
        self.label_mic_level = tk.Label(
            self.root, text="マイク: -- dBFS", font=(family, _COOLDOWN_FONT_SIZE), bg="#f0f0f0", fg="#444"
        )
        self.label_mic_level.pack(pady=2)

        # ----- 追随モード表示パネル + 楽章開始ボタン -----
        # 「いま OLTW が音を追えているのか / 慣性で進めているのか / 慣性 cap
        # で止まっているのか」を運用者が一目で判別できるようにする。
        # waiting (lock-in 前) / tracking (通常) / inertia / capped の 4 状態を
        # 背景色付きラベルで明示。「▶ 楽章開始」ボタンは指揮者の振り出しに
        # 合わせて押すと OLTW を強制 lock-in する — lock-in 成立後は
        # no-op になるため `_render_follower_mode` で自動的に非表示に切り替える
        # （音楽が進行しているのに「楽章開始」ボタンが残っていると運用上
        # 違和感があるため）。
        self.mode_frame = tk.Frame(self.root, bg="#f0f0f0")
        self.mode_frame.pack(pady=8, fill="x", padx=20)

        self.label_mode = tk.Label(
            self.mode_frame,
            text="⏸ 待機中",
            font=(family, _MODE_FONT_SIZE, "bold"),
            bg=_MODE_COLOR_WAITING[0],
            fg=_MODE_COLOR_WAITING[1],
            padx=20, pady=10,
            anchor="w",
        )
        self.label_mode.pack(side=tk.LEFT, fill="x", expand=True)

        self.button_force_lock_in = tk.Button(
            self.mode_frame,
            text="▶ 楽章開始",
            font=(family, _BUTTON_FONT_SIZE, "bold"),
            command=self._on_force_lock_in_clicked,
            padx=12, pady=6,
        )
        self.button_force_lock_in.pack(side=tk.RIGHT, padx=(10, 0))
        # Track current button visibility so we only call pack/forget when
        # the state actually changes (cheap; avoids spurious geometry work
        # on every 100ms poll tick).
        self._button_visible = True

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
                "N: 次の楽章   R: 現在の楽章を再ロード   "
                "L / ボタン: 楽章開始（強制 lock-in）"
            ),
            font=(family, _HINT_FONT_SIZE),
            bg="#f0f0f0",
            fg="#888",
        )
        self.label_hints.pack(side=tk.BOTTOM, pady=8)

    def _on_force_lock_in_clicked(self) -> None:
        """Button handler — forward to the application callback and
        trigger a brief blue flash on the mode label as visual
        confirmation that the press registered."""
        try:
            self._on_force_lock_in()
        except Exception as exc:  # noqa: BLE001
            logger.error("force_lock_in callback raised: %s", exc, exc_info=True)
        # Schedule a 1-second flash so the operator sees the click landed.
        now_ms = int(self.root.tk.call("clock", "milliseconds"))
        self._flash_until_ms = now_ms + 1000

    def _render_follower_mode(self, state: dict) -> None:
        """Render the follower-mode panel based on the AppState snapshot.

        Five visual states, in priority order:
          - flash    : just clicked the force-lock-in button (1s)
          - capped   : inertia ran past max_inertia_seconds → position
                       fixed, operator intervention needed (red)
          - inertia  : inertia advancing, countdown shown (orange)
          - tracking : DP tracking with confidence (green)
          - waiting  : pre-lock-in, awaiting downbeat (gray)

        Also toggles the "▶ 楽章開始" button visibility: shown only
        pre-lock-in (where it has a useful effect). After lock-in the
        button is a no-op so we hide it to keep the panel uncluttered;
        the L keybind is still available if the operator ever needs
        to re-arm lock-in after a reset.
        """
        now_ms = int(self.root.tk.call("clock", "milliseconds"))
        if now_ms < self._flash_until_ms:
            bg, fg = _MODE_COLOR_FLASH
            self.label_mode.config(text="🎯 lock-in 強制発動", bg=bg, fg=fg)
            # Don't touch button visibility during the flash — it will be
            # re-evaluated on the next poll tick once the flash expires.
            return

        is_locked = state.get('is_locked_in', False)
        in_inertia = state.get('is_in_inertia', False)
        elapsed = float(state.get('inertia_elapsed_sec', 0.0))
        cap = float(state.get('inertia_cap_sec', 10.0))

        if not is_locked:
            bg, fg = _MODE_COLOR_WAITING
            text = "⏸ 待機中（楽章開始を待っています）"
        elif in_inertia and elapsed >= cap:
            bg, fg = _MODE_COLOR_CAPPED
            text = (
                f"⛔ 慣性停止（位置固定）　"
                f"手動 → / L で復帰してください"
            )
        elif in_inertia:
            remaining = max(0.0, cap - elapsed)
            bg, fg = _MODE_COLOR_INERTIA
            text = (
                f"🌀 慣性進行中　残り {remaining:.1f}s / {cap:.0f}s"
                f"（音が戻れば自動復帰）"
            )
        else:
            bg, fg = _MODE_COLOR_TRACKING
            text = "🎵 追随中"

        self.label_mode.config(text=text, bg=bg, fg=fg)

        # Hide the "▶ 楽章開始" button after lock-in so the panel stops
        # showing a control that has no effect. Re-show it if lock-in
        # somehow drops (e.g. movement reload via R clears _locked_in).
        should_show_button = not is_locked
        if should_show_button and not self._button_visible:
            self.button_force_lock_in.pack(side=tk.RIGHT, padx=(10, 0))
            self._button_visible = True
        elif not should_show_button and self._button_visible:
            self.button_force_lock_in.pack_forget()
            self._button_visible = False

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

            # 小節番号（大きな数字）＋ /全小節数
            measure = state['measure']
            self.label_measure.config(text=str(measure))
            total = state.get('total_measures', 0)
            self.label_measure_total.config(text=f"/ {total}" if total > 0 else "/ --")

            # 拍位置
            beat_in_measure = state.get('beat_in_measure', 1.0)
            self.label_beat.config(text=f"♩ {int(beat_in_measure)}")

            # 確信度（色分け）
            conf = state['confidence']
            if conf > 0.6:
                color = "green"
            elif conf > 0.4:
                color = "orange"
            else:
                color = "red"
            self.label_confidence.config(text=f"{int(conf*100)}%", fg=color)

            # 次のトリガー
            next_trig = state['next_trigger_measure']
            if next_trig:
                self.label_next_trigger.config(text=f"次のトリガー: {next_trig} 小節目")
            else:
                self.label_next_trigger.config(text="次のトリガー: --")

            # 慣性モード (legacy field — kept for backward compat; the
            # primary follower-mode rendering is in label_mode below).
            self.label_inertia.config(text="")

            # ----- 追随モード表示の更新 -----
            self._render_follower_mode(state)

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
