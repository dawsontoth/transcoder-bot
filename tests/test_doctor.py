import subprocess
from typing import Any

from tests.helpers import make_config
from transcoder_bot.doctor import run_checks

ENCODERS = """Encoders:
 V....D libx264              libx264 H.264 / AVC / MPEG-4 AVC (codec h264)
 A....D aac                  AAC (Advanced Audio Coding)
"""
FILTERS = """Filters:
 ... scale             V->V       Scale the input video size and/or convert the image format.
 ... transpose         V->V       Transpose input video.
 ... pan               A->A       Remix channels with coefficients (panning).
 ... loudnorm          A->A       EBU R128 loudness normalization
 ... aresample         A->A       Resample audio data.
 ... format            V->V       Convert the input video to one of the specified pixel formats.
 ... setsar            V->V       Set the pixel sample aspect ratio.
"""


def fake_ffmpeg(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
    output = {
        "-version": "ffmpeg version 8.0 Copyright (c) 2000-2025 the FFmpeg developers\n",
        "-encoders": ENCODERS,
        "-filters": FILTERS,
        "-hwaccels": "Hardware acceleration methods:\nvideotoolbox\n",
    }[cmd[-1]]
    return subprocess.CompletedProcess(cmd, 0, output, "")


def by_label(checks):
    return {c.label: c for c in checks}


def test_healthy_setup(tmp_path):
    folder = tmp_path / "HyperDeck"
    folder.mkdir()
    config = make_config(folder, video={"hwaccel": "videotoolbox"})

    checks = by_label(run_checks(config, run=fake_ffmpeg))

    assert checks["ffmpeg"].status == "ok"
    assert "ffmpeg version 8.0" in checks["ffmpeg"].detail
    assert checks["encoder libx264"].status == "ok"
    assert checks["filters"].status == "ok"
    assert checks["hwaccel videotoolbox"].status == "ok"
    assert checks["recordings folder"].status == "ok"
    assert checks["recent recordings"].detail.startswith("0 in the last 48 h")
    assert checks["trash folder"].status == "ok"
    assert checks["Slack"].status == "warn"
    assert "slack.bot_token" in checks["Slack"].detail
    assert str(checks["ffmpeg"]).startswith("✓ ffmpeg: ")


def test_problems_are_reported(tmp_path):
    config = make_config(tmp_path / "gone", video={"encoder": "hevc_videotoolbox"})

    checks = by_label(run_checks(config, run=fake_ffmpeg))

    assert checks["encoder hevc_videotoolbox"].status == "fail"
    assert checks["recordings folder"].status == "fail"
    assert "Is the NAS mounted" in checks["recordings folder"].detail
    assert str(checks["recordings folder"]).startswith("✗ ")


def test_missing_ffmpeg(tmp_path):
    def missing(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise FileNotFoundError(cmd[0])

    checks = by_label(run_checks(make_config(tmp_path), run=missing))

    assert checks["ffmpeg/ffprobe"].status == "fail"
    assert "brew install ffmpeg" in checks["ffmpeg/ffprobe"].detail
