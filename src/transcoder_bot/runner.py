"""Running ffmpeg as a subprocess and reporting its progress."""

from __future__ import annotations

import logging
import shlex
import shutil
import subprocess
import threading
import time
from collections import deque
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Protocol

log = logging.getLogger(__name__)

# Homebrew's bin directories aren't on launchd's default PATH.
EXTRA_TOOL_DIRS = (Path("/opt/homebrew/bin"), Path("/usr/local/bin"))
STDERR_TAIL_LINES = 400


@dataclass(frozen=True)
class Progress:
    stage: str
    fraction: float | None = None
    speed: float | None = None
    eta_seconds: float | None = None


ProgressCallback = Callable[[Progress], None]


class RunFfmpeg(Protocol):
    def __call__(
        self,
        cmd: Sequence[str],
        *,
        duration: float | None,
        stage: str,
        on_progress: ProgressCallback | None,
    ) -> str: ...


class FfmpegError(RuntimeError):
    def __init__(self, cmd: Sequence[str], returncode: int, stderr: str) -> None:
        self.cmd = list(cmd)
        self.returncode = returncode
        self.stderr = stderr
        lines = [line for line in stderr.strip().splitlines() if line.strip()]
        detail = "; ".join(lines[-3:]) if lines else "no error output"
        super().__init__(f"ffmpeg failed (exit status {returncode}): {detail}")


def find_executable(name: str) -> str:
    """Resolve ``ffmpeg``/``ffprobe`` even when launchd's PATH lacks Homebrew's bin folder."""
    if "/" in name:
        return name
    found = shutil.which(name)
    if found:
        return found
    for directory in EXTRA_TOOL_DIRS:
        candidate = directory / name
        if candidate.is_file():
            return str(candidate)
    return name


def run_ffmpeg(
    cmd: Sequence[str],
    *,
    duration: float | None,
    stage: str,
    on_progress: ProgressCallback | None = None,
) -> str:
    """Run an ffmpeg command that uses ``-progress pipe:1``.

    Progress blocks on stdout are parsed and passed to ``on_progress``. Returns the tail of
    stderr (where filters such as loudnorm print their statistics); raises FfmpegError on
    failure. The ffmpeg process is always cleaned up, even if we're interrupted.
    """
    log.debug("Running: %s", shlex.join(cmd))
    try:
        proc = subprocess.Popen(
            list(cmd),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except FileNotFoundError as exc:
        raise FfmpegError(
            cmd, 127, f"{cmd[0]} not found. Install it with: brew install ffmpeg"
        ) from exc

    assert proc.stdout is not None
    assert proc.stderr is not None
    tail: deque[str] = deque(maxlen=STDERR_TAIL_LINES)
    reader = threading.Thread(target=_drain, args=(proc.stderr, tail), daemon=True)
    reader.start()
    try:
        for progress in iter_progress(proc.stdout, duration=duration, stage=stage):
            if on_progress is not None:
                on_progress(progress)
        returncode = proc.wait()
    finally:
        if proc.poll() is None:
            _stop(proc)
        reader.join(timeout=5)
    stderr = "".join(tail)
    if returncode != 0:
        raise FfmpegError(cmd, returncode, stderr)
    return stderr


def iter_progress(
    lines: Iterable[str], *, duration: float | None, stage: str
) -> Iterable[Progress]:
    """Group ``-progress`` output into blocks of ``key=value`` lines ending in ``progress=``."""
    block: dict[str, str] = {}
    for line in lines:
        key, sep, value = line.strip().partition("=")
        if not sep:
            continue
        block[key] = value.strip()
        if key == "progress":
            yield parse_progress(block, duration=duration, stage=stage)
            block = {}


def parse_progress(fields: Mapping[str, str], *, duration: float | None, stage: str) -> Progress:
    position = _to_float(fields.get("out_time_us"))
    seconds = position / 1_000_000 if position is not None and position >= 0 else None
    speed = _to_float(fields.get("speed", "").rstrip("x"))
    speed = speed if speed and speed > 0 else None

    fraction = None
    eta = None
    if duration and seconds is not None:
        fraction = min(1.0, seconds / duration)
        if speed:
            eta = max(0.0, (duration - seconds) / speed)
    if fields.get("progress") == "end":
        fraction, eta = 1.0, 0.0
    return Progress(stage=stage, fraction=fraction, speed=speed, eta_seconds=eta)


class ThrottledProgress:
    """Forward progress to ``emit`` at most every ``interval`` seconds (and on stage changes).

    Errors from ``emit`` (e.g. a Slack hiccup) are logged, never allowed to abort a transcode.
    """

    def __init__(
        self,
        emit: ProgressCallback,
        interval: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._emit = emit
        self._interval = interval
        self._clock = clock
        self._last_time: float | None = None
        self._last_stage: str | None = None

    def __call__(self, progress: Progress) -> None:
        now = self._clock()
        due = self._last_time is None or now - self._last_time >= self._interval
        if not due and progress.stage == self._last_stage:
            return
        self._last_time, self._last_stage = now, progress.stage
        try:
            self._emit(progress)
        except Exception:
            log.warning("Couldn't report progress", exc_info=True)


def format_progress(progress: Progress) -> str:
    parts = [progress.stage.capitalize()]
    if progress.fraction is not None:
        parts[0] += f" {progress.fraction:.0%}"
    if progress.speed is not None:
        parts.append(f"{progress.speed:.1f}× realtime")
    if progress.eta_seconds is not None and progress.fraction != 1.0:
        if progress.eta_seconds < 60:
            parts.append("less than a minute left")
        else:
            parts.append(f"about {round(progress.eta_seconds / 60)} min left")
    return " · ".join(parts)


def _drain(stream: IO[str], tail: deque[str]) -> None:
    for line in stream:
        tail.append(line)


def _to_float(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _stop(proc: subprocess.Popen[str]) -> None:
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
