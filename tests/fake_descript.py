"""A small stand-in for Descript's API, served on localhost for the tests.

It follows the request and response shapes Descript's own CLI uses.
"""

from __future__ import annotations

import json
import threading
import urllib.parse
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

GOOD_KEY = "dx_bearer_test:dx_secret_test"
JOB_ID = "job-1"
PROJECT_URL = "https://web.descript.com/proj-1"


@dataclass
class FakeDescript:
    base_url: str = ""
    upload_base: str = ""
    # What the API received.
    imports: list[dict[str, Any]] = field(default_factory=list)
    uploads: dict[str, bytes] = field(default_factory=dict)
    upload_headers: dict[str, dict[str, str]] = field(default_factory=dict)
    status_reports: list[dict[str, Any]] = field(default_factory=list)
    job_polls: int = 0
    # How it behaves.
    running_polls: int = 1  # polls answered "running" before the job stops
    rate_limit_first: int = 0  # answer this many API requests with 429 first
    retry_after: str = "0"
    upload_status_code: int = 200
    job_result: dict[str, Any] | None = None  # default: every media imported fine


class _Server(ThreadingHTTPServer):
    state: FakeDescript


class _Handler(BaseHTTPRequestHandler):
    server: _Server

    def log_message(self, format: str, *args: Any) -> None:
        pass  # keep test output quiet

    def do_GET(self) -> None:
        if not self._api_allowed():
            return
        state = self.server.state
        if self.path == "/v1/status":
            self._json(200, {"status": "ok"})
        elif self.path == f"/v1/jobs/{JOB_ID}":
            state.job_polls += 1
            if state.job_polls <= state.running_polls:
                self._json(200, {"job_id": JOB_ID, "job_state": "running"})
                return
            media = state.imports[-1]["add_media"] if state.imports else {}
            result = state.job_result or {
                "status": "success",
                "media_status": {
                    name: {"status": "success", "duration_seconds": 6.0} for name in media
                },
            }
            self._json(
                200,
                {
                    "job_id": JOB_ID,
                    "job_type": "import/project_media",
                    "job_state": "stopped",
                    "result": result,
                },
            )
        else:
            self._json(404, {"error": "not_found"})

    def do_POST(self) -> None:
        if not self._api_allowed():
            return
        state = self.server.state
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if self.path == "/v1/jobs/import/project_media":
            state.imports.append(body)
            upload_urls = {
                name: {"upload_url": f"{state.upload_base}{urllib.parse.quote(name)}"}
                for name, spec in body["add_media"].items()
                if "file_size" in spec
            }
            self._json(
                201,
                {
                    "job_id": JOB_ID,
                    "project_id": "proj-1",
                    "project_url": PROJECT_URL,
                    "upload_urls": upload_urls,
                },
            )
        elif self.path == "/v1/jobs/import/project_media/upload_status":
            state.status_reports.append(body)
            self._json(200, {})
        else:
            self._json(404, {"error": "not_found"})

    def do_PUT(self) -> None:
        state = self.server.state
        name = urllib.parse.unquote(self.path.removeprefix("/upload/"))
        data = self.rfile.read(int(self.headers["Content-Length"]))
        state.uploads[name] = data
        state.upload_headers[name] = dict(self.headers.items())
        if state.upload_status_code == 200:
            self._json(200, {})
        else:
            self._json(state.upload_status_code, {"message": "Signature expired"})

    def _api_allowed(self) -> bool:
        state = self.server.state
        if self.headers.get("Authorization") != f"Bearer {GOOD_KEY}":
            self._json(401, {"error": "unauthorized", "message": "Invalid API key"})
            return False
        if state.rate_limit_first > 0:
            state.rate_limit_first -= 1
            self._json(429, {"error": "rate_limited"}, {"Retry-After": state.retry_after})
            return False
        return True

    def _json(self, code: int, body: Any, headers: dict[str, str] | None = None) -> None:
        raw = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(raw)


@contextmanager
def serve_fake_descript() -> Iterator[FakeDescript]:
    state = FakeDescript()
    server = _Server(("127.0.0.1", 0), _Handler)
    server.state = state
    root = f"http://127.0.0.1:{server.server_port}"
    state.base_url = f"{root}/v1/"
    state.upload_base = f"{root}/upload/"
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
    )
    thread.start()
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()
