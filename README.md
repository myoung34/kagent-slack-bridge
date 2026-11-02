# kagent-slack-bridge

Slack bridge for [kagent](https://kagent.dev) agents over A2A. @mention the
bot in a channel, or DM it, and it sends the message to one kagent agent and
replies in the thread. Once it has replied in a thread, later replies in that
thread reach it without a mention.

- **One session per thread.** The A2A `contextId` is derived from the Slack
  channel and thread (`uuid5`), so a thread keeps its conversation across
  restarts with no stored state. It is also the kagent session id, so the
  thread shows in the kagent UI.
- **No timeout.** It waits as long as the agent runs, with a :eyes: reaction
  while it works.
- **Allowlist.** Only users in `ALLOWED_USER_IDS` get an answer.
- **Questions from the agent** (`input-required`, e.g. kagent's `ask_user`)
  are answered in the kagent UI; the bot posts a link to the session.

## Configuration

| Variable | Value |
| --- | --- |
| `KAGENT_A2A_URL` | Agent's A2A endpoint, e.g. `http://kagent-controller.kagent:8083/api/a2a/kagent/router-agent/` |
| `KAGENT_UI_CHAT_URL` | UI chat base, e.g. `https://kagent.example/agents/kagent/router-agent/chat` |
| `ALLOWED_USER_IDS` | Comma- or space-separated Slack member IDs |
| `SLACK_BOT_TOKEN` | Bot User OAuth Token (`xoxb-`) |
| `SLACK_APP_TOKEN` | App-level token with `connections:write` (`xapp-`) |

Create the Slack app from [`slack-app-manifest.yaml`](slack-app-manifest.yaml).
It uses Socket Mode, so the bot needs no ingress.

## Image

`ghcr.io/myoung34/kagent-slack-bridge:<version>`, amd64 and arm64. Pushing a
`vX.Y.Z` tag builds and publishes `X.Y.Z`.

## Development

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt -r requirements-dev.txt
.venv/bin/ruff check . && .venv/bin/ruff format --check .
.venv/bin/pytest -q
```

`docker build .` runs the same checks in its `test` stage.
