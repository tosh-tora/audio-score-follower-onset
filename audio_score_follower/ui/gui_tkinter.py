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
from audio_score_follower.ui.common import (
    CONFIDENCE_GOOD_THRESHOLD,
    CONFIDENCE_MID_THRESHOLD,
    pick_font_family,
)

logger = logging.getLogger(__name__)

# Font sizes were originally tuned for a 900x500 window with ~12pt body text.
# Bumped to ~2x so the operator can read the GUI from across the pit.
_TITLE_FONT_SIZE = 28
_FILE_FONT_SIZE = 32
_MEASURE_FONT_SIZE = 140
_BEAT_FONT_SIZE = 42
_CONFIDENCE_FONT_SIZE = 24
_TRIGGER_FONT_SIZE = 28
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
_MODE_COLOR_FLASH = ("#2255ff", "white")      # blue bg for start-button flash

# Window geometry scaled ~2x to match the large pit-readable fonts.
_WINDOW_GEOMETRY = "1400x1000"

# dB step for the silence-threshold −/＋ buttons next to the mic level.
# Coarser than the ↑/↓ keys (±0.2 dB) — a click is a deliberate nudge,
# keys auto-repeat for fine adjustment.
_THRESHOLD_BUTTON_STEP_DB = 1.0


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
        on_start: Optional[Callable[[], None]] = None,
        on_end: Optional[Callable[[], None]] = None,
        on_adjust_threshold: Optional[Callable[[float], None]] = None,
    ):
        """
        Initialize GUI.

        Args:
            root: tkinter root window
            state: Shared AppState object
            on_start: callback fired when the operator clicks
                the "▶ 演奏開始" button. Wired by ``main.py`` to
                ``AudioScoreFollowerApp.manual_start``. Optional;
                passes a no-op if omitted so the GUI can run standalone
                in tests.
            on_end: callback fired when the operator clicks the
                "■ 演奏終了" button. Wired by ``main.py`` to
                ``AudioScoreFollowerApp.end_performance`` (stops the
                follower so tracking/triggers halt). Optional; no-op if
                omitted.
            on_adjust_threshold: callback fired with a dB delta when
                the operator clicks the silence-threshold −/＋ buttons
                next to the mic level readout. Wired by ``main.py`` to
                ``AudioScoreFollowerApp.adjust_silence_threshold``
                (no-op in wav/loopback modes where no gate runs).
                Optional for standalone/test use.
        """
        self.root = root
        self.state = state
        self._on_start = on_start or (lambda: None)
        self._on_end = on_end or (lambda: None)
        self._on_adjust_threshold = on_adjust_threshold or (lambda _d: None)

        self.root.title("Sequential Live Follower")
        self.root.geometry(_WINDOW_GEOMETRY)
        self.root.configure(bg="#f0f0f0")

        # Pick a font family that can actually render Japanese.  The previous
        # hard-coded "Arial" has no CJK glyphs, so Japanese filenames (e.g.
        # "運命_冒頭_guide.mxl") rendered as tofu boxes.
        self._font_family = pick_font_family(self.root)

        # Start button flash state (drives a 1s blue label immediately
        # after the button is pressed, then reverts to the regular
        # mode rendering).
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

        # 「次の楽章」インジケータ。N キーで送れる次の楽章があるか、
        # それとも最終楽章で N が効かないのかを常時可視化する（押しても
        # 無反応で「壊れた?」と不安になるのを防ぐ）。
        next_font = font.Font(family=family, size=_CONFIDENCE_FONT_SIZE)
        self.label_next_movement = tk.Label(
            self.root, text="", font=next_font, bg="#f0f0f0", fg="#555",
        )
        self.label_next_movement.pack(pady=(0, 0))

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

        # ずれ検知警告 — mismatch detector が「カウントが演奏からずれた疑い」
        # を立てている間だけ表示する。操作者は ←/→ で手動補正できる。
        # 毎フレーム pack/forget しない（差分時のみ。落とし穴 #6）。
        self.label_mismatch = tk.Label(
            self.root,
            text="⚠ 追随ずれ疑い — ←/→ で補正可",
            font=(family, _CONFIDENCE_FONT_SIZE, "bold"),
            bg="#c62828", fg="white", padx=12, pady=4,
        )
        self._mismatch_visible = False

        # マイクのノイズ抑制フィルター警告 — 起動時に一度だけ判定される
        # mic_effects_warning が非 None の間表示する（mismatch と同じ
        # 差分時のみ pack/forget パターン。落とし穴 #6）。
        self.label_mic_effects = tk.Label(
            self.root,
            text="",
            font=(family, _CONFIDENCE_FONT_SIZE, "bold"),
            bg="#e65100", fg="white", padx=12, pady=4,
            wraplength=1300, justify="center",
        )
        self._mic_effects_visible = False

        # SlideController 起動失敗警告 — 起動時に一度だけ判定される
        # slide_controller_warning が非 None の間表示する（mic_effects と同じ
        # 差分時のみ pack/forget パターン。落とし穴 #6）。
        self.label_slide_warning = tk.Label(
            self.root,
            text="",
            font=(family, _CONFIDENCE_FONT_SIZE, "bold"),
            bg="#e65100", fg="white", padx=12, pady=4,
            wraplength=1300, justify="center",
        )
        self._slide_warning_visible = False

        # マイクレベル — 確信度バーの直下に置く。確信度はマイク入力に直結する
        # ので並べて確認できると運用しやすい。下にある要素（クールダウン等）が
        # ウィンドウ高さの関係で見切れても、入力レベルだけは見えるようにする。
        # 閾値の −/＋ ボタンを併設（Issue #41: ↑/↓ キーだけでは発見性が低い。
        # wav/loopback ではコールバック側が no-op なので disable する）。
        self.mic_frame = tk.Frame(self.root, bg="#f0f0f0")
        self.mic_frame.pack(pady=2)
        self.label_mic_level = tk.Label(
            self.mic_frame, text="マイク: -- dBFS", font=(family, _COOLDOWN_FONT_SIZE), bg="#f0f0f0", fg="#444"
        )
        self.label_mic_level.pack(side=tk.LEFT)
        self.button_thr_down = tk.Button(
            self.mic_frame,
            text="− 閾値",
            font=(family, _HINT_FONT_SIZE),
            command=lambda: self._on_adjust_threshold(-_THRESHOLD_BUTTON_STEP_DB),
            padx=6,
        )
        self.button_thr_down.pack(side=tk.LEFT, padx=(16, 4))
        self.button_thr_up = tk.Button(
            self.mic_frame,
            text="＋ 閾値",
            font=(family, _HINT_FONT_SIZE),
            command=lambda: self._on_adjust_threshold(_THRESHOLD_BUTTON_STEP_DB),
            padx=6,
        )
        self.button_thr_up.pack(side=tk.LEFT, padx=4)
        # Track enabled/disabled so we only reconfigure on change
        # (100ms poll — pitfall #6).
        self._thr_buttons_enabled = True

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

        self.button_start = tk.Button(
            self.mode_frame,
            text="▶ 演奏開始",
            font=(family, _BUTTON_FONT_SIZE, "bold"),
            command=self._on_start_clicked,
            padx=12, pady=6,
        )
        self.button_start.pack(side=tk.RIGHT, padx=(10, 0))
        # Track current button visibility so we only call pack/forget when
        # the state actually changes (cheap; avoids spurious geometry work
        # on every 100ms poll tick).
        self._button_visible = True

        # 「■ 演奏終了」 button (Issue #44): the follower keeps tracking after
        # the music stops, so the operator needs an explicit halt. Shown
        # while the performance is running; hidden while waiting-for-start
        # and after the performance has ended. Packed to the RIGHT before
        # the start button so it sits inboard of it.
        self.button_end = tk.Button(
            self.mode_frame,
            text="■ 演奏終了",
            font=(family, _BUTTON_FONT_SIZE, "bold"),
            command=self._on_end_clicked,
            padx=12, pady=6,
        )
        self._end_button_visible = False

        # 次のトリガー
        trigger_font = font.Font(family=family, size=_TRIGGER_FONT_SIZE)
        self.label_next_trigger = tk.Label(
            self.root, text="次のトリガー: --", font=trigger_font, bg="#f0f0f0", fg="#555"
        )
        self.label_next_trigger.pack(pady=4)

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
                "L / ボタン: 演奏開始（押しズレ±数秒は自動補正）   "
                "E / ボタン: 演奏終了（追随を停止）"
            ),
            font=(family, _HINT_FONT_SIZE),
            bg="#f0f0f0",
            fg="#888",
        )
        self.label_hints.pack(side=tk.BOTTOM, pady=8)

    def _on_start_clicked(self) -> None:
        """Button handler — forward to the application callback and
        trigger a brief blue flash on the mode label as visual
        confirmation that the press registered."""
        try:
            self._on_start()
        except Exception as exc:  # noqa: BLE001
            logger.error("force_lock_in callback raised: %s", exc, exc_info=True)
        # Schedule a 1-second flash so the operator sees the click landed.
        now_ms = int(self.root.tk.call("clock", "milliseconds"))
        self._flash_until_ms = now_ms + 1000

    def _on_end_clicked(self) -> None:
        """Button handler — forward to the end-performance callback. The
        'ended' mode is rendered on the next poll from the state flag, so
        no flash is needed here (the panel visibly changes to the stopped
        banner)."""
        try:
            self._on_end()
        except Exception as exc:  # noqa: BLE001
            logger.error("end_performance callback raised: %s", exc, exc_info=True)

    def _render_follower_mode(self, state: dict) -> None:
        """Render the follower-mode panel based on the AppState snapshot.

        Five visual states, in priority order:
          - flash    : just clicked the start button (1s)
          - capped   : inertia ran past max_inertia_seconds → position
                       fixed, operator intervention needed (red)
          - inertia  : inertia advancing, countdown shown (orange)
          - tracking : DP tracking with confidence (green)
          - waiting  : pre-lock-in, awaiting downbeat (gray)

        Also toggles the "▶ 演奏開始" button visibility: shown while
        waiting for the operator start and pre-lock-in (first press
        starts tracking; a second press force-arms lock-in at the
        downbeat). After lock-in the button is a no-op so we hide it;
        the L keybind is still available if the operator ever needs
        to re-arm after a reset.
        """
        now_ms = int(self.root.tk.call("clock", "milliseconds"))
        if now_ms < self._flash_until_ms:
            bg, fg = _MODE_COLOR_FLASH
            self.label_mode.config(text="🎯 開始を受け付けました", bg=bg, fg=fg)
            # Don't touch button visibility during the flash — it will be
            # re-evaluated on the next poll tick once the flash expires.
            return

        is_locked = state.get('is_locked_in', False)
        in_inertia = state.get('is_in_inertia', False)
        waiting_for_start = state.get('waiting_for_start', False)
        performance_ended = state.get('performance_ended', False)
        elapsed = float(state.get('inertia_elapsed_sec', 0.0))
        cap = float(state.get('inertia_cap_sec', 10.0))

        if performance_ended:
            # Operator pressed 「■ 演奏終了」: follower stopped. Wins over
            # every tracking state (is_locked may still be latched).
            bg, fg = _MODE_COLOR_WAITING
            text = "⏹ 演奏終了（停止中）— R で再追随 / N で次の楽章"
        elif waiting_for_start:
            bg, fg = _MODE_COLOR_WAITING
            text = "⏸ 開始待ち（▶ 演奏開始 を押してください）"
        elif state.get('awaiting_first_sound', False) and not is_locked:
            # Start pressed, performance not confirmed yet (Issue #41):
            # tell the operator what happens if the opening stays below
            # the gate threshold.
            bg, fg = _MODE_COLOR_WAITING
            timeout = float(state.get('start_gate_timeout_sec', 0.0))
            if timeout > 0:
                text = (
                    f"🎧 音を待っています"
                    f"（無音でも {timeout:.0f} 秒後に自動開始）"
                )
            else:
                text = "🎧 音を待っています（閾値超えの音で開始）"
        elif not is_locked:
            bg, fg = _MODE_COLOR_WAITING
            text = "🎧 音を待っています（曲の捕捉中）"
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

        # Button visibility (差分時のみ pack/forget — pitfall #6):
        #   ended            → neither (re-follow is R / N)
        #   waiting-for-start→ start only
        #   running          → end always; start while pre-lock-in so the
        #                      2nd press can still force lock-in
        # The "▶ 演奏開始" button hides after lock-in because it becomes a
        # no-op there; it re-shows if lock-in drops (e.g. reload via R).
        if performance_ended:
            show_start = False
            show_end = False
        elif waiting_for_start:
            show_start = True
            show_end = False
        else:
            show_start = not is_locked
            show_end = True

        # Both pack to the RIGHT of the mode label; relative order is
        # cosmetic (only pre-lock-in shows both at once).
        if show_end and not self._end_button_visible:
            self.button_end.pack(side=tk.RIGHT, padx=(10, 0))
            self._end_button_visible = True
        elif not show_end and self._end_button_visible:
            self.button_end.pack_forget()
            self._end_button_visible = False

        if show_start and not self._button_visible:
            self.button_start.pack(side=tk.RIGHT, padx=(10, 0))
            self._button_visible = True
        elif not show_start and self._button_visible:
            self.button_start.pack_forget()
            self._button_visible = False

    def update_display(self):
        """Update GUI with current state."""
        try:
            state = self.state.get_all()

            # 楽章表示（例: 第1楽章 / 全3楽章）
            mv_num = state.get('movement_number', 1)
            mv_total = state.get('total_movements', 1)
            self.label_movement.config(text=f"第{mv_num}楽章 / 全{mv_total}楽章")

            # 「次の楽章」インジケータ: 次があれば番号を、無ければ最終楽章で
            # N が効かないことを明示する。
            if mv_num < mv_total:
                self.label_next_movement.config(
                    text=f"次の楽章: 第{mv_num + 1}楽章（N キーで移動）", fg="#555"
                )
            else:
                self.label_next_movement.config(
                    text="次の楽章: なし（最終楽章）", fg="#999"
                )

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

            # 確信度（色分け）— 表示は絶対コスト由来の display_confidence を
            # 使う。OLTW 内部の confidence は band 相対値で、無関係な音でも
            # 0.6-0.8 に張り付くため操作者を誤解させる(実測: 無関係なピアノ
            # BGM で内部 conf ~0.4-0.7 / display ~0)。内部値は lock-in・
            # トリガー床の判定用としてそのまま state に残っている。
            conf = state.get('display_confidence', state['confidence'])
            if conf > CONFIDENCE_GOOD_THRESHOLD:
                color = "green"
            elif conf > CONFIDENCE_MID_THRESHOLD:
                color = "orange"
            else:
                color = "red"
            self.label_confidence.config(text=f"{int(conf*100)}%", fg=color)

            # ずれ検知警告 — 差分時のみ pack/forget（落とし穴 #6）
            mismatched = bool(state.get('is_mismatched'))
            if mismatched != self._mismatch_visible:
                self._mismatch_visible = mismatched
                if mismatched:
                    self.label_mismatch.pack(pady=4, before=self.mic_frame)
                else:
                    self.label_mismatch.pack_forget()

            # マイクのノイズ抑制フィルター警告（起動時 one-shot 判定）
            mic_warning = state.get('mic_effects_warning')
            mic_warning_active = bool(mic_warning)
            if mic_warning_active != self._mic_effects_visible:
                self._mic_effects_visible = mic_warning_active
                if mic_warning_active:
                    self.label_mic_effects.config(text=mic_warning)
                    self.label_mic_effects.pack(pady=4, before=self.mic_frame)
                else:
                    self.label_mic_effects.pack_forget()

            # SlideController 起動失敗警告
            slide_warning = state.get('slide_controller_warning')
            slide_warning_active = bool(slide_warning)
            if slide_warning_active != self._slide_warning_visible:
                self._slide_warning_visible = slide_warning_active
                if slide_warning_active:
                    self.label_slide_warning.config(text=slide_warning)
                    self.label_slide_warning.pack(pady=4, before=self.mic_frame)
                else:
                    self.label_slide_warning.pack_forget()

            # 次のトリガー
            next_trig = state['next_trigger_measure']
            if next_trig:
                self.label_next_trigger.config(text=f"次のトリガー: {next_trig} 小節目")
            else:
                self.label_next_trigger.config(text="次のトリガー: --")

            # ----- 追随モード表示の更新 -----
            self._render_follower_mode(state)

            # クールダウン
            if state['cooldown_active']:
                self.label_cooldown.config(text="🔒 クールダウン中")
            else:
                self.label_cooldown.config(text="")

            # マイクレベル（実測 dBFS + 判定閾値を併記）
            mic_available = state.get('mic_monitor_available', False)
            mic_db = state.get('mic_level_db', -120.0)
            gate = state.get('silence_gate_active', False)
            threshold = state.get('silence_threshold_db')
            thr_part = (
                f"（閾値 {threshold:.1f}）" if threshold is not None else ""
            )
            if not mic_available:
                mic_text = "マイク: 監視無効（silence gate 無効）— ログを確認"
                mic_color = "#c60"
            elif gate:
                mic_text = f"マイク: {mic_db:.1f} dBFS{thr_part}  ⚠ 無音（閾値未満）"
                mic_color = "red"
            else:
                mic_text = f"マイク: {mic_db:.1f} dBFS{thr_part}  ✓ 入力検出"
                mic_color = "#2a7"
            self.label_mic_level.config(text=mic_text, fg=mic_color)

            # 閾値 −/＋ ボタンは gate が動くモード（マイク監視あり）でのみ
            # 有効。差分時のみ config（落とし穴 #6）。
            if mic_available != self._thr_buttons_enabled:
                self._thr_buttons_enabled = mic_available
                btn_state = tk.NORMAL if mic_available else tk.DISABLED
                self.button_thr_down.config(state=btn_state)
                self.button_thr_up.config(state=btn_state)

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
