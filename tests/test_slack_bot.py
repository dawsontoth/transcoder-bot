import threading
from typing import Any

from slack_sdk.socket_mode.request import SocketModeRequest

from tests.test_slack_messages import make_poll, payload
from transcoder_bot.slack_bot import SlackBot
from transcoder_bot.slack_messages import PICK_ACTION_PREFIX, Click


class FakeWebClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def chat_postMessage(self, **kwargs: Any) -> dict[str, str]:  # noqa: N802 (slack_sdk name)
        self.calls.append(("chat_postMessage", kwargs))
        return {"ts": "111.222"}

    def chat_update(self, **kwargs: Any) -> dict[str, str]:
        self.calls.append(("chat_update", kwargs))
        return {"ts": kwargs["ts"]}


class FakeSocket:
    def __init__(self) -> None:
        self.acks: list[str] = []

    def send_socket_mode_response(self, response: Any) -> None:
        self.acks.append(response.envelope_id)


def make_bot(**kwargs: Any) -> tuple[SlackBot, FakeWebClient]:
    web = FakeWebClient()
    bot = SlackBot(
        bot_token="xoxb-1",
        app_token="xapp-1",
        channel="C1",
        web_client=web,  # type: ignore[arg-type]
        **kwargs,
    )
    return bot, web


def test_post_and_update_use_the_channel():
    bot, web = make_bot()

    ts = bot.post("hello", thread_ts="1.0")
    bot.update(ts, "edited", blocks=[])

    assert ts == "111.222"
    assert web.calls[0][1]["channel"] == "C1"
    assert web.calls[0][1]["thread_ts"] == "1.0"
    assert web.calls[1] == (
        "chat_update",
        {"channel": "C1", "ts": "111.222", "text": "edited", "blocks": []},
    )


def test_first_valid_click_decides():
    bot, _ = make_bot()
    poll = make_poll("a.mov", "b.mov")
    bot.open_poll(poll)

    assert bot.handle_click(Click(poll.id, "U1", "b.mov")) is None
    assert bot.handle_click(Click(poll.id, "U2", "a.mov")) == "That poll has already closed."

    decision = bot.wait_for_decision(timeout=0)
    assert decision is not None
    assert decision.user_id == "U1"
    assert decision.option is poll.options[1]


def test_clicks_before_or_after_the_poll_are_turned_away():
    bot, _ = make_bot()
    poll = make_poll("a.mov")

    assert bot.handle_click(Click(poll.id, "U1", "a.mov")) == "That poll has already closed."
    bot.open_poll(poll)
    assert bot.wait_for_decision(timeout=0) is None  # timed out
    assert bot.handle_click(Click(poll.id, "U1", "a.mov")) == "That poll has already closed."


def test_only_allowed_users_can_decide():
    bot, _ = make_bot(allowed_user_ids=["U1"])
    poll = make_poll("a.mov")
    bot.open_poll(poll)

    rejection = bot.handle_click(Click(poll.id, "U9", "a.mov"))

    assert rejection is not None
    assert "not one of the people" in rejection
    assert bot.wait_for_decision(timeout=0) is None


def test_wait_returns_as_soon_as_someone_clicks():
    bot, _ = make_bot()
    poll = make_poll("a.mov")
    bot.open_poll(poll)
    timer = threading.Timer(0.05, bot.handle_click, args=[Click(poll.id, "U1", None)])
    timer.start()

    decision = bot.wait_for_decision(timeout=5)

    assert decision is not None
    assert decision.option is None


def test_socket_requests_are_acknowledged_and_handled(monkeypatch):
    bot, _ = make_bot()
    poll = make_poll("a.mov")
    bot.open_poll(poll)
    socket = FakeSocket()
    private_replies: list[str] = []
    monkeypatch.setattr(bot, "_reply_privately", lambda url, text: private_replies.append(text))

    other = SocketModeRequest(type="events_api", envelope_id="e0", payload={})
    wrong_poll = SocketModeRequest(
        type="interactive",
        envelope_id="e1",
        payload=payload(f"{PICK_ACTION_PREFIX}0", '{"poll": "old", "file": "a.mov"}'),
    )
    pick = SocketModeRequest(
        type="interactive",
        envelope_id="e2",
        payload=payload(f"{PICK_ACTION_PREFIX}0", '{"poll": "poll1", "file": "a.mov"}'),
    )
    for request in (other, wrong_poll, pick):
        bot._on_request(socket, request)  # type: ignore[arg-type]

    assert socket.acks == ["e0", "e1", "e2"]
    assert private_replies == ["That poll has already closed."]
    decision = bot.wait_for_decision(timeout=0)
    assert decision is not None
    assert decision.option is poll.options[0]
