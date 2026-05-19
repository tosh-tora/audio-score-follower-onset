# audio-score-follower

オーケストラ向けライブ追随アプリ。
スコア合成 WAV と実演奏録音 (プロ録音 / リハ録音) を **オフラインで対応付け**、
本番ではマイク入力を **同じリファレンス録音に Online DTW で追随** させる。

姉妹プロジェクト [live-score-sync](../live-score-sync) は pymatchmaker (note-based matcher) を
使っていたが、オーケストラの密音響で破綻する弱点があった。本プロジェクトは audio-to-audio で
全てを行うことでその問題を回避する。

## アーキテクチャ

```
オフライン (asf-build):
  MusicXML ─(synth)─▶ score_synth.wav
                            │
                            │ MrMsDTW (synctoolbox)
                            ▼
  reference.wav ◀───────────┘
                            │
                            ▼
                   data/built/<name>/
                     ├─ warping_path.npz   (score_time ↔ reference_time)
                     └─ reference_cens.npy (本番 OLTW 用 CENS)

オンライン (asf-follow):
  マイク ─▶ CENS ─▶ Online DTW ─▶ reference_time
                                       │
                                       ▼ warp 逆引き
                                  score_time → 小節 → Slides
```

## セットアップ

### 1. 環境

```bash
# Windows 本番 (オンライン追随用)
python -m venv .venv
.venv\Scripts\activate
pip install -e .
playwright install chromium

# WSL2 (オフラインビルド用 — pymatchmaker の合成エンジン依存)
python -m venv .venv-wsl
source .venv-wsl/bin/activate
pip install -e ".[synth,dev]"
```

### 2. ディレクトリ配置

```
data/
├── scores/<piece>.xml       # MusicXML
├── reference_audio/<piece>.wav  # プロ録音 or リハ録音
└── built/<piece>/           # asf-build の出力
```

### 3. オフラインビルド (WSL2)

```bash
asf-build \
    --score data/scores/beethoven5.xml \
    --reference data/reference_audio/karajan_1977.wav \
    --output data/built/beethoven5_karajan/ \
    [--start-offset 0.5] \
    [--plot]
```

### 4. ライブ追随 (Windows)

```bash
asf-follow config/beethoven5.json \
    --built data/built/beethoven5_karajan/ \
    --slide-url "https://docs.google.com/presentation/d/<ID>/present"
```

## config.json スキーマ

姉妹プロジェクトと同じ形式 + `built` フィールド。

```json
{
  "settings": {
    "cooldown_seconds": 3.0,
    "silence_threshold_db": -55.0
  },
  "movements": [
    {
      "id": 1,
      "xml_file": "beethoven5.xml",
      "built_dir": "../data/built/beethoven5_karajan",
      "triggers": [
        {"measure": 1, "action": "right", "note": "開始"},
        {"measure": 17, "action": "right", "note": "第二主題"}
      ]
    }
  ]
}
```

## プロジェクト構成

```
audio_score_follower/
├── core/
│   ├── score_mapper.py       # 流用: 拍 ↔ 小節 (アウフタクト対応)
│   ├── slide_controller.py   # 流用: Playwright Slides
│   ├── state_manager.py      # 流用: 状態管理
│   ├── audio_level.py        # 流用: silence gate
│   ├── cooldown_timer.py     # 流用: クールダウン
│   ├── feature_extractor.py  # 新規: CENS 特徴抽出 (librosa)
│   ├── reference_builder.py  # 新規: オフライン MrMsDTW
│   ├── oltw_follower.py      # 新規: Online DTW 本体
│   └── warp_lookup.py        # 新規: reference_time → score_time
├── ui/                       # 流用: Tkinter GUI シェル
├── config/
├── cli/
│   ├── build_reference.py    # 新規: オフラインビルド CLI
│   └── follow.py             # 新規: ライブ追随 CLI
└── main.py                   # 新規: GUI エントリ
tasks/
└── generate_score_wav.py     # 流用: XML → 合成 WAV (WSL2)
tests/
data/                         # gitignore
```

## ライセンス

MIT
