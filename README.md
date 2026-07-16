# audio-score-follower

オーケストラ向けライブ演奏追随アプリ。
スコアから合成した WAV と実演奏録音（プロ録音 / リハ録音）を**オフラインで対応付け**、
本番ではマイク入力を**同じリファレンス録音に Online DTW で追随**させる。
指定の小節に到達すると Google Slides のページを操作して解説等を表示する。

姉妹プロジェクト [live-score-sync](../live-score-sync) の note-based matcher（pymatchmaker）が
オーケストラの密音響で破綻した反省から、本プロジェクトは audio-to-audio で全てを行う。
内部設計・チューニング根拠・開発上の制約は [CLAUDE.md](CLAUDE.md) にまとめてある。

## 特徴

1. **Audio-to-Audio（CENS + onset 融合マッチング）** — 密音響から単音ピッチを抽出せず、
   演奏音を丸ごと **CENS**（クロマのエネルギー分布）として捉えるため、楽器の休み・音量差・
   残響に頑健。さらに **spectral-flux onset** を融合した 2 特徴量距離
   （`chroma_weight × cosine距離 + onset_weight × |onset差|`、既定 0.7/0.3）で、
   chroma が共通になる繰り返しテーマ・平行調の曖昧さを attack パターンの違いで解消する。
   係数は `settings.feature_fusion` で調整可。`reference_onset.npy` のない旧ビルドでは
   自動で CENS 単独にフォールバックする。
2. **二段階マッピング** — オフラインで楽譜 ↔ リファレンス録音を MrMsDTW（synctoolbox）で
   高精度に対応付けて `warping_path.npz` に保存し、本番はマイク ↔ リファレンス録音の
   Online DTW に専念する。リファレンスが「実演奏の音響」なので特徴空間の SN 比が高く、
   本番 PC の計算負荷もレイテンシも小さい。warp 逆引きで reference_time → score_time →
   小節に変換する。
3. **エッジケースへの構造的対策** — 合成 BPM の自動推定・先頭雑音／末尾無音の自動トリム・
   ビルド時 warp path 検証（→「4. オフラインビルド」）、lock-in 二段構えと DP 失敗モードへの
   個別対処（→「OLTW 設計メモ」）、ずれの自動検知（→「ライブ操作」）、ホール本番の運用フロー
   （→「本番ホール運用ガイド」）。

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
                     ├─ warping_path.npz    (score_time ↔ reference_time)
                     ├─ reference_cens.npy  (本番 OLTW 用 CENS)
                     └─ reference_onset.npy (本番 OLTW 用オンセット強度)

オンライン (asf-follow):
  マイク or WAV ─▶ CENS + onset ─▶ Online DTW (fused distance) ─▶ reference_time
                                                                        │
                                                                        ▼ warp 逆引き
                                                                  score_time → 小節 → Slides
```

## セットアップ

オフラインビルド・本番追随の両方が **Windows ネイティブ**で動く（WSL2 不要）。

### 1. 前提

- Python 3.10+
- **FluidSynth**（スコア合成 WAV のレンダリングに使用）
  - [GitHub releases](https://github.com/FluidSynth/fluidsynth/releases) から
    `fluidsynth-vX.Y.Z-win10-x64-glib.zip` を取得し、プロジェクトの `vendor/FluidSynth/` に
    展開する（推奨。`vendor/` は gitignore 済み。LocalAppData 配置は Windows Defender に
    削除される実例があるため非推奨）
  - 別の場所に置く場合は環境変数 `FLUIDSYNTH_EXE` か `--fluidsynth-exe` で指定
- **SoundFont**: MuseScore 4 をインストールすると付属する `MS Basic.sf3` が自動検出される。
  別の SF2/SF3 を使う場合は環境変数 `SF_FILE` で指定

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
├── scores/<piece>.xml          # MusicXML
├── reference_audio/<piece>.mp3 # リファレンス録音 (WAV / MP3)
└── built/<piece>/              # asf-build の出力 (gitignore 対象)
```

**リファレンス録音の選び方（重要）**

本番のホール演奏が DTW で照合される相手はこのリファレンス録音であり、両者の音響的・解釈的な差が小さいほど追従精度が上がる。優先度は以下の通り：

1. **当日ゲネプロ録音（最推奨）** — 同じホール・同じ指揮者・同じテンポ感・同じマイク経路。ゲネプロ終了 → 数十分で `asf-build` → 本番に投入、というフローが理想。
2. **直近リハーサル録音** — 指揮者とテンポは同じだが、ホール残響が違う。
3. **プロ録音 / 過去公演** — 最後の手段。テンポ解釈と残響の両方が違うため `oltw_kwargs` のチューニング (特に `search_width` を広めに) が必要になりやすい。

ジャケット表記の演奏時間ではなく、**実際に追従させる演奏に最も近いテイク**を選ぶこと。

**スコアと録音の繰り返し構造は一致していること（必須）**: 参照録音が繰り返しを省略している
場合は、スコアから繰り返し記号を削除した MXL を別ファイルとして用意し、それをビルドと
`config.json` の `xml_file` の両方に使う。不一致はビルド時検証が検出する。

### 4. オフラインビルド

```powershell
# ファイル名のみ指定（推奨）
# data/scores/<score>、data/reference_audio/<ref>、data/built/<output> に自動解決される
asf-build `
    --score beethoven5.xml `
    --reference karajan_1977.mp3 `
    --output beethoven5_karajan

# フルパスも引き続き使用可（後方互換）
asf-build `
    --score data/scores/beethoven5.xml `
    --reference data/reference_audio/karajan_1977.mp3 `
    --output data/built/beethoven5_karajan
```

**ファイル名ショートハンド**: `--score`・`--reference`・`--output` にディレクトリを含まない
名前を渡すと、以下のデフォルトパスへ自動解決される（ディレクトリを含むパスや絶対パスはそのまま）：

| 引数 | ファイル名のみの場合の解決先 |
|---|---|
| `--score` | `data/scores/<filename>` |
| `--reference` | `data/reference_audio/<filename>` |
| `--output` | `data/built/<name>` |

**全オプション一覧**：

| オプション | 必須 | 既定 | 説明 |
|---|---|---|---|
| `--score` | ○ | — | 楽譜ファイル（MusicXML / MXL）。参照録音と繰り返し構造が一致していること。 |
| `--reference` | ○ | — | リファレンス録音（WAV / FLAC / OGG / MP3 / M4A）。本番でライブ入力が照合される相手。 |
| `--output` | ○ | — | ビルド成果物の出力ディレクトリ。 |
| `--score-wav` | | なし（自動合成） | 事前合成済みのスコア WAV。省略時は `tasks/generate_score_wav.py` が自動で合成する。指定する場合は合成テンポを録音から逆算できないため **`--score-bpm` の明示指定が必須**。 |
| `--score-bpm` | | 自動推定 | スコア合成に使う四分音符 BPM（下記「合成 BPM の自動推定」）。 |
| `--start-offset` | | 自動検出 | 参照録音の先頭からトリムする秒数。`0` で無効（下記「先頭雑音の自動トリム」）。 |
| `--end-trim` | | 自動検出 | 参照録音の末尾からトリムする秒数。`0` で無効（下記「末尾無音の自動トリム」）。 |
| `--sample-rate` | | `22050` | 特徴抽出のサンプリングレート（Hz）。chroma には十分な帯域の MIR 標準値。ビルド成果物に永続化されランタイムに自動伝播。通常変更不要。 |
| `--hop-length` | | `2048` | 特徴抽出のフレーム間隔（サンプル数、≈93ms/フレーム）。同上の自動伝播。通常変更不要（下記）。 |
| `--cens-win` | | `41` | CENS 平滑窓（フレーム数、≈3.8 秒）。同上の自動伝播。通常変更不要（下記）。 |
| `--plot` | | off | アラインメント結果 `warp_path.png` を出力ディレクトリに保存（matplotlib が必要）。 |
| `-v` / `--verbose` | | off | DEBUG レベルのログを出力。 |

**合成 BPM の自動推定**: `--score-bpm` 省略時は `total_beats × 60 / ref_duration` で四分音符
BPM を逆算する（例: 幻想4 = 712 ビート / 281s → BPM 152）。楽譜の指示テンポ（120 等）で固定
合成すると実演奏との 20〜30% のテンポ差で warp path にスキップが大量発生し、ビルド時検証
（slope 上限 4.0×）で失敗するため。推定値は `build_meta.json` の `score_bpm` に永続化される。

**末尾無音の自動トリム**: 参照録音末尾の無音・拍手（ピーク −45dB 以下が 1.5 秒超）を自動検出
してビルド前にカットする。トリムしないと BPM 推定の分母が水増しされ、かつスコアの最終小節群が
無音尾部にマップされて**どの入力でも最後の数小節に到達できなくなる**。
「追随は正常なのに最終小節の数小節手前で頭打ちになる」症状はまずこれを疑う。
`--end-trim <秒>` で手動指定、`0` で無効。トリム量は `build_meta.json` の
`reference_end_trim_sec` に記録される。

**先頭雑音の自動トリム**: 指揮者のブレス・チューニングA音など「鳴っているが楽譜にない」先頭
雑音はエネルギーでは検出できないため、**スコア合成の冒頭と録音の冒頭を比較**して曲の開始位置を
検出しトリムする（前提: 再生開始から数秒以内に演奏が始まること）。比較の落差が弱い場合は
トリムしない安全側の設計（冒頭小節を切り込むより雑音を残す方を選ぶ）。検出結果に違和感が
あれば `--start-offset <秒>` で明示上書き（`0` で無効）。トリム量は `build_meta.json` の
`reference_start_offset_sec` に記録される。

**`--cens-win`**: 窓を縮める A/B（41→21→11）では判別能が改善しなかったため**既定の 41 を推奨**。
実験用フラグとして残している（実測の詳細は CLAUDE.md「確信度の二本立てと特徴量の判別能」）。

**`--hop-length`**: フレームレート = `sample_rate / hop_length`（既定 ≈10.77 Hz）。ランタイムの
マッチング特徴のみに影響し、オフラインの warp path アラインメント（synctoolbox の 50 Hz 固定
パイプライン）には影響しない（Issue #32）。ライブ側と参照側は自動伝播により常に一致する。

**ビルド成果物**: `data/built/<output>/` に以下が生成される：

| ファイル | 内容 |
|---|---|
| `warping_path.npz` | score_time ↔ reference_time の対応表 |
| `reference_cens.npy` | 本番 OLTW 用 CENS 特徴量行列 `(12, N)` |
| `reference_onset.npy` | 本番 OLTW 用オンセット強度列 `(N,)`（CENS+onset 融合に使用） |
| `build_meta.json` | ビルド設定メタデータ（`score_bpm`・`has_onset`・トリム量・特徴量パラメータ等） |

> **注意**: 2026-07 の Issue #32 修正以前にビルドした built dir は warp path がテンポ揺れを
> 捉えない退化したアラインメントで生成されている。小節カウントが数小節ずれるため
> **`asf-build` で再ビルドすること**（フォーマット互換、コマンドは従来どおり）。

### 5. ライブ追随

```powershell
# 通常 (マイク + Google Slides)
python -m audio_score_follower.main config/beethoven5.json `
    --slide-url "https://docs.google.com/presentation/d/<ID>/present" `
    --verbose

# ドライラン (マイクあり、Slides なし。動作確認用)
python -m audio_score_follower.main config/beethoven5.json --verbose

# 診断モード (ファイル入力。ファイル名のみは data/reference_audio/ に自動解決)
python -m audio_score_follower.main config/beethoven5.json `
    --input-wav karajan_1977.mp3 `
    --verbose
```

`--input-wav` モードでは silence gate が自動で無効化され、ファイルを実時間で
CENS+onset → OLTW に流す。アルゴリズムの検証、リファレンス音源との自己整合確認、
別演奏 (alt recording) でのカバレッジ測定に使う。`--play-audio` を足すと同じ音声を
スピーカーからも再生し、耳でもズレを確認できる。

#### リアルタイム可視化ウィンドウ (`--viz`)

`--viz` を付けると追随本体とは別ウィンドウが開き、確信度の成り立ちをフレーム単位で
リアルタイム表示する。全入力モードで使用可。追随ロジックには一切影響しない診断・デモ用
（`--viz` 省略時はこの経路に入らず本番挙動・性能は不変）。

表示は「上＝良い・緑＝良い・高さが揃えば一致」の文法で統一した 4 段構成：

| パネル | 内容 |
|---|---|
| ヘッダ | 現在小節（大）+ 一致度ゲージ（表示確信度 0-100%。本体 GUI と同じ色分け） |
| いまの音 と 楽譜の音 | ライブ入力（シアン）と参照演奏（アンバー）のドレミ 12 成分を並置。高さが揃えば一致。右上にこの瞬間の一致% |
| 一致度の推移 | 一致度の時間履歴の面グラフ。背景に「好調/様子見/迷子ぎみ」の色ゾーン |
| 演奏位置さがし | 近くの小節を聴きくらべた類似度の「山」。頂上＝現在位置の判断。鋭い単峰＝自信あり、平ら・二山＝曖昧 |

#### ヘッドレス計測 (tasks/eval_tracking.py)

GUI を起動せず最速で追従品質を数値化するツール。パラメータ A/B 比較はこれで行う：

```powershell
python tasks/eval_tracking.py `
    --built-dir data/built/幻想4 `
    --score data/scores/幻想4_リピート削除.mxl `
    --input-wav "data/reference_audio/<録音>.mp3" `
    [--csv out.csv] `
    [--follower oltw|posterior] `
    [--oltw-kwargs '{\"display_slew_factor\": 0}']
```

出力: カバレッジ%、小節ジャンプ >1 / >3 の回数、最長・累計 stall、per-frame 前進量の
stddev、トリガー可能フレーム率（`conf >= 0.30`）。`--csv` で per-frame の詳細
（`live_time, ref_frame, dp_ref_frame, measure, confidence, raw_local_cost`）をダンプできる。
`--oltw-kwargs` は config を編集せずデフォルトに JSON を上書きマージする。
`--follower posterior` は実験用の全域観測ベイズフィルタで駆動する（**本番は oltw 固定**。
経緯は CLAUDE.md）。

### 6. ランチャー GUI（引数なし起動）

config 引数を省略すると、CLI オプション相当の項目を GUI で選んで起動できる：

```powershell
python -m audio_score_follower.main
# または
asf-follow
```

| 項目 | 対応する CLI |
|---|---|
| 設定ファイル | 第1引数の config.json（`config/*.json` を列挙、前回使用したものをプリセレクト） |
| 入力ソース: マイク / ループバック / 音源ファイル | （デフォルト）/ `--loopback` / `--input-wav` |
| マイク・ループバックのデバイス選択 | `settings.mic_device` / `--loopback-device` |
| 同時に再生する | `--play-audio` |
| スライド URL | `--slide-url`（空欄 = ドライラン） |
| 無音判定閾値 / トリガ間隔 | `settings.silence_threshold_db` / `settings.cooldown_seconds` |
| 無音測定マージン (dB) | `settings.launcher.silence_margin_db`（無音測定の式にのみ影響） |
| 無音測定ボタン / NCチェックボタン | （下記） |
| 特徴量・確信度モニタを開く | `--viz` |
| 詳細ログ | `-v` |

**無音測定ボタン**: ステージが無音（暗騒音のみ）の状態で測定し、無音判定閾値を自動設定する。

- 「無音測定」→（最低 2 秒以上）→「測定終了」で、その間のマイク dBFS サンプルから
  `中央値 + (中央値 − 10パーセンタイル) + マージン` を閾値に設定
- **マージン**（既定 `2.0` dB）は「無音測定マージン」欄で調整・保存できる（Issue #41）。
  弱音の冒頭で gate が開きにくい会場では小さく（0 や負値も可、−20〜+20 dB）、
  誤開放が気になる会場では大きくする
- 突発音（咳・椅子の軋み）は分布の上側にしか現れないため、下側の分布幅を上側に鏡映する
  この式は偶発音に頑健で、会場の暗騒音の揺らぎ幅にも自動適応する
- 測定は本番の silence gate と同じ RMS 経路（`AudioLevelMonitor`）なのでキャリブレーションの
  ずれがない
- 入力ソースの選択にかかわらず常に押せる。測定は常にマイク欄で選択中のデバイスから行う

**NCチェックボタン**: 選択中のマイクに Windows のノイズ抑制（NC）フィルターが適用されて
いないかを確認する。特徴量（CENS + onset）は未加工のマイク信号を前提としており、NC が
掛かると追随品質が黙って劣化する。

- WinRT（`AudioEffectsManager`）でデバイスに適用中の効果を照会し、ノイズ抑制の検出時は
  警告と「サウンド設定を開く」ボタン（`ms-settings:sound`）を表示
- プログラムからの自動無効化は不可のため、検出後は操作者が Windows のマイクのプロパティ →
  「オーディオ拡張機能」で手動オフにする
- マイクモードでの起動時（CLI 起動含む）にも自動で一度実行され、検出時は運用 GUI 上部に
  警告バナーが出る
- **検出の限界**: マイク内蔵のハードウェア DSP（AirPods、会議用 USB マイク等）や仮想マイク型の
  NC ソフト（NVIDIA Broadcast、Krisp 等 — デバイス名でのみ警告）は Windows の API から見えない。
  pywinrt 未インストール環境・非 Windows では確認不可の旨を表示する

**設定の永続化**:

- 「開始」で選択内容が**選択した config.json に保存**され（ランチャー専用の状態は
  `settings.launcher` ブロック、デバイスと閾値は既存のフラットキー）、次回起動時に復元される
- デバイスは名前スナップショットも記録され、Windows のデバイス番号が変わっても名前一致で
  再マッチされる（見つからなければ既定のデバイスにフォールバック）
- `config/*.json` の列挙先はカレントディレクトリに依存しない（CWD 直下 → リポジトリ直下の
  順にフォールバック。Issue #7）
- **CLI モード（config 引数あり）は `settings.launcher` を無視する**（挙動不変の後方互換。
  `mic_device` 等の既存キーは従来どおり有効で、CLI フラグが優先）

**オフラインビルドを作成…ボタン**: `asf-build` 相当を GUI で実行する画面へ遷移する。

- 楽譜・参照録音・出力名を選んで「ビルド実行」→ ビルドをサブプロセスで起動し進捗ログを
  ストリーム表示（CLI を触らずビルドできる）。詳細設定（BPM・トリム・CENS 窓等）は空欄なら
  自動推定に従う
- 「ビルド成功後に対応する config を生成する」（既定 ON）で、ビルドを指す最小構成の
  config.json（`measure:1` のトリガー 1 つが雛形）を `config/` に生成。トリガーは生成後に
  config を編集して設定する
- 「戻る」でランチャーへ復帰すると生成した config が選択済みで現れ、そのまま追随を起動できる

## config.json スキーマ

`config/` ディレクトリに JSON ファイルを置く。`asf-follow` の第1引数に渡す。

```json
{
  "settings": {
    "cooldown_seconds": 3.0,
    "silence_threshold_db": -55.0,
    "mic_device": null,
    "feature_fusion": {
      "chroma_weight": 0.7,
      "onset_weight": 0.3
    },
    "oltw_kwargs": {
      "search_width": 240,
      "back_inhibit_frames": 30,
      "init_search_width": 30,
      "step_penalty": 0.06,
      "max_advance_per_frame": 50,
      "stuck_dp_reset_seconds": 12.0,
      "stuck_rematch_seconds": 0.0
    }
  },
  "movements": [
    {
      "id": 1,
      "xml_file": "../data/scores/piece.mxl",
      "built_dir": "../data/built/piece_recording",
      "triggers": [
        {"measure": 1,  "action": "right", "note": "開始"},
        {"measure": 17, "note": "第二主題（action 省略 → right 扱い）"}
      ]
    }
  ]
}
```

### settings: 全般

| フィールド | デフォルト | 説明 |
|---|---|---|
| `cooldown_seconds` | `3.0` | トリガ発火後、次のトリガが有効になるまでの最短間隔（秒）。 |
| `silence_threshold_db` | `-55.0` | この dBFS 以下が続いたら OLTW を一時停止（フェルマータ・休符対策）。`--input-wav` モードでは自動無効。ホール本番は観客の咳・空調で暗騒音が **-50 〜 -45 dBFS** 程度まで上がるため、デフォルトのままだと無音区間でも gate が解除されず追従が進む。本番では事前にステージ無人時のレベルを実測し、その値 +3 dBFS 程度を指定する。 |
| `gate_activation_sec` | `0.7` | silence gate が**開く**（追随再開する）のに必要な連続音時間（秒）。咳・ドア音などの一瞬の物音では gate が開かず、ノイズで OLTW が不可逆に前進するのを防ぐ。音楽の立ち上がりは持続するので、実質的なコストは開始時の短い遅延のみ（DP の前方探索が吸収する）。`0` で即時反転（旧挙動）。 |
| `gate_release_sec` | `0.3` | silence gate が**閉じる**（freeze する）のに必要な連続無音時間（秒）。音符間の短い切れ目で freeze/unfreeze が暴れるのを防ぐ。`0` で即時反転。なお gate が freeze/unfreeze を統治するのは「▶ 演奏開始」押下から最初の持続音または見切りタイムアウトまで（「OLTW 設計メモ」参照）。 |
| `start_search_seconds` | `10.0` | マイクモードの手動スタート（▶ 演奏開始）で、押下が音楽の実開始より**遅れた**場合に備えて広げる初回探索窓（秒）。早押し側は silence gate + pre-lock-in 巻き戻しが自動補正する。 |
| `start_gate_timeout_sec` | `3.0` | **見切りスタート**（Issue #41）: 「▶ 演奏開始」押下からこの秒数以内に gate が開かなければ（= 冒頭が閾値より弱い）、音が聞き取れなくても追随を開始する。操作者の押下を音量計より信頼する設計。`0` で無効（従来どおり持続音を待ち続ける）。トレードオフ: 押下がこの秒数より早すぎると、タイムアウト後は雑音上で DP が走り始めるため、**押下は指揮者の振り出し（直前〜直後）で行う**。 |
| `mic_device` | `null` | マイク入力デバイス名または番号。`null` = OS デフォルト。 |
| `loopback_device` | `null` | `--loopback` モードで取得する出力デバイス番号または名前。`null` = OS デフォルト出力。CLI の `--loopback-device` が優先。 |
| `launcher` | (なし) | ランチャー GUI が保存する起動オプション。**CLI 起動時は無視される**。手で編集する必要はない。 |
| `feature_fusion` | (下記) | CENS + onset 融合係数。通常はデフォルトのまま。 |
| `oltw_kwargs` | (下記) | OLTW のチューニングパラメータ。通常はデフォルトのまま。 |

### settings.feature_fusion: 特徴量融合係数

| フィールド | デフォルト | 説明 |
|---|---|---|
| `chroma_weight` | `0.7` | CENS コサイン距離に掛ける重み。 |
| `onset_weight` | `0.3` | オンセット絶対差分に掛ける重み。`0.0` にすると CENS 単独と同等（オンセット無効化）。両方 `0` はエラー。 |

融合距離 = `chroma_weight × (1 - cosine(ref, live)) + onset_weight × |ref_onset - live_onset|`。
和が 1 になるよう正規化される必要はない。`reference_onset.npy` が存在しない場合は
`onset_weight` は無視され CENS 単独で動作する。

### settings.oltw_kwargs: OLTW チューニング

通常変更不要。デフォルトはオーケストラ的密音響 + 別演奏追従を想定して調整済み。

| フィールド | デフォルト | 説明 |
|---|---|---|
| `search_width` | `240` | 探索 band の **前方** 幅（frame、≈22 秒）。広いほどテンポ差を吸収、狭いほどドリフト耐性。 |
| `back_inhibit_frames` | `30` | 探索 band の **後方** 幅（frame、≈2.8 秒）。前方より狭く非対称にすることで、繰り返しテーマでの後退ロックを防止。 |
| `init_search_width` | `30` | 初フレームのみの探索幅。狭くしないと冒頭で誤位置 (5〜20 秒先) にロックする。 |
| `step_penalty` | `0.06` | DP の現状維持・stay にかかる罰則。低いと stuck plateau から脱出できない、高いと race-ahead。 |
| `max_advance_per_frame` | `50` | 1 ライブフレームで進める ref frame の上限（≈4.6s）。「20 小節先に瞬間ジャンプ」を構造的に防止。 |
| `stuck_dp_reset_seconds` | `12.0` | DP が累積コストで後退ロックされた時の救済発動秒数。`0` で無効。 |
| `stuck_rematch_seconds` | `0.0` | スコア全体探索による遠方ジャンプ機構。**デフォルト無効**（繰り返しテーマで誤テレポートするため）。 |
| `lock_in_frames` | `30` | 「曲開始を捉えた (lock-in)」と判定するための連続高 conf フレーム数（≈3 秒）。lock-in 前の silence gate は位置固定、lock-in 後は慣性進行に切り替わる。 |
| `lock_in_confidence` | `0.45` | lock-in カウンタ／慣性復帰で「高 conf」とみなす confidence 閾値。 |
| `inertia_exit_frames` | `3` | 慣性を抜ける（DP に復帰する）のに必要な連続高 conf フレーム数。 |
| `inertia_history_frames` | `40` | 慣性 rate 推定用の position history 窓（≈3.7 秒）。長いほど滑らか、短いほど tempo 変化に追従。 |
| `max_inertia_seconds` | `10.0` | 慣性進行の最大持続秒数。超えたら位置固定に戻り、復帰は手動 →/L 任せ。長フェルマータが多い曲（Bruckner 等）では 20〜30 に増やす。`0.0` で慣性を無効化（legacy: freeze=位置固定）。 |
| `inertia_resync_max_gap_frames` | `None` | 慣性位置と DP 位置の許容ギャップ（frame）。`None` なら `search_width` を使う。 |
| `display_slew_factor` | `3.0` | **表示スルー**: 通常追従中、表示位置が DP 位置を追いかける速度の上限を `max(display_min_advance, 推定rate × factor)` frame/frame に制限する。stall 後の DP キャッチアップが「瞬間テレポート」ではなく短い早送りとして表示される。seek / reset 等の**意図的ジャンプは即時スナップ**。`0` で無効（表示 = 生 DP 位置）。 |
| `display_min_advance` | `2.0` | 表示スルーの 1 フレームあたり前進量の下限。rate 推定が下限 clamp (0.3) に張り付いていてもキャッチアップを保証する。 |
| `low_conf_advance_frames` | `0` | **低 conf 適応キャップ**（実験的・デフォルト無効）: 低 confidence がこのフレーム数連続したら、`max_advance_per_frame` を `max(low_conf_advance_min, ceil(rate × low_conf_advance_factor))` に絞る。高 conf フレームが来たら即フルキャップに復帰。`0` で無効。 |
| `low_conf_advance_factor` | `4.0` | 適応キャップの rate 乗数。 |
| `low_conf_advance_min` | `4` | 適応キャップの下限（frame）。 |
| `mismatch_cost_threshold` | `0.18` | **ずれ検知**: 融合コスト（5 フレーム平滑）がこれを超えるフレームを「ずれ疑い」としてカウント。幻想4 実測校正。`0` で検知・訂正機能ごと無効。 |
| `mismatch_seconds` | `8.0` | ずれ疑いフラグを立てるのに必要な連続超過秒数。正解入力の一過性の高コストで誤検知しないための持続条件。 |
| `mismatch_clear_margin` | `0.03` | フラグ解除のヒステリシス: コストが `threshold − margin` を ~1 秒連続で下回ったら解除。 |
| `mismatch_probe_interval_seconds` | `1.0` | フラグ中の自動リカバリ探索の周期。 |
| `mismatch_recovery_cost_ceiling` | `0.08` | リカバリジャンプの**絶対ガード**: 候補の局所コストが matched 帯以下でなければ跳ばない。 |
| `mismatch_recovery_max_jump_seconds` | `10.0` | リカバリの前方探索窓。有界（スコア全体は探索しない — 自己類似箇所への誤テレポート防止）。大きなずれは操作者が手動で先に補正する前提。 |

### movements

| フィールド | 必須 | 説明 |
|---|---|---|
| `id` | ○ | 楽章番号（任意の整数。順序の識別用）。 |
| `xml_file` | ○ | MusicXML / MXL ファイルのパス。config ファイルからの相対パス可。 |
| `built_dir` | ○ | `asf-build` の出力ディレクトリ。 |
| `triggers` | ○ | スライド操作の定義リスト。`measure`（小節番号）が必須。`action`（`"right"` / `"left"`）は任意で、省略時は `"right"` 扱い。`note` は任意メモ。 |

## ライブ操作

GUI 起動後の操作：

| キー | 動作 |
|------|------|
| **→** / Space | 次のスライド + OLTW を次の trigger 小節に sync + 「post-seek catchup」で実演奏位置を自動検出 |
| **←** | 前のスライド + OLTW を直前 trigger の手前に sync |
| **N** | 次の楽章をロード |
| **R** | 楽章を再ロード (OLTW reset) |
| **L** / 「▶ 演奏開始」ボタン | **マイクモードの初回押下 = 演奏開始の指示**。起動・楽章ロード直後の OLTW は完全待機（咳・物音では一切動かない）で、この押下で追随が解禁される。押下後、閾値超えの持続音（`gate_activation_sec`、既定 0.7 秒連続）で追随開始。冒頭が閾値より弱く gate が開かない場合も `start_gate_timeout_sec`（既定 3 秒）で**見切りスタート**する（Issue #41）。押しズレは自動補正: 遅押しなら初回探索（`start_search_seconds`、既定 10 秒）で実位置に着地。**2 回目以降の押下**（および wav / loopback モード）は従来の強制 lock-in（慣性 arm）。ボタンは lock-in 成立後に自動非表示。L キーは常時有効 |
| **E** / 「■ 演奏終了」ボタン | **演奏終了の指示**（Issue #44）。演奏が終わっても follower は追随を続け、拍手・環境音で小節が進みトリガーが出てしまう。押下で OLTW ワーカーを止めてフレーム供給を絶ち、追随を停止 + 自動トリガーを抑止する。当該楽章に対して終端的で、再び追随するには **R（再ロード）/ N（次楽章）** を押す。ボタンは演奏進行中のみ表示 |
| **↑ / ↓** | 無音判定閾値をその場で **±0.2 dB** 調整（マイクモードのみ。wav/loopback は gate 自体が無効なので no-op）。ランタイムのみの変更で config.json には保存されない |
| 「− 閾値」/「＋ 閾値」ボタン | マイクレベル表示の横。無音判定閾値をその場で **±1 dB** 調整（↑/↓ のボタン版・粗ステップ。同じくランタイムのみで保存されない。マイク監視が無効なモードでは灰色） |

### GUI の確信度表示（コストベース）

GUI の「確信度」は**絶対マッチ品質**（融合局所コストの 5 フレーム平滑を 0.05→0.22 で 1→0 に
線形写像）を表示する。OLTW 内部の confidence（band 相対値）は無関係な音でも 0.6-0.8 に
張り付くため、表示には使わない（実測根拠は CLAUDE.md「確信度の二本立て」）：

| 入力 | 内部 conf | GUI 表示 |
|---|---|---|
| 同録音 | 0.93 | **~98%** |
| 別演奏（正解） | 0.64 | **~74%** |
| 無関係なピアノ BGM | 0.4-0.7 | **~0%** |

既知の限界: 白色ノイズ的な広帯域音は全ピッチクラスを含むため中間値（~50%）を示す。

### ずれの自動検知と有界訂正（mismatch detector）

追随中に**カウントが演奏からずれた疑い**を自動検知する。stuck/rapid reset（前進停止時のみ
発火）と違い、**前進しながらずれている**状態を絶対コストで捉える: 融合コストの 5 フレーム
平滑が `mismatch_cost_threshold`（既定 0.18）を `mismatch_seconds`（既定 8 秒）**連続**で
超えたらフラグを立てる。

フラグ中の動作:

- GUI に赤い「⚠ 追随ずれ疑い — ←/→ で補正可」を表示（操作者への通知が第一目的）
- **自動トリガーを抑止**（ずれた位置でスライドを送らない）
- 1 秒ごとに前方 `mismatch_recovery_max_jump_seconds`（既定 10s）を有界探索し、四重ガード
  （コスト良・際立ち・絶対コスト・2 連続 probe の位置整合）を全て通過したときだけ自動で
  再アンカーする。確信が持てない場合は跳ばずアラート継続（操作者の ←/→ が最終手段）

実測（幻想4）: 正解の別演奏では**誤検知ゼロ**、違う楽章は 79 秒で検知、無関係なピアノ BGM は
48 秒で検知。全シナリオで誤ジャンプゼロ。既知の限界: ①白色ノイズはコスト帯が重なり検知不能
（silence gate が主防御）②ずれ先が自己類似箇所だと検知・自動訂正とも原理的に不能 — この場合も
手動補正は機能する。閾値は幻想4 の実測校正値なので、曲・録音条件が大きく変わったら再校正する
（手順は CLAUDE.md「mismatch 検知」）。

### 演奏とカウントがずれた時の対処

OLTW が遅延・先行した場合、人間が ← / → で補正できる。これらのキーは **単なるスライド送り
ではなく、OLTW 内部位置の再同期も行う**：

**シナリオ: 実演奏 m=20、OLTW がまだ m=10**

1. **→ を 1 回押す**
   - スライドが「次のトリガ位置」（例: m=17）の slide content へ
   - OLTW 位置が m=17 の ref_t に jump
   - その trigger を「発火済み」マーク（自動発火と二重発火しない）
   - **直後のライブフレームで「post-seek catchup」**: m=17〜m=17+22s の範囲を chroma 直接検索、
     実演奏位置 (m=20 付近) で強い match が出れば再 anchor
   - 結果: 1 押下で OLTW が m=20 まで自動追従
2. **目視で確認**：verbose ログに以下が出る
   ```
   OLTW seek: ref_frame=183 (16.99s) [catchup armed]
   Slide right [manual] measure=17 note=...
   Manual sync: OLTW re-anchored to measure 17
   OLTW post-seek catchup: 183→215 (local cost 0.412→0.023, discrim_ratio 0.91, +2.97s forward)
   ```
3. **catchup でも追いつかない場合（演奏が search_width=22s 超先）**：→ を再度押す。
   各押下で「次の未発火 trigger」に進むので、複数回押せば任意の位置まで catch up 可能。

**シナリオ: OLTW が誤って先行**

- **← を 1 回押す**
- スライドが戻る + OLTW が直近発火 trigger の **0.2 秒手前** に jump
- その trigger の発火マーク解除（音楽が再到達したら再発火される）
- catchup は **無効化される** (← は「演奏は手前」の signal なので、forward scan は本末転倒)

### ログ書式

`verbose` モードでは、すべてのスライド操作に発火源が付く：

```
Slide right [auto]   measure=17 note=テーマA      ← OLTW 追従による自動発火
Slide right [manual] measure=17 note=テーマA      ← 人手で →
Slide left  [manual] measure=17 note=テーマA      ← 人手で ←
```

`grep '\[manual\]\|\[auto\]\|Manual sync\|post-seek catchup\|stuck-rematch\|DP reset'` で
後からどう発火したかが追える。

## OLTW 設計メモ（運用者向け要約）

内部設計・チューニング根拠・変更時の注意は [CLAUDE.md](CLAUDE.md)「OLTW の状態機械」参照。
ここでは運用時の設定調整と画面の見方に必要な範囲だけまとめる。

オーケストラ的密音響 + 別演奏追従で起きる失敗モードと対処（config で調整可能）：

| 失敗モード | 対処 |
|-----------|------|
| 初期フレームが誤位置に高確信でロック | `init_search_width=30` (初フレームのみ探索範囲を絞る) |
| 自己類似テーマで band 内後退方向に引きずられる | `back_inhibit_frames=30`（band を非対称化） |
| Band-DP の vert chain で 1 フレームで 20 小節先にジャンプ | `max_advance_per_frame=50` で argmin の探索範囲をキャップ |
| 累積コストの「後退アトラクタ」で stuck | `stuck_dp_reset_seconds=12` で過去メモリ wipe（位置は据置） |
| 人手 → 押下後に OLTW が live 位置に追いつけない | `seek(allow_catchup=True)` で post-seek の局所 forward 再 match |
| stall 後の DP キャッチアップが表示上「瞬間ジャンプ」に見える | `display_slew_factor=3.0` の表示スルー層で表示前進速度を制限（DP 内部は無制限のまま） |

「スコア全体を見て位置を特定」する設計は繰り返し曲で破綻するため避けている。すべての探索は
**`search_width=240` (≈22 秒) 以内**、または操作者明示の seek 直後のみ。

### silence gate と慣性進行

- **gate の統治期間（マイクモード）**: gate が追随の停止/再開を制御するのは「▶ 演奏開始」
  押下から**最初の持続音（または `start_gate_timeout_sec` の見切りタイムアウト）まで**。
  どちらかで「演奏進行中」と確定し（one-shot）、以後は音量が閾値を割っても追随は
  止まらない。弱奏・休符は DP がそのまま追う（設計経緯は CLAUDE.md の Issue #13 /
  #41 の項）
- **freeze（gate 発火）の意味は lock-in の前後で変わる**：

| フェーズ | freeze の挙動 | 理由 |
|---|---|---|
| lock-in 前 | **位置固定** | 曲を捉えていない段階で慣性走行すると誤位置から外挿してしまう |
| lock-in 後 | **慣性進行**（直近テンポで位置前進） | 演奏中の無音は pp / pizz / 短い休符。固定すると次のフォルテで数小節遅延する |

- **lock-in 判定**: confidence ≥ `lock_in_confidence` (0.45) が `lock_in_frames` (30 フレーム
  ≈3 秒) 連続で自動成立（一度立てたら降りない）。「▶ 演奏開始」/ L キーで強制も可能
- **慣性は live を追い越せない**: frame 駆動 + rate clamp + `max_inertia_seconds` (10s) cap +
  慣性中の全域探索禁止、の 4 つの構造的安全弁を持つ（詳細は CLAUDE.md）。cap 到達後は
  位置固定になり、手動 → / L で復帰する

### GUI のモード表示

現在モードが大型ラベルで表示される。「▶ 演奏開始」ボタンは lock-in 前のみ
（成立後は押しても効果がないため自動非表示。楽章再ロードで再表示）、「■ 演奏終了」
ボタンは演奏進行中のみ表示される：

| 表示 | 状態 | 色 |
|---|---|---|
| `⏸ 開始待ち（▶ 演奏開始 を押してください）` | 手動スタート待ち | グレー |
| `🎧 音を待っています（無音でも 3 秒後に自動開始）` | スタート押下後、演奏確定前（gate 開放 or 見切りタイムアウト待ち） | グレー |
| `🎧 音を待っています（曲の捕捉中）` | 演奏確定後、lock-in 前 | グレー |
| `🎵 追随中` | 通常追従 | 緑 |
| `🌀 慣性進行中　残り 6.3s / 10s（音が戻れば自動復帰）` | 慣性モード | 橙 |
| `⛔ 慣性停止（位置固定）　手動 → / L で復帰してください` | 慣性 cap 到達 | 赤 |
| `🎯 開始を受け付けました` | スタート押下後 1 秒のフラッシュ | 青 |
| `⏹ 演奏終了（停止中）— R で再追随 / N で次の楽章` | 「■ 演奏終了」/ E で停止（Issue #44） | グレー |

## 本番ホール運用ガイド

「リファレンス録音にライブを追従させる」設計は、リファレンス側とライブ側の音響的・解釈的な差が大きくなるほど精度が落ちる。本番ホールでは以下の運用面の差が累積するため、事前に切り詰めておく。

### マイク経路

| 経路 | 推奨度 | 備考 |
|---|---|---|
| **ホール PA ミキサーの LINE OUT（AUX/REC バス） → USB オーディオ I/F → PC** | ◎ | 残響・観客ノイズの影響が最小で SNR が最も高い。本番ではこの経路を第一選択にする。ミキサー側で REC バスにステージマイクのみアサインしてもらう |
| **ステージ近接マイク（コンデンサ） → I/F → PC** | ○ | PA に LINE OUT がもらえない時。指揮台または木管前あたり 1 本で十分 |
| **ノート PC 内蔵マイク or 客席後方マイク** | × | 残響に支配され CENS が大きく劣化する。最終手段 |

`config.json` の `mic_device` で USB I/F のデバイス名を明示指定する。OS デフォルトが内蔵マイクに切り替わる事故を防ぐため、本番前に `python -c "import sounddevice; print(sounddevice.query_devices())"` で名前を確定させておく。

### ホール残響への対処

CENS は per-frame L2 正規化と短時間平滑で音量差・短時間ジッタを吸収するが、**2 秒を超える長残響では前小節のスペクトルが現在小節に混ざる**ため chroma が鈍る。対策：

- マイクは可能な限り音源近接（PA LINE OUT が理想）
- 長残響ホール（教会・大ホール）ではリファレンスもゲネプロ録音に切り替える。プロ録音（近接マイク多用）とは残響特性が違いすぎる
- `oltw_kwargs.search_width` を `240` → `320` 程度に広げて、テンポ追従の余裕を確保

### 当日タイムライン（推奨）

```
ゲネプロ開始前   USB I/F + マイク経路で「ステージ無人時の dBFS」を実測
                 → silence_threshold_db を決定
ゲネプロ中       PA LINE OUT を WAV で録音（48 kHz / 16-bit 以上）
ゲネプロ終了     asf-build --reference <ゲネプロ.wav> ...
                 （8 分の楽曲で特徴抽出 + MrMsDTW ≈ 3〜6 分）
ビルド検証       python -m audio_score_follower.main config/<piece>.json \
                   --input-wav <ゲネプロ.wav> --verbose
                 → カバレッジ 100%・conf 0.9+ を確認
本番開始 30 分前 同じ I/F・同じデバイス名で実機起動、無音時の dBFS を再確認
本番             OLTW 起動 → オペレータは ← / → を握って待機
```

### 本番中のリカバリ手順

完全にロストした時のために、オペレータは以下を順に試す：

1. **楽章冒頭で lock-in が立たない場合は「▶ 演奏開始」ボタン / L キーを押す** — 強制 lock-in で慣性モードを armed にしておくと、続く silence gate 発火が位置固定ではなく慣性進行になる
2. **→ を 1 回押す** — 次の trigger 小節に OLTW を anchor、post-seek catchup で 22 秒前方を自動再探索
3. **演奏がさらに先なら → を連打** — 各押下で次の未発火 trigger に進む
4. **演奏が手前なら ← を 1 回** — 直近 trigger 手前 0.2 秒に戻し、再到達で自動発火
5. **完全に外れた / 楽章が分からなくなった** — `R` で現在楽章を再ロード（OLTW を冒頭から再起動）
6. **冒頭の `init_search_width=30` が狭すぎて起動時に外す** — config の `oltw_kwargs.init_search_width` を 60〜90 に上げる（指揮者の取り方が遅い・ルバート気味の冒頭で効く）

### 本番で詰まりやすい落とし穴

| 症状 | 原因 | 事前対処 |
|---|---|---|
| 起動直後に 5〜20 秒先にロックする | `init_search_width` がリファレンスと冒頭テンポが違う指揮者では狭すぎ | ゲネプロ録音をリファレンスにする。または `init_search_width` を上げる |
| 繰り返し記号で先のリピートにテレポート | `stuck_rematch_seconds > 0` だとスコア全体探索が走る | デフォルト `0.0` を維持。変更しない |
| 長い pp / pizzicato で OLTW が止まる | （旧挙動）暗騒音閾値以下に音圧が下がり silence gate が発火していた。現在は演奏確定後の gate は freeze を発火しないため、閾値割れでは止まらない | DP がそのまま追う。それでも停滞する場合はマイク経路の SNR を改善（PA LINE OUT 推奨） |
| 慣性 cap (10s) を超えても演奏が再開しない | 長休符 or 長フェルマータでの想定オーバー | `max_inertia_seconds` を 20〜30 に増やす。または cap 到達時に手動 → で trigger を進める |
| 慣性中の表示位置が実演奏より明らかにずれる | 慣性 rate 推定窓 (`inertia_history_frames=40` ≈3.7s) が rubato に追従しきれない | `inertia_history_frames` を 20 に下げる（応答性 ↑、滑らかさ ↓） |
| マイク経路の confidence が常に 0.2 以下 | ホール残響 or マイク距離 or ゲイン不足 | PA LINE OUT に切り替え、`mic=-20 dBFS 以上` を verbose ログで確認 |

## 診断ワークフロー

問題切り分けの順序：

1. **A. 同録音 file-input**: `--input-wav <リファレンスと同じ音源>` で 100% カバレッジ・confidence 0.95+ になるはず → OLTW 自体は OK
2. **B. 別演奏 file-input**: `--input-wav <別演奏>` で 96-100% カバレッジ目安。conf 0.3〜0.5 は仕様（chroma の差で margin が出にくい）
   - **テスト録音の選び方が保証範囲を決める**: プロ録音（別指揮者・スタジオ）で合格 = 最悪条件でもアルゴリズムが壊れていない確認。**当日ゲネプロ録音（同ホール・同日）で合格 = マイク経路に問題がなければ本番は動く**、という強い確信が得られる。B が通っても C が NG な場合の原因は専らマイク経路。
3. **C. マイク経由**: A/B が OK でこれが NG → マイク経路の SNR 問題が主因
   - PA ミキサーの LINE OUT 分岐 → USB オーディオ I/F に切り替える。内蔵マイクや客席後方マイクはホール残響で CENS が劣化するため最終手段。

`grep 'OLTW' verbose.log | head -50` で起動直後の挙動が追える。`mic=-20dBFS 以上` を確認。

## プロジェクト構成

```
audio_score_follower/
├── core/
│   ├── score_mapper.py       # 拍 ↔ 小節 (アウフタクト対応)
│   ├── slide_controller.py   # Playwright Slides
│   ├── state_manager.py      # GUI 状態管理 (lock-in / inertia / mic level 等を atomic に保持)
│   ├── audio_level.py        # silence gate (マイク dBFS 監視)
│   ├── mic_effects_probe.py  # マイクの NC (ノイズ抑制) フィルター検出 (WinRT)
│   ├── cooldown_timer.py     # クールダウン + 手動 unmark
│   ├── feature_extractor.py  # CENS + onset 特徴抽出 (librosa)。オフライン/オンラインで唯一の経路
│   ├── reference_builder.py  # オフライン MrMsDTW + 先頭/末尾トリム検出
│   ├── oltw_follower.py      # Online DTW 本体 + lock-in / inertia / mismatch 検知
│   ├── warp_lookup.py        # ref_time ↔ score_time ↔ measure (双方向) + warp path 検証
│   ├── synth_locator.py      # FluidSynth / SoundFont の検出
│   ├── viz_feed.py           # --viz 用のスレッド安全なデータ供給層
│   └── follower_worker.py    # マイク (FollowerWorker) + ファイル (FileWorker) + loopback
├── ui/                       # Tkinter GUI シェル + 起動ランチャー + ビルド画面 + viz ウィンドウ
├── config/                   # config.json loader + oltw_kwargs デフォルト定義
├── cli/
│   ├── build_reference.py    # オフラインビルド CLI (asf-build)
│   └── follow.py             # ライブ追随 CLI (asf-follow)
├── launch_options.py         # 起動オプション検証 (CLI/ランチャー共通の純ロジック)
└── main.py                   # GUI エントリ + キーバインディング + silence-gate poll + trigger loop
tasks/
├── generate_score_wav.py     # XML → 合成 WAV (music21 → MIDI → FluidSynth)
└── eval_tracking.py          # 追従品質のヘッドレス計測
tests/                        # pytest
data/                         # gitignore
```

## ライセンス

MIT
