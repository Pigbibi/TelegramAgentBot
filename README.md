# TelegramAgentBot

[简体中文](README_CN.md)

Control live Codex CLI and Claude Code sessions from Telegram. Each topic connects to a tmux window, so you can send instructions remotely and attach to the same terminal locally.

The bot forwards public replies, progress and interactive prompts. Session bindings survive bot restarts; the underlying tmux session remains independent of the bot process.

## Requirements

- Python 3.12+, [uv](https://docs.astral.sh/uv/) and tmux.
- Codex CLI or Claude Code installed and authenticated for the service user.
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
| `TELEGRAM_AGENT_BOT_AGENT_TYPE` | `codex` or `claude` |
| `TELEGRAM_AGENT_BOT_DEFAULT_PROJECTS_PATH` | Project directory shown by the bot |
| `TELEGRAM_AGENT_BOT_TMUX_SOCKET_NAME` | Dedicated tmux socket name |

Authenticate the chosen CLI as the same operating-system user, then start the Linux service:

```bash
systemctl --user daemon-reload
systemctl --user enable --now io.github.telegramagentbot.service
```

For macOS startup, Linux lingering, logs and upgrades, follow [Deployment](docs/deployment.md). Keep the service checkout separate from directories managed by agent tasks.

## Use a session

1. Send a message in a Telegram topic.
2. Select a project and an existing session, or create a session.
3. Choose the agent and available model settings.
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
