#!/usr/bin/env python3
"""
generate_score_wav.py - MusicXML → 合成 WAV (Windows ネイティブ版)

MuseScore 4 CLI を呼んで MusicXML を WAV にレンダリングする。
リファレンスビルド (asf-build) のオフライン工程で使う。

オーケストラ追随用途では合成 WAV は **chroma マッチング** にしか使わないので、
MuseScore 標準音色で十分な品質が得られる。

定テンポ化:
    XML 内の全テンポマーキングを music21 で剥がし、冒頭に単一の
    MetronomeMark(--bpm) を挿入してから MuseScore に渡す。これにより
    accelerando / ritardando や複数の MetronomeMark がある楽曲でも
    本ツールは常に定テンポの合成を出力し、後段の
    ``score_time → beat = score_time * bpm / 60`` という単純換算が成立する。

先頭無音トリム:
    MuseScore は冒頭に短い無音を挟むことがあるので、--top-db 以下の振幅
    部分を librosa.effects.trim で削る。これで ``score_time = 0`` を
    「合成 WAV の最初の音の立ち上がり」に揃え、リファレンス録音側との
    位置合わせを DTW が素直に取れるようにする。

Usage::

    python tasks/generate_score_wav.py \\
        data/scores/beethoven5.xml \\
        -o /tmp/score_synth.wav \\
        --bpm 120

    # MuseScore 実行ファイルを環境変数 or 引数で指定したい場合
    set MSCORE_EXE=C:\\Program Files\\MuseScore 4\\bin\\MuseScore4.exe
    python tasks/generate_score_wav.py score.xml -o out.wav

依存: music21 + librosa + scipy。MuseScore 4 のインストール (公式インストーラ)。
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


_MSCORE_CANDIDATES = [
    Path(r"C:\Program Files\MuseScore 4\bin\MuseScore4.exe"),
    Path(r"C:\Program Files\MuseScore 4\bin\mscore.exe"),
    Path(r"C:\Program Files (x86)\MuseScore 4\bin\MuseScore4.exe"),
    Path(r"C:\Program Files\MuseScore 3\bin\MuseScore3.exe"),
    Path("/usr/bin/mscore"),
    Path("/usr/bin/musescore"),
    Path("/usr/local/bin/mscore"),
    Path("/Applications/MuseScore 4.app/Contents/MacOS/mscore"),
]


def find_mscore(explicit: Optional[Path] = None) -> Path:
    """MuseScore 実行ファイルを発見する。

    優先順:
        1. --mscore-exe で渡されたパス
        2. 環境変数 MSCORE_EXE
        3. PATH 上の MuseScore4 / mscore / MuseScore
        4. 既知のインストール先 (Windows / Linux / macOS)
    """
    if explicit is not None:
        if not explicit.exists():
            raise FileNotFoundError(f"--mscore-exe が存在しません: {explicit}")
        return explicit

    env = os.environ.get("MSCORE_EXE")
    if env:
        p = Path(env)
        if p.exists():
            return p
        logger.warning("MSCORE_EXE=%s が見つからない — 他の候補を探す", env)

    for name in ("MuseScore4", "mscore", "MuseScore", "MuseScore3"):
        which = shutil.which(name)
        if which:
            return Path(which)

    for cand in _MSCORE_CANDIDATES:
        if cand.exists():
            return cand

    raise FileNotFoundError(
        "MuseScore 実行ファイルが見つかりません。\n"
        "  1. MuseScore 4 を https://musescore.org/ja/download からインストール\n"
        "  2. または環境変数 MSCORE_EXE に絶対パスを設定\n"
        "  3. または --mscore-exe フラグで明示\n"
        f"  既知の候補: {[str(c) for c in _MSCORE_CANDIDATES]}"
    )


def preprocess_to_constant_tempo(xml_path: Path, bpm: float) -> Path:
    """XML 内の全テンポマーキングを除去し、冒頭に単一の MetronomeMark を挿入。

    結果は一時 .musicxml ファイルに保存され、そのパスを返す。
    呼び出し側がアンリンクする責任を持つ。
    """
    import music21  # type: ignore — heavy

    logger.info("music21 で XML をロード中: %s", xml_path)
    score = music21.converter.parse(str(xml_path))

    # MetronomeMark / MetricModulation / TempoText を全て除去。
    # ``recurse`` 中に削除すると挙動が壊れるので、まず収集してから削除する。
    targets = []
    for el in score.recurse():
        if isinstance(el, music21.tempo.MetronomeMark):
            targets.append(el)
        elif isinstance(el, music21.tempo.MetricModulation):
            targets.append(el)
        elif isinstance(el, music21.tempo.TempoText):
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

    # 冒頭に単一の MetronomeMark を入れる。
    # パートがあれば最初のパートの最初の measure に、無ければ score 直下に。
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

    tmp_fd, tmp_path = tempfile.mkstemp(suffix=".musicxml")
    os.close(tmp_fd)
    out_path = Path(tmp_path)
    score.write("musicxml", str(out_path))
    logger.info("定テンポ XML 出力: %s", out_path)
    return out_path


def render_with_mscore(
    mscore: Path,
    xml_path: Path,
    out_wav: Path,
    timeout_sec: float = 1200.0,
) -> None:
    """MuseScore CLI で XML を WAV にエクスポート。"""
    cmd = [str(mscore), str(xml_path), "-o", str(out_wav)]
    logger.info("MuseScore 実行: %s", " ".join(cmd))
    completed = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout_sec,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"MuseScore CLI が失敗しました (exit {completed.returncode}):\n"
            f"  stdout: {completed.stdout}\n"
            f"  stderr: {completed.stderr}"
        )
    if not out_wav.exists():
        raise RuntimeError(
            f"MuseScore は exit=0 を返したが出力が無い: {out_wav}\n"
            f"  stdout: {completed.stdout}"
        )
    logger.info("MuseScore 出力: %s (%.1f KB)",
                out_wav, out_wav.stat().st_size / 1024)


def postprocess_audio(
    raw_wav: Path,
    out_wav: Path,
    target_sr: int,
    trim_top_db: float = 60.0,
) -> tuple[float, float]:
    """MuseScore 生 WAV を読み、目的 sr にリサンプル、先頭/末尾の無音を削り、保存。

    Returns:
        (trimmed_seconds_at_head, output_duration_sec)
    """
    import librosa  # type: ignore — heavy
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
        "--mscore-exe", type=Path, default=None,
        help="MuseScore 実行ファイル。省略時は MSCORE_EXE / PATH / 既知パスを探索",
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
        "--mscore-timeout", type=float, default=1200.0,
        help="MuseScore CLI のタイムアウト秒数。大編成スコアは大きくする。default 1200 (20分)",
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
    # librosa/music21 が import 時に numba/matplotlib などを巻き込むと
    # -v 時に大量のバイトコードダンプが流れて読めなくなる。ノイジーな
    # ロガーは常時 INFO 以上に落とす。
    for noisy in ("numba", "matplotlib", "PIL", "fontTools", "music21"):
        logging.getLogger(noisy).setLevel(logging.INFO)

    if not args.score.exists():
        logger.error("スコアファイルが見つかりません: %s", args.score)
        return 1
    args.output.parent.mkdir(parents=True, exist_ok=True)

    try:
        mscore = find_mscore(args.mscore_exe)
        logger.info("MuseScore: %s", mscore)
    except FileNotFoundError as exc:
        logger.error("%s", exc)
        return 1

    temp_xml: Optional[Path] = None
    temp_wav: Optional[Path] = None
    try:
        if args.keep_tempo:
            xml_to_render = args.score
            logger.info("--keep-tempo: XML のテンポを維持して合成")
        else:
            temp_xml = preprocess_to_constant_tempo(args.score, args.bpm)
            xml_to_render = temp_xml

        tmp_fd, tmp_wav_path = tempfile.mkstemp(suffix=".wav")
        os.close(tmp_fd)
        temp_wav = Path(tmp_wav_path)

        render_with_mscore(mscore, xml_to_render, temp_wav, timeout_sec=args.mscore_timeout)

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

    except subprocess.TimeoutExpired:
        logger.error("MuseScore がタイムアウト (%.0f秒超過) — --mscore-timeout でタイムアウトを延ばせます", args.mscore_timeout)
        return 1
    except Exception as exc:
        logger.exception("合成失敗: %s", exc)
        return 1
    finally:
        if temp_xml is not None:
            try:
                temp_xml.unlink()
            except OSError:
                pass
        if temp_wav is not None:
            try:
                temp_wav.unlink()
            except OSError:
                pass

    return 0


if __name__ == "__main__":
    sys.exit(main())
