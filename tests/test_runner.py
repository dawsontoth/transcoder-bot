import logging
import sys
from pathlib import Path

import pytest

from tests.helpers import loudnorm_stderr
from transcoder_bot import runner
from transcoder_bot.runner import (
    FfmpegError,
    Progress,
    ThrottledProgress,
    find_executable,
    format_progress,
    iter_progress,
    parse_progress,
    run_ffmpeg,
)


def test_parse_progress():
    progress = parse_progress(
        {"out_time_us": "30000000", "speed": " 2.5x", "progress": "continue"},
        duration=120,
        stage="encoding",
    )
    assert progress == Progress("encoding", fraction=0.25, speed=2.5, eta_seconds=36.0)


def test_parse_progress_with_unknown_values():
    progress = parse_progress(
        {"out_time_us": "N/A", "speed": "N/A", "progress": "continue"}, duration=None, stage="x"
    )
    assert progress == Progress("x")


def test_parse_progress_end_means_done():
    progress = parse_progress({"progress": "end"}, duration=100, stage="x")
    assert progress.fraction == 1.0
    assert progress.eta_seconds == 0.0


def test_iter_progress_groups_blocks():
    lines = [
        "frame=10\n",
        "out_time_us=5000000\n",
        "speed=1x\n",
        "progress=continue\n",
        "out_time_us=10000000\n",
        "progress=end\n",
        "garbage\n",
    ]
    fractions = [p.fraction for p in iter_progress(lines, duration=10, stage="x")]
    assert fractions == [0.5, 1.0]


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def test_throttled_progress():
    seen: list[Progress] = []
    clock = FakeClock()
    throttle = ThrottledProgress(seen.append, interval=60, clock=clock)

    throttle(Progress("analyzing audio", 0.1))  # first update always goes out
    clock.now = 10
    throttle(Progress("analyzing audio", 0.5))  # too soon
    throttle(Progress("encoding", 0.0))  # new stage goes out immediately
    clock.now = 80
    throttle(Progress("encoding", 0.4))  # interval passed

    assert [(p.stage, p.fraction) for p in seen] == [
        ("analyzing audio", 0.1),
        ("encoding", 0.0),
        ("encoding", 0.4),
    ]


def test_throttled_progress_survives_reporting_errors(caplog):
    def broken(progress: Progress) -> None:
        raise ConnectionError("Slack is down")

    with caplog.at_level(logging.WARNING):
        ThrottledProgress(broken, interval=0)(Progress("encoding"))

    assert "Couldn't report progress" in caplog.text


@pytest.mark.parametrize(
    ("progress", "expected"),
    [
        (Progress("encoding", 0.42, 3.14, 540), "Encoding 42% · 3.1× realtime · about 9 min left"),
        (Progress("encoding", 1.0, 3.0, 0), "Encoding 100% · 3.0× realtime"),
        (
            Progress("encoding", 0.99, 3.0, 20),
            "Encoding 99% · 3.0× realtime · less than a minute left",
        ),
        (Progress("analyzing audio"), "Analyzing audio"),
    ],
)
def test_format_progress(progress, expected):
    assert format_progress(progress) == expected


def write_fake_ffmpeg(path: Path, *, exit_code: int, stderr: str) -> Path:
    """A stand-in ffmpeg that prints -progress blocks on stdout and ``stderr`` on stderr."""
    path.write_text(
        f"""#!{sys.executable}
import sys
for us in (0, 5_000_000, 10_000_000):
    print(f"out_time_us={{us}}\\nspeed=2x\\nprogress=continue", flush=True)
print("progress=end", flush=True)
sys.stderr.write({stderr!r})
sys.exit({exit_code})
"""
    )
    path.chmod(0o755)
    return path


def test_run_ffmpeg_reports_progress_and_returns_stderr(tmp_path):
    fake = write_fake_ffmpeg(tmp_path / "ffmpeg", exit_code=0, stderr=loudnorm_stderr())
    seen: list[Progress] = []

    stderr = run_ffmpeg([str(fake)], duration=10, stage="encoding", on_progress=seen.append)

    assert '"input_i" : "-27.61"' in stderr
    assert [p.fraction for p in seen] == [0.0, 0.5, 1.0, 1.0]


def test_run_ffmpeg_raises_on_failure(tmp_path):
    fake = write_fake_ffmpeg(
        tmp_path / "ffmpeg", exit_code=1, stderr="Stream map '0:a:3' matches no streams.\n"
    )

    with pytest.raises(FfmpegError, match=r"exit status 1.*matches no streams") as info:
        run_ffmpeg([str(fake)], duration=10, stage="encoding")

    assert info.value.returncode == 1


def test_run_ffmpeg_reports_a_missing_binary(tmp_path):
    with pytest.raises(FfmpegError, match="brew install ffmpeg"):
        run_ffmpeg([str(tmp_path / "nope")], duration=None, stage="x")


def test_find_executable(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", "")
    monkeypatch.setattr(runner, "EXTRA_TOOL_DIRS", (tmp_path,))
    (tmp_path / "ffmpeg").touch()

    assert find_executable("/custom/ffmpeg") == "/custom/ffmpeg"
    assert find_executable("ffmpeg") == str(tmp_path / "ffmpeg")
    assert find_executable("ffprobe") == "ffprobe"  # not found: left for the error message
