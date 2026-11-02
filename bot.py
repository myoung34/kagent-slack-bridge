"""Slack <-> kagent bridge: @mention (or DM) the bot, it asks one A2A agent.

Once the bot has replied in a thread, later replies there need no mention.
When the agent pauses on kagent's ask_user or a tool approval, the bot posts
the question and the next thread reply answers it.

Each Slack thread maps to one kagent conversation through a context id
derived from the thread, so threads keep their context across restarts
without any stored state.
"""

import json
import logging
import os
import re
import urllib.request
import uuid
from dataclasses import dataclass
from enum import Enum, auto

from slack_bolt import App, BoltContext
from slack_bolt.adapter.socket_mode import SocketModeHandler
from slack_sdk import WebClient

logger = logging.getLogger(__name__)

# chat.postMessage caps markdown_text at 12,000 characters.
SLACK_MAX = 12_000
_MENTION = re.compile(r"<@[A-Z0-9]+>")
_FAILED = {"failed", "rejected", "canceled"}
_CONFIRMATION = "adk_request_confirmation"
_ASK_USER = "ask_user"
# Slack message metadata event_type marking a bot post that awaits an answer.
_PENDING_EVENT = "kagent_pending"
_APPROVE = {"approve", "approved", "yes", "y", "ok"}
_REJECT = {"reject", "rejected", "deny", "no", "n"}
_DECISION = re.compile(r"\s*(\w+)[\s,.:;!-]*(.*)", re.DOTALL)


class AgentError(Exception):
    """The agent call failed or the task ended unsuccessfully."""


class AnswerError(ValueError):
    """A thread reply does not fit the pending prompt; the message says why."""


@dataclass(frozen=True, slots=True)
class Pending:
    """A task paused on ask_user (questions > 0) or a tool approval (0)."""

    task_id: str
    questions: int


@dataclass(frozen=True, slots=True)
class Reply:
    text: str
    needs_input: bool
    pending: Pending | None = None


class Route(Enum):
    HANDLE = auto()
    IF_BOT_IN_THREAD = auto()
    IGNORE = auto()


def route_message(event: dict[str, str], bot_user_id: str) -> Route:
    """Decide what to do with a `message` event (mentions arrive separately)."""
    if "subtype" in event or "bot_id" in event:
        return Route.IGNORE
    if event.get("channel_type") == "im":
        return Route.HANDLE
    thread_ts = event.get("thread_ts")
    if not thread_ts or thread_ts == event.get("ts"):
        return Route.IGNORE
    if f"<@{bot_user_id}>" in event.get("text", ""):
        return Route.IGNORE
    return Route.IF_BOT_IN_THREAD


def bot_in_thread(messages: list[dict[str, str]], bot_user_id: str) -> bool:
    return any(m.get("user") == bot_user_id for m in messages)


def context_id(channel: str, thread_ts: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"slack:{channel}:{thread_ts}"))


def strip_mention(text: str) -> str:
    return " ".join(_MENTION.sub(" ", text).split())


def is_allowed(user: str, allowed: frozenset[str]) -> bool:
    return user in allowed


def _send(
    parts: list[dict[str, object]], ctx: str, task_id: str | None = None
) -> dict[str, object]:
    message: dict[str, object] = {
        "role": "user",
        "messageId": str(uuid.uuid4()),
        "contextId": ctx,
        "parts": parts,
    }
    if task_id:
        message["taskId"] = task_id
    return {
        "jsonrpc": "2.0",
        "id": str(uuid.uuid4()),
        "method": "message/send",
        "params": {"message": message},
    }


def build_request(text: str, ctx: str) -> dict[str, object]:
    return _send([{"kind": "text", "text": text}], ctx)


def _decision(text: str, pending: Pending) -> dict[str, object]:
    if pending.questions == 1:
        return {"decision_type": "approve", "ask_user_answers": [{"answer": [text]}]}
    if pending.questions > 1:
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        if len(lines) != pending.questions:
            raise AnswerError(
                f"Answer the {pending.questions} questions one per line, in order."
            )
        answers = [{"answer": [line]} for line in lines]
        return {"decision_type": "approve", "ask_user_answers": answers}
    m = _DECISION.match(text)
    word, reason = (m.group(1).lower(), m.group(2).strip()) if m else ("", "")
    if word in _APPROVE:
        return {"decision_type": "approve"}
    if word in _REJECT:
        data: dict[str, object] = {"decision_type": "reject"}
        if reason:
            data["rejection_reason"] = reason
        return data
    raise AnswerError("Reply `approve`, or `reject` with an optional reason.")


def build_answer(text: str, ctx: str, pending: Pending) -> dict[str, object]:
    """Resume a paused task the way the kagent UI does."""
    parts = [
        {"kind": "data", "data": _decision(text, pending), "metadata": {}},
        {"kind": "text", "text": text},
    ]
    return _send(parts, ctx, pending.task_id)


def _text(parts: list[dict[str, object]]) -> str:
    return "\n".join(str(p["text"]) for p in parts if p.get("kind") == "text")


def _calls(data: dict[str, object]) -> list[dict[str, object]]:
    """Tool calls awaiting the user, unwrapping a subagent's own requests."""
    args = data.get("args") or {}
    payload = (args.get("toolConfirmation") or {}).get("payload") or {}
    inner = [p.get("originalFunctionCall") or {} for p in payload.get("hitl_parts", [])]
    return inner or [args.get("originalFunctionCall") or {}]


def _prompt(parts: list[dict[str, object]], task_id: str) -> Reply | None:
    """Describe an adk_request_confirmation part, if the task has one."""
    data = next(
        (
            p["data"]
            for p in parts
            if p.get("kind") == "data" and p["data"].get("name") == _CONFIRMATION
        ),
        None,
    )
    if data is None or not task_id:
        return None
    calls = _calls(data)
    questions = [
        q
        for c in calls
        if c.get("name") == _ASK_USER
        for q in (c.get("args") or {}).get("questions", [])
    ]
    if questions:
        lines = []
        for i, q in enumerate(questions, 1):
            choices = q.get("choices") or []
            suffix = f" _({', '.join(choices)})_" if choices else ""
            lines.append(f"{i}. {q.get('question', '')}{suffix}")
        return Reply(
            text="\n".join(lines),
            needs_input=True,
            pending=Pending(task_id, len(questions)),
        )
    hint = (data.get("args") or {}).get("toolConfirmation", {}).get("hint", "")
    tools = [
        f"`{c.get('name', '?')}` `{json.dumps(c.get('args', {}))[:500]}`" for c in calls
    ]
    text = "\n".join([hint, "Approve this?", *tools]).strip()
    return Reply(text=text, needs_input=True, pending=Pending(task_id, 0))


def parse_result(resp: dict[str, object]) -> Reply:
    if "error" in resp:
        raise AgentError(str(resp["error"]))
    result = resp.get("result")
    if not isinstance(result, dict):
        raise AgentError(f"no result in response: {resp}")
    if result.get("kind") == "message":
        return Reply(text=_text(result.get("parts", [])), needs_input=False)
    status = result.get("status", {})
    state = status.get("state")
    status_parts = (status.get("message") or {}).get("parts", [])
    status_text = _text(status_parts)
    if state in _FAILED:
        raise AgentError(status_text or f"task {state}")
    if state == "input-required":
        prompt = _prompt(status_parts, str(result.get("id", "")))
        return prompt or Reply(text=status_text, needs_input=True)
    artifacts = result.get("artifacts", [])
    text = "\n".join(_text(a.get("parts", [])) for a in artifacts) or status_text
    return Reply(text=text, needs_input=False)


def render(reply: Reply, ui_chat_url: str, ctx: str) -> list[str]:
    text = reply.text
    if reply.pending:
        text += "\n\n_Reply in this thread to answer._"
    elif reply.needs_input:
        text += (
            f"\n\nThe agent is waiting on you. Answer in the kagent UI: "
            f"{ui_chat_url}/{ctx}"
        )
    if not text.strip():
        return ["_(the agent returned no text)_"]
    return [text[i : i + SLACK_MAX] for i in range(0, len(text), SLACK_MAX)]


def pending_metadata(pending: Pending) -> dict[str, object]:
    """Slack message metadata that lets a later thread reply find the task."""
    return {
        "event_type": _PENDING_EVENT,
        "event_payload": {"task_id": pending.task_id, "questions": pending.questions},
    }


def pending_prompt(
    messages: list[dict[str, object]], bot_user_id: str
) -> Pending | None:
    """The prompt awaiting an answer: carried by the bot's latest thread message."""
    last = next((m for m in reversed(messages) if m.get("user") == bot_user_id), None)
    meta = (last or {}).get("metadata") or {}
    if meta.get("event_type") != _PENDING_EVENT:
        return None
    payload = meta.get("event_payload") or {}
    return Pending(str(payload["task_id"]), int(payload["questions"]))


def ask(a2a_url: str, request: dict[str, object]) -> Reply:
    body = json.dumps(request).encode()
    req = urllib.request.Request(a2a_url, body, {"Content-Type": "application/json"})
    # No timeout: homelab-agent turns can legitimately run for a long time.
    with urllib.request.urlopen(req) as r:
        return parse_result(json.load(r))


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    a2a_url = os.environ["KAGENT_A2A_URL"]
    ui_chat_url = os.environ["KAGENT_UI_CHAT_URL"].rstrip("/")
    allowed = frozenset(os.environ["ALLOWED_USER_IDS"].replace(",", " ").split())
    app = App(token=os.environ["SLACK_BOT_TOKEN"])

    def handle(event: dict[str, str], client: WebClient, bot_user_id: str) -> None:
        channel, ts = event["channel"], event["ts"]
        thread_ts = event.get("thread_ts") or ts
        if not is_allowed(event.get("user", ""), allowed):
            logger.warning("ignoring user %s", event.get("user"))
            return
        text = strip_mention(event.get("text", ""))
        if not text:
            return
        ctx = context_id(channel, thread_ts)
        pending = None
        if thread_ts != ts:
            replies = client.conversations_replies(
                channel=channel, ts=thread_ts, include_all_metadata=True
            )
            pending = pending_prompt(replies.get("messages", []), bot_user_id)
        client.reactions_add(channel=channel, timestamp=ts, name="eyes")
        metadata = None
        try:
            if pending:
                request = build_answer(text, ctx, pending)
            else:
                request = build_request(text, ctx)
            reply = ask(a2a_url, request)
            chunks = render(reply, ui_chat_url, ctx)
            if reply.pending:
                metadata = pending_metadata(reply.pending)
        except AnswerError as err:
            # Keep the prompt open: repost it so it stays the bot's latest message.
            chunks = [str(err)]
            metadata = pending_metadata(pending) if pending else None
        except Exception as err:
            logger.exception("agent call failed for context %s", ctx)
            chunks = [f"Agent call failed: `{err}`"]
        for i, chunk in enumerate(chunks, 1):
            client.chat_postMessage(
                channel=channel,
                thread_ts=thread_ts,
                markdown_text=chunk,
                metadata=metadata if i == len(chunks) else None,
            )
        client.reactions_remove(channel=channel, timestamp=ts, name="eyes")

    @app.event("app_mention")
    def on_mention(
        event: dict[str, str], client: WebClient, context: BoltContext
    ) -> None:
        handle(event, client, context.bot_user_id or "")

    @app.event("message")
    def on_message(
        event: dict[str, str], client: WebClient, context: BoltContext
    ) -> None:
        bot_user_id = context.bot_user_id or ""
        route = route_message(event, bot_user_id)
        if route is Route.IF_BOT_IN_THREAD:
            replies = client.conversations_replies(
                channel=event["channel"], ts=event["thread_ts"]
            )
            if not bot_in_thread(replies.get("messages", []), bot_user_id):
                return
            route = Route.HANDLE
        if route is Route.HANDLE:
            handle(event, client, bot_user_id)

    SocketModeHandler(app, os.environ["SLACK_APP_TOKEN"]).start()


if __name__ == "__main__":
    main()
