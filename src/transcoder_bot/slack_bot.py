"""Talking to Slack: posting and updating messages, and receiving button clicks.

Clicks arrive over Socket Mode (a WebSocket the Mac opens to Slack), so the Mac needs no
public URL, port forwarding or web server.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Collection
from types import TracebackType
from typing import Any

from slack_sdk import WebClient
from slack_sdk.socket_mode import SocketModeClient
from slack_sdk.socket_mode.client import BaseSocketModeClient
from slack_sdk.socket_mode.request import SocketModeRequest
from slack_sdk.socket_mode.response import SocketModeResponse
from slack_sdk.webhook import WebhookClient

from transcoder_bot.slack_messages import Block, Click, Decision, Poll, parse_click, resolve_click

log = logging.getLogger(__name__)

# Friendly explanations for the Slack API errors people are most likely to hit during setup.
ERROR_HINTS = {
    "not_in_channel": "invite the bot to the channel: /invite @transcoder-bot",
    "channel_not_found": "slack.channel must be a channel ID like C0123456789, not its name",
    "invalid_auth": "check slack.bot_token and slack.app_token",
    "not_authed": "check slack.bot_token and slack.app_token",
    "token_revoked": "the app was uninstalled or its token regenerated",
    "missing_scope": "add the scope under OAuth & Permissions, then reinstall the app",
}


def describe_error(error: str) -> str:
    """``"not_in_channel"`` → ``"not_in_channel (invite the bot …)"``."""
    hint = ERROR_HINTS.get(error)
    return f"{error} ({hint})" if hint else error


class SlackBot:
    """Posts to one channel and collects the decision for one open poll at a time."""

    def __init__(
        self,
        *,
        bot_token: str,
        app_token: str,
        channel: str,
        allowed_user_ids: Collection[str] = (),
        web_client: WebClient | None = None,
    ) -> None:
        self.channel = channel
        self._web = web_client or WebClient(token=bot_token)
        self._app_token = app_token
        self._allowed = frozenset(allowed_user_ids)
        self._socket: SocketModeClient | None = None
        self._lock = threading.Lock()
        self._poll: Poll | None = None
        self._decision: Decision | None = None
        self._decided = threading.Event()

    def __enter__(self) -> SlackBot:
        self.connect()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def connect(self) -> None:
        """Open the Socket Mode connection (before posting, so no early click is missed)."""
        socket = SocketModeClient(app_token=self._app_token, web_client=self._web)
        socket.socket_mode_request_listeners.append(self._on_request)
        try:
            socket.connect()
        except BaseException:
            socket.close()  # type: ignore[no-untyped-call]  # slack_sdk lacks annotations
            raise
        self._socket = socket

    def close(self) -> None:
        if self._socket is not None:
            self._socket.close()  # type: ignore[no-untyped-call]  # slack_sdk lacks annotations
            self._socket = None

    def post(
        self, text: str, *, blocks: list[Block] | None = None, thread_ts: str | None = None
    ) -> str:
        response = self._web.chat_postMessage(
            channel=self.channel,
            text=text,
            blocks=blocks,
            thread_ts=thread_ts,
            unfurl_links=False,
            unfurl_media=False,
        )
        return str(response["ts"])

    def update(self, ts: str, text: str, *, blocks: list[Block] | None = None) -> None:
        self._web.chat_update(channel=self.channel, ts=ts, text=text, blocks=blocks)

    def open_poll(self, poll: Poll) -> None:
        with self._lock:
            self._poll = poll
            self._decision = None
            self._decided.clear()

    def wait_for_decision(self, timeout: float) -> Decision | None:
        """Block until someone decides or ``timeout`` seconds pass; then close the poll."""
        self._decided.wait(timeout)
        with self._lock:
            decision = self._decision
            self._poll = None
        return decision

    def handle_click(self, click: Click) -> str | None:
        """Record the first valid decision. Returns a message for the clicker if turned away."""
        with self._lock:
            if self._poll is None or self._decision is not None:
                return "That poll has already closed."
            decision, rejection = resolve_click(click, self._poll, self._allowed)
            if decision is not None:
                self._decision = decision
                self._decided.set()
            return rejection

    def _on_request(self, client: BaseSocketModeClient, request: SocketModeRequest) -> None:
        # Acknowledge within Slack's 3-second window, whatever the request is.
        client.send_socket_mode_response(SocketModeResponse(envelope_id=request.envelope_id))
        if request.type != "interactive":
            return
        click = parse_click(request.payload)
        if click is None:
            return
        rejection = self.handle_click(click)
        if rejection and click.response_url:
            self._reply_privately(click.response_url, rejection)

    def _reply_privately(self, response_url: str, text: str) -> None:
        try:
            WebhookClient(response_url).send(
                text=text, response_type="ephemeral", replace_original=False
            )
        except Exception:
            log.warning("Couldn't send a private reply in Slack", exc_info=True)


def check_tokens(bot_token: str, app_token: str) -> dict[str, Any]:
    """Verify both tokens without opening a WebSocket. Raises SlackApiError if either is bad."""
    auth = WebClient(token=bot_token).auth_test()
    WebClient().apps_connections_open(app_token=app_token)
    return {"user": auth.get("user"), "team": auth.get("team"), "url": auth.get("url")}
