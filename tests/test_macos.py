import subprocess
from pathlib import Path
from typing import Any

import pytest

from transcoder_bot import macos
from transcoder_bot.macos import ensure_mounted, keep_awake


class FakeRun:
    def __init__(self, on_call: Any = None, returncode: int = 0) -> None:
        self.calls: list[list[str]] = []
        self.on_call = on_call
        self.returncode = returncode

    def __call__(self, cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        self.calls.append(cmd)
        if self.on_call is not None:
            self.on_call()
        return subprocess.CompletedProcess(cmd, self.returncode, "", "")


def test_existing_folder_needs_no_mounting(tmp_path):
    run = FakeRun()
    ensure_mounted(tmp_path, "smb://nas/share", run=run)
    assert run.calls == []


def test_missing_folder_without_mount_url(tmp_path):
    with pytest.raises(FileNotFoundError, match="Is the NAS mounted"):
        ensure_mounted(tmp_path / "gone", "")


def test_mounts_the_share_with_applescript(tmp_path, monkeypatch):
    monkeypatch.setattr(macos, "IS_MACOS", True)
    folder = tmp_path / "HyperDeck"
    run = FakeRun(on_call=folder.mkdir)

    ensure_mounted(folder, 'smb://nas.local/Hyper"Deck', run=run, sleep=lambda s: None)

    assert run.calls == [["osascript", "-e", 'mount volume "smb://nas.local/Hyper\\"Deck"']]


def test_mount_that_never_appears(tmp_path, monkeypatch):
    monkeypatch.setattr(macos, "IS_MACOS", True)

    with pytest.raises(FileNotFoundError, match="still isn't there"):
        ensure_mounted(tmp_path / "gone", "smb://nas/share", timeout=0, run=FakeRun(returncode=1))


def test_mount_stuck_at_a_login_dialog(tmp_path, monkeypatch):
    monkeypatch.setattr(macos, "IS_MACOS", True)

    def hang(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(cmd, 60)

    with pytest.raises(FileNotFoundError, match="Remember this password"):
        ensure_mounted(tmp_path / "gone", "smb://nas/share", run=hang)


def test_leftover_mount_point_folder_is_not_the_share(monkeypatch):
    # /Volumes/HyperDeck exists but is just a folder on the same volume as /Volumes.
    same_device = type("Stat", (), {"st_dev": 1})()
    monkeypatch.setattr(Path, "is_dir", lambda self: True)
    monkeypatch.setattr(Path, "stat", lambda self, **kwargs: same_device)
    assert not macos.is_available(Path("/Volumes/HyperDeck"))
    assert macos.is_available(Path("/Users/me/Movies"))


def test_keep_awake_is_a_no_op_off_macos(monkeypatch):
    monkeypatch.setattr(macos, "IS_MACOS", False)
    with keep_awake():
        pass


def test_keep_awake_runs_caffeinate_for_our_pid(monkeypatch):
    started: list[list[str]] = []

    class FakeProcess:
        def __init__(self, cmd: list[str]) -> None:
            started.append(cmd)
            self.stopped = False

        def terminate(self) -> None:
            self.stopped = True

        def wait(self) -> int:
            return 0

    monkeypatch.setattr(macos, "IS_MACOS", True)
    monkeypatch.setattr("transcoder_bot.macos.shutil.which", lambda name: "/usr/bin/caffeinate")
    monkeypatch.setattr("transcoder_bot.macos.subprocess.Popen", FakeProcess)
    monkeypatch.setattr("transcoder_bot.macos.os.getpid", lambda: 4242)

    with keep_awake():
        assert started == [["/usr/bin/caffeinate", "-i", "-w", "4242"]]
