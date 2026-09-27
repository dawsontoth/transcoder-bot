"""Slack Block Kit messages for the recording poll, and parsing button clicks (no network)."""

from __future__ import annotations

import json
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from transcoder_bot.formatting import human_duration, human_size
from transcoder_bot.recordings import Recording

Block = dict[str, Any]

PICK_ACTION_PREFIX = "transcoder_bot_pick_"
SKIP_ACTION = "transcoder_bot_skip"
# Slack allows 50 blocks per message, but big messages also hit an (undocumented) total size
# limit, so keep polls short. Older extras are left out of the poll, and left alone.
MAX_OPTIONS = 15


@dataclass(frozen=True)
class PollOption:
    recording: Recording
    duration: float | None = None

    @property
    def key(self) -> str:
        return self.recording.name


@dataclass(frozen=True)
class Poll:
    id: str
    options: tuple[PollOption, ...]
    deadline: datetime
    folder: str
    lookback_hours: float
    transcode_summary: str  # e.g. "rotated 90° counter-clockwise, scaled to 1080p and ..."
    trash_action: str  # e.g. "moved to `_Trash` on the NAS", follows "will be"
    unreadable: tuple[str, ...] = ()
    omitted: int = 0  # older recordings that didn't fit in the poll
    dry_run: bool = False

    def option(self, key: str) -> PollOption | None:
        return next((o for o in self.options if o.key == key), None)


@dataclass(frozen=True)
class Decision:
    user_id: str
    option: PollOption | None  # None means "skip, change nothing"


@dataclass(frozen=True)
class Click:
    poll_id: str
    user_id: str
    file: str | None  # None for the skip button
    response_url: str | None = None


def escape(text: str) -> str:
    """Escape the characters Slack's mrkdwn treats as control characters."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def slack_date(when: datetime) -> str:
    """A timestamp Slack renders in each reader's own time zone, e.g. "Yesterday at 9:30 AM"."""
    fallback = when.astimezone().strftime("%Y-%m-%d %H:%M")
    return f"<!date^{int(when.timestamp())}^{{date_short_pretty}} at {{time}}|{fallback}>"


def poll_text(poll: Poll) -> str:
    """Plain-text fallback used in notifications."""
    count = len(poll.options)
    return f"Which recording should we keep? {count} from the last {poll.lookback_hours:g} hours."


def poll_blocks(poll: Poll) -> list[Block]:
    count = len(poll.options)
    lead = (
        f"Found *{count} recordings* from the last {poll.lookback_hours:g} hours in "
        f"`{escape(poll.folder)}`. Pick the one to keep: it'll be {poll.transcode_summary}, "
        f"and saved next to the original. The others will be {poll.trash_action}."
        if count > 1
        else f"Found *1 recording* from the last {poll.lookback_hours:g} hours in "
        f"`{escape(poll.folder)}`. Keep it to have it {poll.transcode_summary}, and saved "
        "next to the original."
    )
    title = "Which recording should we keep?"
    blocks: list[Block] = [
        _header(f"🎬 {'[Dry run] ' if poll.dry_run else ''}{title}"),
        _section(lead),
        {"type": "divider"},
    ]
    for index, option in enumerate(poll.options):
        block = _section(_option_text(option))
        block["accessory"] = _pick_button(poll, index, option)
        blocks.append(block)

    skip = {
        "type": "button",
        "text": _plain("Skip, change nothing"),
        "action_id": SKIP_ACTION,
        "value": json.dumps({"poll": poll.id}),
    }
    blocks.append({"type": "actions", "elements": [skip]})

    notes = [f"Closes {slack_date(poll.deadline)}. If nobody picks by then, nothing is deleted."]
    if poll.unreadable:
        notes.append(
            "Couldn't read (still recording?): " + ", ".join(escape(n) for n in poll.unreadable)
        )
    if poll.omitted:
        notes.append(f"{poll.omitted} older recording(s) didn't fit here and won't be touched.")
    if poll.dry_run:
        notes.append("Dry run: nothing will actually be moved or transcoded.")
    blocks.append(_context(" ".join(notes)))
    return blocks


def closed_blocks(
    poll: Poll,
    *,
    headline: str,
    status: str,
    chosen_key: str | None = None,
    trashed_keys: Collection[str] = (),
) -> list[Block]:
    """The poll message once it's decided (buttons removed)."""
    lines = []
    for option in poll.options:
        name = escape(option.key)
        if option.key == chosen_key:
            lines.append(f"✅ *{name}*")
        elif option.key in trashed_keys:
            lines.append(f"🗑️ ~{name}~")
        else:
            lines.append(f"• {name}")
    return [_section(headline), _section(_truncate("\n".join(lines), 3000)), _context(status)]


def parse_click(payload: Mapping[str, Any]) -> Click | None:
    """Extract one of our button clicks from a ``block_actions`` interaction payload."""
    if payload.get("type") != "block_actions":
        return None
    user_id = (payload.get("user") or {}).get("id")
    if not isinstance(user_id, str) or not user_id:
        return None
    response_url = payload.get("response_url")
    for action in payload.get("actions") or []:
        action_id = str(action.get("action_id", ""))
        is_pick = action_id.startswith(PICK_ACTION_PREFIX)
        if not is_pick and action_id != SKIP_ACTION:
            continue
        try:
            value = json.loads(action.get("value") or "")
        except json.JSONDecodeError:
            return None
        if not isinstance(value, dict) or not isinstance(value.get("poll"), str):
            return None
        file = value.get("file")
        if is_pick and not isinstance(file, str):
            return None
        return Click(
            poll_id=value["poll"],
            user_id=user_id,
            file=file if is_pick else None,
            response_url=response_url if isinstance(response_url, str) else None,
        )
    return None


def resolve_click(
    click: Click, poll: Poll, allowed_user_ids: Collection[str] = ()
) -> tuple[Decision | None, str | None]:
    """Turn a click into a decision, or a reason to (privately) turn the clicker away."""
    if click.poll_id != poll.id:
        return None, "That poll has already closed."
    if allowed_user_ids and click.user_id not in allowed_user_ids:
        return None, "Sorry, you're not one of the people who can pick recordings."
    if click.file is None:
        return Decision(click.user_id, None), None
    option = poll.option(click.file)
    if option is None:
        return None, "That recording isn't part of this poll."
    return Decision(click.user_id, option), None


def _option_text(option: PollOption) -> str:
    details = [f"finished {slack_date(option.recording.modified)}"]
    if option.duration:
        details.append(human_duration(option.duration))
    details.append(human_size(option.recording.size))
    return f"*{escape(option.key)}*\n" + " · ".join(details)


def _pick_button(poll: Poll, index: int, option: PollOption) -> Block:
    others = len(poll.options) - 1
    consequence = (
        f" The other {others} recording{'s' if others != 1 else ''} will be {poll.trash_action}."
        if others
        else ""
    )
    return {
        "type": "button",
        "text": _plain("Keep this one"),
        "style": "primary",
        "action_id": f"{PICK_ACTION_PREFIX}{index}",
        "value": json.dumps({"poll": poll.id, "file": option.key}),
        "confirm": {
            "title": _plain("Keep this recording?"),
            "text": {
                "type": "mrkdwn",
                "text": _truncate(f"*{escape(option.key)}* will be transcoded.{consequence}", 300),
            },
            "confirm": _plain("Keep it"),
            "deny": _plain("Cancel"),
            "style": "danger" if others else "primary",
        },
    }


def _plain(text: str) -> dict[str, Any]:
    return {"type": "plain_text", "text": text, "emoji": True}


def _header(text: str) -> Block:
    return {"type": "header", "text": _plain(_truncate(text, 150))}


def _section(text: str) -> Block:
    return {"type": "section", "text": {"type": "mrkdwn", "text": _truncate(text, 3000)}}


def _context(text: str) -> Block:
    return {"type": "context", "elements": [{"type": "mrkdwn", "text": _truncate(text, 3000)}]}


def _truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"
