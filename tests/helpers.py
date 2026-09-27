"""Shared test helpers."""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from transcoder_bot.config import Config, parse_config
from transcoder_bot.media import AudioStream, MediaInfo

# A fixed "now" so time-window tests don't depend on the clock.
NOW = datetime(2026, 9, 27, 18, 0, tzinfo=UTC)


def make_config(recordings_dir: Path, **sections: Any) -> Config:
    return parse_config({"recordings_dir": str(recordings_dir), **sections})


def touch(path: Path, *, size: int = 1024, age: timedelta = timedelta(hours=1)) -> Path:
    """Create a file of ``size`` bytes, last modified ``age`` before NOW."""
    path.write_bytes(b"\0" * size)
    stamp = (NOW - age).timestamp()
    os.utime(path, (stamp, stamp))
    return path


def media(
    width: int = 3840,
    height: int = 2160,
    *,
    duration: float = 2700.0,
    channels: tuple[int, ...] = (2,),
    rotation: int = 0,
    codec: str = "prores",
) -> MediaInfo:
    return MediaInfo(
        duration=duration,
        width=width,
        height=height,
        video_codec=codec,
        rotation=rotation,
        frame_rate=29.97,
        audio_streams=tuple(AudioStream("pcm_s24le", c, 48000) for c in channels),
    )


def loudnorm_stderr(
    *,
    input_i: str = "-27.61",
    input_tp: str = "-4.47",
    input_lra: str = "8.06",
    input_thresh: str = "-38.20",
    output_i: str = "-16.02",
    normalization_type: str = "linear",
    target_offset: str = "0.58",
) -> str:
    """stderr as ffmpeg prints it when loudnorm has print_format=json."""
    return f"""[out#0/null @ 0x5617594] video:0kB audio:2250kB subtitle:0kB
size=N/A time=00:45:00.00 bitrate=N/A speed=38.2x
[Parsed_loudnorm_1 @ 0x5617594482c0]
{{
\t"input_i" : "{input_i}",
\t"input_tp" : "{input_tp}",
\t"input_lra" : "{input_lra}",
\t"input_thresh" : "{input_thresh}",
\t"output_i" : "{output_i}",
\t"output_tp" : "-1.50",
\t"output_lra" : "7.90",
\t"output_thresh" : "-26.71",
\t"normalization_type" : "{normalization_type}",
\t"target_offset" : "{target_offset}"
}}
"""
