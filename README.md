# TelegramAgentBot

[简体中文](README_CN.md)

TelegramAgentBot connects Telegram forum topics to agent sessions running in tmux. You can start a project, resume a session, send prompts and files, answer interactive questions, and follow the agent's public output from Telegram. The same session remains available in the terminal.

Supported CLIs: **Codex**, **Claude Code** (provider API or official login), and **Cursor Agent**. One bot installation can offer all three when their CLIs are installed for the service user.

## Requirements

- Python 3.12+, [uv](https://docs.astral.sh/uv/), and tmux
- At least one supported agent CLI, installed and authenticated as the service user
- A Telegram bot added to a supergroup with Topics enabled
- Numeric Telegram user IDs for `ALLOWED_USERS`

Linux systemd and macOS launchd service files are included. The bot controls a real terminal, so give access only to people who may use the configured projects and agent credentials.

## Install

On Linux:

```bash
git clone https://github.com/Pigbibi/TelegramAgentBot.git \
  ~/.telegram-agent-bot/app/TelegramAgentBot
cd ~/.telegram-agent-bot/app/TelegramAgentBot
./scripts/bootstrap-linux.sh
```

On macOS, clone the repository and run `./scripts/bootstrap-macos.sh`. The bootstrap script installs dependencies and the session hook, then creates a service definition. It keeps an existing configuration file.

Edit `~/.telegram-agent-bot/.env` using [`.env.example`](.env.example). Set at least:

| Variable | Value |
| --- | --- |
| `TELEGRAM_BOT_TOKEN` | Token from BotFather |
| `ALLOWED_USERS` | Comma-separated numeric IDs of trusted operators |
| `TELEGRAM_AGENT_BOT_AGENT_TYPE` | Default CLI: `codex`, `claude`, `claudeofficial`, or `cursor` |
| `TELEGRAM_AGENT_BOT_DEFAULT_PROJECTS_PATH` | Directory shown in the project picker |

Start the Linux service:

```bash
systemctl --user daemon-reload
systemctl --user enable --now io.github.telegramagentbot.service
```

See [Deployment](docs/deployment.md) for macOS startup, Linux lingering, upgrades, and logs. Keep the service checkout outside directories agents edit.

## Choose an agent

| Agent | Login and configuration | Input while a turn is running |
| --- | --- | --- |
| Codex | Authenticate the `codex` CLI | Guide the turn or queue the next one in the CLI |
| Claude Code API (`claude`) | Configure the owner-only `claude.env` for a compatible provider | Guide the turn; later input waits in AgentBot's durable queue |
| Claude Code official (`claudeofficial`) | Use the `claude` CLI's own login, without `claude.env` | Guide the turn; later input waits in AgentBot's durable queue |
| Cursor Agent (`cursor`) | Run `agent login` as the service user | Guide the turn or queue the next one in the CLI |

Choose the agent and supported model settings when creating a topic session. Cursor credentials stay in Cursor's CLI storage; AgentBot account snapshots are available for Codex and Claude Code. See [Configuration](docs/configuration.md) for commands, model selection, permissions, and project roots.

## Use a topic

1. Send a message in the Telegram supergroup, then select a project and a new or existing session.
2. Choose the agent and its available settings.
3. Continue in the same topic with text, voice, images, or files. An attachment sent as the first message is kept until the session is ready.

Useful commands:

| Command | Action |
| --- | --- |
| `/steer <message>` | Guide the active turn |
| `/queue <message>` | Queue input for a later turn |
| `/interrupt [message]` | Interrupt, optionally with replacement input |
| `/history` | Show topic history |
| `/health` | Show bot and host health |
| `/unbind` | Detach the topic without stopping its terminal |
| `/kill` | Stop the bound window and remove the binding |

The bot preserves topic bindings and pending input across service restarts. An idle tmux window may be paused and resumed when the next message arrives. If the CLI receives input but the transcript does not confirm it, AgentBot does **not** send it again automatically: first check the agent session, then resend only if it did not receive the message. [Features](docs/features.md) describes the full command and message behavior.

## Operate safely

- Keep `.env`, account files, and `$TELEGRAM_AGENT_BOT_DIR` readable only by the service user.
- Use separate bot tokens and deployments when operators need separate project roots or CLI credentials.
- Keep tmux and optional backend sockets behind SSH or a private network.
- Check active tasks and queued input before upgrading or restarting the service.

Inspect routing without changing sessions:

```bash
telegram-agent-bot doctor --json
```

A successful doctor check describes routing state; it does not confirm that an agent task finished. For logs and recovery steps, see [Deployment](docs/deployment.md#troubleshooting).

## Documentation and development

- [Documentation index](docs/README.md) · [Features](docs/features.md) · [Configuration](docs/configuration.md) · [Deployment](docs/deployment.md)
- [Backend plugins](docs/agent_backend_plugins.md) · [Socket backend](plugins/socket_backend/README.md)
- [GitHub issue bridge](docs/github_codex_bridge.md) · [VPS cleanup timer](docs/vps_cleanup.md)

To work on the code, run `uv sync --extra dev` and follow [Contributing](CONTRIBUTING.md). For help or vulnerability reports, see [Support](SUPPORT.md) and [Security](SECURITY.md).

Licensed under [MIT](LICENSE). Bundled fonts retain their licenses in `src/telegram_agent_bot/fonts/`.
