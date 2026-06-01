# TelegramCodexBot

[中文文档](README_CN.md)

> TelegramCodexBot controls live Codex sessions over Telegram.
> The CLI/package name is `telegram-codex-bot`.

Control Codex sessions remotely through Telegram while keeping tmux as the source of truth. This lets you monitor, answer, interrupt, resume, and clean up real terminal sessions from your phone without switching to a separate SDK session.

## What it does

TelegramCodexBot is a Telegram controller for live Codex sessions:

- `codex` is the default command for new tmux windows
- transcript parsing and monitoring target modern Codex JSONL output under `~/.codex`
- Telegram delivery and topic isolation are hardened for long-running Codex sessions
- tmux stays the source of truth, so you can return to the same terminal session on desktop
- the default backend is local tmux, with an optional plugin interface for center-bot / remote agent-node deployments
- GitHub bridge support can inject structured tasks from issues into Codex tmux sessions

## Features

- **Topic-based sessions** — each Telegram topic maps 1:1 to a tmux window and Codex session
- **Real-time notifications** — assistant replies, thinking, tool calls, tool results, and local command output can be forwarded to Telegram
- **Interactive UI support** — navigate AskUserQuestion, ExitPlanMode, and permission prompts from inline keyboards
- **Voice message transcription** — voice messages can be transcribed with OpenAI and forwarded as text
- **Resume existing sessions** — choose an existing Codex session in a directory and continue from there
- **Closed-session hiding** — sessions closed through topic deletion or cleanup are hidden from the resume picker by default, without deleting their transcripts
- **Topic cleanup** — stale topics, stale tmux windows, and dead bindings are cleaned up more safely
- **Usage/auth recovery** — usage-limit and login failures are reported in Telegram; optional account failover can be enabled when you have saved backup accounts
- **Persistent state** — thread bindings, display names, offsets, and monitor state survive restarts
- **Pluggable agent backend** — local tmux is the default, while advanced users can load a backend plugin for distributed center-bot / agent-node setups
- **GitHub bridge** — optional `telegram-codex-bridge` CLI can poll GitHub issues and inject structured tasks into Codex tmux sessions

## Prerequisites

- **tmux** installed and available in PATH
- **Codex CLI** installed and working locally
- A **Telegram bot** with threaded/forum mode enabled

## Installation

### Option 1: install from GitHub

```bash
# with uv
uv tool install git+https://github.com/Pigbibi/TelegramCodexBot.git

# or with pipx
pipx install git+https://github.com/Pigbibi/TelegramCodexBot.git
```

### Option 2: install from source

```bash
git clone https://github.com/Pigbibi/TelegramCodexBot.git
cd TelegramCodexBot
uv sync
```

## Quick deploy on macOS

For a new Mac or a fresh local setup:

```bash
git clone https://github.com/Pigbibi/TelegramCodexBot.git
cd TelegramCodexBot
chmod +x scripts/bootstrap-macos.sh
./scripts/bootstrap-macos.sh
```

The script does the following:

- run `uv sync`
- create `~/.telegram-codex-bot/.env` from `.env.example` if missing
- install `telegram-codex-bot hook --install` into the active Codex home
- generate a reusable `~/.telegram-codex-bot/bin/telegram-codex-bot-launch`
- generate a LaunchAgent plist for macOS

Required local setup after the script runs:

1. `TELEGRAM_BOT_TOKEN`
2. `ALLOWED_USERS`
3. optional `OPENAI_API_KEY` if you want voice transcription
4. run `codex login`

This project does not use a separate `GPT_SUBSCRIPTION=` env var.
It reuses the local Codex login state:

```bash
codex login
```

If you use multiple Codex accounts or need to refresh login while away from the server, use Telegram commands:

```text
/codexlogin          # refresh the service user's default Codex login
/codexlogin backup   # login and save a named backup account
/codexaccount list
/codexaccount use backup
```

If `~/.telegram-codex-bot/.env` still contains placeholder values, the script will write the
launchd files but will not start the service. After editing `.env`, start it
manually:

```bash
launchctl bootstrap "gui/$(id -u)" ~/Library/LaunchAgents/io.github.telegramcodexbot.plist
launchctl kickstart -k "gui/$(id -u)/io.github.telegramcodexbot"
```

Check status:

```bash
launchctl print "gui/$(id -u)/io.github.telegramcodexbot" | sed -n '1,40p'
tail -n 50 ~/.telegram-codex-bot/logs/telegram-codex-bot.err.log
```

## Quick deploy on Linux / VPS

For a Linux workstation or a VPS with systemd:

```bash
git clone https://github.com/Pigbibi/TelegramCodexBot.git
cd TelegramCodexBot
chmod +x scripts/bootstrap-linux.sh
./scripts/bootstrap-linux.sh
```

The Linux helper:

- runs `uv sync`
- creates `~/.telegram-codex-bot/.env` from `.env.example` if needed
- installs `telegram-codex-bot hook --install`
- writes `~/.telegram-codex-bot/bin/telegram-codex-bot-launch`
- writes a user service at `~/.config/systemd/user/io.github.telegramcodexbot.service`

After that:

1. edit `~/.telegram-codex-bot/.env`
2. run `codex login`
3. start the service if it was not auto-started:

```bash
systemctl --user daemon-reload
systemctl --user enable --now io.github.telegramcodexbot.service
```

On a VPS, if you want the service to keep running after reboot without an
interactive login session:

```bash
sudo loginctl enable-linger "$USER"
```

Check status:

```bash
systemctl --user status io.github.telegramcodexbot.service --no-pager
tail -n 50 ~/.telegram-codex-bot/logs/telegram-codex-bot.err.log
```

## Configuration

### 1. Create a Telegram bot

1. Talk to [@BotFather](https://t.me/BotFather)
2. Create a bot and get the token
3. Open the bot settings mini app
4. Enable **Threaded Mode**

### 2. Create `~/.telegram-codex-bot/.env`

```ini
TELEGRAM_BOT_TOKEN=your_bot_token_here
ALLOWED_USERS=your_telegram_user_id
TELEGRAM_CODEX_BOT_CODEX_COMMAND=codex
TELEGRAM_CODEX_BOT_AUTO_UPDATE=true
TELEGRAM_CODEX_BOT_SHOW_COMMENTARY_MESSAGES=true
```

For most setups, this is the only file you need to edit.

### GitHub bridge

If you want GitHub issues to be handed off to Codex sessions, keep the bridge
configuration local and use the template docs in:

- `docs/github_codex_bridge.md`
- `docs/github_codex_bridge.sample.json`

The bridge supports two local modes:

- `targets`: poll one or more repositories directly and hand each issue to its
  configured tmux window
- `orchestrator`: consume the monthly issue from a control-plane repository and
  relay it to a single runner window

The real config belongs at `~/.telegram-codex-bot/github_codex_bridge.json` and should not
be committed.

### Required variables

| Variable | Description |
| --- | --- |
| `TELEGRAM_BOT_TOKEN` | Bot token from @BotFather |
| `ALLOWED_USERS` | Comma-separated Telegram user IDs allowed to control the bot |

### Common optional variables

| Variable | Default | Description |
| --- | --- | --- |
| `TELEGRAM_CODEX_BOT_DIR` | `~/.telegram-codex-bot` | Config and state directory |
| `TELEGRAM_CODEX_BOT_BACKEND` | `local` | Agent backend ID. `local` keeps the single-machine tmux behavior |
| `TELEGRAM_CODEX_BOT_BACKEND_PLUGINS` | _(none)_ | Comma-separated Python modules that register optional agent backends |
| `TELEGRAM_CODEX_BOT_TMUX_SESSION_NAME` | `telegram-codex-bot` | tmux session name used by the bot |
| `TELEGRAM_CODEX_BOT_CODEX_COMMAND` | `codex` | Command used when creating a new window |
| `TELEGRAM_CODEX_BOT_CODEX_BYPASS_HOOK_TRUST` | `false` | Append Codex `--dangerously-bypass-hook-trust` for unattended hosts after you have vetted the configured hooks |
| `TELEGRAM_CODEX_BOT_CODEX_PROJECTS_PATH` | `~/.codex` | Transcript root to scan |
| `TELEGRAM_CODEX_BOT_DEFAULT_PROJECTS_PATH` | `~/Projects` | Default directory shown when creating a new session |
| `TELEGRAM_CODEX_BOT_PROJECT_ROOTS` | _(none)_ | Optional named roots shown before directory browsing |
| `TELEGRAM_CODEX_BOT_MONITOR_POLL_INTERVAL` | `2.0` | Poll interval in seconds |
| `TELEGRAM_CODEX_BOT_ENABLE_ACCOUNT_ROTATION` | `false` | Automatically rotate to the next saved account after `usage_limit_exceeded` |
| `TELEGRAM_CODEX_BOT_STATUS_POLL_INTERVAL` | `1.0` | Terminal status polling interval in seconds; active `Working (...)` status edits keep Telegram refreshed |
| `TELEGRAM_CODEX_BOT_STATUS_REPOST_INTERVAL` | `60.0` | Re-send long-running `Thinking` status after this many seconds so Telegram topics visibly stay active; set `0` to only edit in place |
| `TELEGRAM_CODEX_BOT_AGENT_INPUT_QUEUE_MAX_SIZE` | `20` | Maximum bot-held inputs per session while Codex shows an interactive prompt; regular busy-state inputs are sent to Codex directly |
| `TELEGRAM_CODEX_BOT_AGENT_INPUT_QUEUE_MAX_WAIT_SECONDS` | `1800` | Drop bot-held inputs after this many seconds if Codex never becomes ready; set `0` to disable expiry |
| `TELEGRAM_CODEX_BOT_AUTO_UPDATE` | `false` | On startup, check and fast-forward git source installs |
| `TELEGRAM_CODEX_BOT_UPDATE_INTERVAL_SECONDS` | `86400` | Minimum seconds between automatic update checks |
| `TELEGRAM_CODEX_BOT_UPDATE_REQUIRE_IDLE` | `true` | Apply automatic updates only when no Codex pane is active |
| `TELEGRAM_CODEX_BOT_UPDATE_BUSY_RETRY_SECONDS` | `300` | Retry delay when automatic update is deferred by active work |
| `TELEGRAM_CODEX_BOT_UPDATE_REMOTE` | git remote | Optional git remote override for updates |
| `TELEGRAM_CODEX_BOT_UPDATE_BRANCH` | git branch | Optional git branch override for updates |
| `TELEGRAM_CODEX_BOT_UPDATE_RUN_UV_SYNC` | `true` | Run `uv sync` after a successful git update |
| `TELEGRAM_CODEX_BOT_CODEX_UPDATE_CHECK` | `false` | Check npm for Codex CLI updates during the idle update loop |
| `TELEGRAM_CODEX_BOT_CODEX_UPDATE_NPM` | `npm` | npm command used for Codex CLI checks/updates; can be `sudo -n npm` if explicitly allowed |
| `TELEGRAM_CODEX_BOT_CODEX_AUTO_UPDATE` | `false` | Run `npm install -g @openai/codex@latest` when an idle Codex update exists |
| `TELEGRAM_CODEX_BOT_SHOW_COMMENTARY_MESSAGES` | `false` | Forward Codex commentary/thinking messages |
| `TELEGRAM_CODEX_BOT_SHOW_TOOL_CALLS` | `true` | Forward tool call notifications and outputs |
| `TELEGRAM_CODEX_BOT_SHOW_BASH_TOOL_CALLS` | `true` | Forward Bash command and output notifications; set `false` to hide Bash only |
| `TELEGRAM_CODEX_BOT_SHOW_HIDDEN_DIRS` | `false` | Show dot-directories in the directory picker |
| `OPENAI_API_KEY` | _(none)_ | Used for voice transcription |
| `OPENAI_BASE_URL` | `https://api.openai.com/v1` | Custom OpenAI-compatible endpoint |

Telegram formatting uses MarkdownV2 with plain-text fallback when needed.

### Project Roots

To choose a computer, VPS, or mounted workspace before browsing directories,
configure named roots:

```ini
TELEGRAM_CODEX_BOT_PROJECT_ROOTS=Local=~/Projects,Remote=/mnt/remote-projects
```

When `TELEGRAM_CODEX_BOT_PROJECT_ROOTS` is set, a new Telegram topic first shows a
computer/VPS picker, even if only one root is configured. After selecting one,
the normal directory browser starts at that root and does not navigate above it.
Other computers or VPSes must be reachable as local paths from the machine
running telegram-codex-bot, for example through SSHFS or NFS mounts.

### Agent Backends

TelegramCodexBot starts with the `local` backend by default. This is the
existing single-machine mode: Telegram talks to a local tmux session, and the
bot monitors local Codex transcript files.

Single-machine mode is the supported default. The backend interface exists so
center-bot / remote agent-node deployments can be added without changing the
normal local workflow.

If you are not running remote agent nodes, leave `TELEGRAM_CODEX_BOT_BACKEND`
unset or set it to `local`. You do not need to install `plugins/socket_backend`,
run `telegram-codex-agent-node`, or configure socket node addresses.

Optional backends can be loaded as plugins. A plugin can expose a backend
through the `telegram_codex_bot.backends` entry point group, or through a module
listed in `TELEGRAM_CODEX_BOT_BACKEND_PLUGINS`.

The repository includes an optional socket backend package under
`plugins/socket_backend/`. It provides a `socket-cluster` center backend and a
`telegram-codex-agent-node` CLI for remote machines. Design notes and operating
details live in `docs/agent_backend_plugins.md`.

```ini
TELEGRAM_CODEX_BOT_BACKEND=local
```

Plugin module example:

```ini
TELEGRAM_CODEX_BOT_BACKEND=socket-cluster
TELEGRAM_CODEX_BOT_BACKEND_PLUGINS=telegram_codex_bot_socket_backend
TELEGRAM_CODEX_BOT_SOCKET_NODES=macbook=127.0.0.1:8765
```

The core bot loads the configured backend through a backend interface covering
lifecycle and agent operations: `prepare()`, `start(message_callback)`,
`stop()`, `create_session()`, `send_message()`, `send_control()`, and
`capture()`. Backends can also implement the optional browser capability for
remote root selection, directory browsing, and resume-session lookup. The local
backend implements those interfaces by preparing tmux, starting the existing
transcript monitor, and forwarding operations to the current local managers.

Thread bindings are stored in both the legacy local window form and the newer
backend target form when the target is local. This keeps existing state files
rollback-safe while allowing non-local backends to address an agent by
`backend_id`, `node_id`, and `session_id` instead of a tmux window ID.

At the moment this repository does not ship demo screenshots or videos. The
README intentionally avoids embedding old project media.

### Updates

For source-checkout installs created by the bootstrap scripts, set:

```ini
TELEGRAM_CODEX_BOT_AUTO_UPDATE=true
```

While the bot keeps running, it checks the configured git remote at most once
per `TELEGRAM_CODEX_BOT_UPDATE_INTERVAL_SECONDS`. With the default
`TELEGRAM_CODEX_BOT_UPDATE_REQUIRE_IDLE=true`, it first verifies that Telegram delivery queues
are empty and that no Codex tmux pane is working or waiting for interactive
input. If work is active, it waits `TELEGRAM_CODEX_BOT_UPDATE_BUSY_RETRY_SECONDS` and tries
again.

If the checkout is clean and the update can be applied as a fast-forward, telegram-codex-bot
runs `git pull --ff-only`, runs `uv sync`, and restarts itself so the new code is
loaded. Existing Codex tmux windows and conversations are not killed.

Manual commands:

```bash
telegram-codex-bot update --check
telegram-codex-bot update
telegram-codex-bot codex-update --check
telegram-codex-bot codex-update
telegram-codex-bot --version
```

Self-update intentionally skips non-git installs such as `pipx install` or
`uv tool install`, and it also skips a checkout with local modifications.

Codex CLI checks are separate from telegram-codex-bot self-update. The example `.env` enables
`TELEGRAM_CODEX_BOT_CODEX_UPDATE_CHECK=true`, which only reports when a newer npm package is
available. When an update is available, telegram-codex-bot sends allowed users a Telegram
prompt with an upgrade button. Keep `TELEGRAM_CODEX_BOT_CODEX_AUTO_UPDATE=false` unless the
service user should apply Codex CLI updates without confirmation. If the global npm package is
root-owned, set `TELEGRAM_CODEX_BOT_CODEX_UPDATE_NPM=sudo -n npm` only after granting that
non-interactive sudo path deliberately.

### Non-interactive servers / VPS

If Codex runs on a server where you do not want approval prompts in the terminal UI:

```ini
TELEGRAM_CODEX_BOT_CODEX_COMMAND=IS_SANDBOX=1 codex --dangerously-bypass-approvals-and-sandbox
```

## Multi-account login, switching, and failover

By default, new sessions use the service user's normal `~/.codex` login and **automatic account rotation is disabled**. This keeps single-account installs predictable.

Telegram commands:

```text
/codexlogin          # start Codex device login for the default CODEX_HOME
/codexlogin backup   # login into an isolated account home and save it as backup
/codexaccount list
/codexaccount use backup
/codexaccount clear  # go back to the service user's default CODEX_HOME
```

Named accounts are stored under `~/.telegram-codex-bot/accounts/`. Switching affects newly created topics only; existing topics keep their current tmux window. Use `/unbind` when you want the current topic to start a fresh session with the selected account.

If you want usage-limit failover, set `TELEGRAM_CODEX_BOT_ENABLE_ACCOUNT_ROTATION=true`. When a live session emits `usage_limit_exceeded`, TelegramCodexBot marks that window as exhausted; on the next message it can create a fresh tmux window on the next saved account and forward the message there. This is **session rotation**, not seamless continuation of the exact same Codex session.

## Session tracking

By default, this project scans Codex transcript files under `~/.codex`.

If you want automatic session-to-window tracking via the CLI hook, install it with:

```bash
telegram-codex-bot hook --install
```

This command enables Codex hooks in the active Codex home:

- `$CODEX_HOME/config.toml` and `$CODEX_HOME/hooks.json` when `CODEX_HOME` is set
- otherwise `~/.codex/config.toml` and `~/.codex/hooks.json`

Manual equivalent:

`~/.codex/config.toml`

```toml
[features]
hooks = true
```

`~/.codex/hooks.json`

```json
{
  "hooks": {
    "SessionStart": [
      {
        "matcher": "startup|resume",
        "hooks": [
          {
            "type": "command",
            "command": "telegram-codex-bot hook",
            "statusMessage": "Registering Codex session",
            "timeout": 5
          }
        ]
      }
    ]
  }
}
```

The hook writes window/session mappings into `$TELEGRAM_CODEX_BOT_DIR/session_map.json`, which helps the bot keep tmux windows associated with Codex sessions even after clears or restarts.

On unattended hosts, newer Codex versions may require hook trust before the
first session starts. After you have verified that `$CODEX_HOME/hooks.json`
contains only hooks you expect, set:

```bash
TELEGRAM_CODEX_BOT_CODEX_BYPASS_HOOK_TRUST=true
```

This makes new bot-managed Codex windows start with
`--dangerously-bypass-hook-trust`, avoiding a hidden terminal prompt that would
otherwise block the first Telegram message from reaching the transcript.

## Usage

```bash
# installed tool
telegram-codex-bot

# from source
uv run telegram-codex-bot
```

### Bot commands

| Command | Description |
| --- | --- |
| `/start` | Show the welcome message |
| `/history` | Show message history for the current topic |
| `/screenshot` | Capture the current terminal pane |
| `/esc`, `/interrupt` | Send Escape to Codex |
| `/kill` | Kill the bound tmux window and clean up the topic binding |
| `/unbind` | Unbind the topic without killing the running tmux window |
| `/usage` | Open Codex usage info in the TUI and send the parsed result |
| `/codexlogin [name]` | Start Codex device login from Telegram |
| `/codexaccount` | List, save, select, or clear saved Codex accounts |

### Forwarded Codex slash commands

| Command | Description |
| --- | --- |
| `/clear` | Clear conversation history |
| `/compact` | Compact context |
| `/cost` | Show token/cost usage |
| `/goal` | Set or update the session goal |
| `/help` | Show Codex help |
| `/memory` | Edit AGENTS.md |
| `/model` | Switch the model |

Other unknown slash commands are forwarded to Codex as-is.

## Topic workflow

**1 topic = 1 tmux window = 1 active session.**

### Start a session from Telegram

1. Create a new Telegram topic
2. Send any message
3. Pick a directory from the browser
4. Resume an existing session or create a new one
5. TelegramCodexBot creates a tmux window and forwards your pending message

If the bot finds an existing **tracked** tmux window for that directory, it can
offer that window for binding. Untracked windows are ignored on purpose so a
topic does not attach to a terminal that has no reliable session mapping yet.

### Continue working

After a topic is bound, just keep sending text or voice messages in that topic.

### Stop working

- close/delete the Telegram topic, or
- use `/kill`, or
- use `/unbind` if you want to keep the tmux window alive but detach the topic

If you close/delete a topic (or the bot cleans up a dead topic/window), the
associated Codex session is hidden from the resume picker by default. The
underlying transcript file is kept on disk under `~/.codex`.

## Notifications

The monitor polls transcript files and can forward:

- assistant replies
- commentary / thinking output
- tool use and tool results
- local command output
- public progress updates visible in tmux, such as `Explored`, `Ran`, `Searched`, and `Searching the web`
- usage-limit exhaustion events

Public progress updates come from text already visible in the terminal UI. They
are not hidden model reasoning dumps.

## Running Codex manually in tmux

```bash
tmux attach -t telegram-codex-bot
tmux new-window -n myproject -c ~/Code/myproject
codex
```

The window must live inside the configured `telegram-codex-bot` tmux session.

## Data storage

| Path | Description |
| --- | --- |
| `$TELEGRAM_CODEX_BOT_DIR/state.json` | Thread bindings/targets, window state, display names, offsets, and hidden closed-session IDs |
| `$TELEGRAM_CODEX_BOT_DIR/session_map.json` | Hook-generated tmux window ↔ session mappings |
| `$TELEGRAM_CODEX_BOT_DIR/monitor_state.json` | Monitor byte offsets per session |
| `$TELEGRAM_CODEX_BOT_DIR/pending_topic_deletions.json` | Deferred topic deletions after local cleanup |
| `~/.codex/` | Codex transcript root (read-only) |
| `~/.telegram-codex-bot/accounts/` | Optional saved account homes and snapshots |

## File structure

```text
src/telegram_codex_bot/
├── __init__.py
├── account_manager.py
├── agent_io.py
├── backends/
├── bot.py
├── bridge.py
├── config.py
├── hook.py
├── main.py
├── markdown_v2.py
├── monitor_state.py
├── screenshot.py
├── session.py
├── session_monitor.py
├── terminal_parser.py
├── tmux_manager.py
├── transcribe.py
├── transcript_parser.py
├── utils.py
└── handlers/
```

## License

This project is distributed under the MIT License.
Copyright and license notices are kept in `LICENSE`.
Bundled fonts keep their own license files under `src/telegram_codex_bot/fonts/`.
