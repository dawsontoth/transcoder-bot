import dataclasses
import json
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from tests.helpers import NOW
from transcoder_bot.recordings import Recording
from transcoder_bot.slack_messages import (
    PICK_ACTION_PREFIX,
    SKIP_ACTION,
    Click,
    Decision,
    Poll,
    PollOption,
    closed_blocks,
    escape,
    parse_click,
    poll_blocks,
    poll_text,
    resolve_click,
    slack_date,
)


def option(name: str, hours_ago: float = 1, size: int = 70 * 2**30) -> PollOption:
    recording = Recording(Path("/NAS") / name, size, NOW - timedelta(hours=hours_ago))
    return PollOption(recording, duration=2712.0)


def make_poll(*names: str, **overrides: Any) -> Poll:
    poll = Poll(
        id="poll1",
        options=tuple(option(n) for n in names),
        deadline=NOW + timedelta(hours=12),
        folder="/Volumes/HyperDeck",
        lookback_hours=48.0,
        transcode_summary="rotated 90° counter-clockwise, scaled to 1080p and normalized",
        trash_action="moved to `_Trash` on the NAS",
    )
    return dataclasses.replace(poll, **overrides)


def buttons(blocks):
    found = [b["accessory"] for b in blocks if "accessory" in b]
    for block in blocks:
        if block["type"] == "actions":
            found += block["elements"]
    return found


def test_poll_lists_each_recording_with_a_button():
    poll = make_poll("Service_0930.mov", "Service_1100.mov")

    blocks = poll_blocks(poll)

    assert blocks[0]["type"] == "header"
    assert "Which recording should we keep?" in blocks[0]["text"]["text"]
    assert "*2 recordings*" in blocks[1]["text"]["text"]
    assert "moved to `_Trash` on the NAS" in blocks[1]["text"]["text"]
    options = [b for b in blocks if "accessory" in b]
    assert "*Service_0930.mov*" in options[0]["text"]["text"]
    assert "45m 12s · 70.0 GiB" in options[0]["text"]["text"]
    assert "<!date^" in options[0]["text"]["text"]
    assert len(blocks) <= 50


def test_pick_buttons_carry_the_poll_and_file_and_ask_for_confirmation():
    poll = make_poll("a.mov", "b.mov", "c.mov")

    pick = buttons(poll_blocks(poll))[1]

    assert pick["action_id"] == f"{PICK_ACTION_PREFIX}1"
    assert json.loads(pick["value"]) == {"poll": "poll1", "file": "b.mov"}
    assert pick["confirm"]["style"] == "danger"
    assert "The other 2 recordings will be moved to `_Trash`" in pick["confirm"]["text"]["text"]


def test_action_ids_are_unique():
    action_ids = [b["action_id"] for b in buttons(poll_blocks(make_poll("a.mov", "b.mov")))]
    assert len(action_ids) == len(set(action_ids)) == 3
    assert SKIP_ACTION in action_ids


def test_single_recording_poll():
    blocks = poll_blocks(make_poll("only.mov"))

    assert "*1 recording*" in blocks[1]["text"]["text"]
    pick = buttons(blocks)[0]
    assert pick["confirm"]["style"] == "primary"
    assert "other" not in pick["confirm"]["text"]["text"]


def test_notes_and_dry_run():
    poll = make_poll("a.mov", unreadable=("live.mov",), omitted=3, dry_run=True)

    blocks = poll_blocks(poll)

    assert "[Dry run]" in blocks[0]["text"]["text"]
    context = blocks[-1]["elements"][0]["text"]
    assert "nothing is deleted" in context
    assert "live.mov" in context
    assert "3 older recording(s)" in context
    assert "Dry run" in context


def test_text_limits_are_respected():
    poll = make_poll("x" * 400 + ".mov")

    blocks = poll_blocks(poll)

    confirm = buttons(blocks)[0]["confirm"]
    assert len(confirm["text"]["text"]) <= 300
    assert len(confirm["title"]["text"]) <= 100
    assert all(len(b["text"]["text"]) <= 75 for b in buttons(blocks))
    assert len(blocks[0]["text"]["text"]) <= 150


def test_file_names_are_escaped():
    blocks = poll_blocks(make_poll("Q&A <live>.mov"))
    assert "*Q&amp;A &lt;live&gt;.mov*" in blocks[3]["text"]["text"]
    assert escape("a<b>&c") == "a&lt;b&gt;&amp;c"


def test_poll_text():
    assert poll_text(make_poll("a.mov", "b.mov")) == (
        "Which recording should we keep? 2 from the last 48 hours."
    )


def test_slack_date():
    rendered = slack_date(NOW)
    assert rendered.startswith(f"<!date^{int(NOW.timestamp())}^{{date_short_pretty}} at {{time}}|")


def test_closed_poll_has_no_buttons():
    poll = make_poll("a.mov", "b.mov", "c.mov")

    blocks = closed_blocks(
        poll, headline="picked", status="done", chosen_key="b.mov", trashed_keys={"a.mov"}
    )

    assert buttons(blocks) == []
    assert blocks[1]["text"]["text"].splitlines() == ["🗑️ ~a.mov~", "✅ *b.mov*", "• c.mov"]
    assert blocks[2]["elements"][0]["text"] == "done"


def payload(action_id: str, value: str, user: str = "U1", **extra):
    return {
        "type": "block_actions",
        "user": {"id": user},
        "response_url": "https://hooks.slack.com/actions/x",
        "actions": [{"action_id": action_id, "value": value}],
        **extra,
    }


def test_parse_pick_click():
    click = parse_click(payload(f"{PICK_ACTION_PREFIX}0", '{"poll": "p", "file": "a.mov"}'))
    assert click == Click("p", "U1", "a.mov", "https://hooks.slack.com/actions/x")


def test_parse_skip_click():
    click = parse_click(payload(SKIP_ACTION, '{"poll": "p"}'))
    assert click is not None
    assert click.file is None


@pytest.mark.parametrize(
    "data",
    [
        {"type": "view_submission"},
        payload("some_other_app", '{"poll": "p"}'),
        payload(SKIP_ACTION, "not json"),
        payload(SKIP_ACTION, '["p"]'),
        payload(f"{PICK_ACTION_PREFIX}0", '{"poll": "p"}'),
        payload(SKIP_ACTION, '{"poll": "p"}', user=""),
    ],
)
def test_ignores_other_payloads(data):
    assert parse_click(data) is None


def test_resolve_click():
    poll = make_poll("a.mov", "b.mov")

    assert resolve_click(Click("poll1", "U1", "b.mov"), poll) == (
        Decision("U1", poll.options[1]),
        None,
    )
    assert resolve_click(Click("poll1", "U1", None), poll) == (Decision("U1", None), None)
    assert resolve_click(Click("old", "U1", "a.mov"), poll)[1] == "That poll has already closed."
    assert "isn't part of this poll" in str(resolve_click(Click("poll1", "U1", "z.mov"), poll)[1])
    decision, rejection = resolve_click(Click("poll1", "U2", "a.mov"), poll, {"U1"})
    assert decision is None
    assert rejection is not None
    assert "not one of the people" in rejection
