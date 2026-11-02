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
    assert bot.parse_result(resp) == bot.Reply(
        text="which node?", needs_input=True, pending=None
    )


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


def _confirmation(args: dict[str, object]) -> dict[str, object]:
    """An input-required task paused on kagent's adk_request_confirmation."""
    return {
        "result": {
            "kind": "task",
            "id": "task-1",
            "status": {
                "state": "input-required",
                "message": {
                    "parts": [
                        {
                            "kind": "data",
                            "data": {"name": "adk_request_confirmation", "args": args},
                        }
                    ]
                },
            },
        }
    }


def _ask_user(*questions: dict[str, object]) -> dict[str, object]:
    return {"name": "ask_user", "args": {"questions": list(questions)}, "id": "c1"}


def test_parse_ask_user_shows_questions_and_choices() -> None:
    resp = _confirmation(
        {
            "originalFunctionCall": _ask_user(
                {"question": "Which node?", "choices": ["a", "b"]},
                {"question": "Why?"},
            ),
            "toolConfirmation": {"hint": "Which node?; Why?"},
        }
    )
    reply = bot.parse_result(resp)
    assert reply.pending == bot.Pending(task_id="task-1", questions=2)
    assert "Which node?" in reply.text
    assert "a, b" in reply.text
    assert "Why?" in reply.text


def test_parse_subagent_ask_user_unwraps_hitl_parts() -> None:
    # Shape seen from router-agent -> homelab-agent -> ask_user.
    resp = _confirmation(
        {
            "originalFunctionCall": {
                "name": "kagent__NS__homelab_agent",
                "args": {"request": "front door"},
                "id": "outer",
            },
            "toolConfirmation": {
                "hint": "Remote agent requires approval for tool(s): ask_user",
                "payload": {
                    "hitl_parts": [
                        {
                            "name": "adk_request_confirmation",
                            "originalFunctionCall": _ask_user(
                                {"question": "Need a HA token."}
                            ),
                        }
                    ],
                    "task_id": "sub-task",
                },
            },
        }
    )
    reply = bot.parse_result(resp)
    assert reply.pending == bot.Pending(task_id="task-1", questions=1)
    assert "Need a HA token." in reply.text
    assert "homelab_agent" not in reply.text


def test_parse_tool_approval_names_the_tool() -> None:
    resp = _confirmation(
        {
            "originalFunctionCall": {
                "name": "delete_pod",
                "args": {"name": "jellyfin-0"},
                "id": "c1",
            },
            "toolConfirmation": {"hint": "delete_pod requires approval"},
        }
    )
    reply = bot.parse_result(resp)
    assert reply.pending == bot.Pending(task_id="task-1", questions=0)
    assert "delete_pod" in reply.text
    assert "jellyfin-0" in reply.text


def test_render_pending_asks_for_a_thread_reply_not_the_ui() -> None:
    reply = bot.Reply(text="Which node?", needs_input=True, pending=bot.Pending("t", 1))
    out = bot.render(reply, "https://kagent.example/chat", "abc")
    assert "kagent.example" not in out[-1]
    assert "Reply in this thread" in out[-1]


def _bot_msg(pending: bot.Pending | None) -> dict[str, object]:
    msg: dict[str, object] = {"user": BOT, "bot_id": "B1", "text": "q"}
    if pending:
        msg["metadata"] = bot.pending_metadata(pending)
    return msg


@pytest.mark.parametrize(
    ("messages", "expected"),
    [
        # The prompt is the bot's latest message; the user's answer follows it.
        (
            [{"user": "U1"}, _bot_msg(bot.Pending("t", 2)), {"user": "U1"}],
            bot.Pending("t", 2),
        ),
        # The bot replied again since, so the prompt was answered.
        ([_bot_msg(bot.Pending("t", 2)), {"user": "U1"}, _bot_msg(None)], None),
        ([{"user": "U1"}, _bot_msg(None)], None),
        ([{"user": "U1"}], None),
    ],
)
def test_pending_prompt(
    messages: list[dict[str, object]], expected: bot.Pending | None
) -> None:
    assert bot.pending_prompt(messages, BOT) == expected


def _decision(req: dict[str, object]) -> tuple[dict[str, object], str]:
    msg = req["params"]["message"]  # type: ignore[index]
    data = next(p["data"] for p in msg["parts"] if p["kind"] == "data")
    return data, msg["taskId"]


def test_answer_single_question_keeps_the_whole_reply() -> None:
    req = bot.build_answer("line one\nline two", "ctx-1", bot.Pending("t", 1))
    data, task_id = _decision(req)
    assert task_id == "t"
    assert req["params"]["message"]["contextId"] == "ctx-1"  # type: ignore[index]
    assert data == {
        "decision_type": "approve",
        "ask_user_answers": [{"answer": ["line one\nline two"]}],
    }


def test_answer_several_questions_one_per_line() -> None:
    req = bot.build_answer("a\n\n b ", "c", bot.Pending("t", 2))
    data, _ = _decision(req)
    assert data["ask_user_answers"] == [{"answer": ["a"]}, {"answer": ["b"]}]


def test_answer_line_count_must_match_questions() -> None:
    with pytest.raises(bot.AnswerError, match="2 questions"):
        bot.build_answer("only one", "c", bot.Pending("t", 2))


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("approve", {"decision_type": "approve"}),
        ("Yes", {"decision_type": "approve"}),
        ("reject", {"decision_type": "reject"}),
        (
            "no, too risky",
            {"decision_type": "reject", "rejection_reason": "too risky"},
        ),
    ],
)
def test_answer_approval(text: str, expected: dict[str, str]) -> None:
    data, task_id = _decision(bot.build_answer(text, "c", bot.Pending("t", 0)))
    assert task_id == "t"
    assert data == expected


def test_answer_approval_needs_a_clear_decision() -> None:
    with pytest.raises(bot.AnswerError, match="approve"):
        bot.build_answer("maybe later", "c", bot.Pending("t", 0))
