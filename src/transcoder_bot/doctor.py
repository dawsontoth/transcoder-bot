"""``transcoder-bot doctor``: check that everything the job needs is in place."""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

from transcoder_bot.config import Config
from transcoder_bot.formatting import human_size
from transcoder_bot.macos import is_available
from transcoder_bot.recordings import find_recordings, is_transcoded
from transcoder_bot.trash import trash_root

REQUIRED_FILTERS = ("scale", "transpose", "pan", "loudnorm", "aresample", "format", "setsar")


@dataclass(frozen=True)
class Check:
    status: str  # "ok", "warn" or "fail"
    label: str
    detail: str = ""

    def __str__(self) -> str:
        mark = {"ok": "✓", "warn": "!", "fail": "✗"}[self.status]
        return f"{mark} {self.label}" + (f": {self.detail}" if self.detail else "")


def run_checks(
    config: Config,
    *,
    slack_test_message: bool = False,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> Iterator[Check]:
    yield from _ffmpeg_checks(config, run)
    yield from _folder_checks(config)
    yield from _slack_checks(config, post_test=slack_test_message)
    yield _descript_check(config)


def _ffmpeg_checks(
    config: Config, run: Callable[..., subprocess.CompletedProcess[str]]
) -> Iterator[Check]:
    def ffmpeg(*args: str, tool: str = config.ffmpeg) -> str:
        proc = run([tool, "-hide_banner", *args], capture_output=True, text=True, check=False)
        return str(proc.stdout)

    try:
        version = ffmpeg("-version").splitlines()[0]
        yield Check("ok", "ffmpeg", f"{version} ({config.ffmpeg})")
        ffmpeg("-version", tool=config.ffprobe)
        yield Check("ok", "ffprobe", config.ffprobe)
    except (OSError, IndexError):
        yield Check("fail", "ffmpeg/ffprobe", "not found. Install with: brew install ffmpeg")
        return

    encoders = ffmpeg("-encoders")
    for encoder in (config.video.encoder, "aac"):
        if re.search(rf"\s{re.escape(encoder)}\s", encoders):
            yield Check("ok", f"encoder {encoder}")
        else:
            yield Check("fail", f"encoder {encoder}", "not available in this ffmpeg build")

    filters = ffmpeg("-filters")
    missing = [f for f in REQUIRED_FILTERS if not re.search(rf"\s{f}\s", filters)]
    if missing:
        yield Check("fail", "filters", f"missing {', '.join(missing)}")
    else:
        yield Check("ok", "filters", ", ".join(REQUIRED_FILTERS))

    if config.video.hwaccel:
        accels = ffmpeg("-hwaccels").split()
        ok = config.video.hwaccel in accels
        yield Check(
            "ok" if ok else "fail", f"hwaccel {config.video.hwaccel}", "" if ok else "unavailable"
        )


def _folder_checks(config: Config) -> Iterator[Check]:
    folder = config.recordings_dir
    if not folder.is_dir():
        hint = " (mount_url is set, so the job will try to mount it)" if config.mount_url else ""
        yield Check("fail", "recordings folder", f"{folder} not found. Is the NAS mounted?{hint}")
    elif not is_available(folder):
        yield Check(
            "fail",
            "recordings folder",
            f"{folder} is an empty leftover folder, not the NAS share. Eject the share and "
            "reconnect it (see Troubleshooting in the README).",
        )
    else:
        yield _writable_check("recordings folder", folder)
        try:
            recordings = find_recordings(config)
            pending = sum(1 for r in recordings if not is_transcoded(r, config))
            yield Check(
                "ok",
                "recent recordings",
                f"{len(recordings)} in the last {config.scan.lookback_hours:g} h, "
                f"{pending} not transcoded yet",
            )
        except OSError as exc:
            yield Check("fail", "recent recordings", str(exc))
        if config.trash.mode == "folder":
            yield _trash_check(config)

    temp = config.temp_dir or Path(tempfile.gettempdir())
    if temp.is_dir():
        free = human_size(shutil.disk_usage(temp).free)
        yield Check("ok", "temp folder", f"{temp} ({free} free)")
    else:
        yield Check("fail", "temp folder", f"{temp} doesn't exist")


def _trash_check(config: Config) -> Check:
    root = trash_root(config.recordings_dir, config.trash)
    existing = root if root.exists() else root.parent
    if not existing.is_dir():
        return Check("fail", "trash folder", f"{existing} doesn't exist")
    same_volume = existing.stat().st_dev == config.recordings_dir.stat().st_dev
    if not same_volume:
        return Check(
            "warn",
            "trash folder",
            f"{root} is on a different volume, so trashing copies whole recordings",
        )
    days = config.trash.retention_days
    kept = f"kept {days} day(s)" if days else "kept until you empty it"
    return Check("ok", "trash folder", f"{root} (same volume, {kept})")


def _writable_check(label: str, folder: Path) -> Check:
    try:
        with tempfile.NamedTemporaryFile(dir=folder, prefix=".transcoder-bot-check-"):
            pass
    except OSError as exc:
        hint = ""
        if "Operation not permitted" in str(exc):
            hint = " (macOS privacy settings: see Troubleshooting in the README)"
        return Check("fail", label, f"{folder} isn't writable: {exc}{hint}")
    return Check("ok", label, f"{folder} (readable and writable)")


def _slack_checks(config: Config, *, post_test: bool) -> Iterator[Check]:
    slack = config.slack
    if not slack.enabled:
        missing = ", ".join(slack.missing())
        yield Check("warn", "Slack", f"not configured ({missing}); the poll command won't run")
        return
    from slack_sdk import WebClient
    from slack_sdk.errors import SlackApiError

    from transcoder_bot.slack_bot import check_tokens, describe_error

    try:
        who = check_tokens(slack.bot_token, slack.app_token)
        yield Check("ok", "Slack tokens", f"signed in as @{who['user']} in {who['team']}")
    except SlackApiError as exc:
        yield Check("fail", "Slack tokens", describe_error(str(exc.response.get("error"))))
        return
    except OSError as exc:
        yield Check("fail", "Slack", f"can't reach Slack: {exc}")
        return
    if not slack.allowed_user_ids:
        yield Check("warn", "Slack", "allowed_user_ids is empty, so anyone in the channel can pick")
    if post_test:
        try:
            WebClient(token=slack.bot_token).chat_postMessage(
                channel=slack.channel, text="👋 transcoder-bot can post here."
            )
            yield Check("ok", "Slack channel", f"posted a test message to {slack.channel}")
        except SlackApiError as exc:
            yield Check("fail", "Slack channel", describe_error(str(exc.response.get("error"))))


def _descript_check(config: Config) -> Check:
    if not config.descript.enabled:
        return Check(
            "warn", "Descript", "not configured (descript.api_key), so nothing will be uploaded"
        )
    from transcoder_bot.descript import DescriptClient, DescriptError

    try:
        DescriptClient(config.descript.api_key, base_url=config.descript.api_url).check()
    except DescriptError as exc:
        return Check("fail", "Descript", str(exc))
    return Check("ok", "Descript", "API key works; finished videos will be uploaded")
