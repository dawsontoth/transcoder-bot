from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from tests.helpers import NOW, make_config, media, touch
from transcoder_bot.config import Config
from transcoder_bot.loudnorm import LoudnessStats
from transcoder_bot.media import MediaInfo, ProbeError
from transcoder_bot.poll import PollRunner, describe_transcode
from transcoder_bot.runner import Progress, ProgressCallback
from transcoder_bot.slack_messages import Decision, Poll
from transcoder_bot.state import PendingPoll, StateStore
from transcoder_bot.transcode import TranscodeResult, output_path_for

TODAY = NOW.astimezone().date().isoformat()


@dataclass
class FakeSlack:
    decide: Callable[[Poll], Decision | None]
    channel: str = "C1"
    posts: list[dict[str, Any]] = field(default_factory=list)
    updates: list[dict[str, Any]] = field(default_factory=list)
    poll: Poll | None = None

    def open_poll(self, poll: Poll) -> None:
        self.poll = poll

    def wait_for_decision(self, timeout: float) -> Decision | None:
        assert self.poll is not None
        return self.decide(self.poll)

    def post(self, text: str, *, blocks=None, thread_ts=None) -> str:
        ts = f"{len(self.posts) + 1}.0"
        self.posts.append({"ts": ts, "text": text, "blocks": blocks, "thread_ts": thread_ts})
        return ts

    def update(self, ts: str, text: str, *, blocks=None) -> None:
        self.updates.append({"ts": ts, "text": text, "blocks": blocks})

    def last_status(self) -> str:
        """The context line of the most recent update to the poll message."""
        blocks = [u["blocks"] for u in self.updates if u["ts"] == "1.0" and u["blocks"]][-1]
        return str(blocks[-1]["elements"][0]["text"])

    def headline(self) -> str:
        blocks = [u["blocks"] for u in self.updates if u["ts"] == "1.0" and u["blocks"]][-1]
        return str(blocks[0]["text"]["text"])

    def thread(self) -> list[str]:
        texts = [p["text"] for p in self.posts if p["thread_ts"]]
        return texts + [u["text"] for u in self.updates if u["ts"] != "1.0"]


class FakeTranscoder:
    def __init__(self, config: Config, *, error: Exception | None = None) -> None:
        self.config = config
        self.error = error
        self.calls: list[Path] = []

    def output_path(self, source: Path) -> Path:
        return output_path_for(source, self.config.output)

    def transcode(
        self, source: Path, *, force: bool = False, on_progress: ProgressCallback | None = None
    ) -> TranscodeResult:
        self.calls.append(source)
        if on_progress is not None:
            on_progress(Progress("encoding", 0.5, 2.0, 60))
        if self.error is not None:
            raise self.error
        output = self.output_path(source)
        output.write_bytes(b"x" * 2048)
        stats = LoudnessStats(-27.6, -4.5, 8.0, -38.2, 0.5, output_i=-16.0)
        return TranscodeResult(source, output, 2700.0, 600.0, 2048, stats, stats)


def pick(name: str, user: str = "U1") -> Callable[[Poll], Decision]:
    def decide(poll: Poll) -> Decision:
        option = poll.option(name)
        assert option is not None
        return Decision(user, option)

    return decide


def probe_ok(path: Path) -> MediaInfo:
    return media(duration=2712.0)


@pytest.fixture
def folder(tmp_path):
    folder = tmp_path / "HyperDeck"
    folder.mkdir()
    touch(folder / "take1.mov", age=timedelta(hours=3))
    touch(folder / "take2.mov", age=timedelta(hours=2))
    touch(folder / "take3.mov", age=timedelta(hours=1))
    return folder


def make_runner(folder: Path, slack: FakeSlack, **kwargs: Any) -> PollRunner:
    config = kwargs.pop("config", None) or make_config(folder)
    transcoder = kwargs.pop("transcoder", None) or FakeTranscoder(config)
    kwargs.setdefault("probe_fn", probe_ok)
    return PollRunner(config, slack, transcoder, now=lambda: NOW, **kwargs)


def test_pick_trashes_the_others_and_transcodes_the_pick(folder, tmp_path):
    slack = FakeSlack(pick("take2.mov"))
    state = StateStore(tmp_path / "state")
    runner = make_runner(folder, slack, state=state)

    outcome = runner.run()

    assert outcome.status == "done"
    assert outcome.chosen == folder / "take2.mov"
    assert runner.transcoder.calls == [folder / "take2.mov"]  # type: ignore[attr-defined]
    assert sorted(p.name for p in folder.iterdir()) == ["_Trash", "take2.mov", "take2_1080p.mp4"]
    assert sorted(p.name for p in (folder / "_Trash" / TODAY).iterdir()) == [
        "take1.mov",
        "take3.mov",
    ]
    # The poll itself.
    poll_post = slack.posts[0]
    assert poll_post["thread_ts"] is None
    assert [o.key for o in slack.poll.options] == ["take1.mov", "take2.mov", "take3.mov"]  # type: ignore[union-attr]
    # Progress and results in the thread, final status on the poll message.
    thread = slack.thread()
    assert any("2 recording(s) moved to `_Trash`" in t for t in thread)
    assert any(t.startswith("⚙️ Encoding 50%") for t in thread)
    assert any("✅ Saved *take2_1080p.mp4*" in t and "now -16.0 LUFS" in t for t in thread)
    assert "<@U1> picked *take2.mov*" in slack.headline()
    assert slack.last_status() == "✅ Saved *take2_1080p.mp4* next to the original."
    assert state.pending_poll() is None


def test_skip_changes_nothing(folder):
    slack = FakeSlack(lambda poll: Decision("U1", None))
    runner = make_runner(folder, slack)

    outcome = runner.run()

    assert outcome.status == "skipped"
    assert runner.transcoder.calls == []  # type: ignore[attr-defined]
    assert sorted(p.name for p in folder.iterdir()) == ["take1.mov", "take2.mov", "take3.mov"]
    assert "skipped this one" in slack.headline()


def test_timeout_changes_nothing(folder):
    slack = FakeSlack(lambda poll: None)

    outcome = make_runner(folder, slack).run(timeout_hours=2)

    assert outcome.status == "expired"
    assert len(list(folder.iterdir())) == 3
    assert "Nobody picked a recording within 2 hours" in slack.headline()


def test_nothing_new(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    slack = FakeSlack(lambda poll: None)

    assert make_runner(empty, slack).run().status == "nothing-new"
    assert slack.posts == []

    config = make_config(empty, slack={"notify_when_empty": True})
    make_runner(empty, slack, config=config).run()
    assert "No new recordings" in slack.posts[0]["text"]


def test_already_transcoded_and_unreadable_recordings_are_left_out(folder):
    touch(folder / "take1_1080p.mp4")

    def probe(path: Path) -> MediaInfo:
        if path.name == "take3.mov":
            raise ProbeError("moov atom not found")
        return media()

    slack = FakeSlack(lambda poll: None)
    make_runner(folder, slack, probe_fn=probe).run()

    assert slack.poll is not None
    assert [o.key for o in slack.poll.options] == ["take2.mov"]
    assert slack.poll.unreadable == ("take3.mov",)


def test_dry_run_moves_and_transcodes_nothing(folder):
    slack = FakeSlack(pick("take1.mov"))
    runner = make_runner(folder, slack, dry_run=True)

    outcome = runner.run()

    assert outcome.status == "done"
    assert len(outcome.trashed) == 2
    assert runner.transcoder.calls == []  # type: ignore[attr-defined]
    assert sorted(p.name for p in folder.iterdir()) == ["take1.mov", "take2.mov", "take3.mov"]
    assert any("Would transcode *take1.mov* → *take1_1080p.mp4*" in t for t in slack.thread())


def test_transcode_failure_is_reported(folder):
    slack = FakeSlack(pick("take1.mov"))
    config = make_config(folder)
    transcoder = FakeTranscoder(config, error=RuntimeError("ffmpeg failed (exit status 1)"))

    outcome = make_runner(folder, slack, config=config, transcoder=transcoder).run()

    assert outcome.status == "failed"
    assert outcome.error == "ffmpeg failed (exit status 1)"
    assert len(outcome.trashed) == 2
    assert any("❌ Transcoding *take1.mov* failed" in t for t in slack.thread())
    assert slack.last_status() == "❌ Transcoding failed. Details in the thread."


def test_recordings_deleted_by_hand_meanwhile_are_skipped(folder):
    def decide(poll: Poll) -> Decision:
        (folder / "take3.mov").unlink()
        return pick("take1.mov")(poll)

    outcome = make_runner(folder, FakeSlack(decide)).run()

    assert outcome.status == "done"
    assert [t.source.name for t in outcome.trashed] == ["take2.mov"]


def test_trash_problems_do_not_stop_the_transcode(folder):
    def decide(poll: Poll) -> Decision:
        (folder / "_Trash").write_text("a file where the trash folder should be")
        return pick("take1.mov")(poll)

    slack = FakeSlack(decide)
    runner = make_runner(folder, slack)

    outcome = runner.run()

    assert outcome.status == "done"
    assert outcome.trashed == ()
    assert any(t.startswith("⚠️ Couldn't trash: take2.mov (") for t in slack.thread())
    assert runner.transcoder.calls == [folder / "take1.mov"]  # type: ignore[attr-defined]


def test_progress_updates_can_be_turned_off(folder):
    slack = FakeSlack(pick("take1.mov"))
    config = make_config(folder, slack={"progress_update_seconds": 0})

    make_runner(folder, slack, config=config).run()

    assert not any("Encoding 50%" in t for t in slack.thread())


def test_an_interrupted_poll_is_closed(folder, tmp_path):
    def interrupted(poll: Poll) -> Decision:
        raise KeyboardInterrupt

    slack = FakeSlack(interrupted)
    state = StateStore(tmp_path / "state")

    with pytest.raises(KeyboardInterrupt):
        make_runner(folder, slack, state=state).run()

    assert "interrupted" in slack.headline()
    assert state.pending_poll() is None


def test_a_stale_poll_from_a_crashed_run_is_closed_first(folder, tmp_path):
    state = StateStore(tmp_path / "state")
    state.set_pending_poll(PendingPoll(channel="C1", ts="99.0", poll_id="old"))
    slack = FakeSlack(lambda poll: None)

    make_runner(folder, slack, state=state).run()

    assert slack.updates[0]["ts"] == "99.0"
    assert "interrupted" in slack.updates[0]["text"]
    assert state.pending_poll() is None


def test_unexpected_errors_are_posted_and_raised(folder):
    def unmounted() -> None:
        raise FileNotFoundError("Recordings folder /Volumes/HyperDeck not found")

    slack = FakeSlack(lambda poll: None)

    with pytest.raises(FileNotFoundError):
        make_runner(folder, slack, prepare=unmounted).run()

    assert slack.posts[0]["text"].startswith("❌ transcoder-bot ran into a problem: Recordings")


def test_old_trash_is_purged(folder):
    old = folder / "_Trash" / "2026-09-01"
    old.mkdir(parents=True)

    make_runner(folder, FakeSlack(lambda poll: None)).run()

    assert not old.exists()


def test_describe_transcode(tmp_path):
    assert describe_transcode(make_config(tmp_path)) == (
        "rotated 90° counter-clockwise, scaled to 1080p and loudness-normalized"
    )
    config = make_config(tmp_path, video={"rotate": "none"}, audio={"normalize": False})
    assert describe_transcode(config) == "scaled to 1080p"
