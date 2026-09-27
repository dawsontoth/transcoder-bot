import sys

import pytest

from transcoder_bot.media import ProbeError, parse_probe, probe

# Trimmed ffprobe output for a HyperDeck-style 2160p ProRes recording with 16 audio channels.
HYPERDECK_PROBE = {
    "streams": [
        {
            "index": 0,
            "codec_name": "prores",
            "codec_type": "video",
            "width": 3840,
            "height": 2160,
            "avg_frame_rate": "30000/1001",
            "duration": "2700.033333",
        },
        {
            "index": 1,
            "codec_name": "pcm_s24le",
            "codec_type": "audio",
            "sample_rate": "48000",
            "channels": 16,
        },
        {"index": 2, "codec_type": "data", "codec_tag_string": "tmcd"},
    ],
    "format": {"format_name": "mov,mp4,m4a,3gp,3g2,mj2", "duration": "2700.040000"},
}


def test_parses_a_hyperdeck_recording():
    info = parse_probe(HYPERDECK_PROBE)

    assert info.width == 3840
    assert info.height == 2160
    assert info.video_codec == "prores"
    assert info.duration == pytest.approx(2700.04)
    assert info.frame_rate == pytest.approx(29.97, abs=0.01)
    assert info.rotation == 0
    assert info.display_size == (3840, 2160)
    assert len(info.audio_streams) == 1
    assert info.audio_streams[0].channels == 16
    assert info.audio_streams[0].sample_rate == 48000


def test_rotation_metadata_swaps_the_display_size():
    data = {
        "streams": [
            {
                "codec_type": "video",
                "width": 1920,
                "height": 1080,
                "side_data_list": [{"side_data_type": "Display Matrix", "rotation": -90}],
            }
        ],
        "format": {"duration": "10"},
    }

    info = parse_probe(data)

    assert info.rotation == 270
    assert info.display_size == (1080, 1920)


def test_legacy_rotate_tag():
    data = {
        "streams": [
            {"codec_type": "video", "width": 1920, "height": 1080, "tags": {"rotate": "90"}}
        ],
        "format": {"duration": "10"},
    }
    assert parse_probe(data).rotation == 90


def test_falls_back_to_the_stream_duration():
    data = {
        "streams": [{"codec_type": "video", "width": 8, "height": 8, "duration": "12.5"}],
        "format": {},
    }
    assert parse_probe(data).duration == 12.5


def test_missing_duration_means_unreadable():
    data = {"streams": [{"codec_type": "video", "width": 8, "height": 8}], "format": {}}
    with pytest.raises(ProbeError, match="still recording"):
        parse_probe(data, name="take.mov")


def test_cover_art_is_not_video():
    data = {
        "streams": [
            {"codec_type": "video", "width": 8, "height": 8, "disposition": {"attached_pic": 1}}
        ],
        "format": {"duration": "10"},
    }
    with pytest.raises(ProbeError, match="no video stream"):
        parse_probe(data)


def test_ignores_audio_streams_without_channels():
    data = {
        "streams": [
            {"codec_type": "video", "width": 8, "height": 8},
            {"codec_type": "audio", "codec_name": "aac", "channels": 0},
        ],
        "format": {"duration": "1"},
    }
    assert parse_probe(data).audio_streams == ()


def test_unknown_frame_rate():
    data = {
        "streams": [{"codec_type": "video", "width": 8, "height": 8, "avg_frame_rate": "0/0"}],
        "format": {"duration": "1"},
    }
    assert parse_probe(data).frame_rate is None


def test_probe_reports_a_missing_ffprobe(tmp_path):
    with pytest.raises(ProbeError, match="brew install ffmpeg"):
        probe(tmp_path / "x.mov", ffprobe=str(tmp_path / "no-such-ffprobe"))


def test_probe_reports_ffprobe_errors(tmp_path):
    fake = tmp_path / "ffprobe"
    fake.write_text(
        f"#!{sys.executable}\nimport sys\n"
        "print('moov atom not found', file=sys.stderr)\nsys.exit(1)\n"
    )
    fake.chmod(0o755)

    with pytest.raises(ProbeError, match=r"take\.mov: moov atom not found"):
        probe(tmp_path / "take.mov", ffprobe=str(fake))
