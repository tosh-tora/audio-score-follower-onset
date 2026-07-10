#!/usr/bin/env python3
"""ui/launcher.py - Startup launcher GUI.

Shown when ``python -m audio_score_follower.main`` (or ``asf-follow``)
is invoked WITHOUT a config argument. Lets the operator pick the config
file and all CLI-equivalent runtime options, persists the selections
back into the chosen config.json (``settings.launcher`` block + the
flat device/tuning keys), then returns a validated LaunchOptions for
main() to construct the app from.

Runs on its own ``tk.Tk()`` root which is destroyed before returning,
so AudioScoreFollowerApp can create its own root afterwards.
"""

from __future__ import annotations

import logging
import math
import os
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Optional

from audio_score_follower.launch_options import (
    INPUT_SOURCE_LOOPBACK,
    INPUT_SOURCE_MIC,
    INPUT_SOURCE_WAV,
    LaunchOptions,
    coerce_device,
    compute_silence_threshold,
    default_config_dir,
    read_launcher_settings,
    rematch_device,
    resolve_input_wav,
    save_launcher_settings,
    validate,
)
from audio_score_follower.core.mic_effects_probe import (
    SOUND_SETTINGS_URI,
    probe_capture_effects,
)
from audio_score_follower.ui.gui_tkinter import _pick_font_family

logger = logging.getLogger(__name__)

_WINDOW_GEOMETRY = "760x780"
_DEFAULT_DEVICE_LABEL = "既定のデバイス"
# Poll interval for the silence-threshold measurement. The monitor's RMS
# block is 1024/16000 ≈ 64ms, so 60ms samples each block roughly once.
_MEASURE_POLL_MS = 60


def list_input_devices() -> list[tuple[int, str, str]]:
    """Enumerate input-capable devices as (index, raw_name, display_label).

    Returns [] on any failure — PortAudio initialisation can be hostile
    (same defensive stance as AudioLevelMonitor); the launcher falls
    back to a manual-entry field in that case.
    """
    try:
        import sounddevice as sd

        hostapis = sd.query_hostapis()
        out = []
        for idx, dev in enumerate(sd.query_devices()):
            if dev.get("max_input_channels", 0) <= 0:
                continue
            api = hostapis[dev["hostapi"]]["name"] if hostapis else "?"
            out.append((idx, dev["name"], f"{idx}: {dev['name']} [{api}]"))
        return out
    except BaseException as exc:  # noqa: BLE001
        logger.warning("Input device enumeration failed: %s", exc)
        return []


def list_output_devices_wasapi() -> list[tuple[int, str, str]]:
    """Enumerate WASAPI output devices (loopback capture sources).

    Loopback capture is a WASAPI-only feature, so other host APIs
    (MME/DirectSound duplicates of the same hardware) are filtered out.
    Returns [] on any failure.
    """
    try:
        import sounddevice as sd

        hostapis = sd.query_hostapis()
        wasapi_ids = {
            i for i, api in enumerate(hostapis) if "WASAPI" in api["name"].upper()
        }
        out = []
        for idx, dev in enumerate(sd.query_devices()):
            if dev.get("max_output_channels", 0) <= 0:
                continue
            if dev["hostapi"] not in wasapi_ids:
                continue
            out.append((idx, dev["name"], f"{idx}: {dev['name']} [WASAPI]"))
        return out
    except BaseException as exc:  # noqa: BLE001
        logger.warning("Output device enumeration failed: %s", exc)
        return []


class _DevicePicker:
    """Combobox over an enumerated device list, or a manual Entry fallback.

    value() returns None (default device), an int index, or a name/index
    string typed into the fallback entry (run through coerce_device).
    """

    def __init__(
        self,
        parent: tk.Widget,
        devices: list[tuple[int, str, str]],
        default_label: str,
    ) -> None:
        self.devices = devices
        self._var = tk.StringVar()
        if devices:
            labels = [default_label] + [label for _, _, label in devices]
            self.widget: tk.Widget = ttk.Combobox(
                parent, textvariable=self._var, values=labels, state="readonly",
                width=52,
            )
            self._var.set(default_label)
            self._fallback = False
        else:
            self.widget = ttk.Entry(parent, textvariable=self._var, width=54)
            self._fallback = True
        self._default_label = default_label

    def value(self):
        if self._fallback:
            text = self._var.get().strip()
            return coerce_device(text) if text else None
        sel = self._var.get()
        if sel == self._default_label:
            return None
        for idx, _, label in self.devices:
            if label == sel:
                return idx
        return None

    def selected_name(self) -> Optional[str]:
        """Raw device name of the current selection (for persistence)."""
        val = self.value()
        if isinstance(val, int):
            for idx, name, _ in self.devices:
                if idx == val:
                    return name
        return None

    def restore(self, stored_device, stored_name: Optional[str]) -> None:
        if self._fallback:
            self._var.set("" if stored_device is None else str(stored_device))
            return
        if isinstance(stored_device, str):
            # Device persisted by name (hand-edited config): match by name.
            stored_name = stored_name or stored_device
            stored_device = None
        idx = rematch_device(
            stored_device, stored_name,
            [(i, n) for i, n, _ in self.devices],
        )
        if idx is None:
            self._var.set(self._default_label)
            return
        for i, _, label in self.devices:
            if i == idx:
                self._var.set(label)
                return
        self._var.set(self._default_label)

    def set_enabled(self, enabled: bool) -> None:
        if self._fallback:
            self.widget.configure(state="normal" if enabled else "disabled")
        else:
            self.widget.configure(state="readonly" if enabled else "disabled")


class _LauncherWindow:
    def __init__(self, root: tk.Tk, config_dir: Path) -> None:
        self.root = root
        self.config_dir = config_dir
        self.result: Optional[LaunchOptions] = None

        root.title("audio-score-follower ランチャー")
        root.geometry(_WINDOW_GEOMETRY)
        family = _pick_font_family(root)
        self._font = (family, 12)
        self._font_small = (family, 10)
        style = ttk.Style(root)
        style.configure(".", font=self._font)
        root.option_add("*Font", self._font)

        self._config_paths: list[Path] = []
        # Silence-threshold measurement state: the monitor is non-None
        # only while a measurement is running.
        self._measure_monitor = None
        self._measure_samples: list[float] = []
        self._build_widgets()
        self._refresh_config_list()
        root.protocol("WM_DELETE_WINDOW", self._on_cancel)

    # ------------------------------------------------------------ widgets
    def _build_widgets(self) -> None:
        pad = {"padx": 10, "pady": 4}
        body = ttk.Frame(self.root, padding=10)
        body.pack(fill="both", expand=True)

        # --- config file -------------------------------------------------
        frm_cfg = ttk.LabelFrame(body, text="設定ファイル (config.json)")
        frm_cfg.pack(fill="x", **pad)
        self.var_config = tk.StringVar()
        self.combo_config = ttk.Combobox(
            frm_cfg, textvariable=self.var_config, state="readonly", width=60
        )
        self.combo_config.pack(side="left", fill="x", expand=True, padx=8, pady=6)
        self.combo_config.bind("<<ComboboxSelected>>", self._on_config_selected)
        ttk.Button(frm_cfg, text="参照…", command=self._on_browse_config).pack(
            side="left", padx=8, pady=6
        )
        self.label_config_error = ttk.Label(body, text="", foreground="#c00")
        self.label_config_error.pack(fill="x", padx=10)

        # --- input source ------------------------------------------------
        frm_src = ttk.LabelFrame(body, text="入力ソース")
        frm_src.pack(fill="x", **pad)
        self.var_source = tk.StringVar(value=INPUT_SOURCE_MIC)

        # mic row
        ttk.Radiobutton(
            frm_src, text="マイク", variable=self.var_source,
            value=INPUT_SOURCE_MIC, command=self._on_source_changed,
        ).grid(row=0, column=0, sticky="w", padx=8, pady=4)
        self.pick_mic = _DevicePicker(frm_src, list_input_devices(), _DEFAULT_DEVICE_LABEL)
        self.pick_mic.widget.grid(row=0, column=1, sticky="we", padx=8, pady=4)

        # loopback row
        ttk.Radiobutton(
            frm_src, text="ループバック (PC出力)", variable=self.var_source,
            value=INPUT_SOURCE_LOOPBACK, command=self._on_source_changed,
        ).grid(row=1, column=0, sticky="w", padx=8, pady=4)
        self.pick_loopback = _DevicePicker(
            frm_src, list_output_devices_wasapi(), "既定の出力デバイス"
        )
        self.pick_loopback.widget.grid(row=1, column=1, sticky="we", padx=8, pady=4)

        # wav row
        ttk.Radiobutton(
            frm_src, text="音源ファイル", variable=self.var_source,
            value=INPUT_SOURCE_WAV, command=self._on_source_changed,
        ).grid(row=2, column=0, sticky="w", padx=8, pady=4)
        frm_wav = ttk.Frame(frm_src)
        frm_wav.grid(row=2, column=1, sticky="we", padx=8, pady=4)
        self.var_wav = tk.StringVar()
        self.entry_wav = ttk.Entry(frm_wav, textvariable=self.var_wav, width=42)
        self.entry_wav.pack(side="left", fill="x", expand=True)
        self.button_wav = ttk.Button(frm_wav, text="参照…", command=self._on_browse_wav)
        self.button_wav.pack(side="left", padx=6)
        self.var_play_audio = tk.BooleanVar(value=False)
        self.check_play_audio = ttk.Checkbutton(
            frm_src, text="同時に再生する (--play-audio)",
            variable=self.var_play_audio,
        )
        self.check_play_audio.grid(row=3, column=1, sticky="w", padx=8, pady=2)
        frm_src.columnconfigure(1, weight=1)

        if not self.pick_mic.devices or not self.pick_loopback.devices:
            ttk.Label(
                frm_src,
                text="デバイス一覧を取得できませんでした — 番号または名前を直接入力してください",
                foreground="#c60", font=self._font_small,
            ).grid(row=4, column=0, columnspan=2, sticky="w", padx=8, pady=2)

        # --- slide url ---------------------------------------------------
        frm_url = ttk.LabelFrame(body, text="スライド URL (--slide-url)")
        frm_url.pack(fill="x", **pad)
        self.var_slide_url = tk.StringVar()
        ttk.Entry(frm_url, textvariable=self.var_slide_url).pack(
            fill="x", padx=8, pady=4
        )
        ttk.Label(
            frm_url, text="空欄 = ドライラン（スライド操作なし）",
            foreground="#888", font=self._font_small,
        ).pack(anchor="w", padx=8, pady=(0, 4))

        # --- tuning / verbose ---------------------------------------------
        frm_adv = ttk.LabelFrame(body, text="詳細設定")
        frm_adv.pack(fill="x", **pad)
        ttk.Label(frm_adv, text="無音判定閾値 silence_threshold_db (dBFS):").grid(
            row=0, column=0, sticky="w", padx=8, pady=4
        )
        self.var_silence = tk.StringVar(value="-55.0")
        ttk.Spinbox(
            frm_adv, textvariable=self.var_silence,
            from_=-120.0, to=0.0, increment=1.0, width=8,
        ).grid(row=0, column=1, sticky="w", padx=8, pady=4)
        self.button_measure = ttk.Button(
            frm_adv, text="無音測定", command=self._on_toggle_measure
        )
        self.button_measure.grid(row=0, column=2, sticky="w", padx=8, pady=4)
        self.label_measure = ttk.Label(
            frm_adv,
            text="無音（暗騒音のみ）の状態でマイクから測定し、閾値を自動設定します",
            foreground="#888", font=self._font_small,
        )
        self.label_measure.grid(
            row=1, column=0, columnspan=3, sticky="w", padx=8
        )
        self.button_check_nc = ttk.Button(
            frm_adv, text="マイクのノイズキャンセル検知", command=self._on_check_nc
        )
        self.button_check_nc.grid(row=2, column=0, sticky="w", padx=8, pady=4)
        self.label_nc = ttk.Label(
            frm_adv, text="", font=self._font_small, wraplength=680, justify="left",
        )
        self.label_nc.grid(row=3, column=0, columnspan=3, sticky="w", padx=8)
        self.button_open_sound_settings = ttk.Button(
            frm_adv, text="サウンド設定を開く", command=self._on_open_sound_settings,
        )
        self._nc_settings_button_visible = False

        ttk.Label(frm_adv, text="トリガ間隔 cooldown_seconds (秒):").grid(
            row=4, column=0, sticky="w", padx=8, pady=4
        )
        self.var_cooldown = tk.StringVar(value="3.0")
        ttk.Spinbox(
            frm_adv, textvariable=self.var_cooldown,
            from_=0.0, to=60.0, increment=0.5, width=8,
        ).grid(row=4, column=1, sticky="w", padx=8, pady=4)
        self.var_verbose = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            frm_adv, text="詳細ログ (-v)", variable=self.var_verbose
        ).grid(row=5, column=0, sticky="w", padx=8, pady=4)
        self.var_viz = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            frm_adv, text="特徴量・確信度モニタを開く (--viz)", variable=self.var_viz
        ).grid(row=6, column=0, columnspan=2, sticky="w", padx=8, pady=4)

        # --- buttons -------------------------------------------------------
        frm_btn = ttk.Frame(body)
        frm_btn.pack(fill="x", pady=12)
        self.button_start = ttk.Button(frm_btn, text="開始", command=self._on_start)
        self.button_start.pack(side="right", padx=10, ipadx=20)
        ttk.Button(frm_btn, text="キャンセル", command=self._on_cancel).pack(
            side="right", padx=10
        )
        ttk.Button(
            frm_btn, text="オフラインビルドを作成…", command=self._on_open_build
        ).pack(side="left", padx=10)

        self._on_source_changed()

    # ------------------------------------------------------------ config list
    def _refresh_config_list(self) -> None:
        # mtime-descending: the launcher rewrites the config on every 開始,
        # so the most recently used config naturally sorts first (manual
        # edits also bump mtime — acceptable).
        try:
            paths = sorted(
                self.config_dir.glob("*.json"),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
        except OSError:
            paths = []
        self._config_paths = paths
        self.combo_config["values"] = [str(p) for p in paths]
        if paths:
            self.var_config.set(str(paths[0]))
            self._on_config_selected()
        else:
            self.label_config_error.configure(
                text=f"{self.config_dir}/ に JSON がありません。参照から選択してください"
            )
            self.button_start.configure(state="disabled")

    def _current_config_path(self) -> Optional[Path]:
        raw = self.var_config.get().strip()
        return Path(raw) if raw else None

    def _on_browse_config(self) -> None:
        initial = str(self.config_dir) if self.config_dir.exists() else "."
        chosen = filedialog.askopenfilename(
            parent=self.root, title="設定ファイルを選択",
            initialdir=initial, filetypes=[("JSON", "*.json"), ("すべて", "*.*")],
        )
        if not chosen:
            return
        values = list(self.combo_config["values"])
        if chosen not in values:
            values.insert(0, chosen)
            self.combo_config["values"] = values
        self.var_config.set(chosen)
        self._on_config_selected()

    def _on_config_selected(self, _event=None) -> None:
        """Repopulate the whole form from the selected config's saved state."""
        path = self._current_config_path()
        if path is None:
            return
        try:
            saved = read_launcher_settings(path)
        except (ValueError, OSError) as exc:
            self.label_config_error.configure(text=f"{path.name}: {exc}")
            self.button_start.configure(state="disabled")
            return
        self.label_config_error.configure(text="")
        self.button_start.configure(state="normal")

        self.var_source.set(saved["input_source"])
        self.var_slide_url.set(saved["slide_url"] or "")
        self.var_wav.set(saved["input_wav"] or "")
        self.var_play_audio.set(bool(saved["play_audio"]))
        self.var_verbose.set(bool(saved["verbose"]))
        self.var_viz.set(bool(saved["viz"]))
        self.var_silence.set(str(saved["silence_threshold_db"]))
        self.var_cooldown.set(str(saved["cooldown_seconds"]))
        self.pick_mic.restore(saved["mic_device"], saved["mic_device_name"])
        self.pick_loopback.restore(
            saved["loopback_device"], saved["loopback_device_name"]
        )
        self._on_source_changed()

    # ------------------------------------------------------------ source state
    def _on_source_changed(self) -> None:
        src = self.var_source.get()
        self.pick_mic.set_enabled(src == INPUT_SOURCE_MIC)
        self.pick_loopback.set_enabled(src == INPUT_SOURCE_LOOPBACK)
        wav_state = "normal" if src == INPUT_SOURCE_WAV else "disabled"
        self.entry_wav.configure(state=wav_state)
        self.button_wav.configure(state=wav_state)
        self.check_play_audio.configure(state=wav_state)
        # 無音測定 stays enabled in every mode: the threshold is persisted
        # and applies whenever mic mode runs later, so the operator may
        # measure the hall while e.g. a wav-mode config is selected.
        # Measurement always captures from the mic picker's device.

    # ------------------------------------------------- silence measurement
    def _on_toggle_measure(self) -> None:
        if self._measure_monitor is None:
            self._start_measure()
        else:
            self._finish_measure()

    def _start_measure(self) -> None:
        # Reuse AudioLevelMonitor — the exact RMS pipeline the production
        # silence gate compares this threshold against, so there is no
        # calibration mismatch between measurement and runtime.
        from audio_score_follower.core.audio_level import AudioLevelMonitor

        monitor = AudioLevelMonitor(device=self.pick_mic.value())
        monitor.start(timeout_sec=5.0)
        if not monitor.is_available():
            monitor.stop()
            self.label_measure.configure(
                text="マイクを開けませんでした — デバイス選択を確認してください",
                foreground="#c00",
            )
            return
        self._measure_monitor = monitor
        self._measure_samples = []
        self.button_measure.configure(text="測定終了")
        self.label_measure.configure(foreground="#555")
        self.root.after(_MEASURE_POLL_MS, self._poll_measure)

    def _poll_measure(self) -> None:
        if self._measure_monitor is None:
            return  # measurement finished/aborted; drop the pending tick
        db = self._measure_monitor.get_level_db()
        if math.isfinite(db):
            self._measure_samples.append(db)
        self.label_measure.configure(
            text=f"無音状態で測定中… 現在 {db:.1f} dBFS "
                 f"(サンプル {len(self._measure_samples)})"
        )
        self.root.after(_MEASURE_POLL_MS, self._poll_measure)

    def _finish_measure(self) -> None:
        monitor = self._measure_monitor
        self._measure_monitor = None
        monitor.stop()
        self.button_measure.configure(text="無音測定")
        try:
            result = compute_silence_threshold(self._measure_samples)
        except ValueError as exc:
            self.label_measure.configure(text=f"測定失敗: {exc}", foreground="#c00")
            return
        self.var_silence.set(f"{result.threshold_db:.1f}")
        self.label_measure.configure(
            text=(
                f"閾値を {result.threshold_db:.1f} dBFS に設定しました "
                f"(中央値 {result.median_db:.1f} / p10 {result.p10_db:.1f} / "
                f"n={result.count})"
            ),
            foreground="#080",
        )
        logger.info(
            "Silence measurement: threshold=%.1f dBFS (median=%.1f p10=%.1f n=%d)",
            result.threshold_db, result.median_db, result.p10_db, result.count,
        )

    def _abort_measure(self) -> None:
        """Stop a running measurement without applying a threshold."""
        if self._measure_monitor is None:
            return
        monitor = self._measure_monitor
        self._measure_monitor = None
        monitor.stop()
        self.button_measure.configure(text="無音測定")
        self.label_measure.configure(text="")

    # ---------------------------------------------- noise-suppression check
    def _on_check_nc(self) -> None:
        """Probe the currently-selected mic for OS-level noise suppression.

        Always reads from the mic picker regardless of the active input
        source, mirroring the 無音測定 button (Issue #19 lesson: source-
        linked disabling confused operators before). The WinRT call
        blocks the UI briefly (~100-300ms observed); acceptable for a
        one-shot pre-flight check.
        """
        self.button_check_nc.configure(state="disabled")
        self.label_nc.configure(text="確認中…", foreground="#555")
        self.root.update_idletasks()
        try:
            report = probe_capture_effects(self.pick_mic.value())
        finally:
            self.button_check_nc.configure(state="normal")

        headline = report.headline_ja()
        if report.has_noise_suppression or report.suspicious_name:
            color = "#c00"
        elif not report.probe_available or not report.device_matched:
            color = "#c60"
        else:
            color = "#080"
        self.label_nc.configure(text=headline, foreground=color)

        show_settings_button = report.has_noise_suppression
        if show_settings_button and not self._nc_settings_button_visible:
            self.button_open_sound_settings.grid(
                row=2, column=1, columnspan=2, sticky="w", padx=8, pady=4
            )
            self._nc_settings_button_visible = True
        elif not show_settings_button and self._nc_settings_button_visible:
            self.button_open_sound_settings.grid_forget()
            self._nc_settings_button_visible = False

    def _on_open_sound_settings(self) -> None:
        try:
            os.startfile(SOUND_SETTINGS_URI)  # noqa: S606 (fixed OS URI, no user input)
        except OSError as exc:
            logger.warning("Failed to open sound settings: %s", exc)
            messagebox.showwarning(
                "起動エラー",
                f"サウンド設定を開けませんでした ({exc})。\n"
                "手動でマイクのプロパティ→「レベル」/「拡張機能」タブを確認してください。",
                parent=self.root,
            )

    def _on_browse_wav(self) -> None:
        initial = str(Path("data") / "reference_audio")
        if not Path(initial).exists():
            initial = "."
        chosen = filedialog.askopenfilename(
            parent=self.root, title="音源ファイルを選択", initialdir=initial,
            filetypes=[
                ("音声ファイル", "*.wav *.mp3 *.flac *.ogg *.m4a"),
                ("すべて", "*.*"),
            ],
        )
        if chosen:
            self.var_wav.set(chosen)

    # ------------------------------------------------------------ start/cancel
    def _build_options(self) -> Optional[LaunchOptions]:
        path = self._current_config_path()
        if path is None:
            messagebox.showerror("入力エラー", "設定ファイルを選択してください", parent=self.root)
            return None
        try:
            silence = float(self.var_silence.get())
            cooldown = float(self.var_cooldown.get())
        except ValueError:
            messagebox.showerror(
                "入力エラー", "詳細設定の数値が不正です", parent=self.root
            )
            return None
        wav_raw = self.var_wav.get().strip()
        src = self.var_source.get()
        return LaunchOptions(
            config_path=path,
            slide_url=self.var_slide_url.get().strip() or None,
            input_source=src,
            input_wav=resolve_input_wav(Path(wav_raw)) if wav_raw else None,
            play_audio=bool(self.var_play_audio.get()) and src == INPUT_SOURCE_WAV,
            mic_device=self.pick_mic.value(),
            loopback_device=self.pick_loopback.value(),
            verbose=bool(self.var_verbose.get()),
            silence_threshold_db=silence,
            cooldown_seconds=cooldown,
            viz=bool(self.var_viz.get()),
        )

    def _on_start(self) -> None:
        # Release the measurement mic stream before the app opens its own.
        self._abort_measure()
        opts = self._build_options()
        if opts is None:
            return
        errors = validate(opts)
        if errors:
            messagebox.showerror("入力エラー", "\n".join(errors), parent=self.root)
            return
        try:
            save_launcher_settings(
                opts.config_path, opts,
                mic_device_name=self.pick_mic.selected_name(),
                loopback_device_name=self.pick_loopback.selected_name(),
            )
        except OSError as exc:
            messagebox.showwarning(
                "保存エラー",
                f"設定の保存に失敗しました ({exc})。\n起動は続行します。",
                parent=self.root,
            )
        self.result = opts
        self.root.destroy()

    def _on_cancel(self) -> None:
        self._abort_measure()
        self.result = None
        self.root.destroy()

    # ------------------------------------------------------------ offline build
    def _on_open_build(self) -> None:
        """Open the offline-build screen; pre-select a config it generates.

        Lazy import keeps the launcher's cold-start free of the build
        screen's module until the operator actually asks for it.
        """
        self._abort_measure()  # release the measurement mic before navigating
        from audio_score_follower.ui.build_window import run_build_window

        generated = run_build_window(self.root, self.config_dir)
        if generated is None:
            return
        self._refresh_config_list()
        path_str = str(generated)
        values = list(self.combo_config["values"])
        if path_str not in values:
            values.insert(0, path_str)
            self.combo_config["values"] = values
        self.var_config.set(path_str)
        self._on_config_selected()


def run_launcher(config_dir: Optional[Path] = None) -> Optional[LaunchOptions]:
    """Show the launcher window (blocking). None = cancelled / closed.

    ``config_dir`` defaults to the project's config/ directory resolved
    independently of the CWD (Issue #7); pass an explicit path to
    override (tests do).

    The Tk root is destroyed before returning so the follower app can
    create its own root afterwards.
    """
    if config_dir is None:
        config_dir = default_config_dir()
    root = tk.Tk()
    try:
        window = _LauncherWindow(root, config_dir)
        root.mainloop()
        return window.result
    finally:
        try:
            root.destroy()
        except tk.TclError:
            pass  # already destroyed by 開始/キャンセル
