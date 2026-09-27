from collections.abc import Iterator, Sequence
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from tests.fake_descript import GOOD_KEY, JOB_ID, PROJECT_URL, FakeDescript, serve_fake_descript
from tests.helpers import NOW, make_config, media
from transcoder_bot.descript import (
    DescriptClient,
    DescriptError,
    DescriptUploader,
    _retry_after,
    project_name,
)
from transcoder_bot.media import AudioStream, MediaInfo
from transcoder_bot.runner import Progress, ProgressCallback

TODAY = NOW.astimezone().date().isoformat()


class FakeClock:
    """A clock that only moves when something sleeps."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


@pytest.fixture
def fake(monkeypatch) -> Iterator[FakeDescript]:
    # Talk to the local fake directly, even if the environment sets an HTTP proxy.
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    with serve_fake_descript() as server:
        yield server


@pytest.fixture
def video(tmp_path: Path) -> Path:
    path = tmp_path / "HyperDeck" / "Service_1080p.mp4"
    path.parent.mkdir()
    path.write_bytes(b"\x00\x00\x00\x18ftypmp42" + bytes(range(256)) * 40)
    return path


def make_uploader(
    fake: FakeDescript, tmp_path: Path, clock: FakeClock | None = None, **descript: Any
):
    clock = clock or FakeClock()
    config = make_config(tmp_path, descript={"api_key": GOOD_KEY, **descript})
    client = DescriptClient(GOOD_KEY, base_url=fake.base_url, sleep=clock.sleep, clock=clock)
    return DescriptUploader(config, client, clock=clock)


def test_project_name():
    assert project_name("{date} {stem}", title="Service", recorded=NOW) == f"{TODAY} Service"
    assert project_name("{stem}", title="Service", recorded=NOW) == "Service"


def test_check_accepts_a_good_key_and_rejects_a_bad_one(fake):
    DescriptClient(GOOD_KEY, base_url=fake.base_url).check()

    with pytest.raises(DescriptError, match=r"rejected the API key.*: Invalid API key"):
        DescriptClient("dx_bearer_nope:dx_secret_nope", base_url=fake.base_url).check()


def test_uploads_into_a_new_project(fake, video, tmp_path):
    progress: list[Progress] = []

    upload = make_uploader(fake, tmp_path).upload(
        video, title="Service", recorded=NOW, on_progress=progress.append
    )

    (request,) = fake.imports
    size = video.stat().st_size
    assert request == {
        "project_name": f"{TODAY} Service",
        "add_media": {"Service_1080p.mp4": {"content_type": "video/mp4", "file_size": size}},
        "add_compositions": [{"name": "Service", "clips": [{"media": "Service_1080p.mp4"}]}],
    }
    assert fake.uploads["Service_1080p.mp4"] == video.read_bytes()
    headers = fake.upload_headers["Service_1080p.mp4"]
    assert headers["Content-Type"] == "application/octet-stream"
    assert headers["Content-Length"] == str(size)
    assert "Authorization" not in headers  # the signed URL is the credential
    assert fake.job_polls == 2  # "running", then "stopped"
    assert upload.project_url == PROJECT_URL
    assert upload.project_name == f"{TODAY} Service"
    assert upload.uploaded_bytes == size
    assert upload.finished
    assert not upload.shrunk
    assert progress[-1] == Progress("uploading to Descript", 1.0, None, progress[-1].eta_seconds)


def test_optional_settings_are_sent(fake, video, tmp_path):
    uploader = make_uploader(
        fake, tmp_path, folder="HyperDeck", team_access="edit", language="en", project_name="{stem}"
    )

    uploader.upload(video, title="Service", recorded=NOW, name="Custom name")

    (request,) = fake.imports
    assert request["project_name"] == "Custom name"
    assert request["folder_name"] == "HyperDeck"
    assert request["team_access"] == "edit"
    assert request["add_media"]["Service_1080p.mp4"]["language"] == "en"


def test_rate_limits_are_retried(fake):
    fake.rate_limit_first = 2
    fake.retry_after = "3"
    clock = FakeClock()

    DescriptClient(GOOD_KEY, base_url=fake.base_url, sleep=clock.sleep, clock=clock).check()

    assert clock.sleeps == [3.0, 3.0]


def test_rate_limits_eventually_give_up(fake):
    fake.rate_limit_first = 10
    clock = FakeClock()
    client = DescriptClient(
        GOOD_KEY, base_url=fake.base_url, max_retries=2, sleep=clock.sleep, clock=clock
    )

    with pytest.raises(DescriptError, match="rate-limiting"):
        client.check()


def test_failed_import_is_an_error(fake, video, tmp_path):
    fake.job_result = {"status": "error", "error_message": "Unsupported codec"}

    with pytest.raises(DescriptError, match="Unsupported codec"):
        make_uploader(fake, tmp_path).upload(video, title="Service", recorded=NOW)


def test_failed_media_is_an_error(fake, video, tmp_path):
    fake.job_result = {
        "status": "success",
        "media_status": {"Service_1080p.mp4": {"status": "failed", "error_message": "Corrupt"}},
    }

    with pytest.raises(DescriptError, match=r"Service_1080p\.mp4 \(Corrupt\)"):
        make_uploader(fake, tmp_path).upload(video, title="Service", recorded=NOW)


def test_rejected_upload_is_reported_to_descript(fake, video, tmp_path):
    fake.upload_status_code = 403

    with pytest.raises(DescriptError, match="HTTP 403"):
        make_uploader(fake, tmp_path).upload(video, title="Service", recorded=NOW)

    (report,) = fake.status_reports
    assert report["job_id"] == JOB_ID
    assert report["media_id"] == "Service_1080p.mp4"
    assert report["status"] == "failed"


def test_stops_waiting_after_wait_minutes(fake, video, tmp_path):
    fake.running_polls = 1000
    clock = FakeClock()

    upload = make_uploader(fake, tmp_path, clock, wait_minutes=1).upload(
        video, title="Service", recorded=NOW
    )

    assert not upload.finished
    assert upload.project_url == PROJECT_URL
    assert sum(clock.sleeps) >= 60


def test_can_skip_waiting(fake, video, tmp_path):
    upload = make_uploader(fake, tmp_path, wait_minutes=0).upload(
        video, title="Service", recorded=NOW
    )

    assert not upload.finished
    assert fake.job_polls == 0


def test_retry_after_can_be_a_date():
    later = (NOW + timedelta(days=36500)).strftime("%a, %d %b %Y %H:%M:%S GMT")
    assert _retry_after("7") == 7.0
    assert _retry_after(later) is not None
    assert _retry_after("soon") is None
    assert _retry_after(None) is None


class FakeShrink:
    """Stands in for ffmpeg making the smaller copy: writes ``sizes`` bytes, one per attempt."""

    def __init__(self, *sizes: int) -> None:
        self.sizes = list(sizes)
        self.commands: list[list[str]] = []

    def __call__(
        self,
        cmd: Sequence[str],
        *,
        duration: float | None,
        stage: str,
        on_progress: ProgressCallback | None,
    ) -> str:
        self.commands.append(list(cmd))
        Path(cmd[-1]).write_bytes(b"x" * self.sizes.pop(0))
        return ""


def aac_video(duration: float = 60.0) -> MediaInfo:
    info = media(1080, 1920, duration=duration)
    return MediaInfo(
        duration=info.duration,
        width=info.width,
        height=info.height,
        video_codec="h264",
        audio_streams=(AudioStream("aac", 2, 48000, bit_rate=192_000),),
    )


def shrinking_uploader(fake, tmp_path, run: FakeShrink, **kwargs: Any) -> DescriptUploader:
    config = make_config(
        tmp_path, temp_dir=str(tmp_path), descript={"api_key": GOOD_KEY, "max_upload_gb": 1e-6}
    )
    clock = FakeClock()
    client = DescriptClient(GOOD_KEY, base_url=fake.base_url, sleep=clock.sleep, clock=clock)
    # A 1000-byte limit and a very short video keep the numbers small but realistic.
    return DescriptUploader(
        config, client, run=run, probe_fn=lambda path: aac_video(0.001), clock=clock, **kwargs
    )


def test_videos_over_the_limit_get_a_smaller_copy(fake, video, tmp_path):
    run = FakeShrink(900)

    upload = shrinking_uploader(fake, tmp_path, run, min_video_bitrate=0).upload(
        video, title="Service", recorded=NOW
    )

    assert upload.shrunk
    assert upload.uploaded_bytes == 900
    assert fake.uploads["Service_1080p.mp4"] == b"x" * 900
    assert fake.imports[0]["add_media"]["Service_1080p.mp4"]["file_size"] == 900
    (cmd,) = run.commands
    assert cmd[cmd.index("-c:a") + 1] == "copy"  # AAC audio is kept as it is
    assert "-b:v" in cmd
    assert video.read_bytes().startswith(b"\x00\x00\x00\x18ftyp")  # the original is untouched


def test_a_copy_that_is_still_too_big_is_redone_smaller(fake, video, tmp_path):
    run = FakeShrink(1500, 900)

    shrinking_uploader(fake, tmp_path, run, min_video_bitrate=0).upload(
        video, title="Service", recorded=NOW
    )

    first, second = run.commands
    bitrate = [int(c[c.index("-b:v") + 1].rstrip("k")) for c in (first, second)]
    assert bitrate[1] < bitrate[0]


def test_very_long_videos_are_not_squeezed_to_mush(fake, video, tmp_path):
    run = FakeShrink()
    uploader = shrinking_uploader(fake, tmp_path, run, min_video_bitrate=10**9)

    with pytest.raises(DescriptError, match="too long"):
        uploader.upload(video, title="Service", recorded=NOW)

    assert fake.imports == []
