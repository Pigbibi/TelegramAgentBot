# TelegramAgentBot

[简体中文](README_CN.md)

Control live Codex CLI, Claude Code API, Claude Code Official Subscription, and Cursor Agent sessions from Telegram. Each topic connects to a tmux window, so remote messages, native active-turn input, and local terminal access share one session.

The bot forwards public replies, progress and interactive prompts. Session bindings survive bot restarts; the underlying tmux session remains independent of the bot process.

## Requirements

- Python 3.12+, [uv](https://docs.astral.sh/uv/) and tmux.
- Codex CLI, Claude Code, or Cursor Agent CLI installed and authenticated for the service user.
- A Telegram bot with threaded mode enabled and a restricted `ALLOWED_USERS` list.
- Linux/systemd or macOS/launchd for the supplied service setup.

## Quick start

On Linux or a VPS:

```bash
git clone https://github.com/Pigbibi/TelegramAgentBot.git \
  ~/.telegram-agent-bot/app/TelegramAgentBot
cd ~/.telegram-agent-bot/app/TelegramAgentBot
./scripts/bootstrap-linux.sh
```

On macOS, clone the repository and run `./scripts/bootstrap-macos.sh`. The bootstrap installs dependencies, hooks and a service definition while preserving an existing configuration.

Edit `~/.telegram-agent-bot/.env` using the [configuration template](.env.example):

| Setting | Purpose |
| --- | --- |
| `TELEGRAM_BOT_TOKEN` | Bot credential from BotFather |
| `ALLOWED_USERS` | Trusted numeric Telegram user IDs |
| `TELEGRAM_AGENT_BOT_AGENT_TYPE` | `codex`, `claude`, `claudeofficial`, or `cursor` |
| `TELEGRAM_AGENT_BOT_DEFAULT_PROJECTS_PATH` | Project directory shown by the bot |
| `TELEGRAM_AGENT_BOT_TMUX_SOCKET_NAME` | Dedicated tmux socket name |

Authenticate the chosen CLI as the same operating-system user, then start the Linux service:

```bash
systemctl --user daemon-reload
systemctl --user enable --now io.github.telegramagentbot.service
```

For Cursor Agent, install and authenticate Cursor's CLI as the service user, then set:

```ini
TELEGRAM_AGENT_BOT_AGENT_TYPE=cursor
TELEGRAM_AGENT_BOT_CURSOR_COMMAND=agent
# Optional model picker; otherwise Cursor uses its CLI default.
# TELEGRAM_AGENT_BOT_CURSOR_MODELS=gpt-5,sonnet-4
```

Cursor uses the same tmux topic routing and terminal output capture. Its account
storage is not copied into AgentBot account snapshots. Authenticate it first in
the service user's interactive terminal with `agent login`.

Claude has two intentionally separate modes:

- `claude` loads the owner-only `claude.env` provider configuration, such as a
  DeepSeek-compatible endpoint.
- `claudeofficial` runs the same `claude` CLI without that environment file and
  uses Claude Code's own subscription/API login and default model selection.

Choose the mode per Telegram topic when creating a session. The picker keeps
provider-specific controls visible only when that CLI supports them. Codex and
Cursor support native active-turn steering and next-turn Tab queueing; Claude
uses the durable AgentBot queue for ordinary next-turn input.

For macOS startup, Linux lingering, logs and upgrades, follow [Deployment](docs/deployment.md). Keep the service checkout separate from directories managed by agent tasks.

## Use a session

1. Send text, an image, or a file in a Telegram topic.
2. Select a project and an existing session, or create a session. A first image or file is retained and sent automatically once the session is ready.
3. Choose the agent and only the model/settings supported by that runtime.
4. Send text, voice, images or files in the same topic.

| Command | Action |
| --- | --- |
| `/steer <message>` | Guide the active turn |
| `/queue <message>` | Send input for a later turn |
| `/interrupt [message]` | Interrupt, optionally with replacement input |
| `/esc` | Send Escape and discard unsent bot input |
| `/history` | Show topic history |
| `/health` | Inspect host and bot health |
| `/unbind` | Detach the topic while keeping its terminal |
| `/kill` | Stop the bound window and remove the binding |

Use one Telegram chat per bot state directory. Topic IDs are scoped by Telegram chat; the bot refuses conflicting cross-chat bindings. See [Features](docs/features.md) for all commands, authentication controls and queue behavior.

### Shared Telegram groups

One deployment can serve multiple operators. Add the bot to a Telegram
supergroup with Topics enabled, and add each operator's numeric Telegram user ID
to `ALLOWED_USERS`. Allowed users share the same group topic binding and durable
input queue; private chats remain isolated per user. The bot's service user,
filesystem access, CLI credentials, and project roots are shared by everyone on
that deployment, so only add trusted operators. A separate bot token and
deployment (often from a fork or private clone) is appropriate for independent
credentials or project roots.

## Operations and security

The bot can control a real terminal. Restrict allowed users, protect its `.env` and state directory, and review the agent's permission settings. Keep tmux and optional backend sockets behind SSH or a private network.

Check routing without restarting sessions:

```bash
telegram-agent-bot doctor --json
```

Before upgrading or restarting the bot, inspect active tasks and queued input. A green health check does not establish that an agent task completed.

## Documentation

- [Configuration](docs/configuration.md) · [Deployment](docs/deployment.md)
- [Features and commands](docs/features.md) · [Documentation index](docs/README.md)
- [Backend plugins](docs/agent_backend_plugins.md) · [Socket backend](plugins/socket_backend/README.md)
- [GitHub issue bridge](docs/github_codex_bridge.md) · [VPS cleanup](docs/vps_cleanup.md)

For development, run `uv sync --dev`, then the checks in [CONTRIBUTING.md](CONTRIBUTING.md). Bundled fonts retain their own licenses in `src/telegram_agent_bot/fonts/`.

## Support and contributing

[Support](SUPPORT.md) · [Contributing](CONTRIBUTING.md) · [Security](SECURITY.md) · [Code of conduct](CODE_OF_CONDUCT.md)

## License

[MIT](LICENSE).
