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

オフラインビルド・本番追随の両方が **Windows ネイティブ**で動く (WSL2 不要)。
合成は MuseScore 4 CLI に委ねる。

### 1. 前提

- Python 3.10+
- MuseScore 4 ([公式インストーラ](https://musescore.org/ja/download)、無料)
  - 自動検出パス: `C:\Program Files\MuseScore 4\bin\MuseScore4.exe`
  - 別の場所にある場合は環境変数 `MSCORE_EXE` か `--mscore-exe` で指定

### 2. Python 環境

```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -e ".[dev]"
playwright install chromium
```

**synctoolbox の注意**: 上記 `pip install` で synctoolbox は古い numpy / pandas / music21 を
要求して解決に失敗することがある (1.4.1 時点)。失敗した場合は `--no-deps` で入れ直す:

```powershell
pip install --no-deps synctoolbox libfmp ipython
```

実行時の numpy 2.x 互換問題は `reference_builder.py` 側でモンキーパッチ済み。

### 3. ディレクトリ配置

```
data/
├── scores/<piece>.xml       # MusicXML
├── reference_audio/<piece>.wav  # プロ録音 or リハ録音
└── built/<piece>/           # asf-build の出力
```

### 4. オフラインビルド

```powershell
asf-build `
    --score data/scores/beethoven5.xml `
    --reference data/reference_audio/karajan_1977.wav `
    --output data/built/beethoven5_karajan/ `
    [--start-offset 0.5] `
    [--plot]
```

### 5. ライブ追随

```powershell
asf-follow config/beethoven5.json `
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
└── generate_score_wav.py     # XML → 合成 WAV (MuseScore 4 CLI, Windows ネイティブ)
tests/
data/                         # gitignore
```

## ライセンス

MIT
