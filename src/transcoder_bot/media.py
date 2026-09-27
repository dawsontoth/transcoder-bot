"""Reading media properties with ffprobe."""

from __future__ import annotations

import json
import math
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class ProbeError(RuntimeError):
    """ffprobe couldn't read the file (missing, damaged, or still being recorded)."""


@dataclass(frozen=True)
class AudioStream:
    codec: str
    channels: int
    sample_rate: int
    bit_rate: int | None = None


@dataclass(frozen=True)
class MediaInfo:
    duration: float
    width: int
    height: int
    video_codec: str
    rotation: int = 0
    frame_rate: float | None = None
    audio_streams: tuple[AudioStream, ...] = ()

    @property
    def display_size(self) -> tuple[int, int]:
        """Frame size as ffmpeg hands it to filters, i.e. after applying rotation metadata."""
        if self.rotation in (90, 270):
            return self.height, self.width
        return self.width, self.height


def probe(path: Path, ffprobe: str = "ffprobe", *, timeout: float = 120.0) -> MediaInfo:
    cmd = [
        ffprobe,
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        "-i",
        str(path),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
    except FileNotFoundError as exc:
        raise ProbeError(
            f"ffprobe not found ({ffprobe}). Install it with: brew install ffmpeg"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise ProbeError(f"ffprobe timed out reading {path.name}") from exc
    if proc.returncode != 0:
        lines = proc.stderr.strip().splitlines()
        detail = lines[-1] if lines else f"ffprobe exited with status {proc.returncode}"
        raise ProbeError(f"Can't read {path.name}: {detail}")
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise ProbeError(f"ffprobe returned unexpected output for {path.name}") from exc
    return parse_probe(data, name=path.name)


def parse_probe(data: Mapping[str, Any], *, name: str = "file") -> MediaInfo:
    """Turn ``ffprobe -show_format -show_streams -print_format json`` output into MediaInfo."""
    streams: list[dict[str, Any]] = list(data.get("streams") or [])
    video = next(
        (
            s
            for s in streams
            if s.get("codec_type") == "video"
            and not (s.get("disposition") or {}).get("attached_pic")
        ),
        None,
    )
    if video is None:
        raise ProbeError(f"{name} has no video stream")

    duration = _number((data.get("format") or {}).get("duration")) or _number(video.get("duration"))
    if not duration or duration <= 0:
        raise ProbeError(f"{name} has no readable duration (is it still recording?)")

    audio = tuple(
        AudioStream(
            codec=str(s.get("codec_name", "unknown")),
            channels=int(s.get("channels") or 0),
            sample_rate=int(_number(s.get("sample_rate")) or 0),
            bit_rate=int(_number(s.get("bit_rate")) or 0) or None,
        )
        for s in streams
        if s.get("codec_type") == "audio" and int(s.get("channels") or 0) > 0
    )
    return MediaInfo(
        duration=duration,
        width=int(video["width"]),
        height=int(video["height"]),
        video_codec=str(video.get("codec_name", "unknown")),
        rotation=_rotation(video),
        frame_rate=_frame_rate(video.get("avg_frame_rate")),
        audio_streams=audio,
    )


def _number(value: object) -> float | None:
    if not isinstance(value, str | int | float) or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except ValueError:
        return None
    return number if math.isfinite(number) else None


def _frame_rate(value: object) -> float | None:
    if not isinstance(value, str) or "/" not in value:
        return _number(value)
    num, _, den = value.partition("/")
    numerator, denominator = _number(num), _number(den)
    if not numerator or not denominator:
        return None
    return numerator / denominator


def _rotation(stream: Mapping[str, Any]) -> int:
    """Rotation from the display matrix (or the legacy ``rotate`` tag), normalized to 0-359."""
    for side_data in stream.get("side_data_list") or []:
        rotation = _number(side_data.get("rotation"))
        if rotation is not None:
            return round(rotation) % 360
    rotation = _number((stream.get("tags") or {}).get("rotate"))
    return round(rotation) % 360 if rotation is not None else 0
