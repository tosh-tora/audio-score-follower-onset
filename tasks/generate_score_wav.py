#!/usr/bin/env python3
"""
ガイドスコアから pymatchmaker と同じ方式で合成 WAV を作る診断ツール。

スコア自身を再生してマイクから入れることで「ガイド vs 実演奏」の
クロマ不一致を排除し、pymatchmaker DTW 自体の動作品質を切り分ける。

Usage (WSL2):
    python tasks/generate_score_wav.py \\
        /mnt/c/Users/toshi/projects/live-score-sync/work/outbox/運命_冒頭_guide.mxl \\
        -o /tmp/score_synth.wav \\
        --bpm 120

その後:
    1. 出力 WAV を Windows のスピーカーで再生 (PowerShell 等経由でも、
       VLC / Windows Media Player でも可)
    2. 再生中に別端末で本アプリを起動:
         SLF_BEAT_LOG=/tmp/score_self_match.csv \\
         python -m sequential_live_follower.main config.json \\
           --slide-url <URL> -v
    3. 60 秒程度回したら GUI を閉じる
    4. CSV をユーザー側で Claude に共有

期待される結果 (DTW が正常なら):
    raw_beat は時間と線形に上昇 (bpm/60 beats/sec の傾き、120 BPM なら 2 b/s)
    stall 比率は低い (<50%)、瞬間ジャンプは少ない

依存: partitura + fluidsynth (INSTALLATION_JP.md でセットアップ済の想定)
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np

try:
    from scipy.io import wavfile
except ImportError:
    print("scipy が必要です: pip install scipy", file=sys.stderr)
    sys.exit(1)

try:
    import partitura
except ImportError:
    print("partitura が必要です (WSL2 venv で実行してください)", file=sys.stderr)
    sys.exit(1)

try:
    # pymatchmaker 内部関数 — pymatchmaker と同じ合成を再現するため借用
    from matchmaker.utils.misc import generate_score_audio
except ImportError:
    print(
        "matchmaker.utils.misc.generate_score_audio が見つかりません。"
        "pip install pymatchmaker が WSL2 venv で済んでいるか確認してください。",
        file=sys.stderr,
    )
    sys.exit(1)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("score", help="MusicXML / MXL ファイルへのパス")
    parser.add_argument(
        "-o", "--output", default="score_synth.wav",
        help="出力 WAV のパス (デフォルト: score_synth.wav)",
    )
    parser.add_argument(
        "--bpm", type=float, default=120.0,
        help="合成 BPM (4分音符 BPM)。デフォルト 120",
    )
    parser.add_argument(
        "--samplerate", type=int, default=22050,
        help="サンプルレート (デフォルト 22050、pymatchmaker のデフォルトと一致)",
    )
    args = parser.parse_args()

    score_path = Path(args.score)
    if not score_path.exists():
        print(f"スコアファイルが見つかりません: {score_path}", file=sys.stderr)
        return 1

    print(f"Loading score: {score_path}")
    loaded = partitura.load_score(str(score_path))

    # pymatchmaker と同じく merge_parts で 1 つにまとめる
    from partitura.score import merge_parts
    parts = list(loaded.parts) if hasattr(loaded, "parts") else [loaded]
    score = merge_parts(parts)
    print(f"Score loaded: {score}")

    print(f"Synthesizing audio at {args.bpm} BPM, samplerate={args.samplerate}...")
    audio = generate_score_audio(score, bpm=args.bpm, samplerate=args.samplerate)
    print(f"Generated {len(audio)} samples ({len(audio) / args.samplerate:.2f} s)")

    # 振幅正規化 (clipping 回避) して int16 WAV に
    audio_arr = np.asarray(audio, dtype=np.float32)
    peak = float(np.abs(audio_arr).max() or 1.0)
    normalized = (audio_arr / peak * 0.9 * 32767).astype(np.int16)

    out_path = Path(args.output)
    wavfile.write(str(out_path), args.samplerate, normalized)
    print(f"Wrote WAV: {out_path} ({out_path.stat().st_size / 1024:.1f} KB)")
    print()
    print("次の手順:")
    print(f"  1. {out_path} をスピーカーで再生 (音量 注意)")
    print("  2. 別端末でアプリ起動:")
    print(
        "     SLF_BEAT_LOG=/tmp/score_self_match.csv "
        "python -m sequential_live_follower.main config.json "
        "--slide-url <URL> -v"
    )
    print("  3. 60 秒経ったら GUI を閉じる")
    print("  4. /tmp/score_self_match.csv を共有")
    return 0


if __name__ == "__main__":
    sys.exit(main())
