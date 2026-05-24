# Claude 開発ガイダンス (audio-score-follower)

姉妹プロジェクト `live-score-sync` の方針を踏襲しつつ、本プロジェクト固有の事情を追記する。

このファイルは **別セッションの Claude が冷えた状態から仕様を把握するための地図** を兼ねる。「どのファイルが何を担当しているか」「なぜそうなっているか」「触ってはいけない箇所」を網羅すること。

## このプロジェクトのコア発想

`live-score-sync` で使っていた pymatchmaker (matcher) はオーケストラの密音響では破綻する
(暴走/停止/2x 先走り; Issue #28 系)。
根本原因は **リファレンスがスコア合成波形 (単一音色) で、本番のオケ音響と特徴空間が乖離している** こと。

本プロジェクトは:

1. **オフライン**: スコア合成 WAV (MuseScore 4 CLI でレンダリング) と、実演奏
   (プロ録音 → リハ録音) を **synctoolbox の MrMsDTW** で対応付け、
   `warping_path` (score_time ↔ reference_time) を保存。
2. **オンライン**: マイク入力を **同じリファレンス録音** に対して Online DTW で追随。
   出力 reference_time を warp で逆引きして score_time → 小節へ。

本番 OLTW のリファレンスが「実演奏の音響」になるため、特徴空間の SN 比が劇的に上がる。
全工程 Windows ネイティブで完結する (WSL2 不要)。

## コード地図（別セッション向けの最短把握ガイド）

| 触る目的 | 場所 |
|---|---|
| OLTW のアルゴリズム本体・DP recurrence・lock-in / inertia 状態機械 | `audio_score_follower/core/oltw_follower.py` |
| 配信される検出結果 (`FollowResult`) の構造 | 同上、ファイル上部 dataclass |
| 特徴量 (CENS) のパラメータ・抽出経路 | `audio_score_follower/core/feature_extractor.py`（**オフラインビルドと本番が共有する唯一の経路**） |
| ref_time ↔ score_time ↔ measure の変換 | `audio_score_follower/core/warp_lookup.py` |
| マイク dBFS 監視 + silence 判定 | `audio_score_follower/core/audio_level.py` |
| Tkinter 起動・キーバインド・silence-gate poll・trigger 発火ループ | `audio_score_follower/main.py` |
| GUI のレイアウト・モード表示・楽章開始ボタン | `audio_score_follower/ui/gui_tkinter.py` |
| GUI ↔ ワーカースレッド間で共有される atomic 状態 | `audio_score_follower/core/state_manager.py` (`AppState`) |
| config.json のパース・`oltw_kwargs` デフォルト | `audio_score_follower/config/loader.py` |
| マイク (`FollowerWorker`) / ファイル (`FileWorker`) のスレッド管理 | `audio_score_follower/core/follower_worker.py` |
| オフラインビルド (`asf-build`) | `audio_score_follower/cli/build_reference.py` + `core/reference_builder.py` |
| MuseScore 4 CLI 連携 (合成 WAV 生成) | `tasks/generate_score_wav.py` |

ユーザーが「**ボタンの挙動を変えたい**」と言ったら `ui/gui_tkinter.py` 単発で済むことが多い。「**追随ロジックが暴走する**」なら `core/oltw_follower.py` の DP recurrence と band 計算。「**測度がずれる**」なら `core/warp_lookup.py` か `core/score_mapper.py`。「**マイクで動かない**」なら `core/audio_level.py` と `_check_silence_gate` (main.py)。

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
- GUI「▶ 楽章開始」ボタン / **L キー** で `force_lock_in()` を呼んで強制的に立てることも可能

### 慣性 (inertia) のトリガー

**`freeze()` のみ** が慣性入りのトリガー。lock-in 後に `freeze()` されると `_inertia_active=True` になり、その後の `process_frame()` は DP を裏で走らせつつ表示位置は慣性で前進させる。

**低 DP confidence 単独では慣性に入らない**。これは過去の regression 対応:
> オーケストラの pp passage は DP が正しい位置を低マージンで追えている状態が多い。慣性で上書きすると別演奏のカバレッジが 100% → 34% に落ちる regression を実測。低 conf 自動 entry を完全削除した。

### `_current_ref_pos` と `_inertia_ref_pos` の分離

- **`_current_ref_pos`**（int）: DP-owned anchor。DP の更新ロジックのみが変更する
- **`_inertia_ref_pos`**（float）: 慣性中の表示位置。`_advance_inertia()` のみが変更する
- **公開プロパティ `current_ref_frame`**: 慣性 active なら `int(_inertia_ref_pos)`、それ以外は `_current_ref_pos`

**触ってはいけない**: `_advance_inertia()` で `_current_ref_pos` を書き換えると DP の band がずれ、stuck_dp_reset 後の D_prev 初期化が狂って DP が壊れる。過去にこの罠にハマった。

### 慣性中の安全弁

`live-score-sync` の `inertia_engine.py` が「テンポ外挿が演奏を追い越して measure 1 に snap back する」regression で捨てられた経緯がある。本実装はこれを構造的に防ぐ 4 つの安全弁を持つ:

1. **物理上限**: live frame 1 つにつき ref を `+inertia_rate` だけ進める。経過時間ではなくフレーム数駆動なので、構造的に live より速くなれない
2. **rate の制限**: clamp `[0.3, 2.0]`、`_last_good_rate` キャッシュで quick re-entry にも安定
3. **絶対 cap**: `max_inertia_seconds` (=10.0) を越えたら位置固定に戻す
4. **慣性中は `_try_global_rematch` を抑制**: スコア全体探索による遠方ジャンプを禁止（自己類似テーマへの誤テレポート防止）

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
                 └─ 「▶ 楽章開始」ボタンを is_locked_in に応じて pack/forget
```

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
- OLTW を変更したら **少なくとも `tests/test_oltw_follower.py` 全 31 ケース** が通ることを確認

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

## このプロジェクト特有の注意点

### 特徴量の同一性
- オフラインビルドと本番 OLTW で **CENS のパラメータ** (sr, hop_length, win_length, log compression 係数) が完全一致していないと DTW が外れる
- `audio_score_follower/core/feature_extractor.py` を **唯一の経路** として、両側から呼ぶ
- パラメータを変えたらビルド済み `reference_cens.npy` も作り直す (`asf-build` を再実行)
- `WarpLookup` は `built_dir` から `feature_config` を読んで OLTW に注入する（手で渡さない）

### 合成は MuseScore 4 CLI で行う (Windows ネイティブ)
- `tasks/generate_score_wav.py` は MuseScore 4 の CLI (`mscore --export-to`) を呼ぶ
- music21 で XML のテンポマーキングを剥がし、冒頭に単一 MetronomeMark を入れて定テンポ合成を保証する
- 検出: 環境変数 `MSCORE_EXE` → PATH → 既知パス (`C:\Program Files\MuseScore 4\bin\MuseScore4.exe` 等)
- WSL2 は **不要**。オフラインビルドも本番追随も Windows で完結する

### リハ録音冒頭の指揮ブレス
- リハ録音冒頭の指揮者ブレス・椅子の音は **スコアに無い**ので、warp が外れやすい
- `asf-build --start-offset` で先頭を手動カットする運用

### silence gate / 慣性進行
- マイク dBFS が `silence_threshold_db` を下回ると `AudioLevelMonitor` が gate を立て、`main._check_silence_gate` 経由で `oltw.freeze()` を呼ぶ
- `freeze()` の挙動は **lock-in の前後で意味が変わる**（上記「OLTW の状態機械」参照）
- `--input-wav` モードでは silence gate を完全に無効化する（マイクが開かれていないため、polling すると常に -120 dBFS になる）
- 旧 `live-score-sync` の `inertia_engine.py` を踏襲したくなる衝動は捨てること。経過時間外挿は live より速くなる罠にハマる。本実装は frame 駆動

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

## 基本原則
- **シンプルさ優先**: 変更は最小限に
- **手抜きなし**: 根本原因を見つける
- **提案の独立検証**: ユーザの指示でも、コード・テスト・履歴を確認して前提が正しいか独立に検証する
