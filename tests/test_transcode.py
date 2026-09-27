from collections.abc import Sequence
from pathlib import Path

import pytest

from tests.helpers import loudnorm_stderr, make_config, media
from transcoder_bot.config import ConfigError
from transcoder_bot.media import MediaInfo
from transcoder_bot.runner import FfmpegError, ProgressCallback
from transcoder_bot.transcode import (
    OutputExistsError,
    TranscodeError,
    Transcoder,
    is_output_file,
    output_path_for,
    publish,
)


class FakeFfmpeg:
    """Records commands; 'encodes' by writing the output file named last on the command line."""

    def __init__(self, *, measure_stderr: str | None = None, fail_encode: bool = False) -> None:
        self.commands: list[list[str]] = []
        self.measure_stderr = measure_stderr if measure_stderr is not None else loudnorm_stderr()
        self.fail_encode = fail_encode

    def __call__(
        self,
        cmd: Sequence[str],
        *,
        duration: float | None,
        stage: str,
        on_progress: ProgressCallback | None,
    ) -> str:
        self.commands.append(list(cmd))
        if cmd[-1] == "-":  # the loudness measurement pass writes to the null muxer
            return self.measure_stderr
        if self.fail_encode:
            raise FfmpegError(cmd, 1, "Conversion failed!")
        Path(cmd[-1]).write_bytes(b"encoded video")
        return loudnorm_stderr(input_i="-27.50", output_i="-16.01")


class FakeProbe:
    def __init__(self, source: MediaInfo, output: MediaInfo | None = None) -> None:
        self.source = source
        self.output = output or media(1080, 1920, channels=(2,))

    def __call__(self, path: Path) -> MediaInfo:
        return self.output if path.name.endswith("_1080p.mp4") else self.source


@pytest.fixture
def setup(tmp_path):
    recordings = tmp_path / "HyperDeck"
    recordings.mkdir()
    temp = tmp_path / "temp"
    temp.mkdir()
    source = recordings / "Service.mov"
    source.write_bytes(b"prores")
    config = make_config(recordings, temp_dir=str(temp))
    return config, source, temp


def test_output_naming(tmp_path):
    config = make_config(tmp_path)
    assert output_path_for(Path("/NAS/Service.mov"), config.output) == Path(
        "/NAS/Service_1080p.mp4"
    )
    assert is_output_file(Path("/NAS/Service_1080p.mp4"), config.output)
    assert is_output_file(Path("/NAS/Service_1080p.MP4"), config.output)
    assert not is_output_file(Path("/NAS/Service.mp4"), config.output)
    assert not is_output_file(Path("/NAS/Service_1080p.mov"), config.output)


def test_transcodes_next_to_the_source(setup):
    config, source, temp = setup
    ffmpeg = FakeFfmpeg()
    transcoder = Transcoder(config, run=ffmpeg, probe_fn=FakeProbe(media()))

    result = transcoder.transcode(source)

    dest = source.with_name("Service_1080p.mp4")
    assert result.output == dest
    assert dest.read_bytes() == b"encoded video"
    assert result.output_bytes == len(b"encoded video")
    assert result.loudness_before is not None
    assert result.loudness_before.input_i == -27.61
    assert result.loudness_after is not None
    assert result.loudness_after.output_i == -16.01
    measure, encode = ffmpeg.commands
    assert measure[-1] == "-"
    assert "measured_I=-27.61" in encode[encode.index("-af") + 1]
    # Nothing left behind: no partial copy on the NAS, no temp files.
    assert sorted(p.name for p in source.parent.iterdir()) == ["Service.mov", "Service_1080p.mp4"]
    assert list(temp.iterdir()) == []


def test_refuses_to_redo_existing_output(setup):
    config, source, _ = setup
    source.with_name("Service_1080p.mp4").write_bytes(b"old")
    transcoder = Transcoder(config, run=FakeFfmpeg(), probe_fn=FakeProbe(media()))

    with pytest.raises(OutputExistsError):
        transcoder.transcode(source)

    transcoder.transcode(source, force=True)
    assert source.with_name("Service_1080p.mp4").read_bytes() == b"encoded video"


def test_missing_source(setup):
    config, source, _ = setup
    transcoder = Transcoder(config, run=FakeFfmpeg(), probe_fn=FakeProbe(media()))
    with pytest.raises(TranscodeError, match="doesn't exist"):
        transcoder.transcode(source.with_name("nope.mov"))


def test_recording_without_audio(setup):
    config, source, _ = setup
    ffmpeg = FakeFfmpeg()
    probe = FakeProbe(media(channels=()), output=media(1080, 1920, channels=()))

    Transcoder(config, run=ffmpeg, probe_fn=probe).transcode(source)

    (encode,) = ffmpeg.commands
    assert "-af" not in encode


def test_silent_audio_is_not_normalized(setup):
    config, source, _ = setup
    ffmpeg = FakeFfmpeg(measure_stderr=loudnorm_stderr(input_i="-inf", input_tp="-inf"))

    result = Transcoder(config, run=ffmpeg, probe_fn=FakeProbe(media())).transcode(source)

    encode = ffmpeg.commands[-1]
    assert "loudnorm" not in encode[encode.index("-af") + 1]
    assert result.loudness_before is None


def test_normalization_can_be_turned_off(setup):
    config, source, _ = setup
    config = make_config(config.recordings_dir, audio={"normalize": False})
    ffmpeg = FakeFfmpeg()

    Transcoder(config, run=ffmpeg, probe_fn=FakeProbe(media())).transcode(source)

    assert len(ffmpeg.commands) == 1


def test_missing_audio_stream_is_a_config_error(setup):
    config, source, _ = setup
    config = make_config(config.recordings_dir, audio={"stream": 2})
    transcoder = Transcoder(config, run=FakeFfmpeg(), probe_fn=FakeProbe(media()))
    with pytest.raises(ConfigError, match="only has 1 audio stream"):
        transcoder.transcode(source)


def test_bad_encode_is_never_published(setup):
    config, source, _ = setup
    wrong_size = media(1920, 1080)  # not rotated
    transcoder = Transcoder(config, run=FakeFfmpeg(), probe_fn=FakeProbe(media(), wrong_size))

    with pytest.raises(TranscodeError, match="expected 1080x1920"):
        transcoder.transcode(source)

    assert not source.with_name("Service_1080p.mp4").exists()


def test_truncated_encode_is_never_published(setup):
    config, source, _ = setup
    short = media(1080, 1920, duration=600)
    transcoder = Transcoder(config, run=FakeFfmpeg(), probe_fn=FakeProbe(media(), short))

    with pytest.raises(TranscodeError, match="10m 00s long"):
        transcoder.transcode(source)


def test_ffmpeg_failure_leaves_nothing_behind(setup):
    config, source, temp = setup
    transcoder = Transcoder(config, run=FakeFfmpeg(fail_encode=True), probe_fn=FakeProbe(media()))

    with pytest.raises(FfmpegError):
        transcoder.transcode(source)

    assert [p.name for p in source.parent.iterdir()] == ["Service.mov"]
    assert list(temp.iterdir()) == []


def test_checks_free_space_before_encoding(setup, monkeypatch):
    config, source, _ = setup

    class Usage:
        free = 1024

    monkeypatch.setattr("transcoder_bot.transcode.shutil.disk_usage", lambda path: Usage())
    ffmpeg = FakeFfmpeg()
    transcoder = Transcoder(config, run=ffmpeg, probe_fn=FakeProbe(media()))

    with pytest.raises(TranscodeError, match="Not enough free space"):
        transcoder.transcode(source)
    assert all(cmd[-1] == "-" for cmd in ffmpeg.commands)  # never started encoding


def test_describe(setup):
    config, source, _ = setup
    transcoder = Transcoder(config, run=FakeFfmpeg(), probe_fn=FakeProbe(media(channels=(16,))))

    text = transcoder.describe(source)

    assert "Service_1080p.mp4" in text
    assert "3840x2160 prores, 45m 00s → 1080x1920 libx264, 25 Mbit/s" in text
    assert "transpose=dir=cclock" in text
    assert "16 ch" in text
    assert "loudnorm to -16 LUFS" in text


def test_publish_is_atomic(tmp_path, monkeypatch):
    staged = tmp_path / "staged.mp4"
    staged.write_bytes(b"data")
    dest = tmp_path / "nas" / "out.mp4"
    dest.parent.mkdir()

    def fail(self: Path, target: Path) -> Path:
        raise OSError("network went away")

    monkeypatch.setattr(Path, "replace", fail)
    with pytest.raises(OSError, match="network went away"):
        publish(staged, dest)

    assert list(dest.parent.iterdir()) == []  # the hidden partial copy was removed
