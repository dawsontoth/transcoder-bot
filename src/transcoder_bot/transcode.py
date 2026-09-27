"""Transcoding one recording: probe, measure loudness, encode, verify, then publish next to it."""

from __future__ import annotations

import contextlib
import functools
import logging
import os
import shutil
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from transcoder_bot import commands, loudnorm
from transcoder_bot.config import Config, ConfigError, OutputConfig
from transcoder_bot.formatting import human_duration, human_size
from transcoder_bot.loudnorm import LoudnessStats, LoudnormError
from transcoder_bot.media import MediaInfo, ProbeError, probe
from transcoder_bot.runner import ProgressCallback, RunFfmpeg, run_ffmpeg

log = logging.getLogger(__name__)

# Generous upper bound for a 1080p H.264/HEVC encode plus AAC audio; used to check free space.
ESTIMATED_BITS_PER_SECOND = 25_000_000


class TranscodeError(RuntimeError):
    """The transcode couldn't be completed."""


class OutputExistsError(TranscodeError):
    """The recording has already been transcoded."""


@dataclass(frozen=True)
class TranscodeResult:
    source: Path
    output: Path
    media_duration: float
    elapsed: float
    output_bytes: int
    loudness_before: LoudnessStats | None = None
    loudness_after: LoudnessStats | None = None

    def summary(self) -> str:
        text = (
            f"{self.output.name} ({human_size(self.output_bytes)}, "
            f"{human_duration(self.media_duration)} of video in {human_duration(self.elapsed)})"
        )
        if self.loudness_before is not None:
            text += f"; audio {self.loudness_before.input_i:.1f} LUFS"
            if self.loudness_after is not None and self.loudness_after.output_i is not None:
                text += f" → {self.loudness_after.output_i:.1f} LUFS"
        return text


def output_path_for(source: Path, output: OutputConfig) -> Path:
    """``/NAS/Service.mov`` → ``/NAS/Service_1080p.mp4``."""
    return source.with_name(f"{source.stem}{output.suffix}{output.extension}")


def is_output_file(path: Path, output: OutputConfig) -> bool:
    """Whether ``path`` looks like something this tool produced (so scans skip it)."""
    return path.suffix.lower() == output.extension.lower() and path.stem.endswith(output.suffix)


def ensure_free_space(directory: Path, needed_bytes: int, *, what: str) -> None:
    free = shutil.disk_usage(directory).free
    if free < needed_bytes:
        raise TranscodeError(
            f"Not enough free space in {what} ({directory}): {human_size(free)} free, "
            f"about {human_size(needed_bytes)} needed"
        )


def publish(staged: Path, dest: Path) -> None:
    """Copy the finished file next to the source under a hidden name, then rename it into
    place, so nobody ever sees (or picks up) a half-copied file."""
    partial = dest.with_name(f".{dest.name}.{os.getpid()}.partial")
    try:
        shutil.copyfile(staged, partial)
        partial.replace(dest)
    except BaseException:
        with contextlib.suppress(OSError):
            partial.unlink()
        raise


class Transcoder:
    """Rotate, downscale and loudness-normalize a recording, saving the result beside it."""

    def __init__(
        self,
        config: Config,
        *,
        run: RunFfmpeg = run_ffmpeg,
        probe_fn: Callable[[Path], MediaInfo] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.config = config
        self._run = run
        self._probe = probe_fn or functools.partial(probe, ffprobe=config.ffprobe)
        self._clock = clock

    def output_path(self, source: Path) -> Path:
        return output_path_for(source, self.config.output)

    def describe(self, source: Path) -> str:
        """What ``transcode`` would do, without writing anything (used by ``--dry-run``)."""
        cfg = self.config
        source = source.expanduser().absolute()
        info = self._probe(source)
        dest = self.output_path(source)
        out_w, out_h = commands.output_size(info, cfg.video)
        lines = [
            f"{source} → {dest}" + ("  (already exists)" if dest.exists() else ""),
            f"  video: {info.width}x{info.height} {info.video_codec}, "
            f"{human_duration(info.duration)} → {out_w}x{out_h} {cfg.video.encoder}",
            f"  video filters: {commands.video_filter(info, cfg.video)}",
        ]
        audio_index = self._audio_index(source, info)
        if audio_index is None:
            lines.append("  audio: none")
        else:
            stream = info.audio_streams[audio_index]
            pan = commands.pan_filter(stream.channels, cfg.audio)
            steps = [pan]
            if cfg.audio.normalize:
                steps.append(
                    f"two-pass loudnorm to {cfg.audio.target_lufs:g} LUFS / "
                    f"{cfg.audio.true_peak:g} dBTP"
                )
            steps.append(f"aac {cfg.audio.bitrate}")
            lines.append(
                f"  audio: stream {audio_index} ({stream.channels} ch {stream.codec}) → "
                + " → ".join(steps)
            )
        return "\n".join(lines)

    def transcode(
        self,
        source: Path,
        *,
        force: bool = False,
        on_progress: ProgressCallback | None = None,
    ) -> TranscodeResult:
        cfg = self.config
        source = source.expanduser().absolute()
        if not source.is_file():
            raise TranscodeError(f"{source} doesn't exist")
        dest = self.output_path(source)
        if dest == source:
            raise TranscodeError(f"Output would overwrite the source ({source})")
        if dest.exists() and not force:
            raise OutputExistsError(f"{dest.name} already exists (use --force to redo it)")

        started = self._clock()
        info = self._probe(source)
        log.info(
            "Transcoding %s (%dx%d %s, %s)",
            source.name,
            info.width,
            info.height,
            info.video_codec,
            human_duration(info.duration),
        )

        audio_index = self._audio_index(source, info)
        pan = None
        before = None
        if audio_index is not None:
            pan = commands.pan_filter(info.audio_streams[audio_index].channels, cfg.audio)
            if cfg.audio.normalize:
                before = self._measure(source, info, audio_index, pan, on_progress)

        needed = int(info.duration * ESTIMATED_BITS_PER_SECOND / 8)
        with tempfile.TemporaryDirectory(prefix="transcoder-bot-", dir=cfg.temp_dir) as tmp:
            staged = Path(tmp) / dest.name
            ensure_free_space(Path(tmp), needed, what="the temp folder")
            ensure_free_space(dest.parent, needed, what="the recordings folder")
            cmd = commands.encode_command(
                cfg, info, source, staged, audio_index=audio_index, pan=pan, loudness=before
            )
            stderr = self._run(
                cmd, duration=info.duration, stage="encoding", on_progress=on_progress
            )
            after = _parse_optional_stats(stderr) if before is not None else None
            self._verify(info, staged, expect_audio=audio_index is not None)
            output_bytes = staged.stat().st_size
            log.info("Copying %s next to the source", dest.name)
            publish(staged, dest)

        result = TranscodeResult(
            source=source,
            output=dest,
            media_duration=info.duration,
            elapsed=self._clock() - started,
            output_bytes=output_bytes,
            loudness_before=before,
            loudness_after=after,
        )
        log.info("Finished %s", result.summary())
        return result

    def _audio_index(self, source: Path, info: MediaInfo) -> int | None:
        streams = info.audio_streams
        if not streams:
            log.warning("%s has no audio; the output will be silent", source.name)
            return None
        if self.config.audio.stream >= len(streams):
            raise ConfigError(
                f"audio.stream is {self.config.audio.stream}, but {source.name} only has "
                f"{len(streams)} audio stream(s) (the first is 0)"
            )
        return self.config.audio.stream

    def _measure(
        self,
        source: Path,
        info: MediaInfo,
        audio_index: int,
        pan: str,
        on_progress: ProgressCallback | None,
    ) -> LoudnessStats | None:
        cmd = commands.measure_command(self.config, source, audio_index=audio_index, pan=pan)
        stderr = self._run(
            cmd, duration=info.duration, stage="analyzing audio", on_progress=on_progress
        )
        stats = loudnorm.parse_stats(stderr)
        if stats.is_silent:
            log.warning("%s's audio is silent; skipping loudness normalization", source.name)
            return None
        log.info(
            "Measured loudness: %.1f LUFS integrated, %.1f dBTP peak, %.1f LU range",
            stats.input_i,
            stats.input_tp,
            stats.input_lra,
        )
        return stats

    def _verify(self, source_info: MediaInfo, staged: Path, *, expect_audio: bool) -> None:
        """Sanity-check the encode before it replaces anything on the NAS."""
        try:
            out = self._probe(staged)
        except ProbeError as exc:
            raise TranscodeError(f"The encoded file is unreadable: {exc}") from exc
        expected = commands.output_size(source_info, self.config.video)
        if (out.width, out.height) != expected:
            raise TranscodeError(
                f"Encoded video is {out.width}x{out.height}, expected {expected[0]}x{expected[1]}"
            )
        tolerance = max(2.0, 0.01 * source_info.duration)
        if abs(out.duration - source_info.duration) > tolerance:
            raise TranscodeError(
                f"Encoded video is {human_duration(out.duration)} long, but the source is "
                f"{human_duration(source_info.duration)}"
            )
        if expect_audio and not out.audio_streams:
            raise TranscodeError("Encoded video has no audio")


def _parse_optional_stats(stderr: str) -> LoudnessStats | None:
    try:
        stats = loudnorm.parse_stats(stderr)
    except LoudnormError:
        log.debug("No loudnorm statistics in the encode output")
        return None
    if stats.normalization_type:
        log.info(
            "Loudness normalized to %s LUFS (%s)",
            f"{stats.output_i:.1f}" if stats.output_i is not None else "?",
            stats.normalization_type,
        )
    return stats
