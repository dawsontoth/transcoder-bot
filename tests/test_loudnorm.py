import math

import pytest

from tests.helpers import loudnorm_stderr
from transcoder_bot.config import AudioConfig
from transcoder_bot.loudnorm import (
    LoudnessStats,
    LoudnormError,
    apply_filter,
    measure_filter,
    parse_stats,
)


def test_parses_ffmpeg_output():
    stats = parse_stats(loudnorm_stderr())

    assert stats.input_i == -27.61
    assert stats.input_tp == -4.47
    assert stats.input_lra == 8.06
    assert stats.input_thresh == -38.2
    assert stats.target_offset == 0.58
    assert stats.output_i == -16.02
    assert stats.normalization_type == "linear"
    assert not stats.is_silent


def test_silence_is_detected():
    stats = parse_stats(loudnorm_stderr(input_i="-inf", input_tp="-inf"))
    assert math.isinf(stats.input_i)
    assert stats.is_silent


def test_uses_the_last_block():
    text = loudnorm_stderr(input_i="-30.00") + loudnorm_stderr(input_i="-20.00")
    assert parse_stats(text).input_i == -20.0


def test_tolerates_log_prefixes_on_every_line():
    prefixed = "\n".join(
        f"[Parsed_loudnorm_0 @ 0x1] {line}" for line in loudnorm_stderr().splitlines()
    )
    assert parse_stats(prefixed).input_i == -27.61


def test_missing_statistics():
    with pytest.raises(LoudnormError, match="didn't print"):
        parse_stats("Conversion failed!")


def test_incomplete_statistics():
    with pytest.raises(LoudnormError, match="Couldn't parse"):
        parse_stats('{ "input_i" : "-20.0" }')


def test_measure_filter():
    assert measure_filter(AudioConfig(target_lufs=-14, true_peak=-1, lra=20)) == (
        "loudnorm=I=-14:TP=-1:LRA=20:print_format=json"
    )


def test_apply_filter_passes_measurements_and_clamps_them():
    stats = LoudnessStats(
        input_i=-120.0, input_tp=-4.5, input_lra=150.0, input_thresh=-130.0, target_offset=0.5
    )
    assert apply_filter(AudioConfig(), stats) == (
        "loudnorm=I=-16:TP=-1.5:LRA=11:measured_I=-99.00:measured_TP=-4.50"
        ":measured_LRA=99.00:measured_thresh=-99.00:offset=0.50:linear=true:print_format=json"
    )
