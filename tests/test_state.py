from pathlib import Path

import pytest

from transcoder_bot import state as state_module
from transcoder_bot.state import (
    AlreadyRunningError,
    PendingPoll,
    StateStore,
    default_state_dir,
)


def test_lock_is_exclusive(tmp_path):
    store = StateStore(tmp_path)

    with store.lock(), pytest.raises(AlreadyRunningError, match="in progress"), store.lock():
        pass

    with store.lock():  # released again
        pass


def test_pending_poll_round_trip(tmp_path):
    store = StateStore(tmp_path / "state")
    assert store.pending_poll() is None

    poll = PendingPoll(channel="C1", ts="1700000000.000100", poll_id="abc")
    store.set_pending_poll(poll)
    assert store.pending_poll() == poll

    store.clear_pending_poll()
    assert store.pending_poll() is None
    store.clear_pending_poll()  # clearing twice is fine


def test_corrupt_pending_poll_is_ignored(tmp_path):
    (tmp_path / "pending-poll.json").write_text("{not json")
    assert StateStore(tmp_path).pending_poll() is None


def test_default_state_dir(monkeypatch):
    assert default_state_dir({"TRANSCODER_BOT_STATE_DIR": "/tmp/x"}) == Path("/tmp/x")

    monkeypatch.setattr(state_module, "IS_MACOS", True)
    assert default_state_dir({}) == (
        Path.home() / "Library" / "Application Support" / "transcoder-bot"
    )

    monkeypatch.setattr(state_module, "IS_MACOS", False)
    assert default_state_dir({"XDG_STATE_HOME": "/xdg"}) == Path("/xdg/transcoder-bot")
