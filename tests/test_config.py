import logging
import stat
import tomllib
from pathlib import Path

import pytest

from transcoder_bot.config import (
    DEFAULT_CONFIG_PATH,
    ConfigError,
    SlackConfig,
    bitrate_kbps,
    example_config_text,
    load_config,
    parse_config,
    resolve_config_path,
    write_example_config,
)


def test_minimal_config_uses_defaults():
    config = parse_config({"recordings_dir": "/Volumes/HyperDeck"})

    assert config.recordings_dir == Path("/Volumes/HyperDeck")
    assert config.scan.lookback_hours == 48
    assert config.video.rotate == "ccw"
    assert config.video.short_side == 1080
    assert config.audio.target_lufs == -16
    assert config.audio.channels == (1, 2)
    assert config.output.suffix == "_1080p"
    assert config.trash.mode == "folder"
    assert not config.slack.enabled


def test_example_config_documents_the_defaults():
    data = tomllib.loads(example_config_text())

    # Every value in the example is the default, so it must parse to the same thing
    # as a config containing nothing but recordings_dir.
    assert parse_config(data) == parse_config({"recordings_dir": data["recordings_dir"]})


def test_recordings_dir_is_required():
    with pytest.raises(ConfigError, match="recordings_dir is required"):
        parse_config({})
    with pytest.raises(ConfigError, match="recordings_dir is required"):
        parse_config({"recordings_dir": "  "})


def test_relative_paths_resolve_against_the_config_folder(tmp_path):
    config = parse_config({"recordings_dir": "footage", "temp_dir": "scratch"}, base_dir=tmp_path)

    assert config.recordings_dir == tmp_path / "footage"
    assert config.temp_dir == tmp_path / "scratch"


def test_home_is_expanded():
    config = parse_config({"recordings_dir": "~/Movies"})
    assert config.recordings_dir == Path.home() / "Movies"


@pytest.mark.parametrize(
    ("data", "message"),
    [
        ({"recording_dir": "/x"}, "Unknown setting"),
        ({"video": {"rotation": "ccw"}}, r"Unknown setting\(s\) in \[video\]: rotation"),
        ({"video": "fast"}, r"\[video\] must be a table"),
        ({"video": {"crf": "20"}}, "video.crf must be a whole number"),
        ({"video": {"crf": True}}, "video.crf must be a whole number"),
        ({"audio": {"target_lufs": "-16"}}, "audio.target_lufs must be a number"),
        ({"audio": {"normalize": "yes"}}, "audio.normalize must be true or false"),
        ({"scan": {"extensions": ".mov"}}, "scan.extensions must be a list"),
        ({"scan": {"extensions": [1]}}, r"scan.extensions\[0\] must be a string"),
        ({"mount_url": 5}, "mount_url must be a string"),
    ],
)
def test_rejects_unknown_keys_and_wrong_types(data, message):
    with pytest.raises(ConfigError, match=message):
        parse_config({"recordings_dir": "/x", **data})


@pytest.mark.parametrize(
    ("section", "values", "message"),
    [
        ("video", {"rotate": "left"}, "video.rotate"),
        ("video", {"short_side": 1081}, "video.short_side"),
        ("video", {"encoder": "libvpx"}, "video.encoder"),
        ("video", {"bitrate": "25"}, "video.bitrate"),
        ("video", {"bitrate": "25 Mbps"}, "video.bitrate"),
        ("video", {"bitrate": "fast"}, "video.bitrate"),
        ("video", {"crf": 60}, "video.crf"),
        ("video", {"preset": "ludicrous"}, "video.preset"),
        ("audio", {"target_lufs": -3}, "audio.target_lufs"),
        ("audio", {"true_peak": 1}, "audio.true_peak"),
        ("audio", {"lra": 60}, "audio.lra"),
        ("audio", {"channels": []}, "audio.channels"),
        ("audio", {"channels": [0]}, "audio.channels"),
        ("audio", {"channels": [1, 2, 3]}, "audio.channels"),
        ("audio", {"bitrate": "192"}, "audio.bitrate"),
        ("output", {"suffix": ""}, "output.suffix can't be empty"),
        ("output", {"suffix": "/x"}, "output.suffix"),
        ("output", {"extension": ".mkv"}, "output.extension"),
        ("trash", {"mode": "shred"}, "trash.mode"),
        ("trash", {"retention_days": -1}, "trash.retention_days"),
        ("scan", {"lookback_hours": 0}, "scan.lookback_hours"),
        ("slack", {"bot_token": "xapp-1"}, "xoxb-"),
        ("slack", {"app_token": "xoxb-1"}, "xapp-"),
        ("descript", {"team_access": "owner"}, "descript.team_access"),
        ("descript", {"language": "English"}, "descript.language"),
        ("descript", {"wait_minutes": -1}, "descript.wait_minutes"),
        ("descript", {"project_name": "{title}"}, "placeholders"),
        ("descript", {"project_name": " "}, "descript.project_name can't be empty"),
        ("descript", {"api_url": "descriptapi.com"}, "descript.api_url"),
    ],
)
def test_rejects_out_of_range_values(section, values, message):
    with pytest.raises(ConfigError, match=message):
        parse_config({"recordings_dir": "/x", section: values})


def test_extensions_are_normalized():
    config = parse_config({"recordings_dir": "/x", "scan": {"extensions": ["MOV", ".Mp4"]}})
    assert config.scan.extensions == (".mov", ".mp4")


def test_slack_tokens_come_from_the_environment():
    config = parse_config(
        {"recordings_dir": "/x", "slack": {"bot_token": "xoxb-file", "channel": "C1"}},
        env={"SLACK_APP_TOKEN": "xapp-env"},
    )

    assert config.slack.bot_token == "xoxb-file"
    assert config.slack.app_token == "xapp-env"
    assert config.slack.enabled


def test_environment_tokens_are_validated():
    with pytest.raises(ConfigError, match="xoxb-"):
        parse_config({"recordings_dir": "/x"}, env={"SLACK_BOT_TOKEN": "nope"})


def test_slack_lists_missing_settings():
    assert SlackConfig(bot_token="xoxb-1").missing() == ["slack.app_token", "slack.channel"]


def test_tokens_are_not_in_repr():
    assert "xoxb-secret" not in repr(SlackConfig(bot_token="xoxb-secret"))


def test_config_path_precedence(tmp_path):
    explicit = tmp_path / "explicit.toml"
    from_env = tmp_path / "env.toml"
    env = {"TRANSCODER_BOT_CONFIG": str(from_env)}

    assert resolve_config_path(explicit, env) == explicit
    assert resolve_config_path(None, env) == from_env
    assert resolve_config_path(None, {}) == DEFAULT_CONFIG_PATH.expanduser()


def test_load_config_reads_a_file(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('recordings_dir = "/Volumes/NAS"\n[video]\ncrf = 18\n')

    config = load_config(path, env={})

    assert config.recordings_dir == Path("/Volumes/NAS")
    assert config.video.crf == 18


def test_load_config_explains_a_missing_file(tmp_path):
    with pytest.raises(ConfigError, match="init-config"):
        load_config(tmp_path / "nope.toml", env={})


def test_load_config_reports_invalid_toml(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("recordings_dir = \n")
    with pytest.raises(ConfigError, match="isn't valid TOML"):
        load_config(path, env={})


def test_warns_when_tokens_are_readable_by_others(tmp_path, caplog):
    path = tmp_path / "config.toml"
    path.write_text('recordings_dir = "/x"\n[slack]\nbot_token = "xoxb-1"\n')
    path.chmod(0o644)

    with caplog.at_level(logging.WARNING):
        load_config(path, env={})

    assert "chmod 600" in caplog.text


def test_write_example_config_is_private(tmp_path):
    dest = tmp_path / "nested" / "config.toml"

    write_example_config(dest)

    assert stat.S_IMODE(dest.stat().st_mode) == 0o600
    assert dest.read_text() == example_config_text()
    with pytest.raises(ConfigError, match="already exists"):
        write_example_config(dest)
    write_example_config(dest, force=True)


def test_descript_is_off_until_it_has_a_key():
    config = parse_config({"recordings_dir": "/x"})
    assert not config.descript.enabled
    assert config.descript.project_name == "{date} {stem}"

    config = parse_config({"recordings_dir": "/x"}, env={"DESCRIPT_API_KEY": "dx_bearer_a:dx_b"})
    assert config.descript.enabled
    assert config.descript.api_key == "dx_bearer_a:dx_b"
    assert "dx_bearer_a" not in repr(config.descript)


@pytest.mark.parametrize(
    ("text", "kbps"), [("25M", 25_000), ("25m", 25_000), ("12.5M", 12_500), ("8000k", 8_000)]
)
def test_bitrate_kbps(text, kbps):
    assert bitrate_kbps(text) == kbps


def test_video_aims_for_25_mbps_by_default():
    config = parse_config({"recordings_dir": "/x"})
    assert config.video.bitrate == "25M"
    assert parse_config({"recordings_dir": "/x", "video": {"bitrate": ""}}).video.bitrate == ""
