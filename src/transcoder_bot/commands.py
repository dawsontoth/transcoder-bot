"""Pure builders for ffmpeg filter graphs and command lines (no I/O, easy to unit test)."""

from __future__ import annotations

from pathlib import Path

from transcoder_bot import loudnorm
from transcoder_bot.config import (
    SOFTWARE_ENCODERS,
    AudioConfig,
    Config,
    ConfigError,
    VideoConfig,
    bitrate_kbps,
)
from transcoder_bot.loudnorm import LoudnessStats
from transcoder_bot.media import MediaInfo

ROTATE_FILTERS: dict[str, tuple[str, ...]] = {
    "ccw": ("transpose=dir=cclock",),  # 90° counter-clockwise, no flip
    "cw": ("transpose=dir=clock",),
    "180": ("hflip", "vflip"),
    "none": (),
}

_BASE_ARGS = ("-hide_banner", "-nostdin", "-nostats", "-progress", "pipe:1")


def scaled_size(width: int, height: int, short_side: int) -> tuple[int, int]:
    """Downscale (never upscale) so the short edge is at most ``short_side``, keeping the aspect
    ratio and even dimensions (required for 4:2:0 video)."""
    factor = min(1.0, short_side / min(width, height))
    return _even(width * factor), _even(height * factor)


def output_size(info: MediaInfo, video: VideoConfig) -> tuple[int, int]:
    """Width and height of the finished video."""
    width, height = scaled_size(*info.display_size, video.short_side)
    return (height, width) if video.rotate in ("cw", "ccw") else (width, height)


def video_filter(info: MediaInfo, video: VideoConfig) -> str:
    width, height = info.display_size
    target_w, target_h = scaled_size(width, height, video.short_side)
    filters: list[str] = []
    if (target_w, target_h) != (width, height):
        # Scale before rotating: transposing a 1080p frame is 4x less work than a 4K one.
        filters += [f"scale={target_w}:{target_h}:flags=lanczos", "setsar=1"]
    filters += ROTATE_FILTERS[video.rotate]
    filters.append("format=yuv420p")  # 8-bit 4:2:0, which every player and phone handles
    return ",".join(filters)


def pan_filter(source_channels: int, audio: AudioConfig) -> str:
    """Route the configured source channels to a stereo pair.

    HyperDecks can record 2-16 embedded channels; usually the program mix is on channels 1-2.
    A mono source is always sent to both sides.
    """
    if source_channels <= 1:
        picks = [0]
    else:
        picks = [channel - 1 for channel in audio.channels]
        missing = [p + 1 for p in picks if p >= source_channels]
        if missing:
            raise ConfigError(
                f"audio.channels includes channel {missing[0]}, but the recording's audio "
                f"only has {source_channels} channels"
            )
    if len(picks) == 1:
        left = right = f"c{picks[0]}"
    elif audio.mono:
        gain = 1 / len(picks)
        left = right = "+".join(f"{gain:g}*c{p}" for p in picks)
    else:
        left, right = f"c{picks[0]}", f"c{picks[1]}"
    return f"pan=stereo|c0={left}|c1={right}"


def audio_filter(pan: str, audio: AudioConfig, loudness: LoudnessStats | None) -> str:
    """Channel routing, then (optionally) loudness normalization, then a fixed sample rate.

    The trailing ``aresample`` matters: loudnorm upsamples to 192 kHz in dynamic mode.
    """
    parts = [pan]
    if loudness is not None:
        parts.append(loudnorm.apply_filter(audio, loudness))
    parts.append(f"aresample={audio.sample_rate}")
    return ",".join(parts)


def video_encoder_args(video: VideoConfig) -> list[str]:
    kbps = bitrate_kbps(video.bitrate) if video.bitrate else None
    if video.encoder in SOFTWARE_ENCODERS:
        args = ["-c:v", video.encoder, "-preset", video.preset]
        if kbps:
            # Aim for an average bitrate. The peak cap keeps busy scenes playable anywhere.
            args += [
                "-b:v",
                f"{kbps}k",
                "-maxrate",
                f"{kbps * 3 // 2}k",
                "-bufsize",
                f"{kbps * 2}k",
            ]
        else:
            args += ["-crf", str(video.crf)]
    else:
        args = ["-c:v", video.encoder]
        args += ["-b:v", f"{kbps}k"] if kbps else ["-q:v", str(video.vt_quality)]
    if video.encoder in ("libx264", "h264_videotoolbox"):
        args += ["-profile:v", "high"]
    else:
        args += ["-tag:v", "hvc1"]  # lets QuickTime and iOS recognise HEVC in MP4
    return [*args, *video.extra_args]


def audio_encoder_args(audio: AudioConfig) -> list[str]:
    return ["-c:a", "aac", "-b:a", audio.bitrate]


def measure_command(config: Config, source: Path, *, audio_index: int, pan: str) -> list[str]:
    """Pass 1: decode only the audio track and print loudness statistics."""
    return [
        config.ffmpeg,
        *_BASE_ARGS,
        "-i",
        str(source),
        "-map",
        f"0:a:{audio_index}",
        "-af",
        f"{pan},{loudnorm.measure_filter(config.audio)}",
        "-f",
        "null",
        "-",
    ]


def encode_command(
    config: Config,
    info: MediaInfo,
    source: Path,
    dest: Path,
    *,
    audio_index: int | None,
    pan: str | None,
    loudness: LoudnessStats | None,
) -> list[str]:
    """Pass 2: rotate + scale the video, normalize the audio and write ``dest``."""
    cmd = [config.ffmpeg, *_BASE_ARGS, "-y"]
    if config.video.hwaccel:
        cmd += ["-hwaccel", config.video.hwaccel]
    cmd += ["-i", str(source), "-map", "0:v:0"]
    cmd += ["-vf", video_filter(info, config.video), *video_encoder_args(config.video)]
    if audio_index is not None and pan is not None:
        cmd += ["-map", f"0:a:{audio_index}"]
        cmd += ["-af", audio_filter(pan, config.audio, loudness)]
        cmd += audio_encoder_args(config.audio)
    cmd += ["-map_metadata", "0", "-movflags", "+faststart", str(dest)]
    return cmd


def _even(value: float) -> int:
    return max(2, 2 * round(value / 2))
