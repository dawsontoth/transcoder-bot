from pathlib import Path

import pytest

from tests.helpers import make_config, media
from transcoder_bot.commands import (
    audio_filter,
    encode_command,
    measure_command,
    output_size,
    pan_filter,
    scaled_size,
    shrink_command,
    video_encoder_args,
    video_filter,
)
from transcoder_bot.config import AudioConfig, ConfigError, VideoConfig
from transcoder_bot.loudnorm import LoudnessStats

STATS = LoudnessStats(
    input_i=-27.61, input_tp=-4.47, input_lra=8.06, input_thresh=-38.2, target_offset=0.58
)


@pytest.mark.parametrize(
    ("size", "expected"),
    [
        ((3840, 2160), (1920, 1080)),  # UHD 4K
        ((4096, 2160), (2048, 1080)),  # DCI 4K
        ((2160, 3840), (1080, 1920)),  # already portrait
        ((1920, 1080), (1920, 1080)),  # 1080p: untouched
        ((1280, 720), (1280, 720)),  # never upscale
        ((1921, 1081), (1920, 1080)),  # odd sizes become even
    ],
)
def test_scaled_size(size, expected):
    width, height = size
    assert scaled_size(width, height, 1080) == expected


def test_output_size_accounts_for_rotation():
    info = media(3840, 2160)
    assert output_size(info, VideoConfig(rotate="ccw")) == (1080, 1920)
    assert output_size(info, VideoConfig(rotate="cw")) == (1080, 1920)
    assert output_size(info, VideoConfig(rotate="180")) == (1920, 1080)
    assert output_size(info, VideoConfig(rotate="none")) == (1920, 1080)


def test_video_filter_scales_before_rotating_counter_clockwise():
    assert video_filter(media(3840, 2160), VideoConfig()) == (
        "scale=1920:1080:flags=lanczos,setsar=1,transpose=dir=cclock,format=yuv420p"
    )


def test_video_filter_skips_scaling_for_1080p_sources():
    assert video_filter(media(1920, 1080), VideoConfig()) == "transpose=dir=cclock,format=yuv420p"


@pytest.mark.parametrize(
    ("rotate", "expected"),
    [
        ("cw", "transpose=dir=clock,format=yuv420p"),
        ("180", "hflip,vflip,format=yuv420p"),
        ("none", "format=yuv420p"),
    ],
)
def test_video_filter_rotations(rotate, expected):
    assert video_filter(media(1920, 1080), VideoConfig(rotate=rotate)) == expected


def test_video_filter_uses_the_displayed_size_of_rotated_sources():
    # A phone clip stored as 3840x2160 but flagged to display portrait.
    info = media(3840, 2160, rotation=90)
    assert video_filter(info, VideoConfig(rotate="none")).startswith("scale=1080:1920:")


@pytest.mark.parametrize(
    ("source_channels", "audio", "expected"),
    [
        (2, AudioConfig(), "pan=stereo|c0=c0|c1=c1"),
        (16, AudioConfig(), "pan=stereo|c0=c0|c1=c1"),
        (1, AudioConfig(), "pan=stereo|c0=c0|c1=c0"),
        (2, AudioConfig(channels=(1,)), "pan=stereo|c0=c0|c1=c0"),
        (16, AudioConfig(channels=(3, 4)), "pan=stereo|c0=c2|c1=c3"),
        (2, AudioConfig(mono=True), "pan=stereo|c0=0.5*c0+0.5*c1|c1=0.5*c0+0.5*c1"),
        (1, AudioConfig(channels=(3, 4)), "pan=stereo|c0=c0|c1=c0"),
    ],
)
def test_pan_filter(source_channels, audio, expected):
    assert pan_filter(source_channels, audio) == expected


def test_pan_filter_rejects_channels_the_recording_lacks():
    with pytest.raises(ConfigError, match="channel 3, but the recording's audio only has 2"):
        pan_filter(2, AudioConfig(channels=(3, 4)))


def test_audio_filter_normalizes_then_resamples():
    chain = audio_filter("pan=stereo|c0=c0|c1=c1", AudioConfig(), STATS)
    parts = chain.split(",")

    assert parts[0] == "pan=stereo|c0=c0|c1=c1"
    assert parts[1].startswith("loudnorm=I=-16:TP=-1.5:LRA=11:measured_I=-27.61:")
    assert parts[-1] == "aresample=48000"


def test_audio_filter_without_normalization():
    assert audio_filter("pan=x", AudioConfig(), None) == "pan=x,aresample=48000"


@pytest.mark.parametrize(
    ("encoder", "expected"),
    [
        ("libx264", ["-c:v", "libx264", "-preset", "medium", "-crf", "20", "-profile:v", "high"]),
        ("libx265", ["-c:v", "libx265", "-preset", "medium", "-crf", "20", "-tag:v", "hvc1"]),
        ("h264_videotoolbox", ["-c:v", "h264_videotoolbox", "-q:v", "65", "-profile:v", "high"]),
        ("hevc_videotoolbox", ["-c:v", "hevc_videotoolbox", "-q:v", "65", "-tag:v", "hvc1"]),
    ],
)
def test_video_encoder_args(encoder, expected):
    assert video_encoder_args(VideoConfig(encoder=encoder)) == expected


def test_extra_args_are_appended():
    args = video_encoder_args(VideoConfig(extra_args=("-g", "60")))
    assert args[-2:] == ["-g", "60"]


def test_measure_command_only_decodes_audio(tmp_path):
    config = make_config(tmp_path)
    cmd = measure_command(config, Path("/NAS/take.mov"), audio_index=1, pan="pan=x")

    assert cmd[0] == "ffmpeg"
    assert cmd[cmd.index("-map") + 1] == "0:a:1"
    assert cmd[cmd.index("-af") + 1] == "pan=x,loudnorm=I=-16:TP=-1.5:LRA=11:print_format=json"
    assert cmd[-3:] == ["-f", "null", "-"]
    assert "-progress" in cmd


def test_encode_command(tmp_path):
    config = make_config(tmp_path, video={"hwaccel": "videotoolbox"})
    cmd = encode_command(
        config,
        media(),
        Path("/NAS/take.mov"),
        Path("/tmp/take_1080p.mp4"),
        audio_index=0,
        pan="pan=stereo|c0=c0|c1=c1",
        loudness=STATS,
    )

    assert cmd.index("-hwaccel") < cmd.index("-i")
    assert cmd[cmd.index("-i") + 1] == "/NAS/take.mov"
    maps = [cmd[i + 1] for i, arg in enumerate(cmd) if arg == "-map"]
    assert maps == ["0:v:0", "0:a:0"]
    assert "transpose=dir=cclock" in cmd[cmd.index("-vf") + 1]
    assert "linear=true" in cmd[cmd.index("-af") + 1]
    assert cmd[cmd.index("-c:a") + 1] == "aac"
    assert cmd[cmd.index("-movflags") + 1] == "+faststart"
    assert cmd[-1] == "/tmp/take_1080p.mp4"


def test_encode_command_without_audio(tmp_path):
    cmd = encode_command(
        make_config(tmp_path),
        media(channels=()),
        Path("/NAS/take.mov"),
        Path("/tmp/out.mp4"),
        audio_index=None,
        pan=None,
        loudness=None,
    )
    assert "-af" not in cmd
    assert "-c:a" not in cmd
    assert "-hwaccel" not in cmd


def test_shrink_command_caps_the_bitrate(tmp_path):
    cmd = shrink_command(
        make_config(tmp_path),
        Path("/NAS/take_1080p.mp4"),
        Path("/tmp/take_1080p.mp4"),
        video_bitrate=2_600_000,
        copy_audio=True,
    )

    assert cmd[cmd.index("-c:v") + 1] == "libx264"
    assert cmd[cmd.index("-b:v") + 1] == "2600k"
    assert cmd[cmd.index("-maxrate") + 1] == "3900k"
    assert cmd[cmd.index("-bufsize") + 1] == "7800k"
    assert cmd[cmd.index("-c:a") + 1] == "copy"
    assert cmd[-1] == "/tmp/take_1080p.mp4"


def test_shrink_command_with_videotoolbox_and_new_audio(tmp_path):
    config = make_config(tmp_path, video={"encoder": "hevc_videotoolbox"})

    cmd = shrink_command(
        config, Path("/in.mp4"), Path("/out.mp4"), video_bitrate=2_000_000, copy_audio=False
    )

    assert cmd[cmd.index("-c:v") + 1] == "h264_videotoolbox"  # H.264 for Descript either way
    assert "-maxrate" not in cmd
    assert cmd[cmd.index("-c:a") + 1 : cmd.index("-c:a") + 4] == ["aac", "-b:a", "192k"]
