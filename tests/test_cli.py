import logging
import os
import time
from pathlib import Path

import pytest

from tests.fake_descript import GOOD_KEY, serve_fake_descript
from transcoder_bot.cli import main


@pytest.fixture
def config_file(tmp_path, monkeypatch):
    monkeypatch.setenv("TRANSCODER_BOT_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    monkeypatch.delenv("SLACK_APP_TOKEN", raising=False)
    monkeypatch.delenv("DESCRIPT_API_KEY", raising=False)
    folder = tmp_path / "HyperDeck"
    folder.mkdir()
    path = tmp_path / "config.toml"
    path.write_text(f'recordings_dir = "{folder}"\n')
    return path


def test_init_config(tmp_path, capsys):
    path = tmp_path / "config" / "config.toml"

    assert main(["--config", str(path), "init-config"]) == 0
    assert path.exists()
    assert "transcoder-bot doctor" in capsys.readouterr().out
    assert main(["--config", str(path), "init-config"]) == 2  # won't overwrite


def test_missing_config_is_explained(tmp_path, caplog):
    with caplog.at_level(logging.ERROR):
        assert main(["--config", str(tmp_path / "nope.toml"), "scan"]) == 2
    assert "init-config" in caplog.text


def aged(path: Path, seconds: float) -> Path:
    """Create ``path`` as if it was last written ``seconds`` ago (in real time)."""
    path.write_bytes(b"\0" * 1024)
    stamp = time.time() - seconds
    os.utime(path, (stamp, stamp))
    return path


def test_scan_lists_recordings(config_file, capsys):
    folder = config_file.parent / "HyperDeck"
    aged(folder / "Earlier.mov", 3600)
    aged(folder / "Recording-now.mov", 10)  # too fresh: may still be recording

    assert main(["--config", str(config_file), "scan"]) == 0

    out = capsys.readouterr().out
    assert "Earlier.mov" in out
    assert "unreadable" in out  # it's not a real video
    assert "Recording-now.mov" not in out


def test_transcode_needs_files_or_new(config_file, caplog):
    with caplog.at_level(logging.ERROR):
        assert main(["--config", str(config_file), "transcode"]) == 2
    assert "--new" in caplog.text


def test_transcode_dry_run_reports_unreadable_files(config_file, tmp_path, caplog):
    bogus = tmp_path / "HyperDeck" / "bogus.mov"
    bogus.write_text("not a video")

    with caplog.at_level(logging.ERROR):
        assert main(["--config", str(config_file), "transcode", "--dry-run", str(bogus)]) == 1
    assert "bogus.mov" in caplog.text


def test_poll_requires_slack_settings(config_file, caplog):
    with caplog.at_level(logging.ERROR):
        assert main(["--config", str(config_file), "poll"]) == 2
    assert "slack.bot_token" in caplog.text


def test_unmounted_nas_is_a_clean_error(tmp_path, caplog, monkeypatch):
    monkeypatch.setenv("TRANSCODER_BOT_STATE_DIR", str(tmp_path / "state"))
    path = tmp_path / "config.toml"
    path.write_text(f'recordings_dir = "{tmp_path / "unmounted"}"\n')

    with caplog.at_level(logging.ERROR):
        assert main(["--config", str(path), "transcode", "--new"]) == 1
    assert "Is the NAS mounted" in caplog.text


def test_schedule_print(config_file, capsys):
    assert main(["--config", str(config_file), "schedule", "print", "--at", "17:45"]) == 0

    out = capsys.readouterr().out
    assert "<key>Hour</key>\n\t\t<integer>17</integer>" in out
    assert "<string>transcode</string>\n\t\t<string>--new</string>" in out  # no Slack config
    assert f"<string>{config_file.absolute()}</string>" in out


def test_purge_trash_dry_run(config_file, capsys):
    old = config_file.parent / "HyperDeck" / "_Trash" / "2020-01-01"
    old.mkdir(parents=True)

    assert main(["--config", str(config_file), "purge-trash", "--dry-run"]) == 0

    assert f"Would delete {old}" in capsys.readouterr().out
    assert old.exists()


def test_version(capsys):
    with pytest.raises(SystemExit):
        main(["--version"])
    assert "transcoder-bot" in capsys.readouterr().out


def test_descript_upload_needs_a_key(config_file, tmp_path, caplog):
    video = tmp_path / "HyperDeck" / "Service_1080p.mp4"
    video.write_bytes(b"video")

    with caplog.at_level(logging.ERROR):
        assert main(["--config", str(config_file), "descript-upload", str(video)]) == 2
    assert "DESCRIPT_API_KEY" in caplog.text


def test_descript_upload_names_the_project_after_the_recording(config_file, monkeypatch):
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    monkeypatch.setenv("no_proxy", "127.0.0.1")
    folder = config_file.parent / "HyperDeck"
    recording = aged(folder / "Service.mov", 3 * 86400)
    video = folder / "Service_1080p.mp4"
    video.write_bytes(b"video")
    recorded = time.strftime("%Y-%m-%d", time.localtime(recording.stat().st_mtime))

    with serve_fake_descript() as fake:
        config_file.write_text(
            config_file.read_text()
            + f'[descript]\napi_key = "{GOOD_KEY}"\napi_url = "{fake.base_url}"\n'
            + "wait_minutes = 0\n"
        )
        config_file.chmod(0o600)
        assert main(["--config", str(config_file), "descript-upload", str(video)]) == 0

    (request,) = fake.imports
    assert request["project_name"] == f"{recorded} Service"
    assert fake.uploads == {"Service_1080p.mp4": b"video"}
