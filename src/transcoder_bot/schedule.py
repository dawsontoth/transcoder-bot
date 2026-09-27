"""Running transcoder-bot daily with a launchd LaunchAgent."""

from __future__ import annotations

import os
import plistlib
import subprocess
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

LABEL = "local.transcoder-bot"
WEEKDAYS = {"sun": 0, "mon": 1, "tue": 2, "wed": 3, "thu": 4, "fri": 5, "sat": 6}
DEFAULT_PATH = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"


def agent_path(label: str = LABEL) -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{label}.plist"


def default_log_path() -> Path:
    return Path.home() / "Library" / "Logs" / "transcoder-bot.log"


def parse_time(value: str) -> tuple[int, int]:
    """``"18:30"`` → ``(18, 30)``."""
    hour_text, sep, minute_text = value.strip().partition(":")
    try:
        if not sep:
            raise ValueError
        hour, minute = int(hour_text), int(minute_text)
    except ValueError:
        raise ValueError(f"Time must look like 18:30, not {value!r}") from None
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f"Time must look like 18:30, not {value!r}")
    return hour, minute


def parse_days(value: str | None) -> list[int]:
    """``"sun,wed"`` → ``[0, 3]``; empty means every day."""
    if not value:
        return []
    days = []
    for part in value.split(","):
        key = part.strip().lower()[:3]
        if key not in WEEKDAYS:
            raise ValueError(f"Unknown day {part.strip()!r}; use e.g. sun,mon,tue")
        days.append(WEEKDAYS[key])
    return sorted(set(days))


def build_agent(
    *,
    program: Sequence[str],
    hour: int,
    minute: int,
    weekdays: Sequence[int] = (),
    log_path: Path,
    label: str = LABEL,
) -> dict[str, Any]:
    interval: dict[str, int] | list[dict[str, int]]
    if weekdays:
        interval = [{"Weekday": day, "Hour": hour, "Minute": minute} for day in weekdays]
    else:
        interval = {"Hour": hour, "Minute": minute}
    return {
        "Label": label,
        "ProgramArguments": list(program),
        "StartCalendarInterval": interval,
        "StandardOutPath": str(log_path),
        "StandardErrorPath": str(log_path),
        "EnvironmentVariables": {"PATH": DEFAULT_PATH, "PYTHONUNBUFFERED": "1"},
        # Without this, launchd throttles the job's CPU and I/O as low-priority background
        # work, which makes a long ffmpeg encode crawl.
        "ProcessType": "Interactive",
        "RunAtLoad": False,
    }


def write_agent(agent: dict[str, Any], dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    with dest.open("wb") as fh:
        plistlib.dump(agent, fh)


def load_agent(plist: Path, label: str = LABEL, *, attempts: int = 5) -> None:
    """(Re)load the agent into the current user's GUI session, where the NAS is mounted."""
    domain = f"gui/{os.getuid()}"
    subprocess.run(["launchctl", "bootout", f"{domain}/{label}"], capture_output=True, check=False)
    # bootout finishes asynchronously; bootstrapping too soon fails with "Bootstrap failed: 5".
    for attempt in range(1, attempts + 1):
        result = subprocess.run(
            ["launchctl", "bootstrap", domain, str(plist)],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode == 0:
            return
        if attempt < attempts:
            time.sleep(1)
    raise RuntimeError(f"launchctl bootstrap failed: {result.stderr.strip()}")


def unload_agent(label: str = LABEL) -> None:
    subprocess.run(
        ["launchctl", "bootout", f"gui/{os.getuid()}/{label}"], capture_output=True, check=False
    )
