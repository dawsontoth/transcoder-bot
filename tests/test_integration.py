"""End-to-end transcodes with the real ffmpeg (skipped when it isn't installed)."""

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from tests.fake_descript import GOOD_KEY, serve_fake_descript
from tests.helpers import make_config
from tests.test_poll import FakeSlack, pick
from transcoder_bot import loudnorm
from transcoder_bot.cli import main
from transcoder_bot.descript import DescriptUploader
from transcoder_bot.media import probe
from transcoder_bot.poll import PollRunner
from transcoder_bot.transcode import Transcoder

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not (shutil.which("ffmpeg") and shutil.which("ffprobe")), reason="needs ffmpeg"
    ),
]

# A small stand-in for a 3840x2160 recording: same 16:9 shape, much faster to encode.
WIDTH, HEIGHT = 640, 360
SHORT_SIDE = 180


def make_recording(path: Path, *, seconds: int = 6) -> Path:
    """A HyperDeck-like ProRes .mov: a red top-left quadrant (so we can tell which way it was
    rotated) and 4 channels of 24-bit PCM: quiet, varying audio on channels 1-2, silence on 3-4.
    """
    video = (
        f"color=black:s={WIDTH}x{HEIGHT}:r=25:d={seconds},"
        f"drawbox=x=0:y=0:w={WIDTH // 2}:h={HEIGHT // 2}:color=red:t=fill"
    )
    audio = (
        f"anoisesrc=color=pink:amplitude=0.02:d={seconds}:r=48000,"
        "volume='if(lt(mod(t,4),2),1,0.5)':eval=frame,"
        "pan=4c|c0=c0|c1=c0|c2=0*c0|c3=0*c0"
    )
    subprocess.run(
        [
            *("ffmpeg", "-hide_banner", "-loglevel", "error", "-y"),
            *("-f", "lavfi", "-i", video, "-f", "lavfi", "-i", audio),
            *("-c:v", "prores_ks", "-profile:v", "0", "-c:a", "pcm_s24le", str(path)),
        ],
        check=True,
    )
    return path


def probe_streams(path: Path) -> dict[str, dict[str, Any]]:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", str(path)],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return {s["codec_type"]: s for s in json.loads(out)["streams"]}


def red_quadrants(path: Path, width: int, height: int) -> set[str]:
    """Which quadrants of a frame from the middle of the video are red."""
    frame = subprocess.run(
        [
            *("ffmpeg", "-v", "error", "-ss", "2", "-i", str(path), "-frames:v", "1"),
            *("-f", "rawvideo", "-pix_fmt", "rgb24", "-"),
        ],
        capture_output=True,
        check=True,
    ).stdout
    assert len(frame) == width * height * 3
    red = set()
    for name, (fx, fy) in {
        "top-left": (0.25, 0.25),
        "top-right": (0.75, 0.25),
        "bottom-left": (0.25, 0.75),
        "bottom-right": (0.75, 0.75),
    }.items():
        i = (int(height * fy) * width + int(width * fx)) * 3
        r, g, b = frame[i : i + 3]
        if r > 150 and g < 90 and b < 90:
            red.add(name)
    return red


def measured_loudness(path: Path) -> float:
    result = subprocess.run(
        [
            *("ffmpeg", "-hide_banner", "-nostats", "-i", str(path)),
            *("-af", "loudnorm=print_format=json", "-f", "null", "-"),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return loudnorm.parse_stats(result.stderr).input_i


@pytest.fixture
def recording(tmp_path):
    folder = tmp_path / "HyperDeck"
    folder.mkdir()
    return make_recording(folder / "Service.mov")


def test_rotates_scales_and_normalizes(recording, tmp_path):
    temp = tmp_path / "temp"
    temp.mkdir()
    config = make_config(
        recording.parent, temp_dir=str(temp), video={"short_side": SHORT_SIDE, "preset": "veryfast"}
    )

    result = Transcoder(config).transcode(recording)

    output = recording.with_name("Service_1080p.mp4")
    assert result.output == output
    streams = probe_streams(output)
    video, audio = streams["video"], streams["audio"]
    assert (video["width"], video["height"]) == (SHORT_SIDE, SHORT_SIDE * 16 // 9)
    assert video["codec_name"] == "h264"
    assert video["pix_fmt"] == "yuv420p"
    assert (audio["codec_name"], audio["channels"], audio["sample_rate"]) == ("aac", 2, "48000")

    # Rotated 90° counter-clockwise: the red top-left corner ends up bottom-left.
    assert red_quadrants(output, video["width"], video["height"]) == {"bottom-left"}

    assert result.loudness_before is not None
    assert result.loudness_before.input_i < -30  # it started out quiet...
    assert measured_loudness(output) == pytest.approx(-16, abs=1.5)  # ...and now it isn't

    assert sorted(p.name for p in recording.parent.iterdir()) == [
        "Service.mov",
        "Service_1080p.mp4",
    ]
    assert list(temp.iterdir()) == []


def test_command_line_clockwise_rotation(recording, tmp_path, monkeypatch):
    monkeypatch.setenv("TRANSCODER_BOT_STATE_DIR", str(tmp_path / "state"))
    config = tmp_path / "config.toml"
    config.write_text(
        f'recordings_dir = "{recording.parent}"\n'
        f'[video]\nrotate = "cw"\nshort_side = {SHORT_SIDE}\npreset = "veryfast"\n'
    )

    assert main(["--config", str(config), "transcode", str(recording)]) == 0

    output = recording.with_name("Service_1080p.mp4")
    video = probe_streams(output)["video"]
    assert red_quadrants(output, video["width"], video["height"]) == {"top-right"}


def test_silent_channels_skip_normalization(recording):
    config = make_config(
        recording.parent,
        video={"short_side": SHORT_SIDE, "preset": "veryfast"},
        audio={"channels": [3, 4]},  # the silent ones
    )

    result = Transcoder(config).transcode(recording)

    assert result.loudness_before is None
    assert probe_streams(result.output)["audio"]["channels"] == 2


def test_poll_keeps_the_pick_and_trashes_the_rest(recording, tmp_path):
    other = recording.with_name("Rehearsal.mov")
    shutil.copy(recording, other)
    for path in (recording, other):  # "finished" an hour ago
        stamp = path.stat().st_mtime - 3600
        os.utime(path, (stamp, stamp))
    config = make_config(recording.parent, video={"short_side": SHORT_SIDE, "preset": "veryfast"})
    slack = FakeSlack(pick("Service.mov"))

    outcome = PollRunner(config, slack, Transcoder(config), probe_fn=probe).run()

    assert outcome.status == "done"
    folder = recording.parent
    assert sorted(p.name for p in folder.iterdir()) == [
        "Service.mov",
        "Service_1080p.mp4",
        "_Trash",
    ]
    (trashed,) = outcome.trashed
    assert trashed.destination is not None
    assert trashed.destination.parent.parent == folder / "_Trash"
    assert trashed.destination.read_bytes() == recording.read_bytes()
    assert slack.last_status() == "✅ Saved *Service_1080p.mp4* next to the original."


def test_videos_over_descripts_limit_are_sent_as_a_smaller_copy(tmp_path, monkeypatch):
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    monkeypatch.setenv("no_proxy", "127.0.0.1")
    video = tmp_path / "Busy_1080p.mp4"
    subprocess.run(
        [
            *("ffmpeg", "-hide_banner", "-loglevel", "error", "-y"),
            *("-f", "lavfi", "-i", "testsrc2=s=320x568:r=30:d=4,noise=alls=60:allf=t"),
            *("-f", "lavfi", "-i", "sine=f=440:d=4:r=48000"),
            *("-c:v", "libx264", "-preset", "veryfast", "-crf", "16", "-pix_fmt", "yuv420p"),
            *("-c:a", "aac", "-b:a", "64k", "-movflags", "+faststart", str(video)),
        ],
        check=True,
    )
    limit = video.stat().st_size // 3

    with serve_fake_descript() as fake:
        config = make_config(
            tmp_path,
            temp_dir=str(tmp_path),
            video={"preset": "veryfast"},
            descript={
                "api_key": GOOD_KEY,
                "api_url": fake.base_url,
                "max_upload_gb": limit / 1e9,
                "wait_minutes": 0,
            },
        )
        upload = DescriptUploader(config, min_video_bitrate=0).upload(video, title="Busy")

    assert upload.shrunk
    sent = tmp_path / "sent.mp4"
    sent.write_bytes(fake.uploads["Busy_1080p.mp4"])
    assert sent.stat().st_size <= limit
    streams = probe_streams(sent)
    assert (streams["video"]["width"], streams["video"]["height"]) == (320, 568)
    assert streams["video"]["codec_name"] == "h264"
    assert streams["audio"]["codec_name"] == "aac"
