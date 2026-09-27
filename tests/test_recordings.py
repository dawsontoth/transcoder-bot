from datetime import timedelta

import pytest

from tests.helpers import NOW, make_config, touch
from transcoder_bot.recordings import find_recordings, find_untranscoded


@pytest.fixture
def folder(tmp_path):
    folder = tmp_path / "HyperDeck"
    folder.mkdir()
    return folder


def names(recordings):
    return [r.name for r in recordings]


def test_finds_recent_recordings_oldest_first(folder):
    touch(folder / "Service_B.mov", age=timedelta(hours=2))
    touch(folder / "Service_A.MOV", age=timedelta(hours=30))
    touch(folder / "Clip.mp4", age=timedelta(minutes=30))
    touch(folder / "Clip.mxf", age=timedelta(hours=47))

    found = find_recordings(make_config(folder), now=NOW)

    assert names(found) == ["Clip.mxf", "Service_A.MOV", "Service_B.mov", "Clip.mp4"]
    assert found[0].size == 1024
    assert found[0].modified == NOW - timedelta(hours=47)


def test_skips_everything_that_is_not_a_finished_recording(folder):
    touch(folder / "keep.mov")
    touch(folder / "too-old.mov", age=timedelta(hours=49))
    touch(folder / "still-recording.mov", age=timedelta(minutes=2))
    touch(folder / "._keep.mov")  # macOS AppleDouble file on an SMB share
    touch(folder / ".DS_Store")
    touch(folder / ".keep_1080p.mp4.123.partial")
    touch(folder / "keep_1080p.mp4")  # our own output
    touch(folder / "notes.txt")
    touch(folder / "empty.mov", size=0)
    (folder / "folder.mov").mkdir()
    (folder / "_Trash" / "2026-09-26").mkdir(parents=True)
    touch(folder / "_Trash" / "2026-09-26" / "trashed.mov")

    assert names(find_recordings(make_config(folder), now=NOW)) == ["keep.mov"]


def test_window_follows_the_config(folder):
    touch(folder / "a.mov", age=timedelta(hours=60))
    touch(folder / "b.mov", age=timedelta(minutes=2))
    config = make_config(folder, scan={"lookback_hours": 72, "min_age_minutes": 0})

    assert names(find_recordings(config, now=NOW)) == ["a.mov", "b.mov"]


def test_untranscoded_skips_recordings_with_an_output(folder):
    touch(folder / "done.mov")
    touch(folder / "done_1080p.mp4")
    touch(folder / "todo.mov")

    assert names(find_untranscoded(make_config(folder), now=NOW)) == ["todo.mov"]


def test_missing_folder(tmp_path):
    with pytest.raises(FileNotFoundError, match="Is the NAS mounted"):
        find_recordings(make_config(tmp_path / "gone"), now=NOW)
