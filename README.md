# audio-score-follower

オーケストラ向けライブ追随アプリ。
スコア合成 WAV と実演奏録音 (プロ録音 / リハ録音) を **オフラインで対応付け**、
本番ではマイク入力を **同じリファレンス録音に Online DTW で追随** させる。

姉妹プロジェクト [live-score-sync](../live-score-sync) は pymatchmaker (note-based matcher) を
使っていたが、オーケストラの密音響で破綻する弱点があった。本プロジェクトは audio-to-audio で
全てを行うことでその問題を回避する。

## 特徴

### 1. Note-based ではなく Audio-to-Audio（CENS マッチング）

オーケストラの密音響、ホール残響、和音の混ざり合いの中で、単音レベルのピッチをマイクから正確に
抽出するのは至難の業。本プロジェクトは演奏音を丸ごと **CENS (Chroma Energy Normalized
Statistics)** に変換し、オーケストラ全体の「響きのエネルギー分布」として捉える。これにより
楽器ごとの休みの影響を無効化できる ─ 弦楽器が長大に休んでいても、木管・金管の響きが
リファレンスと合致していれば迷子にならない。

### 2. 二段階マッピング（オフライン MrMsDTW + オンライン OLTW）

楽譜とマイクをリアルタイムで直接結びつけるのではなく、

- **オフライン**: 楽譜 (XML) ↔ リファレンス WAV を MrMsDTW（マルチスケール DTW）で
  高精度に対応付け → `warping_path.npz`
- **オンライン**: リファレンス WAV ↔ 本番マイクを Online DTW で追随

事前計算した warp path を介すことで、本番中の PC は「既知の音声ファイルとの Online DTW」だけに
集中すればよく、レイテンシが劇的に抑えられる。warp 逆引きで reference_time → score_time → 小節 に
変換する。

### 3. エッジケースへの対処

実際の音楽追従で直面する事態に構造的な対策を持つ:

- **テンポの自動推定** (`asf-build`): 録音の duration とスコアの総ビート数から合成 BPM を逆算
  (`total_beats * 60 / ref_duration`)。楽譜のテンポ指示は目安にすぎず、実演奏では数十%ずれる
  ことが普通なため、固定 120 BPM だと warp path にスキップが大量発生して破綻する。`asf-build`
  は `--score-bpm` 未指定なら自動推定する
- **ビルド時の warp path 検証**: 勾配上限 4.0x・カバレッジ差 5 小節以内でチェック。スコアと
  録音の繰り返し構造が不整合な場合に即検出
- **lock-in 二段構え**: 冒頭ノイズでの誤位置固定を防ぎつつ、曲を捉えた後の silence gate は
  慣性進行に切り替え（詳細は「OLTW 設計メモ」）
- **5 つの DP 失敗モードへの個別対処**: 冒頭誤ロック・自己類似テーマでの後退・vert chain ジャンプ・
  後退アトラクタ stuck・手動 seek 後の追いつき（同上）
- **ホール本番運用**: PA LINE OUT 経路・暗騒音閾値の実測・ゲネプロ録音を当日リファレンスに使う
  フロー（詳細は「本番ホール運用ガイド」）

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
  マイク or WAV ─▶ CENS ─▶ Online DTW ─▶ reference_time
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

### 4. オフラインビルド

```powershell
asf-build `
    --score data/scores/beethoven5.xml `
    --reference data/reference_audio/karajan_1977.mp3 `
    --output data/built/beethoven5_karajan/ `
    [--score-bpm 152] `
    [--start-offset 0.5] `
    [--plot]
```

**合成 BPM の自動推定**: `--score-bpm` を省略すると、参照録音の duration と楽譜の総ビート数
から `total_beats * 60 / ref_duration` で四分音符 BPM を自動算出する。例えばベルリン・フィルの
Berlioz 幻想4 (281s 録音, 712 ビート) なら推定 BPM=152 となり、ログに次が出る:

```
Estimated synth tempo: 712.0 beats / 281.05s ref → BPM=152.00 (quarter-note).
Override with --score-bpm if undesirable.
```

楽譜の指示テンポ (たいてい 120 等) で固定合成すると、実演奏との 20-30% のテンポ差で warp path に
スキップが大量発生し、ビルド時バリデーション (`max_slope=4.0x`) で失敗することがある。自動推定で
これを回避する。`--score-wav` を渡す場合は合成テンポを録音から逆算できないため、`--score-bpm`
の明示指定が必須。

### 5. ライブ追随

```powershell
# 通常 (マイク + Google Slides)
python -m audio_score_follower.main config/beethoven5.json `
    --slide-url "https://docs.google.com/presentation/d/<ID>/present" `
    --verbose

# ドライラン (マイクあり、Slides なし。動作確認用)
python -m audio_score_follower.main config/beethoven5.json --verbose

# 診断モード (マイク経由を完全に外して、ファイル音源で動作確認)
python -m audio_score_follower.main config/beethoven5.json `
    --input-wav data/reference_audio/karajan_1977.mp3 `
    --verbose
```

`--input-wav` モードでは silence gate が自動で無効化され、ファイルを実時間で
CENS → OLTW に流す。アルゴリズムの検証、リファレンス音源との自己整合確認、
別演奏 (alt recording) でのカバレッジ測定に使う。

## config.json スキーマ

`config/` ディレクトリに JSON ファイルを置く。`asf-follow` の第1引数に渡す。

```json
{
  "settings": {
    "cooldown_seconds": 3.0,
    "silence_threshold_db": -55.0,
    "mic_device": null,
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
        {"measure": 17, "action": "right", "note": "第二主題"}
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
| `mic_device` | `null` | マイク入力デバイス名または番号。`null` = OS デフォルト。 |
| `oltw_kwargs` | (下記) | OLTW のチューニングパラメータ。通常はデフォルトのまま。 |

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
| `lock_in_confidence` | `0.45` | lock-in カウンタ／慣性復帰 (`_maybe_resync_from_dp`) で「高 conf」とみなす confidence 閾値。 |
| `inertia_enter_frames` | `5` | （予約フィールド。現状は silence gate `freeze()` のみが慣性入りのトリガー） |
| `inertia_exit_frames` | `3` | `_maybe_resync_from_dp` で慣性を抜けるのに必要な連続高 conf フレーム数。 |
| `inertia_history_frames` | `40` | 慣性 rate 推定用の position history 窓（≈3.7 秒）。長いほど滑らか、短いほど tempo 変化に追従。 |
| `max_inertia_seconds` | `10.0` | 慣性進行の最大持続秒数。超えたら位置固定に戻り、復帰は手動 →/L 任せ。長フェルマータが多い曲（Bruckner 等）では 20〜30 に増やす。`0.0` で慣性を無効化（legacy: freeze=位置固定）。 |
| `inertia_resync_max_gap_frames` | `None` | 慣性位置と DP 位置の許容ギャップ（frame）。`None` なら `search_width` を使う。 |

### movements

| フィールド | 必須 | 説明 |
|---|---|---|
| `id` | ○ | 楽章番号（任意の整数。順序の識別用）。 |
| `xml_file` | ○ | MusicXML / MXL ファイルのパス。config ファイルからの相対パス可。 |
| `built_dir` | ○ | `asf-build` の出力ディレクトリ。 |
| `triggers` | ○ | スライド操作の定義リスト。`measure`（小節番号）と `action`（`"right"` / `"left"`）が必須。`note` は任意メモ。 |

## ライブ操作

GUI 起動後の操作：

| キー | 動作 |
|------|------|
| **→** / Space | 次のスライド + OLTW を次の trigger 小節に sync + 「post-seek catchup」で実演奏位置を自動検出 |
| **←** | 前のスライド + OLTW を直前 trigger の手前に sync |
| **N** | 次の楽章をロード |
| **R** | 楽章を再ロード (OLTW reset) |
| **L** / 「▶ 楽章開始」ボタン | OLTW を強制 lock-in。指揮者の振り出しに合わせて押すと、自動 lock-in (≈3 秒の confident 追従) を待たずに慣性モードを armed にできる。lock-in 後の silence gate は **freeze ではなく慣性進行**になる。**ボタンは lock-in 成立後に自動で非表示になる**（押しても no-op なので運用画面から消す）。L キーバインドは常時有効なので、楽章再ロード (R) 等で lock-in が降りた場合は L か再表示されたボタンで再 arm 可能 |

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

## OLTW 設計メモ

オーケストラ的密音響 + 別演奏追従に必要だった、5 つの独立な失敗モードへの対処：

| 失敗モード | 対処 |
|-----------|------|
| 初期フレームが誤位置に高確信でロック | `init_search_width=30` (初フレームのみ探索範囲を絞る) |
| 自己類似テーマで band 内後退方向に引きずられる | `back_inhibit_frames=30`（band を非対称化） |
| Band-DP の vert chain で 1 フレームで 20 小節先にジャンプ | `max_advance_per_frame=50` で argmin の探索範囲をキャップ |
| 累積コストの「後退アトラクタ」で stuck | `stuck_dp_reset_seconds=12` で過去メモリ wipe（位置は据置） |
| 人手 → 押下後に OLTW が live 位置に追いつけない | `seek(allow_catchup=True)` で post-seek の局所 forward 再 match |

「**スコア全体を見て位置を特定**」する設計は繰り返し曲で破綻するため避けている。
すべての探索は **`search_width=240` (≈22 秒) 以内** または操作者明示の seek 直後のみ。

### silence gate と慣性進行（lock-in 二段構え）

silence gate（マイク無音検出）が freeze() を発火したとき、**何が起きるかは曲の進行段階による**：

| フェーズ | freeze() の挙動 | 理由 |
|---|---|---|
| lock-in 前（冒頭ノイズ・暗騒音追従中） | **位置固定**（legacy） | DP がまだ曲を捉えていない段階で慣性走行すると、冒頭ノイズの「適当な位置」を起点に外挿して大幅にずれる |
| lock-in 後（曲開始を捉えた以降） | **慣性進行**（直近 rate で位置前進） | 演奏中に「曲の進行が止まる」ことはあり得ない。silence は楽器の pp / pizz / 短い休符。位置を固定すると次のフォルテで数小節遅延する |

**lock-in 判定**: 初期化期間（`init_search_width` フレーム）を越え、`confidence >= lock_in_confidence (=0.45)` が `lock_in_frames (=30)` 連続フレーム（≈3 秒）成立。単調ラッチ（一度立てたら降りない）。**GUI「▶ 楽章開始」ボタン / L キー** で指揮者の振り出しに合わせて強制 lock-in も可能。

**慣性のトリガー**: silence gate の `freeze()` のみ。低 DP confidence 単独では慣性に入らない（オーケストラの pp passage は DP が正しい位置を低マージンで追えている状態が多いため、慣性で上書きすると別演奏のカバレッジが悪化する。実測で 100%→34% の regression を確認したため除外した）。

**慣性進行の安全弁**（live-score-sync の「テンポ外挿が演奏を追い越して冒頭 measure 1 へ snap back する regression」を再発させないための設計）：

1. **物理上限**: live frame 1 つにつき ref を `+inertia_rate` だけ進める（rate 自体は直近 ≈3.7 秒の DP 履歴から算出、clamp [0.3, 2.0]、`_last_good_rate` キャッシュで快速 re-entry にも安定）。経過時間に依存する外挿ではないため、構造的に live より速くなれない。
2. **絶対 cap**: `max_inertia_seconds (=10)` を越えたら位置固定。蓄積誤差が許容範囲を超えるので潔く止める。
3. **慣性中は global rematch を抑制**: スコア全体探索による遠方ジャンプは慣性中に走らせない（自己類似テーマへの誤テレポート防止）。
4. **DP は unfreeze 後も走らせ続け、復帰判定で snap back**: `unfreeze()` は `_inertia_active` を即座にはクリアせず、後続フレームの DP が confidence ≥ `lock_in_confidence` を `inertia_exit_frames (=3)` 連続成立させ、かつ DP 位置と慣性位置のギャップが `search_width` 以内なら `seek(dp_pos, allow_catchup=True)` で snap back（既存 post-seek catchup を再利用）。これが「前後マッチングして復帰」の実体。

GUI には現在モードが大型ラベルで表示され、ラベル右側の「▶ 楽章開始」ボタンは **lock-in 前のみ** 表示される：

| 表示 | 状態 | 色 | ボタン |
|---|---|---|---|
| `⏸ 待機中（楽章開始を待っています）` | lock-in 前 | グレー | 表示 |
| `🎵 追随中　conf: 0.78` | 通常追従 | 緑 | 非表示 |
| `🌀 慣性進行中　残り 6.3s / 10s` | 慣性モード（カウントダウン付き） | 橙 | 非表示 |
| `⛔ 慣性停止（位置固定）` | 慣性 cap 到達。手動 → / L で復帰 | 赤 | 非表示 |
| `🎯 lock-in 強制発動` | 「▶ 楽章開始」ボタン押下後 1 秒 | 青フラッシュ | (現状維持) |

ボタンの自動非表示は「音楽が進行しているのに『楽章開始』ボタンが残っているのは違和感がある」という運用上のフィードバックを受けた対応。lock-in 成立後はボタンが押されても `force_lock_in()` が no-op になるため、ボタンを画面から消して状態表示パネルだけで OLTW の状態を伝える。再 arm が必要なケース（楽章再ロード等）では `is_locked_in=False` に戻るのでボタンも自動的に再表示される。

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
                 （8 分の楽曲で MrMsDTW ≈ 1〜2 分）
ビルド検証       python -m audio_score_follower.main config/<piece>.json \
                   --input-wav <ゲネプロ.wav> --verbose
                 → カバレッジ 100%・conf 0.9+ を確認
本番開始 30 分前 同じ I/F・同じデバイス名で実機起動、無音時の dBFS を再確認
本番             OLTW 起動 → オペレータは ← / → を握って待機
```

### 本番中のリカバリ手順

完全にロストした時のために、オペレータは以下を順に試す：

1. **楽章冒頭で lock-in が立たない場合は「▶ 楽章開始」ボタン / L キーを押す** — 強制 lock-in で慣性モードを armed にしておくと、続く silence gate 発火が位置固定ではなく慣性進行になる
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
| 長い pp / pizzicato で OLTW が止まる | 暗騒音閾値以下に音圧が下がり silence gate が発火、lock-in 前なら位置固定になる | 「▶ 楽章開始」ボタン or L キーで強制 lock-in しておく。以降は慣性進行が効く |
| 慣性 cap (10s) を超えても演奏が再開しない | 長休符 or 長フェルマータでの想定オーバー | `max_inertia_seconds` を 20〜30 に増やす。または cap 到達時に手動 → で trigger を進める |
| 慣性中の表示位置が実演奏より明らかにずれる | 慣性 rate 推定窓 (`inertia_history_frames=40` ≈3.7s) が rubato に追従しきれない | `inertia_history_frames` を 20 に下げる（応答性 ↑、滑らかさ ↓） |
| マイク経路の confidence が常に 0.2 以下 | ホール残響 or マイク距離 or ゲイン不足 | PA LINE OUT に切り替え、`mic=-20 dBFS 以上` を verbose ログで確認 |

## 診断ワークフロー

問題切り分けの順序：

1. **A. 同録音 file-input**: `--input-wav <リファレンスと同じ音源>` で 100% カバレッジ・confidence 0.95+ になるはず → OLTW 自体は OK
2. **B. 別演奏 file-input**: `--input-wav <別演奏>` で 96-100% カバレッジ目安。conf 0.3〜0.5 は仕様（chroma の差で margin が出にくい）
   - **テスト録音の選び方が保証範囲を決める**: プロ録音（別指揮者・スタジオ）で合格 = 最悪条件でもアルゴリズムが壊れていない確認。**当日ゲネプロ録音（同ホール・同日）で合格 = マイク経路に問題がなければ本番は動く**、という強い確信が得られる。B が通っても C が NG な場合の原因は専らマイク経路（上記「本番ホール運用ガイド」参照）。
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
│   ├── cooldown_timer.py     # クールダウン + 手動 unmark
│   ├── feature_extractor.py  # CENS 特徴抽出 (librosa)。オフライン/オンラインで唯一の経路
│   ├── reference_builder.py  # オフライン MrMsDTW
│   ├── oltw_follower.py      # Online DTW 本体 + lock-in latch + inertia + seek() / post-seek catchup
│   ├── warp_lookup.py        # ref_time ↔ score_time ↔ measure (双方向)
│   └── follower_worker.py    # マイク (FollowerWorker) + ファイル (FileWorker)
├── ui/                       # Tkinter GUI シェル (モード表示パネル + 楽章開始ボタン)
├── config/                   # config.json loader + oltw_kwargs デフォルト定義
├── cli/
│   ├── build_reference.py    # オフラインビルド CLI
│   └── follow.py             # ライブ追随 CLI
└── main.py                   # GUI エントリ + キーバインディング + silence-gate poll
tasks/
└── generate_score_wav.py     # XML → 合成 WAV (MuseScore 4 CLI, Windows ネイティブ)
tests/                        # pytest (現在 47 テスト。test_oltw_follower.py が lock-in/inertia を 31 ケースでカバー)
data/                         # gitignore
```

## ライセンス

MIT
