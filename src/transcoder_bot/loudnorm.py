"""Two-pass EBU R128 loudness normalization with ffmpeg's ``loudnorm`` filter.

Pass 1 measures the recording; pass 2 feeds those measurements back so loudnorm can apply a
single, linear gain change. (loudnorm falls back to gentle dynamic processing when a linear
gain would push peaks over the true-peak limit, or when the recording's loudness range is wider
than the ``lra`` target.)
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass

from transcoder_bot.config import AudioConfig

# EBU R128's absolute gate: anything quieter than this is treated as silence.
SILENCE_LUFS = -70.0

_STATS_BLOCK = re.compile(r"\{[^{}]*\"input_i\"[^{}]*\}", re.DOTALL)
_LOG_PREFIX = re.compile(r"^\s*\[[^\]\n]*\]\s?", re.MULTILINE)


class LoudnormError(RuntimeError):
    """ffmpeg's output didn't contain the loudnorm statistics."""


@dataclass(frozen=True)
class LoudnessStats:
    input_i: float
    input_tp: float
    input_lra: float
    input_thresh: float
    target_offset: float
    output_i: float | None = None
    output_tp: float | None = None
    normalization_type: str | None = None

    @property
    def is_silent(self) -> bool:
        return not math.isfinite(self.input_i) or self.input_i <= SILENCE_LUFS


def measure_filter(audio: AudioConfig) -> str:
    return f"loudnorm={_targets(audio)}:print_format=json"


def apply_filter(audio: AudioConfig, stats: LoudnessStats) -> str:
    # Clamp to the option ranges loudnorm accepts so an odd measurement can't fail the encode.
    measured = (
        f"measured_I={_clamp(stats.input_i, -99, 0):.2f}"
        f":measured_TP={_clamp(stats.input_tp, -99, 99):.2f}"
        f":measured_LRA={_clamp(stats.input_lra, 0, 99):.2f}"
        f":measured_thresh={_clamp(stats.input_thresh, -99, 0):.2f}"
        f":offset={_clamp(stats.target_offset, -99, 99):.2f}"
    )
    return f"loudnorm={_targets(audio)}:{measured}:linear=true:print_format=json"


def parse_stats(ffmpeg_stderr: str) -> LoudnessStats:
    """Extract the JSON block loudnorm prints to stderr when ffmpeg finishes."""
    blocks = _STATS_BLOCK.findall(ffmpeg_stderr)
    if not blocks:
        raise LoudnormError("ffmpeg didn't print loudnorm statistics")
    try:
        raw = json.loads(_LOG_PREFIX.sub("", blocks[-1]))
        return LoudnessStats(
            input_i=float(raw["input_i"]),
            input_tp=float(raw["input_tp"]),
            input_lra=float(raw["input_lra"]),
            input_thresh=float(raw["input_thresh"]),
            target_offset=float(raw["target_offset"]),
            output_i=_optional_float(raw.get("output_i")),
            output_tp=_optional_float(raw.get("output_tp")),
            normalization_type=raw.get("normalization_type"),
        )
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        raise LoudnormError(f"Couldn't parse loudnorm statistics: {exc}") from exc


def _targets(audio: AudioConfig) -> str:
    return f"I={audio.target_lufs:g}:TP={audio.true_peak:g}:LRA={audio.lra:g}"


def _clamp(value: float, low: float, high: float) -> float:
    if math.isnan(value):
        return low
    return max(low, min(high, value))


def _optional_float(value: object) -> float | None:
    if value is None:
        return None
    try:
        return float(str(value))
    except ValueError:
        return None
