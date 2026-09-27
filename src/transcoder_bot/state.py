"""Small bits of local state: a run lock, and the Slack poll that's currently open."""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from types import TracebackType
from typing import IO

from transcoder_bot.macos import IS_MACOS

STATE_DIR_ENV_VAR = "TRANSCODER_BOT_STATE_DIR"


class AlreadyRunningError(RuntimeError):
    """Another transcoder-bot run holds the lock."""


def default_state_dir(env: Mapping[str, str] | None = None) -> Path:
    env = os.environ if env is None else env
    if env.get(STATE_DIR_ENV_VAR):
        return Path(env[STATE_DIR_ENV_VAR]).expanduser()
    if IS_MACOS:
        return Path.home() / "Library" / "Application Support" / "transcoder-bot"
    base = env.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "transcoder-bot"


@dataclass(frozen=True)
class PendingPoll:
    channel: str
    ts: str
    poll_id: str


class RunLock:
    """An exclusive, non-blocking ``flock`` so scheduled and manual runs don't collide.

    The OS releases it automatically if the process dies.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._fh: IO[str] | None = None

    def __enter__(self) -> RunLock:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fh = self.path.open("a+", encoding="utf-8")
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            fh.seek(0)
            holder = fh.read().strip() or "unknown"
            fh.close()
            raise AlreadyRunningError(
                f"Another transcoder-bot run is in progress (pid {holder})"
            ) from None
        fh.seek(0)
        fh.truncate()
        fh.write(str(os.getpid()))
        fh.flush()
        self._fh = fh
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._fh is not None:
            fcntl.flock(self._fh, fcntl.LOCK_UN)
            self._fh.close()
            self._fh = None


class StateStore:
    def __init__(self, directory: Path) -> None:
        self.directory = directory

    def lock(self) -> RunLock:
        return RunLock(self.directory / "run.lock")

    @property
    def _pending_path(self) -> Path:
        return self.directory / "pending-poll.json"

    def pending_poll(self) -> PendingPoll | None:
        try:
            data = json.loads(self._pending_path.read_text(encoding="utf-8"))
            return PendingPoll(
                channel=str(data["channel"]), ts=str(data["ts"]), poll_id=str(data["poll_id"])
            )
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def set_pending_poll(self, poll: PendingPoll) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        tmp = self._pending_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(poll)), encoding="utf-8")
        tmp.replace(self._pending_path)

    def clear_pending_poll(self) -> None:
        with contextlib.suppress(FileNotFoundError):
            self._pending_path.unlink()
