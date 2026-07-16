"""Tests for audio_score_follower.launch_options (no Tk, no sounddevice)."""

import argparse
import json
import math
from pathlib import Path

import numpy as np
import pytest

from audio_score_follower.launch_options import (
    DEFAULT_SILENCE_MARGIN_DB,
    DEFAULT_SILENCE_THRESHOLD_DB,
    INPUT_SOURCE_LOOPBACK,
    INPUT_SOURCE_MIC,
    INPUT_SOURCE_WAV,
    MIN_SILENCE_SAMPLES,
    LaunchOptions,
    coerce_device,
    compute_silence_threshold,
    default_config_dir,
    from_cli_args,
    read_launcher_settings,
    rematch_device,
    resolve_input_wav,
    save_launcher_settings,
    validate,
)


# ---------------------------------------------------------------- fixtures
@pytest.fixture
def config_file(tmp_path: Path) -> Path:
    """Config with Japanese strings, an unknown custom key, and movements."""
    data = {
        "settings": {
            "cooldown_seconds": 5.0,
            "silence_threshold_db": -48.0,
            "custom_key": "keep-me",
            "oltw_kwargs": {"search_width": 300},
        },
        "movements": [
            {
                "id": 1,
                "xml_file": "../data/scores/幻想交響曲5.mxl",
                "built_dir": "../data/built/幻想交響曲5",
                "triggers": [{"measure": 1, "action": "right", "note": "開始"}],
            }
        ],
    }
    path = tmp_path / "config.json"
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _mic_opts(config_path: Path, **kw) -> LaunchOptions:
    return LaunchOptions(config_path=config_path, input_source=INPUT_SOURCE_MIC, **kw)


# ---------------------------------------------------------------- effective wav
class TestEffectiveInputWav:
    def test_mic_source_ignores_stale_wav_path(self, config_file, tmp_path):
        # 回帰防止: ランチャーは wav 欄の前回値を保持したまま起動できる。
        # input_source=mic なのに input_wav を app に渡すと wav モードで
        # 起動してしまい「マイクを選んだのに監視無効・勝手に追随開始」になる。
        wav = tmp_path / "x.wav"
        wav.write_bytes(b"\x00")
        opts = _mic_opts(config_file, input_wav=wav)
        assert opts.effective_input_wav is None

    def test_wav_source_passes_through(self, config_file, tmp_path):
        wav = tmp_path / "x.wav"
        wav.write_bytes(b"\x00")
        opts = LaunchOptions(
            config_path=config_file, input_source=INPUT_SOURCE_WAV, input_wav=wav
        )
        assert opts.effective_input_wav == wav

    def test_loopback_source_ignores_wav(self, config_file, tmp_path):
        wav = tmp_path / "x.wav"
        wav.write_bytes(b"\x00")
        opts = LaunchOptions(
            config_path=config_file,
            input_source=INPUT_SOURCE_LOOPBACK,
            input_wav=wav,
        )
        assert opts.effective_input_wav is None


# ---------------------------------------------------------------- config loader
class TestConfigLoaderStartGateTimeout:
    """settings.start_gate_timeout_sec — 見切りスタート deadline (Issue #41)."""

    def _loader_with(self, config_file, tmp_path, value):
        data = json.loads(config_file.read_text(encoding="utf-8"))
        data["settings"]["start_gate_timeout_sec"] = value
        path = tmp_path / "config_timeout.json"
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        from audio_score_follower.config.loader import ConfigLoader
        return ConfigLoader(str(path))

    def test_default(self, config_file):
        from audio_score_follower.config.loader import ConfigLoader
        loader = ConfigLoader(str(config_file))
        assert loader.get_start_gate_timeout_sec() == 3.0

    def test_settings_override(self, config_file, tmp_path):
        loader = self._loader_with(config_file, tmp_path, 7.5)
        assert loader.get_start_gate_timeout_sec() == 7.5

    def test_zero_disables(self, config_file, tmp_path):
        loader = self._loader_with(config_file, tmp_path, 0)
        assert loader.get_start_gate_timeout_sec() == 0.0

    def test_negative_clamps_to_zero(self, config_file, tmp_path):
        loader = self._loader_with(config_file, tmp_path, -2.0)
        assert loader.get_start_gate_timeout_sec() == 0.0

    def test_non_numeric_falls_back_to_default(self, config_file, tmp_path):
        loader = self._loader_with(config_file, tmp_path, "fast")
        assert loader.get_start_gate_timeout_sec() == 3.0


class TestConfigLoaderStartSearch:
    def test_default(self, config_file):
        from audio_score_follower.config.loader import ConfigLoader
        loader = ConfigLoader(str(config_file))
        assert loader.get_start_search_seconds() == 10.0

    def test_settings_override(self, config_file, tmp_path):
        import json as _json
        data = _json.loads(config_file.read_text(encoding="utf-8"))
        data["settings"]["start_search_seconds"] = 6.0
        path = tmp_path / "config_override.json"
        path.write_text(_json.dumps(data, ensure_ascii=False), encoding="utf-8")
        from audio_score_follower.config.loader import ConfigLoader
        loader = ConfigLoader(str(path))
        assert loader.get_start_search_seconds() == 6.0


# ---------------------------------------------------------------- resolve
class TestResolveInputWav:
    def test_none(self):
        assert resolve_input_wav(None) is None

    def test_bare_filename_goes_to_reference_audio(self):
        assert resolve_input_wav(Path("karajan.mp3")) == (
            Path("data") / "reference_audio" / "karajan.mp3"
        )

    def test_path_with_directory_untouched(self):
        p = Path("some/dir/file.wav")
        assert resolve_input_wav(p) == p

    def test_absolute_path_untouched(self, tmp_path):
        p = tmp_path / "file.wav"
        assert resolve_input_wav(p) == p


class TestCoerceDevice:
    def test_numeric_string(self):
        assert coerce_device("3") == 3

    def test_name_string(self):
        assert coerce_device("Speakers") == "Speakers"

    def test_none(self):
        assert coerce_device(None) is None

    def test_int_passthrough(self):
        assert coerce_device(5) == 5


# ---------------------------------------------------------------- validate
class TestValidate:
    def test_missing_config(self, tmp_path):
        errors = validate(_mic_opts(tmp_path / "nope.json"))
        assert any("Config not found" in e for e in errors)

    def test_wav_source_without_file(self, config_file):
        opts = LaunchOptions(config_path=config_file, input_source=INPUT_SOURCE_WAV)
        errors = validate(opts)
        assert any("音源ファイル" in e for e in errors)

    def test_wav_source_file_missing(self, config_file, tmp_path):
        opts = LaunchOptions(
            config_path=config_file,
            input_source=INPUT_SOURCE_WAV,
            input_wav=tmp_path / "missing.wav",
        )
        errors = validate(opts)
        assert any("--input-wav not found" in e for e in errors)

    def test_play_audio_requires_wav_source(self, config_file):
        errors = validate(_mic_opts(config_file, play_audio=True))
        assert any("--play-audio" in e for e in errors)

    def test_invalid_source(self, config_file):
        opts = LaunchOptions(config_path=config_file, input_source="bogus")
        assert any("入力ソース" in e for e in validate(opts))

    def test_happy_mic(self, config_file):
        assert validate(_mic_opts(config_file)) == []

    def test_margin_out_of_range(self, config_file):
        errors = validate(_mic_opts(config_file, silence_margin_db=25.0))
        assert any("無音測定マージン" in e for e in errors)
        errors = validate(_mic_opts(config_file, silence_margin_db=-25.0))
        assert any("無音測定マージン" in e for e in errors)

    def test_margin_non_finite(self, config_file):
        errors = validate(_mic_opts(config_file, silence_margin_db=math.nan))
        assert any("無音測定マージン" in e for e in errors)

    def test_margin_in_range_ok(self, config_file):
        assert validate(_mic_opts(config_file, silence_margin_db=-3.0)) == []

    def test_happy_loopback(self, config_file):
        opts = LaunchOptions(
            config_path=config_file, input_source=INPUT_SOURCE_LOOPBACK,
            loopback_device=5,
        )
        assert validate(opts) == []

    def test_happy_wav(self, config_file, tmp_path):
        wav = tmp_path / "x.wav"
        wav.write_bytes(b"\x00")
        opts = LaunchOptions(
            config_path=config_file, input_source=INPUT_SOURCE_WAV,
            input_wav=wav, play_audio=True,
        )
        assert validate(opts) == []


# ---------------------------------------------------------------- from_cli_args
def _namespace(**kw) -> argparse.Namespace:
    defaults = dict(
        config="config/x.json", slide_url=None, input_wav=None,
        play_audio=False, loopback=False, loopback_device=None, verbose=False,
    )
    defaults.update(kw)
    return argparse.Namespace(**defaults)


class TestFromCliArgs:
    def test_default_is_mic(self):
        opts = from_cli_args(_namespace())
        assert opts.input_source == INPUT_SOURCE_MIC
        assert opts.slide_url is None
        assert opts.input_wav is None

    def test_input_wav_maps_to_wav_source(self):
        opts = from_cli_args(_namespace(input_wav=Path("karajan.mp3")))
        assert opts.input_source == INPUT_SOURCE_WAV
        assert opts.input_wav == Path("data") / "reference_audio" / "karajan.mp3"

    def test_loopback_flag(self):
        opts = from_cli_args(_namespace(loopback=True, loopback_device="7"))
        assert opts.input_source == INPUT_SOURCE_LOOPBACK
        assert opts.loopback_device == 7

    def test_verbose_and_slide_url(self):
        opts = from_cli_args(_namespace(verbose=True, slide_url="https://x"))
        assert opts.verbose is True
        assert opts.slide_url == "https://x"

    def test_empty_slide_url_normalised_to_none(self):
        assert from_cli_args(_namespace(slide_url="")).slide_url is None


# ---------------------------------------------------------------- persistence
class TestPersistence:
    def test_round_trip_preserves_unrelated_keys(self, config_file):
        original = json.loads(config_file.read_text(encoding="utf-8"))
        opts = _mic_opts(config_file, mic_device=2, verbose=True)
        save_launcher_settings(config_file, opts, mic_device_name="Mic A")

        data = json.loads(config_file.read_text(encoding="utf-8"))
        assert data["movements"] == original["movements"]
        assert data["settings"]["custom_key"] == "keep-me"
        assert data["settings"]["oltw_kwargs"] == {"search_width": 300}

    def test_japanese_not_escaped(self, config_file):
        save_launcher_settings(config_file, _mic_opts(config_file))
        text = config_file.read_text(encoding="utf-8")
        assert "幻想交響曲5" in text
        assert "\\u" not in text
        assert text.endswith("\n")

    def test_launcher_block_written(self, config_file, tmp_path):
        wav = tmp_path / "rec.wav"
        wav.write_bytes(b"\x00")
        opts = LaunchOptions(
            config_path=config_file, input_source=INPUT_SOURCE_WAV,
            input_wav=wav, play_audio=True, verbose=True,
            slide_url="https://slides", silence_threshold_db=-50.0,
            cooldown_seconds=4.0,
        )
        save_launcher_settings(config_file, opts)
        settings = json.loads(config_file.read_text(encoding="utf-8"))["settings"]
        launcher = settings["launcher"]
        assert launcher["input_source"] == "wav"
        assert launcher["input_wav"] == str(wav)
        assert launcher["play_audio"] is True
        assert launcher["verbose"] is True
        assert launcher["slide_url"] == "https://slides"
        # Silence threshold is mic/venue-dependent and NOT persisted, even
        # when the LaunchOptions carries a value.
        assert "silence_threshold_db" not in settings
        assert settings["cooldown_seconds"] == 4.0

    def test_silence_threshold_never_persisted_and_legacy_key_removed(
        self, config_file
    ):
        # config_file fixture ships a legacy settings.silence_threshold_db
        # (-48.0); saving must drop it so a config carried between venues
        # can't silently reapply a stale threshold.
        before = json.loads(config_file.read_text(encoding="utf-8"))
        assert before["settings"]["silence_threshold_db"] == -48.0
        save_launcher_settings(config_file, _mic_opts(config_file, silence_threshold_db=-40.0))
        settings = json.loads(config_file.read_text(encoding="utf-8"))["settings"]
        assert "silence_threshold_db" not in settings
        # Unrelated keys survive untouched.
        assert settings["custom_key"] == "keep-me"
        assert settings["oltw_kwargs"] == {"search_width": 300}

    def test_only_active_source_device_key_written(self, config_file):
        # mic mode: loopback_device must NOT be touched
        opts = _mic_opts(config_file, mic_device=2, loopback_device=9)
        save_launcher_settings(config_file, opts)
        settings = json.loads(config_file.read_text(encoding="utf-8"))["settings"]
        assert settings["mic_device"] == 2
        assert "loopback_device" not in settings

        # loopback mode: mic_device stays at its previous value
        opts2 = LaunchOptions(
            config_path=config_file, input_source=INPUT_SOURCE_LOOPBACK,
            mic_device=None, loopback_device=9,
        )
        save_launcher_settings(config_file, opts2)
        settings = json.loads(config_file.read_text(encoding="utf-8"))["settings"]
        assert settings["mic_device"] == 2
        assert settings["loopback_device"] == 9

    def test_save_idempotent(self, config_file):
        opts = _mic_opts(config_file, mic_device=1)
        save_launcher_settings(config_file, opts, mic_device_name="Mic A")
        first = config_file.read_text(encoding="utf-8")
        save_launcher_settings(config_file, opts, mic_device_name="Mic A")
        assert config_file.read_text(encoding="utf-8") == first

    def test_settings_block_created_when_absent(self, tmp_path):
        path = tmp_path / "bare.json"
        path.write_text('{"movements": []}', encoding="utf-8")
        save_launcher_settings(path, _mic_opts(path))
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["settings"]["launcher"]["input_source"] == "mic"
        assert data["movements"] == []


class TestReadLauncherSettings:
    def test_defaults_without_launcher_block(self, config_file):
        saved = read_launcher_settings(config_file)
        assert saved["input_source"] == INPUT_SOURCE_MIC
        assert saved["slide_url"] is None
        assert saved["play_audio"] is False
        # The silence threshold is not read back from config (mic/venue
        # dependent) — always the default, ignoring the fixture's -48.0.
        assert saved["silence_threshold_db"] == DEFAULT_SILENCE_THRESHOLD_DB
        # cooldown is still a config-backed flat key
        assert saved["cooldown_seconds"] == 5.0
        assert saved["mic_device"] is None

    def test_merged_after_save(self, config_file):
        opts = LaunchOptions(
            config_path=config_file, input_source=INPUT_SOURCE_LOOPBACK,
            loopback_device=3, verbose=True, slide_url="https://s",
        )
        save_launcher_settings(config_file, opts, loopback_device_name="Speakers")
        saved = read_launcher_settings(config_file)
        assert saved["input_source"] == INPUT_SOURCE_LOOPBACK
        assert saved["loopback_device"] == 3
        assert saved["loopback_device_name"] == "Speakers"
        assert saved["verbose"] is True
        assert saved["slide_url"] == "https://s"

    def test_syntax_error_raises_value_error(self, tmp_path):
        path = tmp_path / "broken.json"
        path.write_text("{ oops", encoding="utf-8")
        with pytest.raises(ValueError, match="JSON 構文エラー"):
            read_launcher_settings(path)

    def test_margin_default_without_launcher_block(self, config_file):
        saved = read_launcher_settings(config_file)
        assert saved["silence_margin_db"] == DEFAULT_SILENCE_MARGIN_DB

    def test_margin_round_trip(self, config_file):
        opts = _mic_opts(config_file, silence_margin_db=0.5)
        save_launcher_settings(config_file, opts)
        saved = read_launcher_settings(config_file)
        assert saved["silence_margin_db"] == 0.5
        launcher = json.loads(config_file.read_text(encoding="utf-8"))[
            "settings"]["launcher"]
        assert launcher["silence_margin_db"] == 0.5


# ---------------------------------------------------------------- silence threshold
class TestComputeSilenceThreshold:
    def test_formula_median_plus_lower_spread_plus_margin(self):
        # Wide ambient fluctuation:
        # p10≈-68, median=-60 → threshold = -60 + 8 + 2 = -50.0
        samples = list(np.linspace(-70.0, -50.0, 100))
        result = compute_silence_threshold(samples)
        assert result.threshold_db == pytest.approx(-50.0, abs=0.3)
        assert result.median_db == pytest.approx(-60.0, abs=0.2)
        assert result.p10_db == pytest.approx(-68.0, abs=0.2)
        assert result.count == 100

    def test_no_spread_floor_on_steady_ambient(self):
        # Very steady noise floor (median ≈ p10): the threshold tracks
        # the measured spread with no floor. A 6dB floor used to push
        # the threshold to median+9dB, and quiet playing never opened
        # the gate (Issue #19) — with manual start + one-shot gate
        # governance the floor's false-start protection is redundant.
        samples = list(np.linspace(-60.5, -59.5, 100))  # spread ≈ 0.4dB
        result = compute_silence_threshold(samples)
        # threshold = median + 0.4 + 2 ≈ -57.6
        assert result.threshold_db == pytest.approx(-57.6, abs=0.3)

    def test_robust_to_incidental_spikes(self):
        # 3% loud spikes (a cough) must not move the threshold: median and
        # p10 both live in the lower, uncontaminated half.
        ambient = list(np.linspace(-62.0, -58.0, 200))
        clean = compute_silence_threshold(ambient)
        spiked = compute_silence_threshold(ambient + [-20.0] * 6)
        assert abs(spiked.threshold_db - clean.threshold_db) < 0.5

    def test_high_percentile_would_not_be_robust(self):
        # Sanity check on the design choice: with 10% contamination a
        # naive 95th percentile lands on the spikes; our estimator stays
        # in the ambient range.
        ambient = list(np.linspace(-62.0, -58.0, 180))
        contaminated = ambient + [-25.0] * 20
        result = compute_silence_threshold(contaminated)
        assert result.threshold_db < -50.0
        assert float(np.percentile(contaminated, 95)) > -30.0

    def test_too_few_samples(self):
        with pytest.raises(ValueError, match="サンプル不足"):
            compute_silence_threshold([-60.0] * (MIN_SILENCE_SAMPLES - 1))

    def test_non_finite_samples_dropped(self):
        samples = [-math.inf] * 10 + [-60.0] * MIN_SILENCE_SAMPLES
        result = compute_silence_threshold(samples)
        assert result.count == MIN_SILENCE_SAMPLES

    def test_clamped_to_valid_range(self):
        # digital silence (-200 dBFS floor) must clamp up to -120
        low = compute_silence_threshold([-200.0] * 50)
        assert low.threshold_db == -120.0
        # absurdly hot ambient clamps down to 0
        high = compute_silence_threshold(
            list(np.linspace(-4.0, -0.1, 50))
        )
        assert high.threshold_db <= 0.0

    def test_custom_margin(self):
        # zero spread → median + 0 + margin
        samples = [-60.0] * 50
        assert compute_silence_threshold(samples, margin_db=5.0).threshold_db == -55.0


# ---------------------------------------------------------------- rematch
class TestRematchDevice:
    DEVICES = [(1, "Mic A"), (4, "Mic B"), (7, "USB Mic")]

    def test_index_and_name_match(self):
        assert rematch_device(4, "Mic B", self.DEVICES) == 4

    def test_name_moved_to_new_index(self):
        # stored index 2 no longer exists; name found at index 7
        assert rematch_device(2, "USB Mic", self.DEVICES) == 7

    def test_device_gone(self):
        assert rematch_device(2, "Gone Mic", self.DEVICES) is None

    def test_index_valid_without_name_snapshot(self):
        assert rematch_device(1, None, self.DEVICES) == 1

    def test_index_valid_but_name_changed(self):
        # index 1 now belongs to a different device and the stored name is
        # absent from the enumeration — distrust the index.
        assert rematch_device(1, "Old Mic", self.DEVICES) is None

    def test_none_stored(self):
        assert rematch_device(None, None, self.DEVICES) is None


# ---------------------------------------------------- default_config_dir
class TestDefaultConfigDir:
    def test_prefers_cwd_config_when_present(self, tmp_path, monkeypatch):
        (tmp_path / "config").mkdir()
        monkeypatch.chdir(tmp_path)
        assert default_config_dir() == Path("config")

    def test_falls_back_to_package_config_from_foreign_cwd(
        self, tmp_path, monkeypatch
    ):
        # No config/ under the CWD (the reboot-from-home scenario, Issue #7):
        # must locate the config/ shipped at the repo root, not return the
        # empty CWD-relative path.
        monkeypatch.chdir(tmp_path)
        assert not (tmp_path / "config").exists()
        result = default_config_dir()
        assert result.is_absolute()
        assert result.name == "config"
        assert result.is_dir()
