#!/usr/bin/env python3
"""
generate_score_wav.py - MusicXML → 合成 WAV (FluidSynth 版)

FluidSynth を使って MusicXML を WAV にレンダリングする。
リファレンスビルド (asf-build) のオフライン工程で使う。

合成エンジン: MuseScore 4 が CLI バッチモードでハングするバグ (v4.6.5 確認)
のため、FluidSynth + MS Basic.sf3 に切り替えた。音質より特徴量 (CENS chroma)
精度が重要で、SF2 で十分。

定テンポ化:
    XML 内の全テンポマーキングを music21 で剥がし、冒頭に単一の
    MetronomeMark(--bpm) を挿入して MIDI エクスポートする。
    FluidSynth はこの MIDI テンポイベントをそのまま使うので
    ``score_time = 0`` が「合成 WAV の最初の音」に揃う。

先頭無音トリム:
    FluidSynth は冒頭に短い無音を挟む場合があるので、
    librosa.effects.trim で削る。

Usage::

    python tasks/generate_score_wav.py \\
        data/scores/beethoven5.xml \\
        -o /tmp/score_synth.wav \\
        --bpm 120

    # FluidSynth / SF ファイルを明示する場合
    set FLUIDSYNTH_EXE=C:\\path\\to\\fluidsynth.exe
    set SF_FILE=C:\\path\\to\\soundfont.sf3

依存: music21 + librosa + scipy。FluidSynth のインストール。
"""

from __future__ import annotations

import argparse
import logging
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


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


def find_fluidsynth(explicit: Optional[Path] = None) -> Path:
    """FluidSynth 実行ファイルを発見する。

    優先順:
        1. explicit 引数 (--fluidsynth-exe)
        2. 環境変数 FLUIDSYNTH_EXE
        3. PATH 上の fluidsynth
        4. 既知のインストール先
    """
    if explicit is not None:
        if not explicit.exists():
            raise FileNotFoundError(f"--fluidsynth-exe が存在しません: {explicit}")
        return explicit

    # Project-local vendor directory (most reliable — no sandbox/virtualization issues)
    repo_root = Path(__file__).resolve().parents[1]
    vendor_dir = repo_root / "vendor" / "FluidSynth"
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

    # Search LocalAppData via multiple methods — env vars can be unreliable.
    local_appdata_candidates: list[Path] = []
    local_appdata_candidates.append(Path.home() / "AppData" / "Local")
    userprofile = os.environ.get("USERPROFILE", "")
    if userprofile:
        local_appdata_candidates.append(Path(userprofile) / "AppData" / "Local")
    for parent in Path(sys.executable).resolve().parents:
        if parent.name.lower() == "local" and parent.parent.name.lower() == "appdata":
            local_appdata_candidates.append(parent)
            break
    for local_appdata in local_appdata_candidates:
        for p in sorted((local_appdata / "FluidSynth").glob("*/bin/fluidsynth.exe")):
            if p.exists():
                return p

    for cand in _FLUIDSYNTH_FIXED_CANDIDATES:
        if cand.exists():
            return cand

    raise FileNotFoundError(
        "FluidSynth 実行ファイルが見つかりません。\n"
        "  1. https://github.com/FluidSynth/fluidsynth/releases から Windows バイナリを取得\n"
        "  2. または環境変数 FLUIDSYNTH_EXE に絶対パスを設定\n"
        "  3. または --fluidsynth-exe フラグで明示"
    )


def find_sf_file(explicit: Optional[Path] = None) -> Path:
    """SF2/SF3 サウンドフォントファイルを発見する。

    優先順:
        1. explicit 引数 (--sf-file)
        2. 環境変数 SF_FILE
        3. 既知のインストール先 (MuseScore 4 付属 MS Basic.sf3 等)
    """
    if explicit is not None:
        if not explicit.exists():
            raise FileNotFoundError(f"--sf-file が存在しません: {explicit}")
        return explicit

    env = os.environ.get("SF_FILE")
    if env:
        p = Path(env)
        if p.exists():
            return p
        logger.warning("SF_FILE=%s が見つからない — 他の候補を探す", env)

    for cand in _SF_CANDIDATES:
        if cand.exists():
            return cand

    raise FileNotFoundError(
        "SF2/SF3 サウンドフォントが見つかりません。\n"
        "  1. MuseScore 4 をインストールすると MS Basic.sf3 が付属する\n"
        "  2. または環境変数 SF_FILE に絶対パスを設定\n"
        "  3. または --sf-file フラグで明示"
    )


def preprocess_to_constant_tempo(xml_path: Path, bpm: float):  # -> music21.stream.Score
    """XML を music21 でパースし、全テンポマーキングを除去して単一 BPM に固定した Score を返す。"""
    import music21  # type: ignore

    logger.info("music21 で XML をロード中: %s", xml_path)
    score = music21.converter.parse(str(xml_path))

    targets = []
    for el in score.recurse():
        if isinstance(el, (music21.tempo.MetronomeMark,
                           music21.tempo.MetricModulation,
                           music21.tempo.TempoText)):
            targets.append(el)
    removed = 0
    for el in targets:
        site = el.activeSite
        if site is not None:
            try:
                site.remove(el)
                removed += 1
            except Exception as exc:  # noqa: BLE001
                logger.debug("除去できなかったテンポ要素: %r (%s)", el, exc)
    logger.info("除去したテンポ要素: %d 個", removed)

    new_mm = music21.tempo.MetronomeMark(
        number=bpm, referent=music21.duration.Duration(1.0)
    )
    inserted = False
    if hasattr(score, "parts") and len(score.parts) > 0:
        first_part = score.parts[0]
        measures = list(first_part.getElementsByClass(music21.stream.Measure))
        if measures:
            measures[0].insert(0, new_mm)
            inserted = True
    if not inserted:
        score.insert(0, new_mm)
    logger.info("冒頭に %.1f BPM の MetronomeMark を挿入", bpm)

    return score


def export_midi(score, midi_path: Path) -> None:
    """music21 Score を MIDI ファイルとして書き出す。"""
    score.write("midi", str(midi_path))
    logger.info("MIDI 出力: %s (%.1f KB)", midi_path, midi_path.stat().st_size / 1024)


def render_with_fluidsynth(
    fluidsynth: Path,
    sf_path: Path,
    midi_path: Path,
    out_wav: Path,
    sample_rate: int = 44100,
) -> None:
    """FluidSynth CLI で MIDI を WAV に変換する。

    -n: no MIDI input  -i: no interactive shell  -F: render to file
    """
    cmd = [
        str(fluidsynth),
        "-ni",
        "-F", str(out_wav),
        "-r", str(sample_rate),
        str(sf_path),
        str(midi_path),
    ]
    logger.info("FluidSynth 実行: %s", " ".join(cmd))
    completed = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=120,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"FluidSynth が失敗しました (exit {completed.returncode}):\n"
            f"  stdout: {completed.stdout}\n"
            f"  stderr: {completed.stderr}"
        )
    if not out_wav.exists():
        raise RuntimeError(
            f"FluidSynth は exit=0 を返したが出力が無い: {out_wav}\n"
            f"  stdout: {completed.stdout}"
        )
    logger.info("FluidSynth 出力: %s (%.1f KB)", out_wav, out_wav.stat().st_size / 1024)


def postprocess_audio(
    raw_wav: Path,
    out_wav: Path,
    target_sr: int,
    trim_top_db: float = 60.0,
) -> tuple[float, float]:
    """生 WAV を目的 sr にリサンプル、先頭/末尾の無音を削り、保存。

    Returns:
        (trimmed_seconds_at_head, output_duration_sec)
    """
    import librosa  # type: ignore
    import numpy as np
    from scipy.io import wavfile  # type: ignore

    logger.info("librosa で読込: %s @ %d Hz", raw_wav, target_sr)
    audio, sr = librosa.load(str(raw_wav), sr=target_sr, mono=True)
    orig_dur = len(audio) / target_sr
    logger.info("ロード: %d samples (%.2f s @ %d Hz)", len(audio), orig_dur, sr)

    audio_trimmed, intervals = librosa.effects.trim(audio, top_db=trim_top_db)
    head_trim_sec = float(intervals[0]) / target_sr if intervals is not None else 0.0
    logger.info(
        "トリム後: %d samples (%.2f s, 冒頭 %.3fs 削減)",
        len(audio_trimmed), len(audio_trimmed) / target_sr, head_trim_sec,
    )

    peak = float(np.abs(audio_trimmed).max() or 1.0)
    normalized = (audio_trimmed / peak * 0.9 * 32767).astype(np.int16)
    wavfile.write(str(out_wav), target_sr, normalized)
    logger.info(
        "書込: %s (%.1f KB, %.2f s)",
        out_wav, out_wav.stat().st_size / 1024,
        len(audio_trimmed) / target_sr,
    )
    return head_trim_sec, len(audio_trimmed) / target_sr


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("score", type=Path, help="MusicXML / MXL ファイル")
    parser.add_argument(
        "-o", "--output", type=Path, default=Path("score_synth.wav"),
        help="出力 WAV パス (default: score_synth.wav)",
    )
    parser.add_argument(
        "--bpm", type=float, default=120.0,
        help="定テンポ BPM (4分音符基準)。default 120",
    )
    parser.add_argument(
        "--samplerate", type=int, default=22050,
        help="出力サンプルレート。default 22050 (CENS デフォルト)",
    )
    parser.add_argument(
        "--fluidsynth-exe", type=Path, default=None,
        help="FluidSynth 実行ファイル。省略時は FLUIDSYNTH_EXE / PATH / 既知パスを探索",
    )
    parser.add_argument(
        "--sf-file", type=Path, default=None,
        help="SF2/SF3 サウンドフォント。省略時は SF_FILE 環境変数 / MuseScore 4 付属 MS Basic.sf3 を探索",
    )
    parser.add_argument(
        "--trim-top-db", type=float, default=60.0,
        help="冒頭末尾無音トリムの dBFS 閾値。default 60",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true",
        help="DEBUG レベルログ",
    )
    parser.add_argument(
        "--keep-tempo", action="store_true",
        help="XML のテンポを保持し、定テンポ正規化を行わない (デバッグ用)",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    for noisy in ("numba", "matplotlib", "PIL", "fontTools", "music21"):
        logging.getLogger(noisy).setLevel(logging.INFO)

    if not args.score.exists():
        logger.error("スコアファイルが見つかりません: %s", args.score)
        return 1
    args.output.parent.mkdir(parents=True, exist_ok=True)

    try:
        fluidsynth = find_fluidsynth(args.fluidsynth_exe)
        logger.info("FluidSynth: %s", fluidsynth)
    except FileNotFoundError as exc:
        logger.error("%s", exc)
        return 1

    try:
        sf_file = find_sf_file(args.sf_file)
        logger.info("SoundFont: %s", sf_file)
    except FileNotFoundError as exc:
        logger.error("%s", exc)
        return 1

    temp_midi: Optional[Path] = None
    temp_wav: Optional[Path] = None
    try:
        import music21  # type: ignore

        if args.keep_tempo:
            logger.info("--keep-tempo: XML のテンポを維持して合成")
            score = music21.converter.parse(str(args.score))
        else:
            score = preprocess_to_constant_tempo(args.score, args.bpm)

        tmp_fd, tmp_midi_path = tempfile.mkstemp(suffix=".mid")
        os.close(tmp_fd)
        temp_midi = Path(tmp_midi_path)
        export_midi(score, temp_midi)

        tmp_fd2, tmp_wav_path = tempfile.mkstemp(suffix=".wav")
        os.close(tmp_fd2)
        temp_wav = Path(tmp_wav_path)

        render_with_fluidsynth(fluidsynth, sf_file, temp_midi, temp_wav)

        head_trim, out_dur = postprocess_audio(
            temp_wav, args.output, args.samplerate, args.trim_top_db
        )

        print()
        print(f"完了: {args.output}")
        print(f"  サイズ: {args.output.stat().st_size / 1024:.1f} KB")
        print(f"  長さ:   {out_dur:.2f} s @ {args.samplerate} Hz")
        print(f"  冒頭トリム: {head_trim*1000:.0f} ms")
        if not args.keep_tempo:
            print(f"  定テンポ: {args.bpm} BPM")

    except Exception as exc:
        logger.exception("合成失敗: %s", exc)
        return 1
    finally:
        for p in (temp_midi, temp_wav):
            if p is not None:
                try:
                    p.unlink()
                except OSError:
                    pass

    return 0


if __name__ == "__main__":
    sys.exit(main())
