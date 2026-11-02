import pytest

import bot


def test_same_thread_gets_same_context() -> None:
    assert bot.context_id("C1", "111.1") == bot.context_id("C1", "111.1")


@pytest.mark.parametrize(("channel", "thread_ts"), [("C2", "111.1"), ("C1", "222.2")])
def test_different_thread_gets_different_context(channel: str, thread_ts: str) -> None:
    assert bot.context_id(channel, thread_ts) != bot.context_id("C1", "111.1")


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("<@U123> restart jellyfin", "restart jellyfin"),
        ("hey <@U123>  what's up ", "hey what's up"),
        ("no mention", "no mention"),
    ],
)
def test_strip_mention(text: str, expected: str) -> None:
    assert bot.strip_mention(text) == expected


def test_parse_completed_task_joins_artifact_text() -> None:
    resp = {
        "result": {
            "status": {"state": "completed"},
            "artifacts": [
                {"parts": [{"kind": "text", "text": "a"}, {"kind": "data"}]},
                {"parts": [{"kind": "text", "text": "b"}]},
            ],
        }
    }
    assert bot.parse_result(resp) == bot.Reply(text="a\nb", needs_input=False)


def test_parse_input_required_flags_it() -> None:
    resp = {
        "result": {
            "status": {
                "state": "input-required",
                "message": {"parts": [{"kind": "text", "text": "which node?"}]},
            }
        }
    }
    assert bot.parse_result(resp) == bot.Reply(text="which node?", needs_input=True)


def test_parse_failed_task_raises_with_its_message() -> None:
    resp = {
        "result": {
            "status": {
                "state": "failed",
                "message": {"parts": [{"kind": "text", "text": "model down"}]},
            }
        }
    }
    with pytest.raises(bot.AgentError, match="model down"):
        bot.parse_result(resp)


def test_parse_jsonrpc_error_raises() -> None:
    with pytest.raises(bot.AgentError, match="invalid request"):
        bot.parse_result({"error": {"code": -32600, "message": "invalid request"}})


def test_render_input_required_links_the_ui_session() -> None:
    out = bot.render(
        bot.Reply(text="which node?", needs_input=True),
        ui_chat_url="https://kagent.example/agents/kagent/router-agent/chat",
        ctx="abc",
    )
    assert out[0].startswith("which node?")
    assert "https://kagent.example/agents/kagent/router-agent/chat/abc" in out[0]


def test_render_empty_reply_says_so() -> None:
    assert bot.render(bot.Reply(text="", needs_input=False), "u", "c") == [
        "_(the agent returned no text)_"
    ]


def test_render_splits_long_text_under_slack_limit() -> None:
    text = "x" * (bot.SLACK_MAX + 10)
    chunks = bot.render(bot.Reply(text=text, needs_input=False), "u", "c")
    assert len(chunks) == 2
    assert all(len(c) <= bot.SLACK_MAX for c in chunks)
    assert "".join(chunks) == text


@pytest.mark.parametrize(
    ("user", "allowed"), [("U1", True), ("U2", False), ("", False)]
)
def test_only_allowlisted_users(user: str, allowed: bool) -> None:
    assert bot.is_allowed(user, frozenset({"U1"})) is allowed


def test_build_request_carries_text_and_context() -> None:
    req = bot.build_request("hi", "ctx-1")
    msg = req["params"]["message"]
    assert req["method"] == "message/send"
    assert msg["contextId"] == "ctx-1"
    assert msg["parts"] == [{"kind": "text", "text": "hi"}]
    assert msg["role"] == "user"


def test_parse_response_without_result_raises() -> None:
    with pytest.raises(bot.AgentError, match="no result"):
        bot.parse_result({"jsonrpc": "2.0", "id": "1"})


BOT = "UBOT"


@pytest.mark.parametrize(
    ("event", "expected"),
    [
        # DMs are always answered.
        ({"channel_type": "im", "ts": "1"}, bot.Route.HANDLE),
        # Thread replies in channels need the bot to be in the thread.
        (
            {"channel_type": "channel", "ts": "2", "thread_ts": "1", "text": "hi"},
            bot.Route.IF_BOT_IN_THREAD,
        ),
        (
            {"channel_type": "group", "ts": "2", "thread_ts": "1", "text": "hi"},
            bot.Route.IF_BOT_IN_THREAD,
        ),
        # Top-level channel messages: only via app_mention.
        ({"channel_type": "channel", "ts": "1", "text": "hi"}, bot.Route.IGNORE),
        (
            {"channel_type": "channel", "ts": "1", "thread_ts": "1", "text": "hi"},
            bot.Route.IGNORE,
        ),
        # A mention in a thread also arrives as app_mention; answer once.
        (
            {
                "channel_type": "channel",
                "ts": "2",
                "thread_ts": "1",
                "text": f"<@{BOT}> hi",
            },
            bot.Route.IGNORE,
        ),
        # Edits, joins and bot posts (including our own).
        (
            {"channel_type": "im", "ts": "1", "subtype": "message_changed"},
            bot.Route.IGNORE,
        ),
        ({"channel_type": "im", "ts": "1", "bot_id": "B1"}, bot.Route.IGNORE),
    ],
)
def test_route_message(event: dict[str, str], expected: bot.Route) -> None:
    assert bot.route_message(event, BOT) is expected


@pytest.mark.parametrize(
    ("messages", "expected"),
    [
        ([{"user": "U1"}, {"user": BOT, "bot_id": "B1"}], True),
        ([{"user": "U1"}, {"user": "U2"}], False),
        ([], False),
    ],
)
def test_bot_in_thread(messages: list[dict[str, str]], expected: bool) -> None:
    assert bot.bot_in_thread(messages, BOT) is expected
