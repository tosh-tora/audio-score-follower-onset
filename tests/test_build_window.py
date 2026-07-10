"""Tests for audio_score_follower.ui.build_window pure logic.

Only the Tk-free helpers (build_command / generate_config_dict /
write_config) are exercised — no Toplevel is created, matching the
headless split used by test_launch_options.
"""

from pathlib import Path

import pytest

from audio_score_follower.config.loader import ConfigLoader
from audio_score_follower.ui.build_window import (
    build_command,
    generate_config_dict,
    write_config,
)

_MODULE = "audio_score_follower.cli.build_reference"


# ---------------------------------------------------------------- build_command
def test_build_command_minimal_omits_optional_flags():
    cmd = build_command(
        Path("score.mxl"), Path("ref.wav"), Path("out"),
        python_exe="python",
    )
    assert cmd[:3] == ["python", "-m", _MODULE]
    # required flags present with absolute paths
    assert "--score" in cmd and "--reference" in cmd and "--output" in cmd
    score_arg = cmd[cmd.index("--score") + 1]
    assert Path(score_arg).is_absolute()
    # nothing at CLI default is emitted
    for flag in (
        "--score-bpm", "--start-offset", "--end-trim",
        "--sample-rate", "--hop-length", "--cens-win", "--plot", "-v",
    ):
        assert flag not in cmd


def test_build_command_optional_flags_present_when_set():
    cmd = build_command(
        Path("s.mxl"), Path("r.wav"), Path("o"),
        python_exe="python",
        score_bpm=152.0, start_offset=1.5, end_trim=8.0,
        sample_rate=44100, hop_length=1024, cens_win=21,
        plot=True, verbose=True,
    )
    assert cmd[cmd.index("--score-bpm") + 1] == "152.0"
    assert cmd[cmd.index("--start-offset") + 1] == "1.5"
    assert cmd[cmd.index("--end-trim") + 1] == "8.0"
    assert cmd[cmd.index("--sample-rate") + 1] == "44100"
    assert cmd[cmd.index("--hop-length") + 1] == "1024"
    assert cmd[cmd.index("--cens-win") + 1] == "21"
    assert "--plot" in cmd
    assert "-v" in cmd


def test_build_command_explicit_zero_start_offset_is_passed():
    # start_offset=0 means "disable head-offset auto-detection" —
    # distinct from None (auto), mirroring end_trim's None-vs-0 split.
    cmd = build_command(Path("s"), Path("r"), Path("o"), start_offset=0.0)
    assert cmd[cmd.index("--start-offset") + 1] == "0.0"


def test_build_command_start_offset_none_omitted():
    cmd = build_command(Path("s"), Path("r"), Path("o"), start_offset=None)
    assert "--start-offset" not in cmd


def test_build_command_explicit_end_trim_zero_is_passed():
    # end_trim=0 means "disable trimming" — distinct from None (auto).
    cmd = build_command(Path("s"), Path("r"), Path("o"), end_trim=0.0)
    assert cmd[cmd.index("--end-trim") + 1] == "0.0"


def test_build_command_end_trim_none_omitted():
    cmd = build_command(Path("s"), Path("r"), Path("o"), end_trim=None)
    assert "--end-trim" not in cmd


# ------------------------------------------------------------ generate_config
def test_generate_config_uses_relative_forward_slash_paths(tmp_path: Path):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    score = tmp_path / "data" / "scores" / "foo.mxl"
    built = tmp_path / "data" / "built" / "foo"
    cfg = generate_config_dict(score, built, config_dir)
    mv = cfg["movements"][0]
    assert mv["xml_file"] == "../data/scores/foo.mxl"
    assert mv["built_dir"] == "../data/built/foo"
    assert "\\" not in mv["xml_file"]  # forward slashes even on Windows


def test_generated_config_passes_configloader(tmp_path: Path):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    score = tmp_path / "data" / "scores" / "foo.mxl"
    built = tmp_path / "data" / "built" / "foo"
    cfg = generate_config_dict(score, built, config_dir)
    config_path = config_dir / "foo.json"
    write_config(config_path, cfg)

    # ConfigLoader validates structure (movements/xml_file/built_dir/triggers)
    # without requiring the referenced files to exist.
    loader = ConfigLoader(str(config_path))
    assert loader.total_movements() == 1
    mv = loader.get_current_movement()
    assert mv["xml_file"] and mv["built_dir"]
    assert mv["triggers"][0]["measure"] == 1
    assert mv["triggers"][0]["action"] == "right"


def test_write_config_is_atomic_and_utf8(tmp_path: Path):
    config_path = tmp_path / "sub" / "夜想曲.json"
    cfg = generate_config_dict(
        tmp_path / "s.mxl", tmp_path / "b", tmp_path
    )
    write_config(config_path, cfg)
    text = config_path.read_text(encoding="utf-8")
    assert "トリガーを編集" in text  # Japanese note preserved (ensure_ascii=False)
    assert not list(config_path.parent.glob("*.tmp"))  # temp file cleaned up
