#!/usr/bin/env python3
"""ui/build_window.py - Offline reference-build screen (Issue #24).

Opened from the startup launcher (``ui/launcher.py``) so an operator can
run the offline build (``asf-build``) and generate a matching
``config.json`` entirely inside the GUI — no command line needed.

Design notes
------------
* The build itself runs as a **subprocess**
  (``python -m audio_score_follower.cli.build_reference``), not in-process.
  This mirrors the existing pattern where ``build_reference._synth_score_wav``
  spawns ``tasks/generate_score_wav.py``, keeps the heavy
  numba/librosa/synctoolbox imports out of the launcher Tk process, and
  isolates a build crash from the launcher. Its stdout+stderr are streamed
  line-by-line into a log pane.
* Tk is not thread-safe, so the subprocess reader runs on a daemon thread
  that only pushes lines into a ``queue.Queue``; the Tk side drains the
  queue from a ``after()`` poll.
* The command-assembly and config-generation logic is factored into pure
  functions (``build_command`` / ``generate_config_dict`` / ``write_config``)
  so they can be unit-tested headlessly (see tests/test_build_window.py),
  matching the launch_options.py split.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import subprocess
import sys
import tempfile
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Optional

from audio_score_follower.ui.gui_tkinter import _pick_font_family

logger = logging.getLogger(__name__)

_WINDOW_GEOMETRY = "960x820"

# CLI defaults (audio_score_follower/cli/build_reference.py). A tuning
# field is only appended to the command when it differs from these, to
# keep the invocation clean and let the CLI own the canonical default.
_DEFAULT_SAMPLE_RATE = 22050
_DEFAULT_HOP_LENGTH = 2048
_DEFAULT_CENS_WIN = 41

_BUILD_MODULE = "audio_score_follower.cli.build_reference"

_SCORE_FILETYPES = [
    ("楽譜ファイル", "*.mxl *.xml *.musicxml"),
    ("すべて", "*.*"),
]
_REFERENCE_FILETYPES = [
    ("音声ファイル", "*.wav *.mp3 *.flac *.ogg *.m4a"),
    ("すべて", "*.*"),
]


# ---------------------------------------------------------------- pure logic

def build_command(
    score: Path,
    reference: Path,
    output: Path,
    *,
    python_exe: str = sys.executable,
    score_bpm: Optional[float] = None,
    start_offset: Optional[float] = None,
    end_trim: Optional[float] = None,
    sample_rate: int = _DEFAULT_SAMPLE_RATE,
    hop_length: int = _DEFAULT_HOP_LENGTH,
    cens_win: int = _DEFAULT_CENS_WIN,
    plot: bool = False,
    verbose: bool = False,
) -> list[str]:
    """Assemble the ``asf-build`` subprocess argv.

    Absolute paths are used for --score/--reference/--output so the build
    does not depend on the subprocess CWD (the CLI leaves absolute paths
    unchanged; only bare filenames get the data/ prefix). Optional tuning
    flags are appended only when they deviate from the CLI default:

    * ``score_bpm`` None    → omit (CLI auto-estimates from the reference)
    * ``start_offset`` None → omit (CLI auto-detects head noise by
      comparing against the score synthesis); an explicit 0 IS passed
      (it means "disable trimming")
    * ``end_trim`` None     → omit (CLI auto-detects trailing silence);
      an explicit 0 IS passed (it means "disable trimming")
    """
    cmd = [
        python_exe, "-m", _BUILD_MODULE,
        "--score", str(Path(score).resolve()),
        "--reference", str(Path(reference).resolve()),
        "--output", str(Path(output).resolve()),
    ]
    if score_bpm is not None:
        cmd += ["--score-bpm", str(score_bpm)]
    if start_offset is not None:
        cmd += ["--start-offset", str(start_offset)]
    if end_trim is not None:
        cmd += ["--end-trim", str(end_trim)]
    if sample_rate != _DEFAULT_SAMPLE_RATE:
        cmd += ["--sample-rate", str(sample_rate)]
    if hop_length != _DEFAULT_HOP_LENGTH:
        cmd += ["--hop-length", str(hop_length)]
    if cens_win != _DEFAULT_CENS_WIN:
        cmd += ["--cens-win", str(cens_win)]
    if plot:
        cmd.append("--plot")
    if verbose:
        cmd.append("-v")
    return cmd


def _rel_or_abs(target: Path, base: Path) -> str:
    """Path of ``target`` relative to ``base`` in forward-slash form.

    Falls back to the absolute forward-slash path when the two live on
    different drives (Windows ``os.path.relpath`` raises ValueError). The
    forward-slash convention matches the existing hand-written configs
    (e.g. ``"../data/scores/foo.mxl"``); ConfigLoader resolves either via
    pathlib so both work at runtime.
    """
    target = Path(target).resolve()
    base = Path(base).resolve()
    try:
        rel = os.path.relpath(target, base)
    except ValueError:
        return target.as_posix()
    return Path(rel).as_posix()


def generate_config_dict(score: Path, built_dir: Path, config_dir: Path) -> dict:
    """Build a minimal, ConfigLoader-valid config dict for a fresh build.

    ``xml_file`` / ``built_dir`` are stored relative to ``config_dir`` so
    the generated file matches the project's existing layout. A single
    measure-1 trigger is scaffolded; the operator edits triggers afterwards.
    """
    return {
        "settings": {
            "cooldown_seconds": 3.0,
            "silence_threshold_db": -55.0,
            "mic_device": None,
        },
        "movements": [
            {
                "id": 1,
                "xml_file": _rel_or_abs(score, config_dir),
                "built_dir": _rel_or_abs(built_dir, config_dir),
                "triggers": [
                    {
                        "measure": 1,
                        "action": "right",
                        "note": "開始（トリガーを編集してください）",
                    }
                ],
            }
        ],
    }


def write_config(config_path: Path, config_dict: dict) -> None:
    """Atomically write ``config_dict`` to ``config_path`` (UTF-8, indent 2).

    tempfile + os.replace, ensure_ascii=False — same approach as
    ``launch_options.save_launcher_settings`` so Japanese notes are kept
    and a partial write can never corrupt an existing config.
    """
    config_path = Path(config_path)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        dir=str(config_path.parent), prefix=config_path.name, suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(config_dict, f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.replace(tmp_path, config_path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    logger.info("Generated config written to %s", config_path)


# ------------------------------------------------------------------- window

class BuildWindow:
    """Toplevel offline-build screen. Set ``result`` before destroying.

    ``result`` is the Path of a generated config.json (or None) — the
    launcher uses it to pre-select the fresh config on return.
    """

    def __init__(self, root: tk.Tk, config_dir: Path) -> None:
        self.root = root
        self.config_dir = Path(config_dir).resolve()
        # config/ and data/ are siblings at the project root (existing
        # convention: built_dir is "../data/built/..." relative to config/).
        self.project_root = self.config_dir.parent
        self.data_dir = self.project_root / "data"
        self.result: Optional[Path] = None

        self._proc: Optional[subprocess.Popen] = None
        self._log_queue: "queue.Queue[tuple]" = queue.Queue()
        self._cancelled = False
        self._generated_config: Optional[Path] = None

        self.top = tk.Toplevel(root)
        self.top.title("オフラインビルド作成")
        self.top.geometry(_WINDOW_GEOMETRY)
        family = _pick_font_family(root)
        self._font = (family, 12)
        self._font_small = (family, 10)
        ttk.Style(self.top).configure(".", font=self._font)
        self.top.option_add("*Font", self._font)

        self._build_widgets()
        self.top.protocol("WM_DELETE_WINDOW", self._on_back)

    # -------------------------------------------------------------- widgets
    def _build_widgets(self) -> None:
        pad = {"padx": 10, "pady": 4}
        body = ttk.Frame(self.top, padding=10)
        body.pack(fill="both", expand=True)

        # --- inputs ------------------------------------------------------
        frm_in = ttk.LabelFrame(body, text="入力")
        frm_in.pack(fill="x", **pad)
        frm_in.columnconfigure(1, weight=1)

        ttk.Label(frm_in, text="楽譜 (MusicXML/MXL):").grid(
            row=0, column=0, sticky="w", padx=8, pady=4
        )
        self.var_score = tk.StringVar()
        ttk.Entry(frm_in, textvariable=self.var_score).grid(
            row=0, column=1, sticky="we", padx=8, pady=4
        )
        ttk.Button(frm_in, text="参照…", command=self._browse_score).grid(
            row=0, column=2, padx=8, pady=4
        )
        ttk.Label(
            frm_in,
            text="※リピートを省略した参照録音に合わせる場合は、繰り返し記号を削除した MXL を選んでください",
            foreground="#888", font=self._font_small, wraplength=900, justify="left",
        ).grid(row=1, column=0, columnspan=3, sticky="w", padx=8)

        ttk.Label(frm_in, text="参照録音 (WAV/MP3/…):").grid(
            row=2, column=0, sticky="w", padx=8, pady=4
        )
        self.var_reference = tk.StringVar()
        ttk.Entry(frm_in, textvariable=self.var_reference).grid(
            row=2, column=1, sticky="we", padx=8, pady=4
        )
        ttk.Button(frm_in, text="参照…", command=self._browse_reference).grid(
            row=2, column=2, padx=8, pady=4
        )

        ttk.Label(frm_in, text="出力名:").grid(
            row=3, column=0, sticky="w", padx=8, pady=4
        )
        self.var_output = tk.StringVar()
        self.var_output.trace_add("write", self._on_output_changed)
        ttk.Entry(frm_in, textvariable=self.var_output).grid(
            row=3, column=1, sticky="we", padx=8, pady=4
        )
        ttk.Label(
            frm_in, text=f"→ {self.data_dir / 'built'} 以下に生成",
            foreground="#888", font=self._font_small,
        ).grid(row=4, column=1, sticky="w", padx=8)

        # --- advanced ----------------------------------------------------
        frm_adv = ttk.LabelFrame(body, text="詳細設定（未入力なら自動）")
        frm_adv.pack(fill="x", **pad)
        self.var_bpm = tk.StringVar()
        self.var_start = tk.StringVar()
        self.var_endtrim = tk.StringVar()
        self.var_cens = tk.StringVar(value=str(_DEFAULT_CENS_WIN))
        self.var_hop = tk.StringVar(value=str(_DEFAULT_HOP_LENGTH))
        self.var_sr = tk.StringVar(value=str(_DEFAULT_SAMPLE_RATE))
        self._adv_field(frm_adv, 0, 0, "スコア BPM (空=自動推定):", self.var_bpm)
        self._adv_field(frm_adv, 0, 3, "先頭トリム start-offset (空=自動検出/0=無効):", self.var_start)
        self._adv_field(frm_adv, 1, 0, "末尾トリム end-trim (空=自動/0=無効):", self.var_endtrim)
        self._adv_field(frm_adv, 1, 3, "CENS 窓 cens-win:", self.var_cens)
        self._adv_field(frm_adv, 2, 0, "hop-length:", self.var_hop)
        self._adv_field(frm_adv, 2, 3, "sample-rate:", self.var_sr)
        self.var_plot = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            frm_adv, text="warp_path.png を出力 (--plot)", variable=self.var_plot
        ).grid(row=3, column=0, columnspan=3, sticky="w", padx=8, pady=4)
        self.var_verbose = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            frm_adv, text="詳細ログ (-v)", variable=self.var_verbose
        ).grid(row=3, column=3, columnspan=3, sticky="w", padx=8, pady=4)

        # --- config generation ------------------------------------------
        frm_cfg = ttk.LabelFrame(body, text="config.json の生成")
        frm_cfg.pack(fill="x", **pad)
        frm_cfg.columnconfigure(1, weight=1)
        self.var_gen_config = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            frm_cfg, text="ビルド成功後に対応する config を生成する",
            variable=self.var_gen_config, command=self._on_gen_config_toggled,
        ).grid(row=0, column=0, columnspan=3, sticky="w", padx=8, pady=4)
        ttk.Label(frm_cfg, text="config 名:").grid(
            row=1, column=0, sticky="w", padx=8, pady=4
        )
        self.var_config_name = tk.StringVar()
        self.entry_config_name = ttk.Entry(frm_cfg, textvariable=self.var_config_name)
        self.entry_config_name.grid(row=1, column=1, sticky="we", padx=8, pady=4)
        # Manual typing here stops the output→config-name auto-mirror.
        self.entry_config_name.bind(
            "<KeyRelease>", lambda _e: setattr(self, "_config_name_touched", True)
        )
        ttk.Label(frm_cfg, text=".json").grid(row=1, column=2, sticky="w", padx=(0, 8))

        # --- buttons -----------------------------------------------------
        frm_btn = ttk.Frame(body)
        frm_btn.pack(fill="x", pady=8)
        self.button_build = ttk.Button(
            frm_btn, text="ビルド実行", command=self._on_build
        )
        self.button_build.pack(side="right", padx=10, ipadx=16)
        self.button_back = ttk.Button(frm_btn, text="戻る", command=self._on_back)
        self.button_back.pack(side="right", padx=10)
        self.label_status = ttk.Label(frm_btn, text="", font=self._font_small)
        self.label_status.pack(side="left", padx=10)

        # --- log ---------------------------------------------------------
        frm_log = ttk.LabelFrame(body, text="ログ")
        frm_log.pack(fill="both", expand=True, **pad)
        self.text_log = tk.Text(
            frm_log, height=10, wrap="none", state="disabled",
            font=("Consolas", 10),
        )
        self.text_log.pack(side="left", fill="both", expand=True, padx=(8, 0), pady=8)
        scroll = ttk.Scrollbar(frm_log, command=self.text_log.yview)
        scroll.pack(side="right", fill="y", padx=(0, 8), pady=8)
        self.text_log.configure(yscrollcommand=scroll.set)

    def _adv_field(self, parent, row, col, label, var) -> None:
        ttk.Label(parent, text=label).grid(
            row=row, column=col, sticky="w", padx=8, pady=4
        )
        ttk.Entry(parent, textvariable=var, width=10).grid(
            row=row, column=col + 1, sticky="w", padx=(0, 8), pady=4
        )

    # ----------------------------------------------------------- callbacks
    def _browse_score(self) -> None:
        initial = self.data_dir / "scores"
        chosen = filedialog.askopenfilename(
            parent=self.top, title="楽譜ファイルを選択",
            initialdir=str(initial if initial.exists() else self.project_root),
            filetypes=_SCORE_FILETYPES,
        )
        if chosen:
            self.var_score.set(chosen)

    def _browse_reference(self) -> None:
        initial = self.data_dir / "reference_audio"
        chosen = filedialog.askopenfilename(
            parent=self.top, title="参照録音を選択",
            initialdir=str(initial if initial.exists() else self.project_root),
            filetypes=_REFERENCE_FILETYPES,
        )
        if chosen:
            self.var_reference.set(chosen)

    def _on_output_changed(self, *_args) -> None:
        # Mirror the output name into the config name until the operator
        # edits the config name themselves.
        if not getattr(self, "_config_name_touched", False):
            self.var_config_name.set(self.var_output.get().strip())

    def _on_gen_config_toggled(self) -> None:
        state = "normal" if self.var_gen_config.get() else "disabled"
        self.entry_config_name.configure(state=state)

    # ------------------------------------------------------------- build
    def _resolved_output(self) -> Path:
        return (self.data_dir / "built" / self.var_output.get().strip()).resolve()

    def _collect_command(self) -> Optional[list[str]]:
        """Validate the form and return the subprocess argv, or None."""
        score = self.var_score.get().strip()
        reference = self.var_reference.get().strip()
        output = self.var_output.get().strip()
        if not score or not Path(score).exists():
            messagebox.showerror("入力エラー", "楽譜ファイルを選択してください", parent=self.top)
            return None
        if not reference or not Path(reference).exists():
            messagebox.showerror("入力エラー", "参照録音を選択してください", parent=self.top)
            return None
        if not output:
            messagebox.showerror("入力エラー", "出力名を入力してください", parent=self.top)
            return None

        def _opt_float(var, name):
            raw = var.get().strip()
            if not raw:
                return None
            return float(raw)

        try:
            score_bpm = _opt_float(self.var_bpm, "BPM")
            start_offset = _opt_float(self.var_start, "start-offset")
            end_trim = _opt_float(self.var_endtrim, "end-trim")
            cens_win = int(self.var_cens.get().strip() or _DEFAULT_CENS_WIN)
            hop_length = int(self.var_hop.get().strip() or _DEFAULT_HOP_LENGTH)
            sample_rate = int(self.var_sr.get().strip() or _DEFAULT_SAMPLE_RATE)
        except ValueError:
            messagebox.showerror(
                "入力エラー", "詳細設定の数値が不正です", parent=self.top
            )
            return None

        return build_command(
            Path(score), Path(reference), self._resolved_output(),
            score_bpm=score_bpm, start_offset=start_offset, end_trim=end_trim,
            sample_rate=sample_rate, hop_length=hop_length, cens_win=cens_win,
            plot=bool(self.var_plot.get()), verbose=bool(self.var_verbose.get()),
        )

    def _on_build(self) -> None:
        if self._proc is not None:  # button is in 中止 state
            self._cancel_build()
            return
        cmd = self._collect_command()
        if cmd is None:
            return
        if self.var_gen_config.get():
            name = self.var_config_name.get().strip()
            if not name:
                messagebox.showerror(
                    "入力エラー", "config 名を入力してください", parent=self.top
                )
                return
            config_path = (self.config_dir / f"{name}.json").resolve()
            if config_path.exists() and not messagebox.askyesno(
                "確認",
                f"{config_path.name} は既に存在します。上書きしますか？",
                parent=self.top,
            ):
                return
            self._pending_config_path = config_path
        else:
            self._pending_config_path = None

        self._start_build(cmd)

    def _start_build(self, cmd: list[str]) -> None:
        self._cancelled = False
        self._generated_config = None
        self._clear_log()
        self._append_log("$ " + " ".join(cmd) + "\n")
        env = dict(os.environ)
        env["PYTHONUNBUFFERED"] = "1"
        try:
            self._proc = subprocess.Popen(
                cmd, cwd=str(self.project_root),
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace", env=env,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except OSError as exc:
            self._append_log(f"\n[起動失敗] {exc}\n")
            messagebox.showerror("起動エラー", f"ビルドを開始できません: {exc}", parent=self.top)
            self._proc = None
            return

        self.button_build.configure(text="中止")
        self.button_back.configure(state="disabled")
        self.label_status.configure(text="ビルド中…", foreground="#06c")
        threading.Thread(
            target=self._reader, args=(self._proc,), daemon=True
        ).start()
        self.top.after(100, self._poll_log)

    def _reader(self, proc: subprocess.Popen) -> None:
        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                self._log_queue.put(("line", line.rstrip("\n")))
        finally:
            proc.wait()
            self._log_queue.put(("done", proc.returncode))

    def _poll_log(self) -> None:
        if not self.top.winfo_exists():
            return  # window torn down (戻る during build); drop pending tick
        try:
            while True:
                kind, payload = self._log_queue.get_nowait()
                if kind == "line":
                    self._append_log(payload + "\n")
                elif kind == "done":
                    self._on_build_finished(payload)
                    return
        except queue.Empty:
            pass
        if self._proc is not None:
            self.top.after(100, self._poll_log)

    def _on_build_finished(self, returncode: int) -> None:
        self._proc = None
        self.button_build.configure(text="ビルド実行")
        self.button_back.configure(state="normal")
        if self._cancelled:
            self.label_status.configure(text="中止しました", foreground="#c60")
            self._append_log("\n[中止しました]\n")
            return
        if returncode != 0:
            self.label_status.configure(
                text=f"ビルド失敗 (exit {returncode})", foreground="#c00"
            )
            self._append_log(f"\n[ビルド失敗 exit {returncode}]\n")
            messagebox.showerror(
                "ビルド失敗",
                f"ビルドが失敗しました (exit {returncode})。ログを確認してください。",
                parent=self.top,
            )
            return
        self.label_status.configure(text="ビルド完了", foreground="#080")
        self._append_log("\n[ビルド完了]\n")
        self._maybe_generate_config()

    def _maybe_generate_config(self) -> None:
        config_path = getattr(self, "_pending_config_path", None)
        if config_path is None:
            messagebox.showinfo(
                "完了",
                f"ビルドが完了しました。\n出力: {self._resolved_output()}",
                parent=self.top,
            )
            return
        try:
            cfg = generate_config_dict(
                Path(self.var_score.get().strip()),
                self._resolved_output(),
                self.config_dir,
            )
            write_config(config_path, cfg)
        except OSError as exc:
            self._append_log(f"\n[config 生成失敗] {exc}\n")
            messagebox.showwarning(
                "config 生成失敗",
                f"ビルドは成功しましたが config の生成に失敗しました: {exc}",
                parent=self.top,
            )
            return
        self._generated_config = config_path
        self._append_log(f"[config 生成] {config_path}\n")
        messagebox.showinfo(
            "完了",
            f"ビルドと config 生成が完了しました。\n"
            f"出力: {self._resolved_output()}\n"
            f"config: {config_path}\n\n"
            f"トリガー（小節→操作）は生成された config を編集してください。",
            parent=self.top,
        )

    def _cancel_build(self) -> None:
        if self._proc is None:
            return
        self._cancelled = True
        self.label_status.configure(text="中止中…", foreground="#c60")
        try:
            self._proc.terminate()
        except OSError as exc:
            logger.warning("Failed to terminate build process: %s", exc)

    # -------------------------------------------------------------- log io
    def _clear_log(self) -> None:
        self.text_log.configure(state="normal")
        self.text_log.delete("1.0", "end")
        self.text_log.configure(state="disabled")

    def _append_log(self, text: str) -> None:
        self.text_log.configure(state="normal")
        self.text_log.insert("end", text)
        self.text_log.see("end")
        self.text_log.configure(state="disabled")

    # --------------------------------------------------------------- close
    def _on_back(self) -> None:
        if self._proc is not None:
            if not messagebox.askyesno(
                "確認", "ビルドを中止して戻りますか？", parent=self.top
            ):
                return
            self._cancel_build()
            # Wait for the process to actually exit before tearing down so
            # the reader thread doesn't touch a destroyed widget.
            try:
                self._proc.wait(timeout=5.0)
            except (subprocess.TimeoutExpired, OSError):
                try:
                    self._proc.kill()
                except OSError:
                    pass
            self._proc = None
        self.result = self._generated_config
        self.top.destroy()


def run_build_window(root: tk.Tk, config_dir: Path) -> Optional[Path]:
    """Open the build screen modally over ``root``; return generated config.

    Hides the launcher root while the build screen is up (screen-to-screen
    navigation) and restores it on return. ``root.wait_window`` runs a
    local event loop until the Toplevel is destroyed. Returns the Path of
    a config.json generated during the session, or None.
    """
    window = BuildWindow(root, config_dir)
    root.withdraw()
    try:
        root.wait_window(window.top)
    finally:
        try:
            root.deiconify()
        except tk.TclError:
            pass
    return window.result
