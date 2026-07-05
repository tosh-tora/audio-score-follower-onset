# audio-score-follower リファクタリング計画書

**対象リポジトリ**: `C:\Users\toshi\projects\audio-score-follower-onset`
**計画時点の HEAD**: `2dbb9b5` (branch: `fix/silence-gate-oneshot-issue13`)
**計画時点のテスト状態**: `python -m pytest tests/ -q` → **136 passed**（全グリーン、実測済み）
**方針**: 挙動を 1 ビットも変えない純リファクタリング。機能追加・仕様変更・依存更新は一切しない。

> 行番号はすべて計画時点のもの。先行項目のコミットで行番号はずれるため、**必ずシンボル名（関数名・クラス名）で再特定**すること。行番号は「近傍のあたり」を付けるためだけに使う。

---

## 1. 現状理解（実行者への文脈共有）

### 1.1 このアプリが何をするか

オーケストラのライブ演奏をマイクで聴き取り、楽譜上の「現在の小節」をリアルタイムに推定して、指定小節に到達したら Google Slides のページ送りを自動で行うアプリ。2 段階マッピングが核:

1. **オフライン** (`asf-build` = `cli/build_reference.py` → `core/reference_builder.py`):
   楽譜 MusicXML を FluidSynth で定テンポ WAV に合成 (`tasks/generate_score_wav.py`) し、実演奏録音と MrMsDTW (synctoolbox) で対応付けて `warping_path.npz` + `reference_cens.npy` + `reference_onset.npy` を保存。
2. **オンライン** (`asf-follow` = `main.py`): マイク入力を CENS 特徴量に変換し、**同じリファレンス録音**に対して Online DTW (`core/oltw_follower.py`) で追随。出力 ref_time を warp で逆引き (`core/warp_lookup.py`) → beat → 小節 (`core/score_mapper.py`)。

### 1.2 モジュール構造マップ

```
audio_score_follower/
├── main.py                 エントリポイント + AudioScoreFollowerApp（GUI 起動、
│                           silence-gate poll、trigger ループ、手動操作、楽章ロード）
├── launch_options.py       起動オプション純ロジック層（Tk/sounddevice 非依存）
├── config/loader.py        config.json パース + oltw_kwargs デフォルト
├── core/
│   ├── oltw_follower.py    ★中核・1675 行。Online DTW の DP recurrence +
│   │                       lock-in / inertia / display-slew の状態機械
│   ├── feature_extractor.py CENS / onset 抽出（オフラインと本番の唯一の共有経路）
│   ├── warp_lookup.py      ref_time↔score_time↔measure 変換 + warp path 検証
│   ├── score_mapper.py     MusicXML → beat↔measure マップ (partitura)
│   ├── follower_worker.py  FollowerWorker(マイク/loopback) + FileWorker(--input-wav)
│   ├── audio_level.py      マイク dBFS 監視 + gate ヒステリシス
│   ├── state_manager.py    AppState（スレッド間共有のロック付き状態）
│   ├── slide_controller.py Playwright で Google Slides を操作
│   ├── cooldown_timer.py   トリガのレート制限
│   └── reference_builder.py オフラインビルド本体（MrMsDTW 実行、成果物保存）
├── cli/
│   ├── build_reference.py  asf-build CLI（BPM 自動推定、末尾無音トリム、検証）
│   └── follow.py           asf-follow の薄いラッパ
└── ui/
    ├── gui_tkinter.py      運用者 GUI（大きい小節番号、モードパネル、開始ボタン）
    └── launcher.py         起動ランチャー GUI（config 引数なし起動時）

tasks/
├── generate_score_wav.py   MusicXML → music21 → MIDI → FluidSynth → WAV
└── eval_tracking.py        追従品質のヘッドレス計測（回帰確認の基盤）

tests/  136 ケース（oltw 50+、launch_options 40+、feature_extractor、
        audio_level、silence_gate、warp_lookup、smoke_reference_builder）
```

### 1.3 依存の向き（重要）

- `main.py` → config/loader, core/*, ui/gui_tkinter, launch_options
- `ui/launcher.py` → launch_options, core/audio_level, ui/gui_tkinter(`_pick_font_family` のみ)
- `cli/build_reference.py` → core/reference_builder, core/score_mapper, core/warp_lookup、**subprocess で** `tasks/generate_score_wav.py` を起動
- `tasks/eval_tracking.py` → sys.path 挿入で `audio_score_follower` を import
- `tasks/generate_score_wav.py` は現状 **audio_score_follower に依存しない**（standalone スクリプト）
- `core/oltw_follower.py` ← follower_worker, main, eval_tracking から呼ばれる。**プロジェクトで最も繊細**。CLAUDE.md の「触ってはいけない」リストが集中する

### 1.4 CLAUDE.md の絶対制約（違反したら壊れる、実測に基づく）

以下は本計画のどの項目でも侵してはならない。詳細な理由はリポジトリの `CLAUDE.md` にある:

1. `_advance_inertia()` で `_current_ref_pos` を書き換えない
2. 低 confidence だけで inertia に入るロジックを追加しない
3. `unfreeze()` で `_inertia_active` を即時クリアしない
4. inertia 中の `_try_global_rematch` を許可しない（`if not self._inertia_active` ガード維持）
5. rapid reset 後の `D_prev` リシード（`D_prev[:pos]=inf, D_prev[pos]=0`）を省かない
6. GUI ボタンを毎フレーム pack/forget しない（`_button_visible` 差分制御を維持）
7. `freeze()`/`unfreeze()`（`_state_lock` 保持中）から `seek()` を呼ばない（非再入ロックでデッドロック）。ロック保持中の再アンカーは `_reseed_at()` 系を使う
8. pre-lock-in `unfreeze()` の DP 再シードを省かない
9. `build_reference()` の `score_bpm` 引数シグネチャを変えない。`generate_score_wav.py` の `--bpm` 受け取り IF を変えない
10. CENS パラメータ（`FeatureConfig`）に触れない — ビルド済み成果物が無効になる

### 1.5 計画時点の git 状態（項目 0 で処理する）

作業ツリーは**クリーンではない**:
- `.gitignore` 修正（`config/*` を ignore に追加）… 未ステージ
- `config/fantastique4.json` / `config/fantastique5.json` の削除がステージ済み（ファイル自体はディスク上に残っている = 追跡だけ外す意図）

これはユーザーが意図した「config をローカル設定として git 管理外にする」変更。リファクタコミットに混ぜないこと（項目 0 参照）。

---

## 2. 項目 0: 安全網の構築（最初に必ず実行）

### 0-a. 既存の未コミット変更を退避コミット

```
git add .gitignore
git commit -m "chore: config/ を per-machine 設定として git 管理外に"
```

コミット後 `git status` がクリーンであること（本計画書 `REFACTORING_PLAN.md` が untracked で残るのは問題ない。コミットに含めるかはユーザーの任意）。`config/*.json` がディスクに残っていること（`ls config/` で確認）。

### 0-b. 作業ブランチ作成

```
git checkout -b refactor/cleanup-2026-07
```

### 0-c. ベースライン確認

```
python -m pytest tests/ -q
```

期待: **136 passed**（warning は無視してよい）。1 つでも fail したら**中断してユーザーに報告**（この計画の前提が崩れている）。

続けて起動系のスモーク（すべて exit 0 でヘルプが出ること）:

```
python -m audio_score_follower.main --help
python tasks/generate_score_wav.py --help
python tasks/eval_tracking.py --help
python -c "import audio_score_follower.main, audio_score_follower.ui.launcher"
```

### 0-d. 特性テストの追加（AppState — 現状テストが無い箇所）

RF-05/RF-06 が `state_manager.py` を触るため、先に現状の挙動を固定する。`tests/test_state_manager.py` を新規作成し、以下をそのままテスト化する:

```python
"""AppState の特性テスト（リファクタ前の挙動固定）。"""
from audio_score_follower.core.state_manager import AppState


def test_get_all_returns_expected_keys():
    state = AppState()
    snap = state.get_all()
    # リファクタ後にキー集合が変わる場合はこのテストも同一コミットで更新する
    assert {"measure", "beat", "beat_in_measure", "confidence",
            "cooldown_active", "next_trigger_measure", "mic_level_db",
            "silence_gate_active", "mic_monitor_available",
            "silence_threshold_db", "waiting_for_start", "is_locked_in",
            "is_in_inertia", "inertia_elapsed_sec", "inertia_cap_sec",
            "movement_id", "xml_file", "movement_number", "total_movements",
            "total_measures", "load_error"} <= set(snap.keys())


def test_update_beat_measure_roundtrip():
    state = AppState()
    state.update_beat_measure(12.5, 4, 2.5)
    snap = state.get_all()
    assert snap["beat"] == 12.5
    assert snap["measure"] == 4
    assert snap["beat_in_measure"] == 2.5


def test_confidence_clamped():
    state = AppState()
    state.set_confidence(1.7)
    assert state.get_all()["confidence"] == 1.0
    state.set_confidence(-0.2)
    assert state.get_all()["confidence"] == 0.0


def test_set_movement_resets_playback_state():
    state = AppState()
    state.update_beat_measure(50.0, 20, 3.0)
    state.set_load_error("dummy")
    state.set_movement(movement_id=2, xml_file="x.mxl", triggers=[],
                       movement_number=2, total_movements=4, total_measures=100)
    snap = state.get_all()
    assert snap["measure"] == 1 and snap["beat"] == 0.0
    assert snap["load_error"] is None
    assert snap["total_measures"] == 100


def test_set_follower_mode():
    state = AppState()
    state.set_follower_mode(is_locked_in=True, is_in_inertia=True,
                            inertia_elapsed_sec=3.5, inertia_cap_sec=10.0)
    snap = state.get_all()
    assert snap["is_locked_in"] and snap["is_in_inertia"]
    assert snap["inertia_elapsed_sec"] == 3.5


def test_cooldown_activate_sets_flag_immediately():
    state = AppState()
    state.activate_cooldown(60.0)  # 長時間にして自動クリアがテスト中に走らないようにする
    assert state.get_all()["cooldown_active"] is True
    state.deactivate_cooldown()
    assert state.get_all()["cooldown_active"] is False
```

さらに `tests/test_config_loader_defaults.py` を新規作成（RF-04 の回帰検出用）:

```python
"""ConfigLoader.default_oltw_kwargs の特性テスト。"""
from audio_score_follower.config.loader import ConfigLoader
from audio_score_follower.core.oltw_follower import OnlineDTWFollower
import numpy as np
from audio_score_follower.core.feature_extractor import FeatureConfig


def test_default_oltw_kwargs_constructs_follower():
    """デフォルト kwargs が OnlineDTWFollower にそのまま渡せることを固定。
    （loader のキーと follower のシグネチャの同期ズレを検出する）"""
    kwargs = ConfigLoader.default_oltw_kwargs()
    ref = np.tile(np.eye(12, dtype=np.float32), 50)[:, :600]
    ref = ref / np.linalg.norm(ref, axis=0, keepdims=True).clip(1e-8)
    follower = OnlineDTWFollower(
        reference_cens=ref, feature_config=FeatureConfig(), **kwargs
    )
    assert follower.n_ref_frames == 600


def test_default_oltw_kwargs_key_values():
    kwargs = ConfigLoader.default_oltw_kwargs()
    assert kwargs["search_width"] == 240
    assert kwargs["max_advance_per_frame"] == 50
    assert kwargs["lock_in_confidence"] == 0.45
```

```
python -m pytest tests/ -q      # 136 + 新規 8 = 144 passed を確認
git add tests/test_state_manager.py tests/test_config_loader_defaults.py
git commit -m "test: AppState / oltw_kwargs デフォルトの特性テストを追加（リファクタ前の挙動固定）"
```

### 0-e. FluidSynth 検出結果の記録（RF-10 の照合用）

```
python -c "import sys; sys.path.insert(0, '.'); from audio_score_follower.cli.build_reference import _find_fluidsynth, _find_sf_file; print('fs:', _find_fluidsynth()); print('sf:', _find_sf_file())"
python -c "import sys; sys.path.insert(0, 'tasks'); import generate_score_wav as g; print('fs:', g.find_fluidsynth() if True else None)" || echo "(fluidsynth 未インストールなら FileNotFoundError で可)"
```

出力（見つかったパス、または None / エラー）をメモしておく。RF-10 完了後に同じコマンドで**同一の結果**になることを確認する。

---

## 3. 作業項目リスト（実行順）

優先順位の考え方: **[削除系（リスク最小・効果は行数と認知負荷の削減）] → [OLTW 内の重複除去（効果大・テスト充実でリスク中）] → [横断的な重複除去] → [命名・小粒]** の順。各項目は独立に 1 コミット。

---

### RF-01: デッドコード削除 — `_try_rapid_reset_catchup`

- **対象**: `audio_score_follower/core/oltw_follower.py:1516-1589`（`def _try_rapid_reset_catchup`）
- **問題**: 呼び出し箇所ゼロの死んだメソッド（74 行）。CLAUDE.md に「rapid reset 後の catchup は実装したが誤ジャンプ連鎖で捨てた」と明記されている機構の残骸。読者が「これはどこから呼ばれるのか」と探して時間を失う。
- **変更**: メソッド全体を削除。**それ以外は 1 文字も変えない**（rapid reset 本体 `_process_subsequent_frame` 内の `_RAPID_RESET_FRAMES` ブロックは呼び出していないので無関係。触らない）。
- **事前確認**: `grep -rn "_try_rapid_reset_catchup" --include="*.py" .` → 定義行のみヒットすること（計画時点で確認済み）。
- **完了条件**: `python -m pytest tests/ -q` → 144 passed。`grep` 再実行でヒット 0。
- **リスク/戻し方**: リスクほぼゼロ。失敗時 `git revert <commit>`。
- **依存**: 項目 0。

---

### RF-02: デッドコード削除 — feature_extractor の未使用 API 3 点

- **対象**: `audio_score_follower/core/feature_extractor.py`
  - `compute_cens_streaming` (132-149 行)
  - `cosine_cost_matrix` (152-170 行)
  - `AudioFeatures` dataclass (275-312 行)
- **問題**: 3 つとも production コードから一切呼ばれていない（テストからのみ参照 or 完全未参照。計画時点で grep 確認済み）。「将来の拡張用」コメント付きだが、fusion の実配線は `fused_local_cost` 経由で完結しており、この 3 つは通っていない。
- **変更**:
  1. 上記 3 シンボルを削除。
  2. `tests/test_feature_extractor.py` から対応テストを削除: `test_cosine_cost_matrix_self_zero`, `test_cosine_cost_matrix_dimension_check`, `test_audio_features_aligned_truncate`、および import 行の `AudioFeatures`, `cosine_cost_matrix`（`compute_cens_streaming` はテスト未参照）。
  3. `oltw_follower.py:527` の docstring 内 `compute_cens_streaming` への言及を `compute_cens` に修正（`process_frame` の Args 説明）。
  4. `feature_extractor.py` モジュール docstring と `AudioFeatures` に言及するコメントがあれば同時に整合（grep: `grep -rn "AudioFeatures\|compute_cens_streaming\|cosine_cost_matrix" --include="*.py" --include="*.md" .` で残存参照ゼロにする。CLAUDE.md / README にこれらの名前は出てこないはずだが必ず確認）。
- **完了条件**: pytest 全緑（144 − 3 = **141 passed**）。grep で 3 名の残存参照ゼロ。
- **リスク/戻し方**: 低。外部スクリプトがこの API を使っている可能性は、リポジトリ内 grep で否定済み。失敗時 `git revert`。
- **依存**: 項目 0。

---

### RF-03: デッドコード削除 — ConfigLoader の未使用メソッド 4 点

- **対象**: `audio_score_follower/config/loader.py`
  - `previous_movement` (165-170 行)
  - `get_movement_triggers` (412-421 行)
  - `get_xml_file_for_movement` (423-433 行)
  - `get_built_dir_for_movement` (435-445 行)
- **問題**: 4 つとも呼び出し箇所ゼロ（テスト含め。計画時点で grep 確認済み）。live-score-sync からのフォーク時の残骸。
- **変更**: 4 メソッドを削除。`get_current_movement` / `next_movement` / `current_movement_number` / `total_movements` / `_auto_discover_mxl` は**使用中なので残す**。
- **事前確認**: `grep -rn "previous_movement\|get_movement_triggers\|get_xml_file_for_movement\|get_built_dir_for_movement" --include="*.py" .` → loader.py の定義のみ。
- **完了条件**: pytest 141 passed。`python -m audio_score_follower.main --help` が exit 0。
- **リスク/戻し方**: 低。`git revert`。
- **依存**: 項目 0。

---

### RF-04: デッドパラメータ削除 — `inertia_enter_frames`

- **対象**:
  - `audio_score_follower/core/oltw_follower.py`: `__init__` シグネチャ (100 行)、docstring (235-238 行)、バリデーション (430-431 行)、`self._inertia_enter_frames = ...` (441 行)
  - `audio_score_follower/config/loader.py`: `get_oltw_kwargs` のデフォルト辞書 `"inertia_enter_frames": 5` とその直前のコメント「inertia_enter_frames is reserved for future use.」(353-354 行)
- **問題**: 値は検証・保存されるが**どこからも読まれない**（低 conf 自動 inertia 入りを削除した際の残骸。CLAUDE.md にもその経緯が明記されている）。「このパラメータを調整すれば挙動が変わる」という誤解を招く。
- **変更**: 上記 4 箇所を削除。`inertia_exit_frames`（実際に使用中）は触らない。
- **事前確認**:
  - `grep -rn "inertia_enter_frames" --include="*.py" --include="*.json" --include="*.md" .` — oltw_follower.py と loader.py 以外にヒットが無いこと。**特に `config/*.json` にこのキーが無いこと**（計画時点で確認済み: 両 config の `oltw_kwargs` は空）。もし README.md / CLAUDE.md にヒットしたら該当行も同コミットで削除・修正する。
- **注意**: loader のデフォルト辞書と `OnlineDTWFollower.__init__` の**両方から**消すこと。片方だけ消すと `TypeError: unexpected keyword argument` で起動不能になる（これを検出するのが項目 0-d の `test_default_oltw_kwargs_constructs_follower`）。
- **完了条件**: pytest 141 passed（特に `tests/test_config_loader_defaults.py` と `tests/test_oltw_follower.py` 全件）。grep でヒット 0。
- **リスク/戻し方**: 低〜中。ユーザーの手元 config.json が `oltw_kwargs.inertia_enter_frames` を持っていた場合のみ起動時 TypeError → `_load_movement` が catch して GUI にエラー表示（無言では壊れない）。計画時点の config には無いことを確認済み。失敗時 `git revert`。
- **依存**: 項目 0（0-d のテストが前提）。

---

### RF-05: AppState のレガシーフィールド削除 + GUI の死に widget 削除

- **対象**:
  - `audio_score_follower/core/state_manager.py`: `inertia_mode` (56 行), `inertia_tempo_bpm` (57 行), `set_inertia_mode` メソッド (195-207 行), `last_trigger_measure` (50 行), `last_trigger_time` (51, 291 行), `get_all` 内の `'inertia_mode'` / `'last_trigger_measure'` キー (109, 112 行), `set_movement` 内の `self.last_trigger_measure = None` (191 行), `__repr__` の `inertia={state['inertia_mode']}` (311 行)
  - `audio_score_follower/ui/gui_tkinter.py`: `label_inertia` の生成 (252-255 行) と `update_display` 内の常時空文字設定 (404-406 行)
- **問題**: すべて live-score-sync の InertiaEngine 時代の残骸。`set_inertia_mode` は呼び出しゼロ、`inertia_mode` は常に False、`last_trigger_measure` は None 以外が代入されることがなく、`last_trigger_time` は書くだけで誰も読まない。GUI の `label_inertia` は毎ポーリングで空文字を設定するだけの死に widget（現行のモード表示は `label_mode` パネルが担う）。
- **変更**:
  1. state_manager から上記フィールド・メソッド・get_all キーを削除。`__repr__` は `f"AppState(measure={...}, beat={...}, confidence={...})"` に修正。
  2. gui_tkinter から `label_inertia` の生成と参照を削除。
  3. `tests/test_state_manager.py`（項目 0-d で作成）の `test_get_all_returns_expected_keys` は部分集合アサーションなので**そのまま通る**。通ることを確認。
  4. 現在の慣性表示は `is_in_inertia` / `set_follower_mode` 系が担っている — **こちらには絶対に触れない**。
- **事前確認**: `grep -rn "inertia_mode\|set_inertia_mode\|inertia_tempo_bpm\|last_trigger_measure\|last_trigger_time\|label_inertia" --include="*.py" .` → state_manager.py と gui_tkinter.py のみにヒット（計画時点で確認済み）。
- **完了条件**: pytest 141 passed。`python -m audio_score_follower.main --help` exit 0。grep で全名称の残存ゼロ。
- **リスク/戻し方**: 低。AppState は複数スレッドから触られるが、削除するのは「誰も読まないフィールド」のみ。失敗時 `git revert`。
- **依存**: 項目 0（0-d）、RF-03 とはファイルが違うので順不同可だがこの順で。

---

### RF-06: AppState の `ui_update_event` 削除

- **対象**: `audio_score_follower/core/state_manager.py` の `self.ui_update_event = threading.Event()` (88 行) と、全 setter 末尾の `self.ui_update_event.set()`（12 箇所）
- **問題**: この Event を `wait()` / `is_set()` する消費者がリポジトリ内に一人もいない（GUI は 100ms の Tk timer ポーリングで `get_all()` を読む設計）。「イベント駆動で UI が更新される」と誤読させる純粋なノイズ。
- **変更**: 属性と全 `.set()` 呼び出しを削除。docstring の「Signals UI updates via event mechanism」記述 (5-6 行) も「GUI は 100ms ポーリングで get_all() を読む」旨に修正。
- **事前確認**: `grep -rn "ui_update_event" --include="*.py" .` → state_manager.py のみ（計画時点で確認済み）。
- **完了条件**: pytest 141 passed（test_state_manager.py 含む）。grep ヒット 0。
- **リスク/戻し方**: 低。`git revert`。
- **依存**: RF-05（同一ファイルなので順番に。RF-05 で消えた setter の分は対象外になっているだけで手順に影響なし）。

---

### RF-07: OLTW — DP 再アンカー処理の重複を共通ヘルパーに抽出

- **対象**: `audio_score_follower/core/oltw_follower.py`
  - `_try_global_rematch` 末尾 (690-697 行)
  - `_try_post_seek_catchup` 末尾 (1507-1514 行)
  - `_reseed_at` 冒頭 (1430-1435 行)
- **問題**: 「`_D_prev` 全消し → seed 設定 → band を 1 幅に → `_current_ref_pos` 更新 → `_display_ref_pos` snap」という 6 行のブロックがコピペで 3 回出現している（RF-01 実施前は 4 回）。将来 4 層目の位置状態を足すとき、1 箇所直し忘れる事故の温床。
- **変更**: lock-free ヘルパーを 1 つ追加し、3 箇所を置換する。

  ```python
  def _anchor_dp_at(self, ref_frame: int, seed_cost: float) -> None:
      """D_prev を全消しして ref_frame に seed_cost で再アンカーし、表示を snap する。

      lock-free ヘルパー: 呼び出し規約は _reseed_at と同じ
      （process_frame 内から、または _state_lock 保持中に呼ぶ）。
      """
      self._D_prev[:] = np.inf
      self._D_prev[ref_frame] = seed_cost
      self._prev_band_lo = ref_frame
      self._prev_band_hi = ref_frame + 1
      self._current_ref_pos = ref_frame
      self._display_ref_pos = float(ref_frame)  # intentional teleport: snap
  ```

  置換:
  - `_try_global_rematch`: `self._anchor_dp_at(new_pos, best_cost)`（元コードは seed に `best_cost` を入れている — **0.0 ではない**。そのまま維持）
  - `_try_post_seek_catchup`: `self._anchor_dp_at(new_pos, best_cost)`
  - `_reseed_at`: 冒頭 6 行を `self._anchor_dp_at(ref_frame, 0.0)` に置換。**それ以降の行（`_live_frame_idx` バンプ、`_cost_history.clear()`、stuck カウンタ群のリセット）は `_reseed_at` に残す**。ここを動かすと global_rematch / post_seek_catchup の挙動が変わってしまう（両者は stuck カウンタをリセットしない、が現行仕様）。
- **してはいけないこと**: `_anchor_dp_at` の中に stuck カウンタリセットや `_pre_lockin_resume_pos` クリアを「ついでに」入れない。呼び出し 3 箇所の副作用差分は現行仕様。
- **完了条件**: `python -m pytest tests/test_oltw_follower.py -q` 全件 + `python -m pytest tests/ -q` → 141 passed。特に `test_seek_jumps_to_target_and_resumes_dp` / `test_seek_with_catchup_finds_real_position_ahead` / `test_prelockin_rewind_discards_noise_window_advance` が通ること。
- **リスク/戻し方**: 中（OLTW の中核だが、変更は純粋な等価変形でテストが厚い）。失敗時 `git revert`。
- **依存**: RF-01（catchup メソッド削除後に行うことで置換箇所が 3 に減る）。

---

### RF-08: OLTW — forward-probe ガード（cost margin + discriminability）の重複抽出

- **対象**: `audio_score_follower/core/oltw_follower.py`
  - `_try_global_rematch` の探索+ガード部 (654-682 行)
  - `_try_post_seek_catchup` の探索+ガード部 (1478-1500 行)
- **問題**: 「window の local cost を計算 → argmin → cost margin ガード → median 比 discriminability ガード」がほぼ同一のまま 2 回書かれている。閾値の意味づけ（CLAUDE.md 記載の実測値 0.60-0.70 vs 0.95）が 2 箇所に分散。
- **変更**: ヘルパーを追加して両者から呼ぶ:

  ```python
  def _probe_decisive_forward_match(
      self,
      live: np.ndarray,
      min_pos: int,
      max_pos: int,
      current_cost: float,
      *,
      log_tag: str,
  ) -> Optional[tuple[int, float, float, float]]:
      """[min_pos, max_pos) から「決定的に良い」forward match を探す。

      2 つのガード（cost margin / discriminability ratio）を通過した場合のみ
      (new_pos, best_cost, ratio, median_cost) を返す。どちらかで棄却したら None。
      """
      if max_pos <= min_pos:
          return None
      costs = self._local_cost_block(min_pos, max_pos, live)
      best_offset = int(np.argmin(costs))
      best_cost = float(costs[best_offset])
      # Guard 1: must beat the current position.
      if current_cost - best_cost < self._stuck_rematch_cost_margin:
          return None
      # Guard 2: relative discriminability.
      median_cost = float(np.median(costs))
      if median_cost <= 0:
          return None
      ratio = (median_cost - best_cost) / median_cost
      if ratio < self._stuck_rematch_min_discriminability_ratio:
          logger.debug(
              "OLTW %s: skipping jump, low discriminability "
              "ratio %.2f (best %.3f, median %.3f)",
              log_tag, ratio, best_cost, median_cost,
          )
          return None
      return min_pos + best_offset, best_cost, ratio, median_cost
  ```

  - `_try_global_rematch`: 冒頭の `min_jump_pos >= self._N` 早期 return と、成功時の INFO ログ（ratio / median を含む既存文言）は**呼び出し側に残す**。probe が None なら `return False`、成功なら INFO ログ → `self._anchor_dp_at(new_pos, best_cost)` → `return True`。`log_tag="stuck-rematch"`。
  - `_try_post_seek_catchup`: `current_cost = self._local_cost_scalar(...)` の計算と成功時 INFO ログは呼び出し側に残す。`log_tag="post-seek catchup"`。
  - ログ文言は既存と**一字一句同じ**に保つ（運用ログの grep 互換のため）。Guard 1 と Guard 2 の**評価順序を入れ替えない**。
- **完了条件**: `python -m pytest tests/ -q` → 141 passed。特に `test_seek_with_catchup_finds_real_position_ahead` / `test_seek_without_catchup_stays_put` / `test_inertia_does_not_global_rematch`。
- **リスク/戻し方**: 中。等価変形だが分岐が多い。失敗時 `git revert`。
- **依存**: RF-07（`_anchor_dp_at` を使うため）。

---

### RF-09: OLTW — メソッド内マジックナンバーのモジュール定数化

- **対象**: `audio_score_follower/core/oltw_follower.py`
  - `_RAPID_RESET_FRAMES = 10`（`_process_subsequent_frame` 内 851 行 — メソッドの中で定義されている）
  - `margin_score = min(1.0, margin / 0.05)` の `0.05`（957 行）と degenerate band の `margin = 0.05`（956 行）
  - argmin タイブレークの `1e-6`（815 行）
- **問題**: チューニング上意味を持つ閾値がメソッド本文に埋まっており、探しにくい。特に confidence の margin 正規化 0.05 は「これを変えると lock_in_confidence の意味が変わる」レベルの結合を持つのに無名。
- **変更**: モジュール先頭（`logger = ...` の直後）に移動:

  ```python
  # ≈0.93 s at default 10.77 Hz frame rate — sustained pure-backward
  # attractor is declared after this many consecutive backward argmins.
  _RAPID_RESET_FRAMES = 10
  # Cost margin between best and second-best DP cell that counts as a
  # fully "decisive" minimum for confidence scoring. Coupled with
  # lock_in_confidence: rescaling this rescales effective confidence.
  _CONFIDENCE_FULL_MARGIN = 0.05
  # Tie-break tolerance for the in-band argmin (float32 noise floor).
  _ARGMIN_TIE_EPS = 1e-6
  ```

  使用箇所を定数参照に置換。既存の説明コメントはメソッド内から定数定義側へ移す（重複させない）。**値は変えない**。
- **完了条件**: pytest 141 passed（confidence 系: `test_self_alignment_high_confidence` ほか全件）。
- **リスク/戻し方**: 低。`git revert`。
- **依存**: RF-07/08 の後（同ファイルの行ズレ回避のため。技術的依存はない）。

---

### RF-10: FluidSynth / SoundFont 検出ロジックの一本化

- **対象**:
  - `audio_score_follower/cli/build_reference.py`: `_find_fluidsynth` (87-135 行), `_find_sf_file` (138-150 行)
  - `tasks/generate_score_wav.py`: `find_fluidsynth` (75-132 行), `find_sf_file` (135-164 行), `_FLUIDSYNTH_FIXED_CANDIDATES` / `_SF_CANDIDATES` (57-72 行)
- **問題**: 検出ロジック（vendor/ → 環境変数 → PATH → LocalAppData 複数手法 → 固定候補）が 2 ファイルにほぼ丸ごと重複（計 ~170 行）。過去に「Defender が LocalAppData の実体を消す」等で検出手順を何度も直した経緯があり、片側だけ直る事故が起きやすい。差分は (a) generate 側は explicit 引数を最優先し見つからなければ raise、build 側は Optional を返す、(b) generate 側は POSIX の固定候補も持つ、の 2 点のみ。
- **変更**:
  1. 新モジュール `audio_score_follower/core/synth_locator.py` を作成:

     ```python
     """FluidSynth 実行ファイルと SoundFont の検出（唯一の実装）。

     検出順序は実運用でのトラブル履歴に基づく（CLAUDE.md 参照）:
     vendor/ 最優先（Defender が LocalAppData の実体を消す事例があるため）。
     """
     from __future__ import annotations
     import logging, os, shutil, sys
     from pathlib import Path
     from typing import Optional

     logger = logging.getLogger(__name__)

     _REPO_ROOT = Path(__file__).resolve().parents[2]

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

     def find_fluidsynth(explicit: Optional[Path] = None) -> Optional[Path]:
         """探索順: explicit → vendor/ → FLUIDSYNTH_EXE → PATH → LocalAppData → 固定候補。
         見つからなければ None（raise しない — メッセージは呼び出し側の責務）。"""
         ...  # 既存 tasks/generate_score_wav.py の find_fluidsynth 本体を移植し、
              # raise の代わりに None を返す。explicit が存在しないパスの場合も
              # None を返し、呼び出し側でエラーメッセージを出す。

     def find_soundfont(explicit: Optional[Path] = None) -> Optional[Path]:
         """探索順: explicit → SF_FILE → 固定候補。見つからなければ None。"""
         ...
     ```

     本体は `tasks/generate_score_wav.py` の実装（LocalAppData の 3 手法探索を含む、より網羅的な側）をそのまま移植する。
  2. `tasks/generate_score_wav.py`:
     - ファイル冒頭（既存の `sys.stdout.reconfigure` ブロックの後）に eval_tracking.py:35 と同じパターンを追加:
       `sys.path.insert(0, str(Path(__file__).resolve().parents[1]))`
     - `find_fluidsynth` / `find_sf_file` / 候補リスト定数を削除し、`from audio_score_follower.core.synth_locator import find_fluidsynth, find_soundfont` に置換。
     - main() 側で None が返ったら**既存と同一文言**の FileNotFoundError 相当のエラーメッセージを `logger.error` して return 1（現行は関数内 raise → main が catch して return 1。ユーザーから見えるメッセージを変えない）。
     - explicit 引数が存在しないパスだった場合の既存メッセージ（`--fluidsynth-exe が存在しません: ...` / `--sf-file が存在しません: ...`）も維持する（呼び出し側で `args.fluidsynth_exe is not None and not args.fluidsynth_exe.exists()` を先にチェック）。
  3. `cli/build_reference.py`: `_find_fluidsynth` / `_find_sf_file` を削除し、locator を import して使用（こちらは従来どおり None 許容で、見つかったときだけ `--fluidsynth-exe` / `--sf-file` を subprocess に付ける）。
  4. 検出順序の差異吸収: build 側は従来 explicit 引数なし・POSIX 候補なしだったが、統一実装では explicit=None で呼ぶため順序は同一、POSIX 候補は Windows では exists()=False で素通りするだけ。**挙動差なし**と言えるが、完了条件の照合で実機確認する。
  5. `debug_fluidsynth.py` は診断用の独立スクリプトなので**触らない**。
- **完了条件**:
  1. pytest 141 passed。
  2. `python tasks/generate_score_wav.py --help` が exit 0。
  3. 項目 0-e で記録した検出結果と、次のコマンドの出力が一致:
     `python -c "from audio_score_follower.core.synth_locator import find_fluidsynth, find_soundfont; print('fs:', find_fluidsynth()); print('sf:', find_soundfont())"`
  4. `grep -rn "def _find_fluidsynth\|def find_fluidsynth\|def _find_sf_file\|def find_sf_file" --include="*.py" .` → synth_locator.py（と debug_fluidsynth.py が関数を持たないこと）のみ。
- **リスク/戻し方**: 中。`asf-build` の実行系（subprocess 連携）に触るため、可能なら実ビルドのスモーク（既存の `data/scores/*.mxl` + `data/reference_audio/*` があれば `asf-build --score ... --reference ... --output /tmp_test_build`）まで行うのが望ましいが、素材が無ければ 1-4 で完了とする。失敗時 `git revert`（新モジュールごと消える）。
- **依存**: 項目 0（0-e の記録）。

---

### RF-11: OnsetNormalizer 生成ロジック（5 秒窓）の一本化

- **対象**:
  - `audio_score_follower/core/follower_worker.py`: `FollowerWorker.__init__` (99-103 行) と `FileWorker.__init__` (384-388 行) の `window = max(1, int(5.0 * self._cfg.effective_frame_rate()))` ブロック
  - `tasks/eval_tracking.py`: `normalizer = OnsetNormalizer(max(1, int(5.0 * frame_rate)))` (148 行)
- **問題**: 「ライブ onset の rolling-max 窓は 5 秒」というチューニング値が 3 箇所にリテラルで散在。1 箇所だけ変えると offline/online/eval の正規化スケールがずれ、fusion 距離の意味が壊れる（CENS パラメータ同一性と同種の罠）。
- **変更**: `feature_extractor.py` の `OnsetNormalizer` にファクトリを追加:

  ```python
  # Rolling-max window for live onset normalisation. Must be identical
  # everywhere live onset is produced (worker, file worker, eval), or the
  # onset term of the fused DP cost changes scale between them.
  LIVE_ONSET_WINDOW_SEC = 5.0

  class OnsetNormalizer:
      @classmethod
      def for_config(cls, cfg: "FeatureConfig") -> "OnsetNormalizer":
          """本番/eval 共通の 5 秒 rolling-max 窓で生成する。"""
          return cls(max(1, int(LIVE_ONSET_WINDOW_SEC * cfg.effective_frame_rate())))
  ```

  3 箇所を `OnsetNormalizer.for_config(self._cfg)`（eval は `OnsetNormalizer.for_config(cfg)`）に置換。
  `tests/test_feature_extractor.py` にテストを 1 本追加:

  ```python
  def test_onset_normalizer_for_config_window():
      from audio_score_follower.core.feature_extractor import (
          FeatureConfig, OnsetNormalizer, LIVE_ONSET_WINDOW_SEC,
      )
      cfg = FeatureConfig()  # 22050/2048 → 10.766 Hz
      n = OnsetNormalizer.for_config(cfg)
      assert n._buf.maxlen == max(1, int(LIVE_ONSET_WINDOW_SEC * cfg.effective_frame_rate()))
  ```
- **完了条件**: pytest → **142 passed**。`python tasks/eval_tracking.py --help` exit 0。
- **リスク/戻し方**: 低（数式の等価移動）。`git revert`。
- **依存**: RF-02（同ファイルの行ズレ回避。技術的依存なし）。

---

### RF-12: GUI 命名の整合 — `on_force_lock_in` 系を実挙動（演奏開始）に合わせて改名

- **対象**:
  - `audio_score_follower/ui/gui_tkinter.py`: コンストラクタ引数 `on_force_lock_in` (98 行), `self._on_force_lock_in` (114 行), `button_force_lock_in` (231-238, 351-354 行), `_on_force_lock_in_clicked` (277 行)
  - `audio_score_follower/main.py`: `FollowerGUI(..., on_force_lock_in=self.manual_start)` (203 行)
- **問題**: Issue #13 対応以降、ボタンの実挙動は「強制 lock-in」ではなく「演奏開始（初回押下は gate 統治の開始、2 回目以降のみ force lock-in）」。ハンドラ名が旧仕様のままで、コード読解時に `manual_start` との対応を毎回確認させられる。ボタンのラベルは既に「▶ 演奏開始」。
- **変更**: 機械的リネームのみ（挙動・ログ文言は不変）:
  - 引数 `on_force_lock_in` → `on_start`
  - `self._on_force_lock_in` → `self._on_start`
  - `button_force_lock_in` → `button_start`
  - `_on_force_lock_in_clicked` → `_on_start_clicked`
  - main.py の呼び出しを `on_start=self.manual_start` に
  - gui_tkinter.py 内の関連 docstring / コメント（「force-lock-in button」等）を「start button」に修正。`_MODE_COLOR_FLASH` のコメントの「force-lock-in flash」も同様。
  - **OLTW 側の `force_lock_in()` メソッド名は正確なので触らない**（あちらは本当に lock-in ラッチを立てる）。
- **事前確認**: `grep -rn "on_force_lock_in\|button_force_lock_in" --include="*.py" .` → gui_tkinter.py と main.py のみ（計画時点で確認済み。テストは参照していない）。
- **完了条件**: pytest 142 passed。`python -m audio_score_follower.main --help` exit 0。grep で旧名称の残存ゼロ。
- **リスク/戻し方**: 低。`git revert`。
- **依存**: RF-05（gui_tkinter.py の行ズレ回避）。

---

### RF-13: `_NullSlideController` を slide_controller.py へ移設 + main.py の import 順序修正

- **対象**: `audio_score_follower/main.py:67-87`（`class _NullSlideController` が import 文の**途中**に定義されており、88 行目以降にまだ import が続く）
- **問題**: import ブロックがクラス定義で分断されていて読みにくい。また Null 実装は SlideController の対になる存在なので `slide_controller.py` にあるべき（1 ファイル 1 責務）。クラス内で `logger` を参照しているが、これは定義位置より後で作られる main.py の `logger` に実行時解決で依存しており、移設で自然に解消する。
- **変更**:
  1. `slide_controller.py` に `NullSlideController`（公開名。アンダースコアを外す）として移動。docstring・メソッドはそのまま。`logger` は slide_controller.py のものを使う。
  2. main.py は `from audio_score_follower.core.slide_controller import NullSlideController, SlideController` とし、使用箇所 (195 行) を `NullSlideController()` に変更。`# type: ignore[assignment]` は維持。
  3. main.py の import ブロックを標準的な並び（stdlib → プロジェクト内、クラス定義は import の後）に整える。**`sys.stdout.reconfigure` の 2 ブロック（49-52 行）は import 途中にある必要があるためそのまま**（launch_options 以降の import より前で実行される現状の位置を維持）。
- **完了条件**: pytest 142 passed。`python -m audio_score_follower.main --help` exit 0。`python -c "from audio_score_follower.core.slide_controller import NullSlideController"` が通る。
- **リスク/戻し方**: 低。`git revert`。
- **依存**: なし（main.py を触る他項目の後に置くのは行ズレ回避のため）。

---

### RF-14: 小粒クリーンアップ一括（挙動不変が自明なもの）

- **対象と変更**（3 点、1 コミット）:
  1. `audio_score_follower/core/audio_level.py:102`: `start()` 内の `import threading` を削除（モジュール先頭 21 行で import 済みの冗長ローカル import）。
  2. `audio_score_follower/core/warp_lookup.py:228-235`: `validate()` 末尾の成功ログが `np.interp` + `np.diff` を**再計算**している。上で計算済みの `slope_per_sec` を変数に持ち回して `float(np.max(slope_per_sec))`（`len(sample_ref) >= 2` のときのみ、それ以外は 0.0）を使う。出力される数値は同一。
  3. `audio_score_follower/main.py:184-186`: `__init__` 内のローカル変数経由の `_SEEK_GRACE_SEC = 2.0` / `self._SEEK_GRACE_SEC = _SEEK_GRACE_SEC` を、モジュール定数 `_SEEK_GRACE_SEC = 2.0`（`_MAX_FRAME_MEASURE_JUMP` の隣、コメントごと移動）にし、使用箇所 (519 行) を `_SEEK_GRACE_SEC` 参照に変更。`self._SEEK_GRACE_SEC` 属性は削除（参照は main.py 内 1 箇所のみ、計画時点で grep 確認済み）。
- **完了条件**: pytest 142 passed。`python -m audio_score_follower.main --help` exit 0。
- **リスク/戻し方**: 極小。`git revert`。
- **依存**: RF-13（main.py の行ズレ回避）。

---

### 実行順まとめと依存グラフ

```
0 (安全網)
├─ RF-01 → RF-07 → RF-08 → RF-09     … oltw_follower.py 系列
├─ RF-02 → RF-11                      … feature_extractor 系列
├─ RF-03, RF-04                       … loader（RF-04 は 0-d のテスト必須）
├─ RF-05 → RF-06                      … state_manager 系列（RF-05 は gui も触る）
├─ RF-05 → RF-12                      … gui_tkinter の行ズレ回避
├─ 0-e → RF-10                        … synth locator
└─ RF-13 → RF-14                      … main.py 系列
```

番号順（RF-01→RF-14）に頭から実行すればすべての依存を満たす。

**トレース検証済み**: RF-01 で消すメソッドは RF-07/08 の対象外になる（先に消すことで置換箇所が減る）。RF-04 は 0-d のテストが loader↔OLTW シグネチャ同期を守る。RF-05 の get_all キー削除は 0-d のテスト（部分集合アサーション）を壊さない。RF-07 の `_anchor_dp_at` は RF-08 の成功パスから呼ばれる（順序固定）。RF-10 の generate_score_wav への sys.path 挿入は RF-11 の eval_tracking と同一の既存パターン。RF-12/13/14 は互いに独立領域。後続項目の前提を壊す変更は存在しない。

---

## 4. やらないことリスト（善意の逸脱の先回り禁止）

以下は「気づいても実施しない」。理由付きで禁止する:

1. **`oltw_follower.py` の分割・クラス抽出（DP / inertia / display を別クラスに等）** — 状態機械の結合が意図的（CLAUDE.md の罠リスト参照）。分割はバグの温床であり、本計画では RF-07/08/09 の局所抽出まで。
2. **挙動を変えるバグ修正** — 下記「発見したバグ」に列挙したものを含め、修正しない（リファクタは挙動不変が前提。修正は別 Issue / 別 PR）。
3. **`FollowerWorker` / `FileWorker` の基底クラス抽出** — ライフサイクル系の重複はあるが、両クラスにテストが無く、音声デバイス絡みで自動検証できない。リスク > 効果。
4. **`ConfigLoader` の movements ミューテーション（`_validate` が `movement["xml_file"]` を書き換える）の設計是正** — 挙動に波及するため対象外。
5. **依存ライブラリの更新・追加**（synctoolbox の monkey-patch 解消を含む） — pyproject.toml に触らない。
6. **`FeatureConfig` / CENS / onset のパラメータや数式の変更** — ビルド済み成果物 (`data/built/*`) が無効になる。
7. **OLTW の閾値・デフォルト値の変更**（`step_penalty`, `search_width`, `lock_in_confidence` 等） — 数値は 1 つも変えない。RF-09 は「名前を付ける」だけ。
8. **ログ文言の「改善」** — 運用時の grep 互換を守る。指示された箇所以外のログメッセージを変えない。
9. **`debug_fluidsynth.py` の削除・修正** — 診断用スクリプト。gitignore の `debug_*.py` 対象であり触らない。
10. **`data/`、`config/*.json`、ビルド成果物への変更** — 一切触らない（RF-04 の事前 grep で該当キーが見つかった場合のみ、そのキー 1 行の削除を許可）。
11. **型ヒントの全面追加、docstring スタイル統一、フォーマッタ (black 等) の適用** —差分が膨れて本質的変更が埋もれる。
12. **README / CLAUDE.md の再構成** — 変更した API に言及している行のピンポイント修正（RF-02/04 の grep で見つかった場合）のみ可。
13. **テストの「ついで」修正・削除** — 指示された削除（RF-02 の 3 本）と追加（0-d, RF-11）以外、既存テストに触らない。テストが落ちたらテストを直すのではなく**作業を戻して報告**。
14. **GitHub への push / PR 作成** — CLAUDE.md の方針（当面 GitHub 同期しない）に従い、ローカルコミットまで。

### 発見したバグ（本計画では修正しない。ユーザーへ報告のこと）

- **`AppState.activate_cooldown` の二重遅延** (`state_manager.py:280-299`): `threading.Timer(duration_sec, clear_cooldown)` で起動される `clear_cooldown` 自身がさらに `time.sleep(duration_sec)` してから `deactivate_cooldown()` を呼ぶため、GUI 上の「🔒 クールダウン中」表示は設定値の **2 倍**の時間残る（トリガの実レート制限は `CooldownTimer` が別途正しく行っているため表示だけの問題）。修正は 1 行（inner 関数の sleep を消す）だが挙動変更なので本計画対象外。
- **`FollowerWorker.start` の loopback エラーログ** (`follower_worker.py:197`): loopback で default device にフォールバックした場合もエラーログは `self._mic_device`（None）を表示し、実際に開こうとしたデバイスが出ない。診断性の問題のみ。

---

## 5. 実行者への指示文（このままコピペして渡す）

```
あなたはこのリポジトリのリファクタリング実行者です。同梱の
「audio-score-follower リファクタリング計画書」だけを根拠に作業してください。

ルール:
1. 計画書の項目 0 → RF-01 → RF-02 → … → RF-14 を番号順に、1 項目ずつ実施する。
2. 1 項目 = 1 コミット。コミットメッセージは「refactor: <項目の要約>」
   （項目 0 は計画書記載のメッセージ）。項目をまたいで変更を混ぜない。
3. 各項目の作業前に、その項目の「事前確認」の grep を必ず実行し、
   計画書の想定と一致することを確かめる。一致しなければ中断して報告。
4. 各項目の完了後、その項目の「完了条件」のコマンドをすべて実行し、
   期待結果（テスト数・exit code・grep 結果）を満たすことを確認してから
   コミットする。満たせない場合は `git checkout .` で作業を破棄し、
   どの項目のどのコマンドがどう失敗したかを報告して停止する。
5. 挙動は 1 ビットも変えない。数値・閾値・ログ文言・公開 API の意味を
   変更しない。計画書の「やらないことリスト」に該当する変更は、
   良い改善に見えても行わない。気づいた問題は報告リストに書き足すだけにする。
6. 行番号は計画時点のものなので、必ずシンボル名で対象を再特定する。
   シンボルが見つからない・形が計画書の記述と食い違う場合は中断して報告。
7. リポジトリの CLAUDE.md「触ってはいけない」制約（計画書 1.4 節に転記済み）を
   常に優先する。
8. GitHub への push はしない。全項目完了後、`git log --oneline` と
   最終の `python -m pytest tests/ -q` の結果、および途中で気づいた
   問題のリストをまとめて報告する。

前提の確認（作業開始前に実施）:
- `git rev-parse --abbrev-ref HEAD` が fix/silence-gate-oneshot-issue13、
  `git log -1 --format=%h` が 2dbb9b5 であること。異なる場合は、計画書の
  行番号ずれが大きい可能性があるため、各項目の「事前確認」grep を
  より慎重に行うこと（シンボルが存在すれば作業は続行してよい）。
- `python -m pytest tests/ -q` が 136 passed であること。
```
