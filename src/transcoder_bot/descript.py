"""Uploading finished videos to Descript for editing (https://docs.descriptapi.com).

This does what Descript's own CLI (``@descript/platform-cli``) does with a local file:

1. ``POST /jobs/import/project_media`` with the file's type and size. Descript creates the
   project and returns a signed upload URL, valid for 3 hours.
2. ``PUT`` the file to that URL.
3. Poll ``GET /jobs/{job_id}`` until Descript has imported (and transcribed) it.

The API only accepts files up to 1 GB, so a bigger video first gets a smaller copy made just
for Descript. The full-quality file on the NAS is never touched.
"""

from __future__ import annotations

import functools
import json
import logging
import mimetypes
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, BinaryIO

from transcoder_bot import __version__, commands
from transcoder_bot.config import Config
from transcoder_bot.formatting import human_size
from transcoder_bot.media import MediaInfo, probe
from transcoder_bot.runner import Progress, ProgressCallback, RunFfmpeg, run_ffmpeg

log = logging.getLogger(__name__)

TERMINAL_JOB_STATES = ("stopped", "cancelled")
# Below this, a long recording squeezed under the size limit would be too rough to edit.
MIN_VIDEO_BITRATE = 1_000_000
# Aim a little under the limit, since an encoder's average bitrate is never exact.
SIZE_HEADROOM = 0.92


class DescriptError(RuntimeError):
    """Descript refused or failed a request."""


class DescriptTimeoutError(DescriptError):
    """Descript didn't finish processing within the time we were willing to wait."""


@dataclass(frozen=True)
class ImportJob:
    job_id: str
    project_id: str
    project_url: str
    upload_urls: dict[str, str]  # media name → signed upload URL


@dataclass(frozen=True)
class DescriptUpload:
    project_name: str
    project_id: str
    project_url: str
    uploaded_bytes: int
    shrunk: bool  # a smaller copy was uploaded to fit the API's size limit
    finished: bool  # Descript finished importing it (False if we stopped waiting)


def project_name(template: str, *, title: str, recorded: datetime) -> str:
    """``"{date} {stem}"`` → ``"2026-09-27 Service"``."""
    return template.format(stem=title, date=recorded.astimezone().date().isoformat()).strip()


class DescriptClient:
    """The few Descript API calls this tool needs."""

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = "https://descriptapi.com/v1/",
        timeout: float = 60.0,
        max_retries: int = 5,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._api_key = api_key
        self._base_url = base_url if base_url.endswith("/") else f"{base_url}/"
        self._timeout = timeout
        self._max_retries = max_retries
        self._sleep = sleep
        self._clock = clock

    def check(self) -> None:
        """Raise DescriptError unless the API key works."""
        self._request("GET", "status")

    def start_import(
        self,
        *,
        project_name: str,
        media: Mapping[str, Mapping[str, Any]],
        compositions: list[dict[str, Any]],
        folder: str = "",
        team_access: str = "",
    ) -> ImportJob:
        body: dict[str, Any] = {
            "project_name": project_name,
            "add_media": {name: dict(spec) for name, spec in media.items()},
            "add_compositions": compositions,
        }
        if folder:
            body["folder_name"] = folder
        if team_access:
            body["team_access"] = team_access
        # Not retried after server errors: the job may exist already, and retrying would
        # create a duplicate project.
        data = self._request("POST", "jobs/import/project_media", body, retry_server_errors=False)
        try:
            urls = {
                str(name): str(info["upload_url"])
                for name, info in (data.get("upload_urls") or {}).items()
            }
            return ImportJob(
                job_id=str(data["job_id"]),
                project_id=str(data["project_id"]),
                project_url=str(data.get("project_url") or ""),
                upload_urls=urls,
            )
        except (KeyError, TypeError, AttributeError) as exc:
            raise DescriptError(f"Unexpected reply from Descript: {data!r}") from exc

    def upload(
        self, url: str, path: Path, *, on_progress: Callable[[int], None] | None = None
    ) -> None:
        """Stream ``path`` to a signed upload URL, reporting the bytes sent so far."""
        size = path.stat().st_size
        with path.open("rb") as fh:
            request = urllib.request.Request(
                url,
                data=_CountingReader(fh, on_progress),
                method="PUT",
                headers={"Content-Type": "application/octet-stream", "Content-Length": str(size)},
            )
            try:
                with urllib.request.urlopen(request, timeout=self._timeout) as response:
                    response.read()
            except urllib.error.HTTPError as exc:
                raise DescriptError(
                    f"Uploading to Descript failed (HTTP {exc.code}) {_error_detail(exc)}".strip()
                ) from exc
            except OSError as exc:
                reason = getattr(exc, "reason", exc)
                raise DescriptError(f"Uploading to Descript failed: {reason}") from exc

    def report_upload_status(self, job_id: str, media_id: str, status: str, detail: str) -> None:
        """Tell Descript an upload didn't happen, so it can close the job. Best effort."""
        body = {"job_id": job_id, "media_id": media_id, "status": status, "detail": detail[:500]}
        try:
            self._request(
                "POST", "jobs/import/project_media/upload_status", body, retry_server_errors=False
            )
        except DescriptError:
            log.debug("Couldn't report the upload status to Descript", exc_info=True)

    def job(self, job_id: str) -> dict[str, Any]:
        return self._request("GET", f"jobs/{urllib.parse.quote(job_id, safe='')}")

    def wait_for_job(
        self, job_id: str, *, timeout: float, interval: float = 10.0
    ) -> dict[str, Any]:
        """Poll until the job stops. Raises DescriptError if it failed."""
        deadline = self._clock() + timeout
        while True:
            job = self.job(job_id)
            if job.get("job_state") in TERMINAL_JOB_STATES:
                _raise_for_failure(job)
                return job
            if self._clock() >= deadline:
                raise DescriptTimeoutError(f"Descript is still working on job {job_id}")
            self._sleep(interval)

    def _request(
        self,
        method: str,
        path: str,
        body: Mapping[str, Any] | None = None,
        *,
        retry_server_errors: bool = True,
    ) -> dict[str, Any]:
        url = urllib.parse.urljoin(self._base_url, path)
        data = json.dumps(body).encode() if body is not None else None
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Accept": "application/json",
            "User-Agent": f"transcoder-bot/{__version__}",
        }
        if data is not None:
            headers["Content-Type"] = "application/json"
        raw = b""
        for attempt in range(self._max_retries + 1):
            request = urllib.request.Request(url, data=data, method=method, headers=headers)
            try:
                with urllib.request.urlopen(request, timeout=self._timeout) as response:
                    raw = response.read()
                break
            except urllib.error.HTTPError as exc:
                retry = exc.code == 429 or (retry_server_errors and exc.code >= 500)
                if not retry or attempt == self._max_retries:
                    raise DescriptError(_describe_http_error(exc)) from exc
                delay = _retry_after(exc.headers.get("Retry-After"))
                if delay is None:
                    delay = _backoff(attempt)
                log.warning("Descript returned HTTP %d; retrying in %.0f s", exc.code, delay)
            except OSError as exc:  # network trouble
                reason = getattr(exc, "reason", exc)
                if not retry_server_errors or attempt == self._max_retries:
                    raise DescriptError(f"Can't reach Descript: {reason}") from exc
                delay = _backoff(attempt)
                log.warning("Can't reach Descript (%s); retrying in %.0f s", reason, delay)
            self._sleep(delay)
        if not raw:
            return {}
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise DescriptError("Descript sent a reply that isn't JSON") from exc
        return parsed if isinstance(parsed, dict) else {"data": parsed}


class DescriptUploader:
    """Put a finished video into a new Descript project, ready for editing."""

    def __init__(
        self,
        config: Config,
        client: DescriptClient | None = None,
        *,
        run: RunFfmpeg = run_ffmpeg,
        probe_fn: Callable[[Path], MediaInfo] | None = None,
        min_video_bitrate: int = MIN_VIDEO_BITRATE,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.config = config
        self.client = client or DescriptClient(
            config.descript.api_key, base_url=config.descript.api_url
        )
        self._run = run
        self._probe = probe_fn or functools.partial(probe, ffprobe=config.ffprobe)
        self._min_video_bitrate = min_video_bitrate
        self._clock = clock

    def upload(
        self,
        video: Path,
        *,
        title: str,
        recorded: datetime | None = None,
        name: str | None = None,
        on_progress: ProgressCallback | None = None,
    ) -> DescriptUpload:
        """Upload ``video`` as a new project named after ``title`` and the recording date."""
        cfg = self.config.descript
        if recorded is None:
            recorded = datetime.fromtimestamp(video.stat().st_mtime, tz=UTC)
        name = name or project_name(cfg.project_name, title=title, recorded=recorded)
        limit = int(cfg.max_upload_gb * 1_000_000_000)
        media_name = video.name
        with tempfile.TemporaryDirectory(prefix="transcoder-bot-", dir=self.config.temp_dir) as tmp:
            source, shrunk = video, False
            if video.stat().st_size > limit:
                source, shrunk = self._shrink(video, Path(tmp), limit, on_progress), True
            size = source.stat().st_size
            spec: dict[str, Any] = {
                "content_type": mimetypes.guess_type(media_name)[0] or "video/mp4",
                "file_size": size,
            }
            if cfg.language:
                spec["language"] = cfg.language
            job = self.client.start_import(
                project_name=name,
                media={media_name: spec},
                compositions=[{"name": title, "clips": [{"media": media_name}]}],
                folder=cfg.folder,
                team_access=cfg.team_access,
            )
            url = job.upload_urls.get(media_name)
            if not url:
                self.client.report_upload_status(
                    job.job_id, media_name, "failed", "No upload URL returned"
                )
                raise DescriptError("Descript didn't return an upload URL")
            log.info("Uploading %s (%s) to Descript as %r", media_name, human_size(size), name)
            self._send(job, media_name, url, source, on_progress)
        finished = self._wait(job)
        log.info("In Descript: %s", job.project_url)
        return DescriptUpload(name, job.project_id, job.project_url, size, shrunk, finished)

    def _send(
        self,
        job: ImportJob,
        media_name: str,
        url: str,
        source: Path,
        on_progress: ProgressCallback | None,
    ) -> None:
        size = source.stat().st_size
        started = self._clock()

        def report(sent: int) -> None:
            if on_progress is None:
                return
            fraction = sent / size if size else 1.0
            elapsed = self._clock() - started
            eta = elapsed / fraction - elapsed if fraction >= 0.01 else None
            on_progress(Progress("uploading to Descript", fraction, None, eta))

        try:
            self.client.upload(url, source, on_progress=report)
        except Exception as exc:
            self.client.report_upload_status(job.job_id, media_name, "failed", str(exc))
            raise
        except BaseException:
            self.client.report_upload_status(job.job_id, media_name, "aborted", "Interrupted")
            raise

    def _wait(self, job: ImportJob) -> bool:
        minutes = self.config.descript.wait_minutes
        if minutes <= 0:
            return False
        log.info("Waiting for Descript to import and transcribe it")
        try:
            self.client.wait_for_job(job.job_id, timeout=minutes * 60)
        except DescriptTimeoutError:
            log.warning("Descript is still processing after %g minutes; it'll carry on", minutes)
            return False
        return True

    def _shrink(
        self, video: Path, folder: Path, limit: int, on_progress: ProgressCallback | None
    ) -> Path:
        """Make a copy of ``video`` that fits in ``limit`` bytes."""
        info = self._probe(video)
        audio = info.audio_streams[0] if info.audio_streams else None
        # Keep AAC audio as it is (no quality loss); anything else becomes AAC 192k.
        copy_audio = audio is not None and audio.codec == "aac"
        audio_bits = (audio.bit_rate or 192_000) if audio is not None else 0
        bitrate = int(limit * SIZE_HEADROOM * 8 / info.duration) - audio_bits
        dest = folder / video.name
        limit_text = f"{self.config.descript.max_upload_gb:g} GB"
        for _attempt in range(3):
            if bitrate < self._min_video_bitrate:
                raise DescriptError(
                    f"{video.name} is too long to squeeze under Descript's {limit_text} API "
                    "limit at a watchable quality. Import it with the Descript app instead."
                )
            log.info(
                "%s is %s, over Descript's %s API limit; making a copy at %.1f Mbit/s",
                video.name,
                human_size(video.stat().st_size),
                limit_text,
                bitrate / 1e6,
            )
            cmd = commands.shrink_command(
                self.config, video, dest, video_bitrate=bitrate, copy_audio=copy_audio
            )
            self._run(
                cmd,
                duration=info.duration,
                stage="making a copy for Descript",
                on_progress=on_progress,
            )
            size = dest.stat().st_size
            if size <= limit:
                return dest
            bitrate = int(bitrate * limit / size * SIZE_HEADROOM)
        raise DescriptError(f"Couldn't make a copy of {video.name} small enough for Descript")


class _CountingReader:
    """Wraps a file so each read reports the running total of bytes read (i.e. sent)."""

    def __init__(self, fh: BinaryIO, on_progress: Callable[[int], None] | None) -> None:
        self._fh = fh
        self._on_progress = on_progress
        self._sent = 0

    def read(self, size: int = -1) -> bytes:
        chunk = self._fh.read(size)
        if chunk:
            self._sent += len(chunk)
            if self._on_progress is not None:
                self._on_progress(self._sent)
        return chunk


def _raise_for_failure(job: Mapping[str, Any]) -> None:
    if job.get("job_state") == "cancelled":
        raise DescriptError("Descript cancelled the import")
    result = job.get("result") or {}
    if result.get("status") == "error":
        raise DescriptError(
            f"Descript couldn't import it: {result.get('error_message') or 'unknown error'}"
        )
    failed = [
        f"{name} ({info.get('error_message') or 'failed'})"
        for name, info in (result.get("media_status") or {}).items()
        if isinstance(info, dict) and info.get("status") == "failed"
    ]
    if failed:
        raise DescriptError(f"Descript couldn't import {', '.join(failed)}")


def _describe_http_error(exc: urllib.error.HTTPError) -> str:
    explanations = {
        401: "Descript rejected the API key (check descript.api_key)",
        402: "Descript says the Drive is out of media minutes or AI credits",
        403: "Descript says this API key isn't allowed to do that",
        413: "Descript says the file is too big",
        429: "Descript is rate-limiting requests; try again later",
    }
    text = explanations.get(exc.code, f"Descript returned HTTP {exc.code}")
    detail = _error_detail(exc)
    return f"{text}: {detail}" if detail else text


def _error_detail(exc: urllib.error.HTTPError) -> str:
    try:
        raw = exc.read().decode("utf-8", "replace").strip()
    except OSError:
        return ""
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return raw[:300]
    if isinstance(data, dict):
        for key in ("message", "error_message", "error"):
            if data.get(key):
                return str(data[key])
    return raw[:300]


def _retry_after(value: str | None) -> float | None:
    """``Retry-After`` is either seconds or an HTTP date."""
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    return max(0.0, (when - datetime.now(UTC)).total_seconds())


def _backoff(attempt: int) -> float:
    return float(min(2.0 * 2.0**attempt, 60.0))
