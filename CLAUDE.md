# Claude 開発ガイダンス (audio-score-follower-onset)

姉妹プロジェクト `live-score-sync` の方針を踏襲しつつ、本プロジェクト固有の事情を追記する。

このファイルは **別セッションの Claude が冷えた状態から仕様を把握するための地図** を兼ねる。「どのファイルが何を担当しているか」「なぜそうなっているか」「触ってはいけない箇所」を網羅すること。

## このシステムが実現すること（目的）

**本番のオーケストラ演奏をマイクでリアルタイム追跡し、指定小節に到達したら Google Slides を自動でページ送りして聴衆向けの解説を表示する。** 追跡アルゴリズム（OLTW）はこの目的のための手段であり、最終出力はスライド操作である。

エンドツーエンドの流れ:

```
マイク → OLTW 追随 → 小節番号 → トリガー判定 (main._trigger_loop)
      → SlideController (Playwright/Chromium) → Google Slides にキー送出
```

### トリガーシステム（main.py + core/cooldown_timer.py + core/slide_controller.py）

- トリガーは `config.json` の `movements[].triggers[]` で定義: `{"measure": N, "action": "right"}`。action は `slide_controller.py` の `_KEY_MAP` でブラウザのキーに変換（`right`→`ArrowRight`、`space`→`Space` 等。未知の action はそのままキー名として送出）
- 専用スレッド `_trigger_loop` が `_TRIGGER_POLL_HZ` (=20Hz) で現在小節を監視
- **発火条件**: 現在小節がトリガー小節に一致 **かつ** smoothed confidence ≥ `_TRIGGER_CONFIDENCE_FLOOR` (=0.30) **かつ** mismatch フラグが立っていない（`state.is_mismatched` — 「mismatch 検知 + 有界前方リカバリ」参照）**かつ** `CooldownTimer.should_trigger()`（`settings.cooldown_seconds`、既定 3.0s）が許可 **かつ** その小節が未発火（`_fired_trigger_measures` で重複防止）
- `_TRIGGER_CONFIDENCE_FLOOR` (0.30) は `lock_in_confidence` (0.45) より意図的に低い: 旧 live-score-sync の InertiaEngine が担っていた「整列が安定するまで発火しない」ガードの代替で、起動直後の measure-1 誤発火だけを防げばよいため
- `--slide-url` 省略時は `NullSlideController`（no-op）で**ドライラン**起動する。スライドなしで追随のみ検証できる
- 手動オーバライド: →/Space キーで「次の未発火トリガーへ進めて発火済みにマーク + OLTW を seek」、← で直前の発火を取り消して戻る（発火順は小節順管理）

「**スライドが送られない / 二重に送られる**」系の問題は `_trigger_loop`（main.py）と `CooldownTimer`、Playwright 側の問題は `core/slide_controller.py` を見る。

## このプロジェクトのコア発想

`live-score-sync` で使っていた pymatchmaker (matcher) はオーケストラの密音響では破綻する
(暴走/停止/2x 先走り; Issue #28 系)。
根本原因は **リファレンスがスコア合成波形 (単一音色) で、本番のオケ音響と特徴空間が乖離している** こと。

本プロジェクトは:

1. **オフライン**: スコア合成 WAV (music21 → MIDI → **FluidSynth** でレンダリング) と、実演奏
   (プロ録音 → リハ録音) を **synctoolbox の MrMsDTW** で対応付け、
   `warping_path` (score_time ↔ reference_time) を保存。
2. **オンライン**: マイク入力を **同じリファレンス録音** に対して Online DTW で追随。
   出力 reference_time を warp で逆引きして score_time → 小節へ。
3. **特徴量は CENS + onset の融合**（リポ名 `-onset` の由来）: OLTW の局所コストは
   chroma (CENS) の cosine 距離と spectral-flux onset の絶対差の加重和。
   自己類似パッセージ（同和声の再現部・反復主題）は chroma を共有するが
   attack envelope は共有しないため、onset が曖昧性を解消する。
   詳細は「特徴量の同一性と CENS+onset 融合」参照。

本番 OLTW のリファレンスが「実演奏の音響」になるため、特徴空間の SN 比が劇的に上がる。
全工程 Windows ネイティブで完結する (WSL2 不要)。

## コード地図（別セッション向けの最短把握ガイド）

| 触る目的 | 場所 |
|---|---|
| OLTW のアルゴリズム本体・DP recurrence・lock-in / inertia 状態機械・**mismatch 検知 + 有界リカバリ** | `audio_score_follower/core/oltw_follower.py` |
| 配信される検出結果 (`FollowResult`) の構造 | 同上、ファイル上部 dataclass |
| 特徴量 (CENS + onset) のパラメータ・抽出経路・**融合コスト `fused_local_cost` / `OnsetNormalizer`** | `audio_score_follower/core/feature_extractor.py`（**オフラインビルドと本番が共有する唯一の経路**） |
| ref_time ↔ score_time ↔ measure の変換・**warp path 検証** | `audio_score_follower/core/warp_lookup.py` |
| マイク dBFS 監視 + silence 判定 | `audio_score_follower/core/audio_level.py` |
| Tkinter 起動・キーバインド・silence-gate poll・trigger 発火ループ・**ランタイムジャンプ検出** | `audio_score_follower/main.py` |
| **Google Slides 自動操作**（Playwright/Chromium、キュー経由の thread-safe キー送出、`NullSlideController`） | `audio_score_follower/core/slide_controller.py` |
| トリガーの小節単位クールダウン（再発火抑止） | `audio_score_follower/core/cooldown_timer.py` |
| FluidSynth / SoundFont 実行ファイル検出の一本化 | `audio_score_follower/core/synth_locator.py` |
| `asf-follow` エントリポイント（`main.main()` への薄い shim） | `audio_score_follower/cli/follow.py` |
| GUI のレイアウト・モード表示・演奏開始ボタン | `audio_score_follower/ui/gui_tkinter.py` |
| **起動ランチャー GUI**（config 引数なし起動時。デバイス列挙・フォーム） | `audio_score_follower/ui/launcher.py` |
| 起動オプションの検証・CLI/ランチャー共通ロジック・**`settings.launcher` の永続化** | `audio_score_follower/launch_options.py`（Tk/sounddevice 非依存の純ロジック） |
| GUI ↔ ワーカースレッド間で共有される atomic 状態 | `audio_score_follower/core/state_manager.py` (`AppState`) |
| config.json のパース・`oltw_kwargs` デフォルト・**`loopback_device`** | `audio_score_follower/config/loader.py` |
| マイク (`FollowerWorker`) / ファイル (`FileWorker`) / **WASAPI ループバック** のスレッド管理 | `audio_score_follower/core/follower_worker.py` |
| **実験資産: 全域観測ベイズフィルタ**（本番不使用・eval の `--follower posterior` 専用。「確信度の二本立てと特徴量の判別能」参照） | `audio_score_follower/core/posterior_follower.py` |
| オフラインビルド (`asf-build`) + **ビルド時 warp path 検証** + **合成 BPM 自動推定** + `--cens-win` | `audio_score_follower/cli/build_reference.py` + `core/reference_builder.py` |
| スコア合成 WAV 生成 (MusicXML → music21 → MIDI → FluidSynth) | `tasks/generate_score_wav.py` |
| **追従品質のヘッドレス計測**（カバレッジ / ジャンプ / stall 統計、パラメータ A/B、`--follower` 切替） | `tasks/eval_tracking.py` |
| **表示確信度**（コストベース。`display_confidence_from_cost` + `_DISPLAY_CONF_COST_LO/HI`） | `audio_score_follower/main.py`（GUI 反映は `state_manager.set_display_confidence` → `gui_tkinter`） |

ユーザーが「**ボタンの挙動を変えたい**」と言ったら `ui/gui_tkinter.py` 単発で済むことが多い。「**追随ロジックが暴走する**」なら `core/oltw_follower.py` の DP recurrence と band 計算。「**測度がずれる**」なら `core/warp_lookup.py` か `core/score_mapper.py`。「**マイクで動かない**」なら `core/audio_level.py` と `_check_silence_gate` (main.py)。「**warp path 検証が落ちる**」なら `core/warp_lookup.py` の `validate()` と、スコアの繰り返し構造を確認する。

## OLTW の状態機械（最重要・別セッション必読）

`OnlineDTWFollower` は以下の状態を持つ。これを正しく理解せずに触ると lock-in / inertia の安全弁を壊しやすい。

### 二段構えの lock-in

| 段階 | `_locked_in` | `freeze()` の意味 |
|---|---|---|
| 冒頭・初期化中 | `False` | **位置固定**（旧来挙動）。冒頭ノイズで誤った位置から慣性外挿しないため |
| 曲の開始を捉えた以降 | `True` | **慣性進行**（直近 rate で位置を前進） |

lock-in 判定:
- `_live_frame_idx > init_search_width`（デフォルト 30 フレーム = 冒頭探索完了）
- かつ **smoothed** confidence ≥ `lock_in_confidence` (=0.45) が `lock_in_frames` (=30) 連続成立
- **単調ラッチ**（一度立てたら降りない。降ろすには `reset()` のみ）
- GUI「▶ 演奏開始」ボタン / **L キー**（= `main.manual_start()`）で強制的に立てることも可能。ただし**マイクモードの初回押下は演奏開始の宣言のみで `force_lock_in()` は呼ばない**（「silence gate / 手動スタート」参照）。2 回目以降の押下・wav/loopback モードで `force_lock_in()` が呼ばれる

### 慣性 (inertia) のトリガー

**`freeze()` のみ** が慣性入りのトリガー。lock-in 後に `freeze()` されると `_inertia_active=True` になり、その後の `process_frame()` は DP を裏で走らせつつ表示位置は慣性で前進させる。

**低 DP confidence 単独では慣性に入らない**。これは過去の regression 対応:
> オーケストラの pp passage は DP が正しい位置を低マージンで追えている状態が多い。慣性で上書きすると別演奏のカバレッジが 100% → 34% に落ちる regression を実測。低 conf 自動 entry を完全削除した。

### `_current_ref_pos` / `_inertia_ref_pos` / `_display_ref_pos` の三層分離

- **`_current_ref_pos`**（int）: DP-owned anchor。DP の更新ロジックのみが変更する
- **`_inertia_ref_pos`**（float）: 慣性中の表示位置。`_advance_inertia()` のみが変更する
- **`_display_ref_pos`**（float）: **表示スルー層**。通常追従中、表示が DP 位置を追いかける速度を `max(display_min_advance=2.0, rate × display_slew_factor=3.0)` frame/frame に制限する出力段レートリミッタ。stall 後の DP キャッチアップ（最大 `max_advance_per_frame`=50 frame ≈4.6s を 1 フレームで）が表示上のテレポートにならない。フレーム駆動なので live を追い越せない（慣性の安全弁 #1 と同じ論法）。`display_slew_factor: 0` で無効化（生 DP 表示）
- **公開プロパティ `current_ref_frame`**: 慣性 active なら `int(_inertia_ref_pos)`、slew 有効なら `int(_display_ref_pos)`、それ以外は `_current_ref_pos`。生 DP 位置は `dp_ref_frame` プロパティ / `FollowResult.dp_ref_frame` で参照

**snap-vs-slew ルール**: `reset()` / `seek()` / 初期アライメント / global rematch / post-seek catchup / rapid-reset catchup / 慣性 resync（seek 経由）という**意図的テレポートでは表示も即スナップ**。スルーがかかるのは通常追従の DP 前進のみ。frozen 中・慣性中のフレームでは `_display_ref_pos` を出力値に同期させ stale gap を残さない。freeze() の慣性開始位置は（slew 有効時）`_display_ref_pos` から始める — DP anchor から始めると freeze 境界で表示が前方ジャンプするため。

**低 conf 適応キャップ**（`low_conf_advance_frames`、デフォルト 0=無効）: 低 conf 連続時に `max_advance_per_frame` を `max(low_conf_advance_min, ceil(rate × low_conf_advance_factor))` に絞る DP 側オプション。前フレームの streak カウンタを使うので DP 再構成なし。幻想4 実測では有意差なしのため無効で出荷。

**計測**: `python tasks/eval_tracking.py --built-dir <dir> --score <mxl> --input-wav <wav> [--oltw-kwargs '{...}']` でヘッドレスにカバレッジ / ジャンプ / stall / 前進 stddev を測る。パラメータ変更の回帰確認はこれで行う（同録音 100% / conf 0.93+、別演奏 97%+ がベースライン。幻想4 実測: slew で別演奏の前進 stddev 0.66→0.42、カバレッジ・conf 無回帰）。**ベースライン数値は幻想4 の特定 built dir / 入力 wav に対する実測値**であり、他曲の絶対基準ではない — 新曲では最初に同録音 / 別演奏の 2 種を流して曲固有のベースラインを取ってから比較する。

**触ってはいけない**: `_advance_inertia()` で `_current_ref_pos` を書き換えると DP の band がずれ、stuck_dp_reset 後の D_prev 初期化が狂って DP が壊れる。過去にこの罠にハマった。

### 慣性中の安全弁

`live-score-sync` の `inertia_engine.py` が「テンポ外挿が演奏を追い越して measure 1 に snap back する」regression で捨てられた経緯がある。本実装はこれを構造的に防ぐ 4 つの安全弁を持つ:

1. **物理上限**: live frame 1 つにつき ref を `+inertia_rate` だけ進める。経過時間ではなくフレーム数駆動なので、構造的に live より速くなれない
2. **rate の制限**: clamp `[0.3, 2.0]`、`_last_good_rate` キャッシュで quick re-entry にも安定
3. **絶対 cap**: `max_inertia_seconds` (=10.0) を越えたら位置固定に戻す
4. **慣性中は `_try_global_rematch` を抑制**: スコア全体探索による遠方ジャンプを禁止（自己類似テーマへの誤テレポート防止）

### stuck_dp_reset と rapid_dp_reset

後退アトラクタ（`D_prev[pos-1] < D_prev[pos] + penalty`）が形成されると DP は monotonicity clamp で固定されたまま前進できなくなる。2 つの逃げルートがある:

| 機構 | 発火条件 | 発火タイミング |
|---|---|---|
| `stuck_dp_reset` | 直近ウィンドウで前進 < 3 フレーム **かつ** 後退試行 ≥ ウィンドウ/4 | `stuck_dp_reset_seconds`（既定 12s）後 |
| `rapid_dp_reset` | 10 フレーム**連続**で argmin が後退を指す（= 毎フレーム後退試行） | ~0.93s 後（即時） |

`rapid_dp_reset` は「純後退アトラクタ」の確定シグナル（毎フレーム後退）にのみ発火する。slow-forward（DP がゆっくり前進しながら偶発的に後退を試みる）では `_consecutive_backward_frames` カウンタが非後退フレームでリセットされるため発火しない。

**触ってはいけない**: rapid reset の発火後は `D_prev[:current_ref_pos]=inf, D_prev[current_ref_pos]=0` にリシードされる。このリシードを省くと後退アトラクタが残り、次フレームでまた即 rapid reset が発火してしまう。

**過去に試して捨てた**: rapid reset 後に「stall 中経過した live frame 数だけ前方ローカル探索してジャンプ」する catchup を実装したが、`raw_cost ~ 0.20` の曖昧 chroma 区間で 15 frame 程度の探索ではノイズと真のマッチを区別できず、誤ジャンプ → 監視 → 再 rapid reset → 誤ジャンプ … のサイクルで遅延を悪化させた（実測: 終端 m=176 → m=172 への regression）。代替案として discriminability ratio ガードを加えたり cost margin を厳しくする方向も考えられるが、まだ実装していない。stall ごとの ~10 frame の永続遅延は当面受け入れる。

### mismatch 検知 + 有界前方リカバリ（2026-07 追加）

stuck/rapid reset は「前進が止まった」ときしか発火しない。**前進しながらずれている**状態（junk 入力上の marching、大きなオフセット）は絶対コストで検知する（`_update_mismatch`、oltw_follower.py）:

- **検知**: smoothed cost（`_cost_history` 平均）> `mismatch_cost_threshold`(0.18) が `mismatch_seconds`(8s) **連続**で `_mismatch_active=True`。lock-in 済み・非 frozen・非慣性のフレームのみカウント。校正根拠: 別演奏（正解）の最長連続超過は 5.4s → 8s 持続で**誤検知ゼロ**（この校正が最重要ゲート。曲が変わったら「別演奏の閾値超え最長連続秒数 < mismatch_seconds」を必ず再確認）
- **フラグ中**: `FollowResult.is_mismatched` → main がトリガー抑止 + GUI「⚠ 追随ずれ疑い」表示。解除はヒステリシス（threshold−0.03 を ~1s）または任意の意図的テレポート
- **リカバリ**: 1s ごとに `_probe_decisive_forward_match` を前方 90s 窓で呼ぶ。**四重ガード**: ①cost margin 0.08 ②discriminability ratio 0.75 ③**絶対 ceiling 0.08**（matched 帯のみ。過去の catchup 失敗は相対ガードのみだったため）④**2 連続 probe の位置整合**（候補が演奏進行 ~1.0 rate と整合。瞬時コストの裾が偶発的に ceiling を割っても、1s 後に整合位置で再発しない限り跳ばない — 違う楽章入力での誤テレポート 2 件をこれで根絶した実測あり）
- **クリア箇所**: `_anchor_dp_at`（全テレポート経路）・`freeze()`・`reset()`・ヒステリシス解除の全てで streak/flag/pending をクリア。手動 seek 後に残ったずれは 8s 後に再検知され probe がリトライする（= ワンショット post-seek catchup の「リトライあり」版）
- **実測の限界（幻想4）**: ①白色ノイズはコスト帯が matched と重なり検知不能 ②ずれ先が自己類似箇所だと局所コストが本当に一致し、検知もリカバリも原理的に不能（offset-60 実験: probe 候補が反復テーマの 2 クラスタ間で振れ、ガードが正しく棄却。検知フラグも「別の提示部で本当にマッチ」して解除される）。**この限界を閾値緩和で破ろうとしないこと** — 別演奏の誤検知ゼロが崩れる

### unfreeze 後の DP 復帰経路

`unfreeze()` は `_frozen=False` のみ立てて、**`_inertia_active=True` のまま残す**。これが「前後マッチングして復帰」の実体。後続フレームの `_process_subsequent_frame` で:

1. DP は通常通り走る（裏で `_current_ref_pos` が進む）
2. 表示は引き続き `_inertia_ref_pos` を見せる
3. `_maybe_resync_from_dp()` が、DP confidence ≥ `lock_in_confidence` を `inertia_exit_frames` (=3) 連続成立 **かつ** DP 位置と慣性位置のギャップが `inertia_resync_max_gap_frames`（None なら `search_width`）以内のときに `seek(dp_pos, allow_catchup=True)` を呼ぶ
4. `seek()` は既存の post-seek catchup を armed にして DP を慣性位置に再 anchor
5. これにより `_inertia_active=False` に戻り、通常追従に snap back する

## GUI の状態反映パス

```
OLTW worker thread
  └─ _on_oltw_result (main.py)
       ├─ state.update_beat_measure(...)
       ├─ state.set_confidence(...)
       └─ state.set_follower_mode(is_locked_in, is_in_inertia,
                                  inertia_elapsed_sec, inertia_cap_sec)

GUI main thread (100ms poll)
  └─ FollowerGUI._poll_state
       └─ update_display
            └─ _render_follower_mode(state)
                 ├─ mode ラベルの色/文字を切り替え
                 └─ 「▶ 演奏開始」ボタンを is_locked_in に応じて pack/forget
```

手動スタートの待機状態は `state.waiting_for_start`（`set_waiting_for_start()`）で GUI に伝わり、`_render_follower_mode` が「⏸ 開始待ち」表示と「▶ 演奏開始」ボタンを制御する。

**触ってはいけない**: `AppState` は複数スレッドから atomic に更新される。フィールドを足すときは `set_xxx()` メソッドを 1 つ追加して原子化する（直接代入は避ける）。`get_all()` の戻り辞書に新フィールドも入れる。

## ワークフロー管理

### 1. プランモードのデフォルト
- 非自明なタスク (3 ステップ以上 / アーキテクチャ判断) は必ずプランモードに入る
- 問題が出たら STOP して再計画する

### 2. サブエージェント戦略
- リサーチ・探索・並列分析はサブエージェントへ
- 1 タスク 1 担当

### 3. 完了前の検証 (必須)
- 動作を証明せずに「完了」と言わない
- **修正→実行→確認ループ**: コードを直したら必ず自分で実行する
- LLM/ツール呼び出し系の修正は `-v` (verbose) で DEBUG ログを見る
- テストが存在する箇所を触ったら `python -m pytest tests/ -q` を通す（uv は PATH に入っていない環境がある）
- OLTW を変更したら **少なくとも `tests/test_oltw_follower.py` の全ケース** が通ることを確認（具体的なテスト数はここに書かない — 腐るため。`pytest tests/test_oltw_follower.py -q` の結果が正）

### 4. 自律的なバグ修正
- バグ報告を受けたらそのまま修正する — 手取り足取りの説明は不要
- CI が落ちていたら指示されなくても直す

### 5. ドキュメント同期
- 機能変更 (CLI オプション・出力構造・パイプラインの変更等) は **同 PR 内で README も更新**
- 別 PR / 別 Issue に分けない
- このファイル (`CLAUDE.md`) も、状態機械や制約の変更時は同時に更新する

### 6. GitHub 同期
- 当面 GitHub との同期はしない (姉妹プロジェクトの方針を踏襲)

### 7. 提案の独立検証
- ユーザの指示でも、コード・テスト・履歴を確認して前提が正しいか独立に検証する
- 特に OLTW の閾値変更提案 (e.g. 「lock_in_confidence を下げよう」) は、別演奏 file-input でカバレッジ regression が出ないかを `--input-wav <別演奏>` で必ず実測する

## スコア・参照音源の構造整合性（必須前提）

**スコア、参照音源（リファレンス録音）、本番ライブ入力の 3 つは繰り返し構造と総小節数が一致していなければならない。** これが一致しない状態でビルドしても OLTW は正常に追随できない。

**この前提の検出可能範囲に注意**: `スコア ↔ 参照音源` の不一致はビルド時 `validate()` が検出できるが、**`参照音源 ↔ 本番ライブ` の不一致（本番で指揮者がリピートの取り方を変える等）はビルド時にもランタイムにも検出・回復する仕組みがない**。OLTW は黙って stall→rapid reset→誤追随のいずれかに陥る。本番前に「当日の演奏はどのリピートを取るか」を確認し、参照録音と一致しない場合はリピート構造を合わせた参照で再ビルドするのが唯一の対策。

### なぜ一致が必要か

MrMsDTW が `スコア合成 WAV ↔ リファレンス録音` を対応付ける際、スコアに繰り返しがあってリファレンスが省略していると、多くのスコア小節を数秒の ref_time に押し込む極端な勾配の warp path が生成される。OLTW が追随中にその区間を通過すると、1 フレーム (~93ms) で数十小節ジャンプとして観測される。

### 合成 BPM の自動推定 (`asf-build`)

`asf-build` は `--score-bpm` 未指定なら、スコアの総ビート数 (`ScoreMapper.get_total_beats()`) と参照録音の duration から四分音符 BPM を逆算する:

```
estimated_bpm = total_beats * 60.0 / ref_duration_sec
```

実例: 幻想4 (178 measures × 4 beats = 712 beats) / 281.04s ≈ 152 BPM。楽譜指示の 120 BPM で固定合成すると、ベルリン・フィルのような速い演奏 (281s) との 27% のテンポ差が MrMsDTW のスキップ大量発生 (21% のステップで score 側のみ進む) を招き、`WarpLookup.validate` (slope 4.0x 上限) で失敗する。

実装は `audio_score_follower/cli/build_reference.py` の `_estimate_score_bpm()` / `_probe_reference_duration()`。`librosa.get_duration(path=...)` で全ロードせず duration だけ取得する。`--start-offset` 指定時はその分を差し引いた duration で推定する。

### 末尾無音の自動トリム（`--end-trim` / `_detect_tail_silence_sec`）

参照録音の末尾無音・拍手（ピーク−45dB 以下が 1.5s 超）は `asf-build` が自動検出してビルド前にカットする（`_detect_tail_silence_sec()`、`librosa.effects.trim` の tail 側のみ使用）。**トリムしないと 2 つの故障が同時に起きる**:

1. BPM 推定の分母（ref_duration）が水増しされ合成テンポが遅くなる
2. MrMsDTW がスコアの最終小節群を無音尾部にマップする → 無音区間の CENS はどの演奏ともマッチしないので、**runtime はどの入力でも最後の数小節に到達できない**

実測: 幻想4 の Berlin 参照録音は末尾 8.23s が無音で、トリム前は全入力が m=173/178 で頭打ち（BPM 152.0）。トリム後は同録音・別演奏 2 種すべて 178/178（BPM 156.6）。トリム量は `build_meta.json` の `reference_end_trim_sec` に永続化。`--end-trim <秒>` で手動指定、`--end-trim 0` で無効化。`build_reference()` には `reference_end_trim_sec` kwarg として渡る（`--start-offset` と同型の tail 版）。

**症状からの逆引き**: 「追随は最後まで正常なのに、最終小節の数小節手前で頭打ちになる」場合はまずこれを疑う。eval CSV の末尾で conf が高く 1:1 前進のまま入力が尽きていたら、参照側の warp が無音に食われている。

**ルール**:
- `--score-bpm` 明示指定: その値を使う（手動オーバライド）
- `--score-bpm` 省略 & `--score-wav` なし: 上式で推定。推定値は `build_meta.json` の `score_bpm` フィールドに永続化される
- `--score-bpm` 省略 & `--score-wav` 指定: **エラー終了**（事前合成 WAV のテンポは録音から逆推定できない）
- 推定値が `[20, 400] BPM` の sanity range 外: ERROR で停止し、明示指定を促す
- 参照 duration が 5.0s 未満: 推定不可エラー

**触ってはいけない**: `build_reference()` の `score_bpm` 引数シグネチャは変更せず、CLI 側で解決した値を渡すだけ。`tasks/generate_score_wav.py` も `--bpm` をそのまま受け取る既存 IF を流用。

### ビルド時検証 (`asf-build` / `WarpLookup.validate()`)

`asf-build` の最終ステップで `WarpLookup.validate(score_mapper)` が呼ばれる（`core/warp_lookup.py`）。2 種類のチェックを実施:

1. **勾配チェック** (`max_slope=4.0×` デフォルト): 1 秒幅の参照時間窓でスコア時間の進み量が 4× を超えたら ValueError。繰り返し省略・カット・構造違いを検出する。
2. **カバレッジチェック** (`max_coverage_diff_measures=5` デフォルト): warp path 末尾の小節番号とスコア総小節数の差が 5 を超えたら ValueError。

どちらかで失敗すると `asf-build` は exit code 1 で終了する（ビルド成果物は保存されるが使用禁止）。

```
ERROR - Warp path validation FAILED:
  warp path に異常な勾配があります: 参照音源の 77.0s-78.0s (1.0s) が
  スコアの 95.8s 分 (小節 58-112) に対応しています (slope=95.8x, 上限=4.0x)。
  参照音源とスコアの繰り返し構造（リピート・カット）が一致していない可能性があります。
```

### ロード時検証 (asf-follow 起動時)

`main._load_movement()` でも同じ `warp.validate(score_mapper)` を呼ぶ。ビルド後にスコアだけ差し替えるなどのミスを防ぐ。失敗すると `state.set_load_error()` が設定され OLTW が起動しない。

### スコアの繰り返し記号の扱い

MusicXML の繰り返し記号 (`<barline><repeat direction="forward/backward"/></barline>`) は MuseScore CLI の合成時に展開される。参照録音が繰り返しを省略している場合は、**スコアから繰り返し記号を削除した MXL を用意**して `asf-build --score` に渡す。繰り返し削除版とオリジナルは別ファイルとして管理し、`config.json` の `xml_file` には繰り返し削除版を指定すること。

### ランタイムジャンプ検出

正常ビルドのロードであっても、万一 3 小節を超える突発ジャンプが起きた場合は `_on_oltw_result` が ERROR ログを出力する (`_MAX_FRAME_MEASURE_JUMP = 3`、ユーザー操作直後の 2 秒はグレースピリオド)。これは warp path が正常でも OLTW の DP が一時的に外れた場合の早期検出用。

## このプロジェクト特有の注意点

### 特徴量の同一性と CENS+onset 融合
- オフラインビルドと本番 OLTW で **CENS と onset の両方のパラメータ** (sr, hop_length, win_length, log compression 係数, onset の正規化窓) が完全一致していないと DTW が外れる
- `audio_score_follower/core/feature_extractor.py` を **唯一の経路** として、両側から呼ぶ
- パラメータを変えたらビルド済み `reference_cens.npy` / `reference_onset.npy` も作り直す (`asf-build` を再実行)
- `WarpLookup` は `built_dir` から `feature_config` を読んで OLTW に注入する（手で渡さない）

**融合コスト**（`fused_local_cost`、feature_extractor.py）:

```
cost[k] = chroma_weight × (1 − <cens_ref[:,k], live_cens>)
        + onset_weight  × |onset_ref[k] − live_onset|
```

- 重みは `config.json` の `settings.feature_fusion`（`chroma_weight` / `onset_weight`、既定 **0.7 / 0.3**、`ConfigLoader.get_feature_fusion()`）。両方 ≥ 0 かつ和 > 0 が必須（違反時は warning + デフォルトに戻す）
- 参照側 onset は `asf-build` が `reference_onset.npy`（global-max 正規化済み）として保存。**欠損時は CENS-only に自動フォールバック**（旧ビルドとの後方互換）
- ライブ側 onset の正規化は `OnsetNormalizer.for_config()`（`LIVE_ONSET_WINDOW_SEC` = 5 秒の rolling-max 窓）に一本化。ワーカーは楽章ごとに作り直されるため窓は自然にリセットされる（インスタンスを長寿命化する場合は `reset()` が必要）
- **触ってはいけない #1**: `LIVE_ONSET_WINDOW_SEC` を変えると参照側（global-max）とライブ側の onset スケールの対応が崩れ、融合距離のバランスが黙って狂う
- **触ってはいけない #2**: fusion 非アクティブ時（onset_weight=0 / reference_onset 欠損）のコストは **chroma_weight を掛けない生 cosine 距離のまま通す**。OLTW の `step_penalty` / `lock_in_confidence` は cost ∈ [0, 2] のスケールでチューニングされており、ここで chroma_weight を掛けると閾値が黙ってリスケールされる（feature_extractor.py に理由コメントあり）

### 確信度の二本立てと特徴量の判別能（2026-07 実測。再実験の前に必読）

**確信度は 2 本ある**:

| | 算出 | 用途 |
|---|---|---|
| 内部 confidence（`FollowResult.confidence`） | band 内の match_score × margin（band **相対**値） | lock-in・トリガー床 (0.30)・慣性復帰。**チューニング済みスケールなので触らない** |
| 表示 confidence（`state.display_confidence`） | 融合コスト 5 フレーム平滑を `_DISPLAY_CONF_COST_LO=0.05 → HI=0.22` で 1→0 に線形写像（**絶対**マッチ品質。`main.display_confidence_from_cost`） | GUI 表示のみ |

分離した理由: 非負 chroma 同士の cosine には床があり（無関係な音でも cos 0.5–0.8）、内部 confidence は**無関係な入力でも 0.6–0.8 に張り付く**。「無関係なピアノ BGM で確信度 70%」という操作者の混乱の正体はこれで、特徴量の失敗ではない（下記実測参照）。LO/HI は幻想4 実測から校正: 同録音 cost p50=0.014 / 別演奏 0.082 / 違う楽章 0.189 / 無関係ピアノ 0.300。曲や録音条件が大きく変わったら eval CSV の `raw_local_cost` 分布で再校正する。

**特徴量の判別能の実測結果**（幻想4、eval_tracking + 合成ピアノ BGM で計測）:

- **無関係なピアノ BGM は特徴空間で明確に分離できている**（cost 0.300 vs matched 0.082 の 3.7 倍）。「特徴を掴めていない」わけではなく、旧表示式が cost 0.30 → 70% に写像していただけ
- **本質的に重なるのは「同一オケの別の楽章」vs「別演奏の正解楽章」**（junk p10 0.095 vs matched p90 0.159）。この重なりは特徴パラメータでは解消できないことを確認済み:
  - onset 重み {0.7/0.3→0.6/0.4→0.5/0.5}: separation 改善なし（−0.064→−0.063→−0.072）
  - `cens_win` {41→21→11}: separation 改善なし（matched と junk のコストが同時に上がるだけ）。**既定 41 を維持**。`asf-build --cens-win` は実験用に残置（build_meta 経由でランタイム自動伝播）
- 白色ノイズ的な広帯域音は全ピッチクラスを含むため表示確信度も中間値（~50%）になる（既知の限界）

**実験資産 `core/posterior_follower.py`**（全域観測ベイズフィルタ）: 「ずれの検知・訂正の根本解決」として実装し A/B したが、別演奏の滑らかさ（jump 8 vs OLTW 0）・ノイズ耐性・オフセット復帰のすべてで OLTW を上回らず**既定化見送り**。オケの自己類似（行進曲テーマの反復）では全域観測が誤マッチ源になり、対策（バンド拘束・近傍優先・有界リカバリ）を入れると OLTW の設計に収束する、が結論。本番経路は無改変（`main.py` は `OnlineDTWFollower` 固定）。テストは `tests/test_posterior_follower.py`、駆動は `eval_tracking --follower posterior` のみ。

### 合成は FluidSynth で行う (Windows ネイティブ)
- `tasks/generate_score_wav.py` は **FluidSynth** を使う。MuseScore 4 は v4.6.5 で CLI バッチモードがハングするバグがあり使用不可。
- フロー: MusicXML → music21 でテンポ正規化 → MIDI → FluidSynth → WAV。合成は 12 分の曲で約 1 分。
- music21 で XML のテンポマーキングを剥がし、冒頭に単一 MetronomeMark を入れてから MIDI エクスポート
- **FluidSynth / SoundFont の検出は `core/synth_locator.py` に一本化**（`find_fluidsynth()` / `find_soundfont()`。build_reference.py と generate_score_wav.py の両方がここを呼ぶ — 検出順を変えるときはここだけ触る）。検出順: プロジェクト内 `vendor/FluidSynth/*/bin/fluidsynth.exe` → 環境変数 `FLUIDSYNTH_EXE` → PATH → 既知パス → `LocalAppData\FluidSynth\...\fluidsynth.exe`。SF ファイルは環境変数 `SF_FILE` → `C:\Program Files\MuseScore 4\sound\MS Basic.sf3` → MuseScore 3 付属 SF3
- **FluidSynth インストール推奨手順**: GitHub releases から `fluidsynth-vX.Y.Z-win10-x64-glib.zip` を取得してプロジェクトの `vendor/FluidSynth/` に展開する。LocalAppData に置くと Windows Defender / Controlled Folder Access に削除されるケースあり (実例: 過去に発生)。`vendor/` は `.gitignore` 済み。
- WSL2 は **不要**。オフラインビルドも本番追随も Windows で完結する

### リハ録音冒頭の指揮ブレス
- リハ録音冒頭の指揮者ブレス・椅子の音は **スコアに無い**ので、warp が外れやすい
- `asf-build --start-offset` で先頭を手動カットする運用

### silence gate / 手動スタート / 慣性進行（3 段構えの誤スタート防御）

マイクモードの「演奏前の誤追随」は 3 層で防ぐ:

1. **手動スタート（マイクモードのみ）**: 起動・楽章ロード直後は `main._performance_started=False` で OLTW を常時 freeze し gate を無視（「▶ 演奏開始」ボタン / L キー = `main.manual_start()` を押すまで一切動かない）。押下で gate 統治に移行。**初回押下では `force_lock_in()` を呼ばない**（早押し時に慣性が無音上を走るため。lock-in は音楽を捉えてから自動ラッチ）。2 回目以降の押下・wav/loopback モードでは従来の強制 lock-in として機能。押しズレ補正: 早押しは gate が持続音まで freeze 維持、遅押しは pre-lock-in unfreeze の armed catchup + `start_search_seconds`（既定 10s）の初回探索幅拡大で実位置に着地
2. **gate ヒステリシス + one-shot 統治**（`audio_level.py` の `_callback` 内状態機械 + `main._check_silence_gate`）: 開くには `gate_activation_sec`（既定 0.7s）の連続音、閉じるには `gate_release_sec`（既定 0.3s）の連続無音。断続ノイズは連続条件のリセットで蓄積しない。**gate が freeze/unfreeze を統治するのはスタート押下から最初の gate 開放まで**（Issue #13 対応）: 最初の持続音で `main._performance_confirmed=True`（演奏確定）になり、以後 gate は freeze を一切発火しない（レベル表示のみ更新）。静かに始まる楽章（幻想4 の 4 楽章冒頭等）は音量が閾値を跨いで上下し、gate close のたびに pre-lock-in rewind が前進を破棄して永遠に lock-in できないため。確定後の弱奏・休符は DP がそのまま追う（pp passage は DP が低マージンで正しく追える実測に基づく）。`_performance_confirmed` は楽章ロード（`_load_movement`）で False にリセット。ヘッドレステストは `tests/test_silence_gate.py`
3. **pre-lock-in rewind**（`oltw_follower.py`）: lock-in 前の gate 開放中の前進は「仮」。`unfreeze()` 時に `_pre_lockin_resume_pos` をスナップショットし、次の pre-lock-in `freeze()` で前進していたら `_reseed_at(snapshot)` で巻き戻す（conf streak もクリア、`_post_seek_catchup_pending` を arm）。**lock-in（自動 or 強制）= point of no return** で以後は巻き戻さない。`seek()` は snapshot をクリアする（手動 seek を gate close が黙って巻き戻さないため）。※one-shot 統治（上記 2）の導入後、マイクモードでは最初の gate 開放以降 freeze が来ないため、この rewind が実際に発火するのは稀（機構は OLTW 側の安全弁としてテストとともに維持）

- マイク dBFS が `silence_threshold_db` を下回ると `AudioLevelMonitor` が gate を立て、`main._check_silence_gate` 経由で `oltw.freeze()` を呼ぶ
- **pre-lock-in の `unfreeze()` は DP を anchor に再シードする**（`_reseed_at`）。これは必須: マイクモードは初フレーム前から frozen になるため `_D_prev` が全 inf のままで、再シードなしだと argmin が均一 inf band の rightmost tie-break になり**音声と無関係に毎フレーム +max_advance_per_frame 暴走**する（「カウントが止まらない」の主犯）。post-lock-in の unfreeze は DP 状態を保持（慣性 resync の前提）
- `freeze()` の挙動は **lock-in の前後で意味が変わる**（上記「OLTW の状態機械」参照）
- `--input-wav` モードおよび `--loopback` モードでは silence gate を完全に無効化する（マイクが開かれていないため polling すると常に -120 dBFS になる）
- 旧 `live-score-sync` の `inertia_engine.py` を踏襲したくなる衝動は捨てること。経過時間外挿は live より速くなる罠にハマる。本実装は frame 駆動

### WASAPI ループバックモード (`--loopback`)

PC のスピーカー / ヘッドホン出力をそのまま OLTW の入力音源として使うモード。GUI テストや音源ファイル再生しながら追随動作を確認する際に使う。

- `FollowerWorker.start()` が `sd.InputStream(..., extra_settings=sd.WasapiSettings(loopback=True))` で開く
- デバイスは `--loopback-device <index or name>` で指定。省略時は `sd.default.device[1]`（OS デフォルト出力）
- `config.json` の `settings.loopback_device` でもデフォルトを固定できる（`ConfigLoader.get_loopback_device()`）
- Windows WASAPI 専用機能。Linux では使えない
- `--loopback` と `--input-wav` は **相互排他**（どちらも実 OLTW 入力源だから）
- silence gate は無効になる（loopback は常に音声あり前提）

### ファイル入力中の音声再生 (`--play-audio`)

`--input-wav <file>` でファイルを OLTW に流しながら、同じ音声をスピーカーから再生するモード。テスト時に耳でもスコアとのズレを確認できる。

- `FileWorker._worker_loop()` が `sd.play(audio, sample_rate)` でノンブロッキング再生
- `FileWorker.stop()` で `sd.stop()` を呼ぶ
- `--play-audio` は `--input-wav` がなければエラー（`main.py` で起動時に検証）
- `--play-audio` と `--loopback` の組み合わせは不要（loopback 側が再生音を直接 capture する）

### 起動ランチャー GUI (`ui/launcher.py` + `launch_options.py`)

- `python -m audio_score_follower.main`（config 引数なし）でランチャー GUI が開き、選択内容は選択された config.json の `settings.launcher` ブロック + 既存フラットキー（`mic_device` / `loopback_device` / `silence_threshold_db` / `cooldown_seconds`）に保存される
- **CLI モード（config 引数あり）は `settings.launcher` を無視する**（後方互換のための意図的な仕様。README にも明記済み）
- `launch_options.py` は Tk / sounddevice を import しない純ロジック層。CLI とランチャーの検証・`--input-wav` ファイル名解決を共有する（`tests/test_launch_options.py` でヘッドレステスト可能）
- **`LaunchOptions.input_wav` を直接 app に渡さない**。ランチャーは wav 欄の前回値をソース切替後も保持・保存するため、`input_source=mic` でも `input_wav` が非 None になり得る。必ず `opts.effective_input_wav`（wav モード以外は None）を使うこと。渡してしまうと wav モードで起動し「マイクを選んだのに監視無効・ボタンなしで自動追随」になる（実際に発生したバグ）
- config の保存は raw JSON の read→update→write（`ConfigLoader` 経由にしない — movements 検証で保存がブロックされるため）。`ensure_ascii=False` で日本語を保持、tempfile + `os.replace` でアトミック
- デバイスは index + 名前スナップショットの両方を保存し、次回起動時に名前一致で index を再マッチ（Windows はデバイス番号が変動するため）
- **無音測定ボタン**: `AudioLevelMonitor` を再利用して暗騒音を測定し、`compute_silence_threshold()` (`launch_options.py`) が `median + (median − p10) + 2dB`（`margin_db` 既定値。Issue #19 で 3dB → 2dB にさらに緩和）で閾値を算出する。**スプレッド床は置かない**（Issue #19 で旧 MIN_SPREAD_DB=6dB を撤廃）: 暗騒音が安定した環境では床が支配して閾値が median+9dB になり、弱音の入力が閾値を越えられず **gate が永遠に開かず追随が始まらない**（実測: threshold=-12.9 dBFS / median=-21.9）。手動スタート + one-shot gate 統治（Issue #13）導入後は誤スタート抑止の主役が手動スタートに移り、gate 誤開放が影響するのは開始押下〜最初の持続音の短い窓だけ（しかも開放には `gate_activation_sec` の連続音が必要）なので、「開かない」失敗の方が致命的。gate は「音量 ≤ 閾値で freeze」なので閾値は暗騒音分布の**上側**に置く必要がある。低いパーセンタイル（下位10%等）をそのまま閾値にすると gate がほぼ発動しなくなり、高いパーセンタイル（95% 等）は測定中の偶発音（咳・会話）で吊り上がる — 下側分布幅の鏡映はその両方を回避する設計。入力ソースの選択にかかわらず常に押せる（測定は常にマイクデバイスから。無効化すると「保存済み config が wav モードだとボタンが押せない」という混乱を生んだ実績があるため、ソース連動の disable は再導入しないこと）

### 単体テストの方針
- OLTW のテストは `np.zeros(12)` を chroma に流して「確実に低 confidence」を作る（ランダム unit vector はたまたま match することがある）
- 慣性挙動のテストは「freeze→unfreeze→低 conf chroma を 30 frame」のように **freeze 経由** で慣性に入れる
- `confidence_smoothing=5` の窓により freeze/unfreeze 直後 1〜2 frame に残響 confidence が残ることがあるので、resync の挙動を見るテストでは `_maybe_resync_from_dp` を mock すること
- 回帰の最終確認は `--input-wav` で同録音 (coverage 100% / conf 0.95+) と別演奏 (coverage 96-100%) の 2 種を流す

## 既知の落とし穴（過去にハマった罠）

1. **`_advance_inertia()` で `_current_ref_pos` を書き換える** → DP band がずれて stuck_dp_reset 後の D_prev 初期化が狂い、DP が壊れる。`_inertia_ref_pos` のみ更新すること
2. **低 conf 自動 inertia 入り** → pp passage で DP の正解位置を慣性 1.0 で上書きし、別演奏カバレッジが 34% に落ちる。**絶対に追加しない**
3. **`unfreeze()` で `_inertia_active=False` に即時クリア** → 表示位置が freeze 時点から DP の停滞位置に backward ジャンプして見える。unfreeze は `_frozen` のみクリアし、慣性は DP resync 経由で抜けさせる
4. **慣性中も `_try_global_rematch` を許可** → 自己類似テーマ（運命冒頭、行進曲動機）で遠方の繰り返しに誤テレポート。`if not self._inertia_active` ガードを必ず維持する
5. **rate fallback 1.0 で resync 後すぐ慣性再入** → `_pos_history` が clear されて 1.0 fallback、overshoot 蓄積、resync gap > search_width で永続復帰不能。`_last_good_rate` キャッシュで救済している
6. **GUI ボタンを毎フレーム pack/forget する** → 100ms ごとに geometry recalc が走って重い。`_button_visible` フラグで差分時のみ操作する
7. **スコアに繰り返し記号を残したまま asf-build を実行** → スコア合成 WAV が繰り返し展開されて参照録音の 2 倍近い長さになり、warp path に極端な勾配が発生（実例: slope=95.8×）。ビルド時検証が即座に検出するが、根本解決はスコアから繰り返し記号を削除した MXL を使うこと
8. **`--loopback` と `--input-wav` を同時に指定** → 両方とも OLTW の実入力源なので相互排他。`main.py` で起動時に検証してエラーにする
9. **`--play-audio` を `--input-wav` なしで使用** → `sd.play()` に渡すデータがないためエラー。`main.py` で起動時に検証する
10. **`freeze()` / `unfreeze()` の中から `seek()` を呼ぶ** → 両者とも `_state_lock` を取るが `threading.Lock` は非再入なので**デッドロック**。ロック保持中の再アンカーは必ず `_reseed_at()`（lock-free ヘルパー、呼び出し元がロック保持）を使う
11. **pre-lock-in `unfreeze()` の DP 再シードを省く** → 初フレーム前から frozen だったケースで `_D_prev` 全 inf のまま DP が走り、毎フレーム +max_advance_per_frame の暴走（音声と無関係にカウントが進み続ける）

## 基本原則
- **シンプルさ優先**: 変更は最小限に
- **手抜きなし**: 根本原因を見つける
- **提案の独立検証**: ユーザの指示でも、コード・テスト・履歴を確認して前提が正しいか独立に検証する
