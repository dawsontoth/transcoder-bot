import errno
from pathlib import Path

import pytest

from tests.helpers import NOW, touch
from transcoder_bot.config import TrashConfig
from transcoder_bot.trash import TrashError, describe_action, purge_trash, trash_files

TODAY = NOW.astimezone().date().isoformat()


@pytest.fixture
def folder(tmp_path):
    folder = tmp_path / "HyperDeck"
    folder.mkdir()
    return folder


def test_moves_files_into_a_dated_trash_folder(folder):
    a, b = touch(folder / "a.mov"), touch(folder / "b.mov")

    results = trash_files([a, b], recordings_dir=folder, trash=TrashConfig(), now=NOW)

    day = folder / "_Trash" / TODAY
    assert [r.destination for r in results] == [day / "a.mov", day / "b.mov"]
    assert sorted(p.name for p in day.iterdir()) == ["a.mov", "b.mov"]
    assert not a.exists()
    assert not b.exists()


def test_never_overwrites_something_already_in_the_trash(folder):
    day = folder / "_Trash" / TODAY
    day.mkdir(parents=True)
    (day / "a.mov").write_text("trashed earlier")
    a = touch(folder / "a.mov")

    (result,) = trash_files([a], recordings_dir=folder, trash=TrashConfig(), now=NOW)

    assert result.destination == day / "a (2).mov"
    assert (day / "a.mov").read_text() == "trashed earlier"


def test_absolute_trash_folder(folder, tmp_path):
    a = touch(folder / "a.mov")
    config = TrashConfig(folder=str(tmp_path / "Bin"))

    (result,) = trash_files([a], recordings_dir=folder, trash=config, now=NOW)

    assert result.destination == tmp_path / "Bin" / TODAY / "a.mov"


def test_refuses_files_outside_the_recordings_folder(folder, tmp_path):
    ok = touch(folder / "ok.mov")
    outside = touch(tmp_path / "outside.mov")

    with pytest.raises(TrashError, match="isn't directly inside"):
        trash_files([ok, outside], recordings_dir=folder, trash=TrashConfig(), now=NOW)

    assert ok.exists()  # nothing is touched unless everything checks out
    assert outside.exists()


def test_refuses_nested_files_and_folders(folder):
    (folder / "sub").mkdir()
    nested = touch(folder / "sub" / "x.mov")

    with pytest.raises(TrashError, match="isn't directly inside"):
        trash_files([nested], recordings_dir=folder, trash=TrashConfig(), now=NOW)
    with pytest.raises(TrashError, match="isn't a file"):
        trash_files([folder / "sub"], recordings_dir=folder, trash=TrashConfig(), now=NOW)


def test_delete_mode(folder):
    a = touch(folder / "a.mov")

    (result,) = trash_files([a], recordings_dir=folder, trash=TrashConfig(mode="delete"), now=NOW)

    assert result.destination is None
    assert list(folder.iterdir()) == []


def test_dry_run_changes_nothing(folder):
    a = touch(folder / "a.mov")

    (result,) = trash_files([a], recordings_dir=folder, trash=TrashConfig(), now=NOW, dry_run=True)

    assert result.destination == folder / "_Trash" / TODAY / "a.mov"
    assert [p.name for p in folder.iterdir()] == ["a.mov"]


def test_falls_back_to_copying_across_volumes(folder, monkeypatch):
    a = touch(folder / "a.mov")
    moved = []

    def cross_device(self: Path, target: Path) -> Path:
        raise OSError(errno.EXDEV, "Cross-device link")

    monkeypatch.setattr(Path, "rename", cross_device)
    monkeypatch.setattr(
        "transcoder_bot.trash.shutil.move", lambda src, dst: moved.append((src, dst))
    )

    trash_files([a], recordings_dir=folder, trash=TrashConfig(), now=NOW)

    assert moved == [(a, folder / "_Trash" / TODAY / "a.mov")]


def make_trash(folder: Path) -> Path:
    root = folder / "_Trash"
    for name in ("2026-09-10", "2026-09-19", "2026-09-26"):
        (root / name).mkdir(parents=True)
        touch(root / name / "x.mov")
    (root / "keep-me").mkdir()
    (root / "2026-09-01").write_text("a file, not a day folder")
    return root


def test_purges_day_folders_older_than_the_retention(folder):
    root = make_trash(folder)

    purged = purge_trash(folder, TrashConfig(retention_days=7), now=NOW)

    assert [p.name for p in purged] == ["2026-09-10", "2026-09-19"]
    assert sorted(p.name for p in root.iterdir()) == ["2026-09-01", "2026-09-26", "keep-me"]


def test_purge_dry_run(folder):
    root = make_trash(folder)

    purged = purge_trash(folder, TrashConfig(retention_days=7), now=NOW, dry_run=True)

    assert len(purged) == 2
    assert len(list(root.iterdir())) == 5


@pytest.mark.parametrize("config", [TrashConfig(retention_days=0), TrashConfig(mode="delete")])
def test_purge_disabled(folder, config):
    make_trash(folder)
    assert purge_trash(folder, config, now=NOW) == []


def test_describe_action():
    assert describe_action(TrashConfig()) == (
        "moved to `_Trash` on the NAS (and deleted for good after 7 days)"
    )
    assert describe_action(TrashConfig(retention_days=1)).endswith("after 1 day)")
    assert describe_action(TrashConfig(retention_days=0)) == "moved to `_Trash` on the NAS"
    assert describe_action(TrashConfig(mode="delete")) == "*permanently deleted*"


def test_skips_files_that_are_already_gone(folder):
    a = touch(folder / "a.mov")

    results = trash_files(
        [folder / "gone.mov", a], recordings_dir=folder, trash=TrashConfig(), now=NOW
    )

    assert [r.source.name for r in results] == ["a.mov"]
