# TelegramAgentBot

[简体中文](README_CN.md)

[![Check](https://github.com/Pigbibi/TelegramAgentBot/actions/workflows/check.yml/badge.svg)](https://github.com/Pigbibi/TelegramAgentBot/actions/workflows/check.yml)
[![Secret Scan](https://github.com/Pigbibi/TelegramAgentBot/actions/workflows/secret-scan.yml/badge.svg)](https://github.com/Pigbibi/TelegramAgentBot/actions/workflows/secret-scan.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

Control Codex CLI and Claude Code sessions from Telegram without replacing the
terminal workflow. TelegramAgentBot maps each Telegram forum topic to a tmux
window, forwards messages to the selected agent, and streams public session
output back to Telegram.

tmux remains the source of truth. You can attach to the same session locally at
any time, and a bot restart does not discard the underlying terminal session.

## Highlights

- One Telegram topic maps to one tmux window and one agent session.
- Start a Codex or Claude Code session, select a model, and choose a reasoning
  level from Telegram.
- Send text, voice messages, images, files, and control keys to a live session.
- Receive assistant replies, public progress, tool summaries, command output,
  and interactive prompts.
- Resume tracked sessions and preserve topic bindings across bot restarts.
- Queue Telegram input while an agent is busy, with bounded concurrency for
  small servers.
- Monitor host health and receive cooldown-controlled operator alerts.
- Use the built-in local tmux backend or install the optional socket backend
  for remote agent nodes.
- Dispatch GitHub issues to existing Codex windows with the optional bridge.

## How it works

```text
Telegram forum topic
        │
        ▼
TelegramAgentBot ─── durable bindings and input queue
        │
        ▼
tmux window ─── Codex CLI or Claude Code
        │
        ▼
agent transcript ─── public output back to Telegram
```

TelegramAgentBot controls a real CLI process. It does not create a separate SDK
conversation, and it does not expose private model reasoning.

## Requirements

- Python 3.12 or newer
- [uv](https://docs.astral.sh/uv/) for the recommended install path
- tmux
- Codex CLI or Claude Code, already installed and authenticated for the service
  user
- A Telegram bot with threaded mode enabled
- Linux with systemd user services, or macOS with launchd, for the included
  bootstrap scripts

## Quick start

### 1. Create a Telegram bot

1. Open [@BotFather](https://t.me/BotFather) in Telegram.
2. Create a bot and copy its token.
3. Open the bot settings and enable **Threaded Mode**.
4. Find the numeric Telegram user IDs that may control the bot.

Only IDs listed in `ALLOWED_USERS` can interact with TelegramAgentBot.

### 2. Install the application

Linux or VPS:

```bash
mkdir -p ~/.telegram-agent-bot/app
git clone https://github.com/Pigbibi/TelegramAgentBot.git \
  ~/.telegram-agent-bot/app/TelegramAgentBot
cd ~/.telegram-agent-bot/app/TelegramAgentBot
./scripts/bootstrap-linux.sh
```

macOS:

```bash
git clone https://github.com/Pigbibi/TelegramAgentBot.git
cd TelegramAgentBot
./scripts/bootstrap-macos.sh
```

The bootstrap script installs dependencies, creates the application directory,
installs the session-tracking hook, and writes the platform service definition.
It does not overwrite an existing configuration file.

Keep a Linux service checkout outside directories that agents can browse or
clean, such as `~/Projects`. The Linux bootstrap validates this boundary.

### 3. Configure the bot

Edit `~/.telegram-agent-bot/.env`:

```ini
TELEGRAM_BOT_TOKEN=replace_with_your_bot_token
ALLOWED_USERS=123456789

TELEGRAM_AGENT_BOT_AGENT_TYPE=codex
TELEGRAM_AGENT_BOT_TMUX_SOCKET_NAME=telegram-agent-bot
TELEGRAM_AGENT_BOT_DEFAULT_PROJECTS_PATH=~/Projects
```

For Claude Code as the default agent:

```ini
TELEGRAM_AGENT_BOT_AGENT_TYPE=claude
TELEGRAM_AGENT_BOT_CLAUDE_COMMAND=claude
```

The annotated configuration template is [`.env.example`](.env.example). See
[Configuration](docs/configuration.md) for supported settings and operational
guidance.

### 4. Authenticate the agent CLI

Run the login command as the same OS user that runs TelegramAgentBot:

```bash
codex login
```

For Claude Code, configure its supported authentication method and confirm that
`claude` starts successfully in a normal terminal.

### 5. Start the service

Linux:

```bash
systemctl --user daemon-reload
systemctl --user enable --now io.github.telegramagentbot.service
systemctl --user status io.github.telegramagentbot.service --no-pager
```

On a VPS, enable lingering if the user service must run without an interactive
login:

```bash
sudo loginctl enable-linger "$USER"
```

macOS:

```bash
launchctl bootstrap "gui/$(id -u)" \
  ~/Library/LaunchAgents/io.github.telegramagentbot.plist
launchctl kickstart -k "gui/$(id -u)/io.github.telegramagentbot"
```

See [Deployment](docs/deployment.md) for logs, upgrades, manual installation,
and service troubleshooting.

## Using the bot

1. Create a forum topic in the Telegram chat.
2. Send a message in that topic.
3. Choose a project directory.
4. Select an existing tracked session or create a new one.
5. Choose the agent, model, reasoning level, and Fast mode when offered.
6. Continue sending text or voice messages in the same topic.

The relationship is always:

```text
one topic = one backend target = one active agent session
```

Use `/unbind` to detach Telegram without stopping the tmux window. Use `/kill`
to stop the bound window and remove its binding.

### Telegram commands

| Command | Purpose |
| --- | --- |
| `/start` | Show the welcome message |
| `/history` | Show message history for the current topic |
| `/mode [clean\|trace]` | Choose concise output or public tool trace |
| `/screenshot` | Capture the visible terminal pane |
| `/esc`, `/interrupt` | Send Escape to the agent |
| `/kill` | Stop the bound window and remove the topic binding |
| `/unbind` | Remove the binding but keep the window running |
| `/usage` | Show Codex usage information |
| `/health` | Show a bounded host and AgentBot health snapshot |
| `/agentlogin [name]` | Authenticate the configured default agent |
| `/agentaccount` | List, select, save, or clear agent accounts |
| `/codexlogin`, `/codexaccount` | Manage Codex authentication |
| `/claudelogin`, `/claudeaccount` | Manage Claude Code authentication |
| `/agentcmd`, `/cmd` | Forward an arbitrary agent slash command |

Agent commands such as `/clear`, `/compact`, `/goal`, `/help`, `/memory`, and
`/model` are forwarded to the active CLI session.

## Security model

TelegramAgentBot can type into a terminal on your behalf. Treat it as remote
shell-adjacent software:

- Keep `ALLOWED_USERS` restricted to trusted numeric user IDs.
- Store `.env`, account snapshots, and bridge configuration outside the Git
  checkout with owner-only permissions.
- Do not expose tmux sockets or the optional socket backend directly to the
  public internet.
- Use SSH or a private network for remote node transport.
- Review agent approval and sandbox settings before enabling unattended use.
- Test backup-account switching on the target host before enabling automatic
  account rotation.

To report a vulnerability, follow [SECURITY.md](SECURITY.md). Do not open a
public issue containing credentials, tokens, private prompts, or exploit
details.

## Documentation

- [Documentation index](docs/README.md)
- [Features](docs/features.md)
- [Configuration](docs/configuration.md)
- [Deployment and upgrades](docs/deployment.md)
- [Agent backend plugins](docs/agent_backend_plugins.md)
- [Socket backend](plugins/socket_backend/README.md)
- [GitHub issue bridge](docs/github_codex_bridge.md)
- [VPS cleanup timer](docs/vps_cleanup.md)

## Contributing and support

Bug reports and focused pull requests are welcome. Read
[CONTRIBUTING.md](CONTRIBUTING.md) before submitting a change and use
[SUPPORT.md](SUPPORT.md) to choose the right support channel. Community
participation is governed by [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md).

## License

TelegramAgentBot is available under the [MIT License](LICENSE). Bundled fonts
retain their licenses under `src/telegram_agent_bot/fonts/`.
