# Claude 開発ガイダンス (audio-score-follower)

姉妹プロジェクト `live-score-sync` の方針を踏襲しつつ、本プロジェクト固有の事情を追記する。

## このプロジェクトのコア発想

`live-score-sync` で使っていた pymatchmaker (matcher) はオーケストラの密音響では破綻する
(暴走/停止/2x 先走り; Issue #28 系)。
根本原因は **リファレンスがスコア合成波形 (単一音色) で、本番のオケ音響と特徴空間が乖離している** こと。

本プロジェクトは:

1. **オフライン**: スコア合成 WAV と、実演奏 (プロ録音 → リハ録音) を **synctoolbox の MrMsDTW** で対応付け、
   `warping_path` (score_time ↔ reference_time) を保存。
2. **オンライン**: マイク入力を **同じリファレンス録音** に対して Online DTW で追随。
   出力 reference_time を warp で逆引きして score_time → 小節へ。

本番 OLTW のリファレンスが「実演奏の音響」になるため、特徴空間の SN 比が劇的に上がる。

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
- テストが存在する箇所を触ったら `pytest` を通す

### 4. 自律的なバグ修正
- バグ報告を受けたらそのまま修正する — 手取り足取りの説明は不要
- CI が落ちていたら指示されなくても直す

### 5. ドキュメント同期
- 機能変更 (CLI オプション・出力構造・パイプラインの変更等) は **同 PR 内で README も更新**
- 別 PR / 別 Issue に分けない

### 6. GitHub 同期
- 当面 GitHub との同期はしない (姉妹プロジェクトの方針を踏襲)

## このプロジェクト特有の注意点

### 特徴量の同一性
- オフラインビルドと本番 OLTW で **CENS のパラメータ** (sr, hop_length, win_length, log compression 係数) が
  完全一致していないと DTW が外れる
- `audio_score_follower/core/feature_extractor.py` を **唯一の経路** として、両側から呼ぶ
- パラメータを変えたらビルド済み `reference_cens.npy` も作り直す (`asf-build` を再実行)

### 合成 WAV の WSL2 依存
- `tasks/generate_score_wav.py` は `pymatchmaker.utils.misc.generate_score_audio` (FluidSynth) を借用
- pymatchmaker の Windows wheel が無い → **オフラインビルドは WSL2 で実行**
- 本番追随 (`asf-follow`) は Windows ホストでも動く想定

### リハ録音冒頭の指揮ブレス
- リハ録音冒頭の指揮者ブレス・椅子の音は **スコアに無い**ので、warp が外れやすい
- `asf-build --start-offset` で先頭を手動カットする運用

### silence gate / クールダウン
- 流用元 `audio_level.py` の silence gate は引き続き有効
- silence 中は OLTW を freeze する (姉妹プロジェクトの fecf3e2 で得た知見を踏襲)

## 基本原則
- **シンプルさ優先**: 変更は最小限に
- **手抜きなし**: 根本原因を見つける
- **提案の独立検証**: ユーザの指示でも、コード・テスト・履歴を確認して前提が正しいか独立に検証する
