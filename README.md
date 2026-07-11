# TelegramAgentBot 2.0.0

[中文文档](README_CN.md)

> TelegramAgentBot controls live Codex CLI / Claude Code sessions over Telegram.
> The CLI/package name is `telegram-agent-bot`.

Control Codex CLI or Claude Code sessions remotely through Telegram while keeping tmux as the source of truth. This lets you monitor, answer, interrupt, resume, and clean up real terminal sessions from your phone without switching to a separate SDK session.

## What it does

TelegramAgentBot is a Telegram controller for live Codex CLI / Claude Code sessions (`TELEGRAM_AGENT_BOT_AGENT_TYPE`):

- New topics can choose `Codex` or `Claude Code`, then choose a configured model and reasoning level. `TELEGRAM_AGENT_BOT_AGENT_TYPE` remains the default.
- Codex and Claude Code topics expose Fast mode as a separate session toggle after model and reasoning selection; it is not a reasoning level.
- transcript parsing and monitoring target `~/.codex` for Codex, or `~/.claude/projects` for Claude Code
- Telegram delivery and topic isolation are hardened for long-running agent sessions
- tmux stays the source of truth, so you can return to the same terminal session on desktop
- the default backend is local tmux, with an optional plugin interface for center-bot / remote agent-node deployments
- GitHub bridge support can inject structured tasks from issues into Codex tmux sessions

## Features

- **Topic-based sessions** — each Telegram topic maps 1:1 to a tmux window and agent session
- **Real-time notifications** — assistant replies, thinking, tool calls, tool results, and local command output can be forwarded to Telegram
- **Interactive UI support** — navigate AskUserQuestion, ExitPlanMode, and permission prompts from inline keyboards
- **Voice message transcription** — voice messages are transcribed (OpenAI, Google Gemini, or multi-provider failover) and forwarded as text
- **Resume existing sessions** — choose an existing session in a directory and continue from there
- **Closed-session hiding** — sessions closed through topic deletion or cleanup are hidden from the resume picker by default, without deleting their transcripts
- **Topic cleanup** — stale topics, stale tmux windows, and dead bindings are cleaned up more safely
- **Usage/auth recovery** — usage-limit and login failures are reported in Telegram; optional account failover can be enabled when you have saved backup accounts
- **Persistent state** — thread bindings, display names, offsets, and monitor state survive restarts
- **Pluggable agent backend** — local tmux is the default, while advanced users can load a backend plugin for distributed center-bot / agent-node setups
- **GitHub bridge** — optional `telegram-agent-bridge` CLI can poll GitHub issues and inject structured tasks into Codex tmux sessions

## Prerequisites

- **tmux** installed and available in PATH
- **Codex CLI** or **Claude Code** installed and working locally
- A **Telegram bot** with threaded/forum mode enabled

## Installation

### Option 1: install from GitHub

```bash
# with uv
uv tool install git+https://github.com/Pigbibi/TelegramAgentBot.git

# or with pipx
pipx install git+https://github.com/Pigbibi/TelegramAgentBot.git
```

### Option 2: install from source

```bash
git clone https://github.com/Pigbibi/TelegramAgentBot.git
cd TelegramAgentBot
uv sync
```

## Quick deploy on macOS

For a new Mac or a fresh local setup:

```bash
git clone https://github.com/Pigbibi/TelegramAgentBot.git
cd TelegramAgentBot
chmod +x scripts/bootstrap-macos.sh
./scripts/bootstrap-macos.sh
```

The script does the following:

- run `uv sync`
- create `~/.telegram-agent-bot/.env` from `.env.example` if missing
- install `telegram-agent-bot hook --install` for the current environment
  (Codex by default)
- generate a reusable `~/.telegram-agent-bot/bin/telegram-agent-bot-launch`
- generate a LaunchAgent plist for macOS

Required local setup after the script runs:

1. `TELEGRAM_BOT_TOKEN`
2. `ALLOWED_USERS`
3. optional `AI_TRANSCRIPTION_OPENAI_API_KEY` if you want voice transcription
4. run `codex login` for Codex CLI, or configure Claude Code auth/settings for Claude mode

If you switch `.env` to Claude mode after bootstrap, rerun:

```bash
uv run telegram-agent-bot hook --install
```

The install command reads `~/.telegram-agent-bot/.env`, so it will write the
hook to `~/.claude/settings.json` when
`TELEGRAM_AGENT_BOT_AGENT_TYPE=claude`.

This project does not use a separate `GPT_SUBSCRIPTION=` env var.
In Codex mode, it reuses the local Codex login state:

```bash
codex login
```

If you use multiple accounts or need to refresh login while away from the server, use Telegram commands:

```text
/agentlogin          # refresh the service user's default agent login
/agentlogin backup   # login and save a named backup account
/agentaccount list
/agentaccount use backup
```

If `~/.telegram-agent-bot/.env` still contains placeholder values, the script will write the
launchd files but will not start the service. After editing `.env`, start it
manually:

```bash
launchctl bootstrap "gui/$(id -u)" ~/Library/LaunchAgents/io.github.telegramagentbot.plist
launchctl kickstart -k "gui/$(id -u)/io.github.telegramagentbot"
```

Check status:

```bash
launchctl print "gui/$(id -u)/io.github.telegramagentbot" | sed -n '1,40p'
tail -n 50 ~/.telegram-agent-bot/logs/telegram-agent-bot.err.log
```

## Quick deploy on Linux / VPS

For a Linux workstation or a VPS with systemd:

```bash
mkdir -p ~/.telegram-agent-bot/app
git clone https://github.com/Pigbibi/TelegramAgentBot.git ~/.telegram-agent-bot/app/TelegramAgentBot
cd ~/.telegram-agent-bot/app/TelegramAgentBot
chmod +x scripts/bootstrap-linux.sh
./scripts/bootstrap-linux.sh
```

Keep the bot checkout outside the project roots that agent sessions can browse
or clean, such as `~/Projects`. The Linux bootstrap refuses an unsafe checkout
inside `TELEGRAM_AGENT_BOT_DEFAULT_PROJECTS_PATH` or
`TELEGRAM_AGENT_BOT_PROJECT_ROOTS`, because the systemd launcher points back to
that checkout and a project cleanup would break the next restart.

The Linux helper:

- runs `uv sync`
- creates `~/.telegram-agent-bot/.env` from `.env.example` if needed
- installs `telegram-agent-bot hook --install` for the current environment
  (Codex by default)
- writes `~/.telegram-agent-bot/bin/telegram-agent-bot-launch`
- writes a user service at `~/.config/systemd/user/io.github.telegramagentbot.service`

After that:

1. edit `~/.telegram-agent-bot/.env`
2. run `codex login` for Codex CLI, or configure Claude Code auth/settings for Claude mode
3. start the service if it was not auto-started:

```bash
systemctl --user daemon-reload
systemctl --user enable --now io.github.telegramagentbot.service
```

If you switch `.env` to Claude mode after bootstrap, rerun
`uv run telegram-agent-bot hook --install` before starting the service.

On a VPS, if you want the service to keep running after reboot without an
interactive login session:

```bash
sudo loginctl enable-linger "$USER"
```

Check status:

```bash
systemctl --user status io.github.telegramagentbot.service --no-pager
tail -n 50 ~/.telegram-agent-bot/logs/telegram-agent-bot.err.log
```

## Configuration

### 1. Create a Telegram bot

1. Talk to [@BotFather](https://t.me/BotFather)
2. Create a bot and get the token
3. Open the bot settings mini app
4. Enable **Threaded Mode**

### 2. Create `~/.telegram-agent-bot/.env`

```ini
TELEGRAM_BOT_TOKEN=your_bot_token_here
ALLOWED_USERS=your_telegram_user_id
TELEGRAM_AGENT_BOT_CODEX_COMMAND=codex
TELEGRAM_AGENT_BOT_AUTO_UPDATE=true
TELEGRAM_AGENT_BOT_CODEX_UPDATE_CHECK=true
TELEGRAM_AGENT_BOT_CODEX_AUTO_UPDATE=true
TELEGRAM_AGENT_BOT_SHOW_COMMENTARY_MESSAGES=false
# Optional Claude Code mode:
# TELEGRAM_AGENT_BOT_AGENT_TYPE=claude
# Change TELEGRAM_AGENT_BOT_CODEX_COMMAND above to claude, or remove it to use
# the Claude default.
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

The real config belongs at `~/.telegram-agent-bot/github_codex_bridge.json` and should not
be committed.

### Required variables

| Variable | Description |
| --- | --- |
| `TELEGRAM_BOT_TOKEN` | Bot token from @BotFather |
| `ALLOWED_USERS` | Comma-separated Telegram user IDs allowed to control the bot |

### Common optional variables

| Variable | Default | Description |
| --- | --- | --- |
| `TELEGRAM_AGENT_BOT_DIR` | `~/.telegram-agent-bot` | Config and state directory |
| `TELEGRAM_AGENT_BOT_AGENT_TYPE` | `codex` | AI CLI to manage: `codex` (Codex CLI) or `claude` (Claude Code) |
| `TELEGRAM_AGENT_BOT_BACKEND` | `local` | Agent backend ID. `local` keeps the single-machine tmux behavior |
| `TELEGRAM_AGENT_BOT_BACKEND_PLUGINS` | _(none)_ | Comma-separated Python modules that register optional agent backends |
| `TELEGRAM_AGENT_BOT_TMUX_SESSION_NAME` | `telegram-agent-bot` | tmux session name used by the bot |
| `TELEGRAM_AGENT_BOT_CODEX_COMMAND` | `codex` for Codex, `claude` for Claude Code | Command used when creating a new window |
| `TELEGRAM_AGENT_BOT_CLAUDE_ENV_FILE` | `~/.telegram-agent-bot/claude.env` | Optional 0600 environment file for Claude Code/DeepSeek; sourced without putting the key in tmux command text |
| `TELEGRAM_AGENT_BOT_CODEX_MODEL` | `gpt-5.4-mini` | Default Codex model for new topics |
| `TELEGRAM_AGENT_BOT_CLAUDE_MODEL` | `deepseek-v4-flash` | Default Claude Code model for new topics; override for another provider |
| `TELEGRAM_AGENT_BOT_CODEX_MODELS` | `auto` | `auto` uses the installed Codex app-server catalog; a comma-separated list pins the choices |
| `TELEGRAM_AGENT_BOT_CLAUDE_MODELS` | `auto` | `auto` queries the configured Anthropic-compatible provider; a comma-separated list pins the choices |
| `TELEGRAM_AGENT_BOT_MODEL_DISCOVERY` | `true` | Refresh automatic model choices once at startup; failed discovery falls back to configured defaults |
| `TELEGRAM_AGENT_BOT_CODEX_BYPASS_HOOK_TRUST` | `false` | Append Codex `--dangerously-bypass-hook-trust` for unattended hosts after you have vetted the configured hooks |
| `TELEGRAM_AGENT_BOT_CODEX_PROJECTS_PATH` | `~/.codex` for Codex, `~/.claude/projects` for Claude Code | Transcript root to scan |
| `TELEGRAM_AGENT_BOT_DEFAULT_PROJECTS_PATH` | `~/Projects` | Default directory shown when creating a new session |
| `TELEGRAM_AGENT_BOT_PROJECT_ROOTS` | _(none)_ | Optional named roots shown before directory browsing |
| `TELEGRAM_AGENT_BOT_MONITOR_POLL_INTERVAL` | `2.0` | Poll interval in seconds |
| `TELEGRAM_AGENT_BOT_ENABLE_ACCOUNT_ROTATION` | `false` | Automatically rotate to the next saved account after `usage_limit_exceeded` |
| `TELEGRAM_AGENT_BOT_STATUS_POLL_INTERVAL` | `1.0` | Terminal status polling interval in seconds; active `Working (...)` status edits keep Telegram refreshed |
| `TELEGRAM_AGENT_BOT_STATUS_REPOST_INTERVAL` | `60.0` | Re-send long-running `Thinking` status after this many seconds so Telegram topics visibly stay active; set `0` to only edit in place |
| `TELEGRAM_AGENT_BOT_AGENT_INPUT_QUEUE_MAX_SIZE` | `20` | Maximum bot-held inputs per session while Codex shows an interactive prompt; regular busy-state inputs are sent to Codex directly |
| `TELEGRAM_AGENT_BOT_AGENT_INPUT_QUEUE_MAX_WAIT_SECONDS` | `1800` | Drop bot-held inputs after this many seconds if Codex never becomes ready; set `0` to disable expiry |
| `TELEGRAM_AGENT_BOT_AGENT_STARTUP_TIMEOUT_SECONDS` | `180` | Maximum wait for a newly launched agent UI before reporting a pending-message failure |
| `TELEGRAM_AGENT_BOT_AUTO_UPDATE` | `false` | On startup, check and fast-forward git source installs |
| `TELEGRAM_AGENT_BOT_UPDATE_INTERVAL_SECONDS` | `86400` | Minimum seconds between automatic update checks |
| `TELEGRAM_AGENT_BOT_UPDATE_REQUIRE_IDLE` | `true` | Apply automatic updates only when no Codex pane is active |
| `TELEGRAM_AGENT_BOT_UPDATE_BUSY_RETRY_SECONDS` | `300` | Retry delay when automatic update is deferred by active work |
| `TELEGRAM_AGENT_BOT_UPDATE_REMOTE` | git remote | Optional git remote override for updates |
| `TELEGRAM_AGENT_BOT_UPDATE_BRANCH` | git branch | Optional git branch override for updates |
| `TELEGRAM_AGENT_BOT_UPDATE_RUN_UV_SYNC` | `true` | Run `uv sync` after a successful git update |
| `TELEGRAM_AGENT_BOT_CODEX_UPDATE_CHECK` | `false` | Check npm for Codex CLI updates during the idle update loop |
| `TELEGRAM_AGENT_BOT_CODEX_UPDATE_NPM` | `npm` | npm command used for Codex CLI checks/updates; can be `sudo -n npm` if explicitly allowed |
| `TELEGRAM_AGENT_BOT_CODEX_AUTO_UPDATE` | `false` | Run `npm install -g @openai/codex@latest` when an idle Codex update exists |
| `TELEGRAM_AGENT_BOT_SHOW_COMMENTARY_MESSAGES` | `false` | Forward intermediary commentary messages; model reasoning is always hidden |
| `TELEGRAM_AGENT_BOT_SHOW_TOOL_CALLS` | `true` | Forward tool call notifications and outputs |
| `TELEGRAM_AGENT_BOT_SHOW_BASH_TOOL_CALLS` | `true` | Forward Bash command and output notifications; set `false` to hide Bash only |
| `TELEGRAM_AGENT_BOT_SHOW_HIDDEN_DIRS` | `false` | Show dot-directories in the directory picker |
| `AI_TRANSCRIPTION_PROVIDERS` | `openai` | Comma-separated provider IDs tried in order (`openai`, `google`) |
| `AI_TRANSCRIPTION_OPENAI_API_KEY` | _(none)_ | API key for OpenAI-compatible transcription |
| `AI_TRANSCRIPTION_OPENAI_BASE_URL` | `https://api.openai.com/v1` | Base URL for OpenAI-compatible endpoint |
| `AI_TRANSCRIPTION_OPENAI_MODEL` | `gpt-4o-transcribe` | Model for OpenAI-compatible provider |
| `AI_TRANSCRIPTION_GOOGLE_API_KEY` | _(none)_ | API key for Google Gemini transcription |
| `AI_TRANSCRIPTION_GOOGLE_MODEL` | `gemini-2.0-flash-lite` | Model for Google Gemini provider |

Telegram formatting uses MarkdownV2 with plain-text fallback when needed.

### Automatic model discovery

When either model list is empty or set to `auto`, the bot refreshes the picker
once during startup. Codex uses the installed CLI's official `model/list`
app-server method, which reflects the current Codex account and build. Claude
Code uses the configured Anthropic-compatible provider's model-list endpoint;
this supports DeepSeek's Anthropic gateway when its `claude.env` contains the
provider credentials. If discovery is unavailable, the configured default
model remains selectable. Set an explicit comma-separated model list when a
gateway uses custom aliases or when you want a pinned catalog.

### Claude Code mode

Set `TELEGRAM_AGENT_BOT_AGENT_TYPE=claude` to manage Claude Code instead of
Codex CLI. In this mode:

- new windows run `claude` by default unless `TELEGRAM_AGENT_BOT_CODEX_COMMAND`
  overrides it; if your `.env` contains `TELEGRAM_AGENT_BOT_CODEX_COMMAND=codex`,
  change it to `claude` or remove that line
- `telegram-agent-bot hook --install` writes the SessionStart hook to
  `~/.claude/settings.json`
- transcripts are scanned from `~/.claude/projects` unless
  `TELEGRAM_AGENT_BOT_CODEX_PROJECTS_PATH` is set
- bot-created Claude homes are isolated with `HOME=<account_home>` and store
  settings/transcripts under `<account_home>/.claude`
- the bot does not rely on `CLAUDE_HOME`

Use `/codexlogin` and `/codexaccount` for Codex accounts, or `/claudelogin` and
`/claudeaccount` for Claude Code accounts. The provider-neutral `/agentlogin` and
`/agentaccount` commands remain available and follow
`TELEGRAM_AGENT_BOT_AGENT_TYPE`. In Claude mode they launch `claude auth login`. Claude Code
subscription/OAuth credentials can be OS- or keychain-dependent, so verify
named-account switching on your target host before relying on automatic
rotation; API-key settings in `settings.json` are copied with the account home.

### 2.0.0 interface notes

- `/codexlogin`, `/codexaccount`, `/claudelogin`, and `/claudeaccount` explicitly target one agent.
- `/agentlogin` and `/agentaccount` remain compatibility aliases for the configured default agent.
- `fast` is no longer a reasoning-effort value. Use the Fast mode toggle after selecting a model and reasoning level. Codex and Claude Code forward this to their respective `/fast` command.
- Upgrade existing deployments and migrate bot commands before restarting them.

### Project Roots

To choose a computer, VPS, or mounted workspace before browsing directories,
configure named roots:

```ini
TELEGRAM_AGENT_BOT_PROJECT_ROOTS=Local=~/Projects,Remote=/mnt/remote-projects
```

When `TELEGRAM_AGENT_BOT_PROJECT_ROOTS` is set, a new Telegram topic first shows a
computer/VPS picker, even if only one root is configured. After selecting one,
the normal directory browser starts at that root and does not navigate above it.
Other computers or VPSes must be reachable as local paths from the machine
running telegram-agent-bot, for example through SSHFS or NFS mounts.

### Agent Backends

TelegramAgentBot starts with the `local` backend by default. This is the
existing single-machine mode: Telegram talks to a local tmux session, and the
bot monitors local agent transcript files.

Single-machine mode is the supported default. The backend interface exists so
center-bot / remote agent-node deployments can be added without changing the
normal local workflow.

If you are not running remote agent nodes, leave `TELEGRAM_AGENT_BOT_BACKEND`
unset or set it to `local`. You do not need to install `plugins/socket_backend`,
run `telegram-agent-node`, or configure socket node addresses.

Optional backends can be loaded as plugins. A plugin can expose a backend
through the `telegram_agent_bot.backends` entry point group, or through a module
listed in `TELEGRAM_AGENT_BOT_BACKEND_PLUGINS`.

The repository includes an optional socket backend package under
`plugins/socket_backend/`. It provides a `socket-cluster` center backend and a
`telegram-agent-node` CLI for remote machines. Design notes and operating
details live in `docs/agent_backend_plugins.md`.

```ini
TELEGRAM_AGENT_BOT_BACKEND=local
```

Plugin module example:

```ini
TELEGRAM_AGENT_BOT_BACKEND=socket-cluster
TELEGRAM_AGENT_BOT_BACKEND_PLUGINS=telegram_agent_bot_socket_backend
TELEGRAM_AGENT_BOT_SOCKET_NODES=macbook=127.0.0.1:8765
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
TELEGRAM_AGENT_BOT_AUTO_UPDATE=true
```

While the bot keeps running, it checks the configured git remote at most once
per `TELEGRAM_AGENT_BOT_UPDATE_INTERVAL_SECONDS`. With the default
`TELEGRAM_AGENT_BOT_UPDATE_REQUIRE_IDLE=true`, it first verifies that Telegram delivery queues
are empty and that no Codex tmux pane is working or waiting for interactive
input. If work is active, it waits `TELEGRAM_AGENT_BOT_UPDATE_BUSY_RETRY_SECONDS` and tries
again.

If the checkout is clean and the update can be applied as a fast-forward, telegram-agent-bot
runs `git pull --ff-only`, runs `uv sync`, and restarts itself so the new code is
loaded. Existing Codex tmux windows and conversations are not killed.

Manual commands:

```bash
telegram-agent-bot update --check
telegram-agent-bot update
telegram-agent-bot codex-update --check
telegram-agent-bot codex-update
telegram-agent-bot --version
```

Self-update intentionally skips non-git installs such as `pipx install` or
`uv tool install`, and it also skips a checkout with local modifications.

Codex CLI checks are separate from telegram-agent-bot self-update. The example `.env` enables
`TELEGRAM_AGENT_BOT_CODEX_UPDATE_CHECK=true` and
`TELEGRAM_AGENT_BOT_CODEX_AUTO_UPDATE=true`, so an idle bot applies newer Codex CLI npm
packages without waiting for Telegram confirmation. If the global npm package is root-owned,
set `TELEGRAM_AGENT_BOT_CODEX_UPDATE_NPM=sudo -n npm` only after granting that
non-interactive sudo path deliberately. Set `TELEGRAM_AGENT_BOT_CODEX_AUTO_UPDATE=false` if you
prefer an upgrade prompt with a confirmation button.

### Non-interactive servers / VPS

If Codex runs on a server where you do not want approval prompts in the terminal UI:

```ini
TELEGRAM_AGENT_BOT_CODEX_COMMAND=IS_SANDBOX=1 codex --dangerously-bypass-approvals-and-sandbox
```

## Multi-account login, switching, and failover

By default, new sessions use the service user's normal agent login (`~/.codex`
for Codex, `~/.claude` for Claude Code) and **automatic account rotation is
disabled**. This keeps single-account installs predictable.

Telegram commands:

```text
/codexlogin          # login to Codex
/codexlogin backup   # login to Codex and save a named account
/codexaccount list
/codexaccount use backup
/claudelogin         # login to Claude Code
/claudelogin backup  # login to Claude Code and save a named account
/claudeaccount list
/claudeaccount use backup
/agentlogin          # compatibility alias for the configured default agent
/agentaccount clear  # clear the configured default agent selection
```

Named accounts are stored under `~/.telegram-agent-bot/accounts/<agent>/`. Switching affects newly created topics only; existing topics keep their current tmux window. Use `/unbind` when you want the current topic to start a fresh session with the selected account. In Claude mode, validate named-account switching on your host before enabling rotation because Claude subscription credentials may be stored outside the copied home on some platforms.

If you want usage-limit failover, set `TELEGRAM_AGENT_BOT_ENABLE_ACCOUNT_ROTATION=true`. When a live session emits `usage_limit_exceeded`, TelegramAgentBot marks that window as exhausted; on the next message it can create a fresh tmux window on the next saved account and forward the message there. This is **session rotation**, not seamless continuation of the exact same agent session.

## Session tracking

By default, this project scans transcript files under `~/.codex` for Codex or
`~/.claude/projects` for Claude Code.

If you want automatic session-to-window tracking via the CLI hook, install it with:

```bash
telegram-agent-bot hook --install
```

For Codex CLI, this command enables hooks in the active Codex home:

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
            "command": "telegram-agent-bot hook",
            "statusMessage": "Registering agent session",
            "timeout": 5
          }
        ]
      }
    ]
  }
}
```

For Claude Code, the same command writes to `~/.claude/settings.json` instead
of `~/.codex/hooks.json`:

```json
{
  "hooks": {
    "SessionStart": [
      {
        "matcher": "startup|resume",
        "hooks": [
          {
            "type": "command",
            "command": "telegram-agent-bot hook",
            "statusMessage": "Registering agent session",
            "timeout": 5
          }
        ]
      }
    ]
  }
}
```

The hook writes window/session mappings into `$TELEGRAM_AGENT_BOT_DIR/session_map.json`, which helps the bot keep tmux windows associated with agent sessions even after clears or restarts.

On unattended hosts, newer Codex versions may require hook trust before the
first session starts. After you have verified that `$CODEX_HOME/hooks.json`
contains only hooks you expect, set:

```bash
TELEGRAM_AGENT_BOT_CODEX_BYPASS_HOOK_TRUST=true
```

This makes new bot-managed Codex windows start with
`--dangerously-bypass-hook-trust`, avoiding a hidden terminal prompt that would
otherwise block the first Telegram message from reaching the transcript.

## Usage

```bash
# installed tool
telegram-agent-bot

# from source
uv run telegram-agent-bot
```

### Bot commands

| Command | Description |
| --- | --- |
| `/start` | Show the welcome message |
| `/history` | Show message history for the current topic |
| `/screenshot` | Capture the current terminal pane |
| `/esc`, `/interrupt` | Send Escape to the agent |
| `/kill` | Kill the bound tmux window and clean up the topic binding |
| `/unbind` | Unbind the topic without killing the running tmux window |
| `/usage` | Open Codex usage info in the TUI and send the parsed result; Codex-specific |
| `/agentlogin [name]` | Start agent login from Telegram |
| `/agentaccount` | List, save, select, or clear saved agent accounts |
| `/codexlogin [name]` | Login to Codex and optionally save a named account |
| `/codexaccount` | Manage saved Codex accounts |
| `/claudelogin [name]` | Login to Claude Code and optionally save a named account |
| `/claudeaccount` | Manage saved Claude Code accounts |

### Forwarded agent slash commands

| Command | Description |
| --- | --- |
| `/clear` | Clear conversation history |
| `/compact` | Compact context |
| `/cost` | Show token/cost usage |
| `/goal` | Set or update the session goal |
| `/agentcmd` / `/cmd` | Forward any agent slash command, e.g. `/agentcmd /review` |
| `/help` | Show Codex help |
| `/memory` | Edit AGENTS.md |
| `/model` | Switch the model |

Other unknown slash commands are forwarded to the current agent as-is.

## Topic workflow

**1 topic = 1 tmux window = 1 active session.**

### Start a session from Telegram

1. Create a new Telegram topic
2. Send any message
3. Pick a directory from the browser
4. Resume an existing session or create a new one
5. TelegramAgentBot creates a tmux window and forwards your pending message

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
associated session is hidden from the resume picker by default. The underlying
transcript file is kept on disk under the configured transcript root.

## Notifications

The monitor polls transcript files and can forward:

- assistant replies
- intermediary commentary output (model reasoning is hidden)
- tool use and tool results
- local command output
- public progress updates visible in tmux, such as `Explored`, `Ran`, `Searched`, and `Searching the web`
- usage-limit exhaustion events

Public progress updates come from text already visible in the terminal UI. They
are not hidden model reasoning dumps.

## Running Codex manually in tmux

```bash
tmux attach -t telegram-agent-bot
tmux new-window -n myproject -c ~/Code/myproject
codex
```

The window must live inside the configured `telegram-agent-bot` tmux session.

## Data storage

| Path | Description |
| --- | --- |
| `$TELEGRAM_AGENT_BOT_DIR/state.json` | Thread bindings/targets, window state, display names, offsets, and hidden closed-session IDs |
| `$TELEGRAM_AGENT_BOT_DIR/session_map.json` | Hook-generated tmux window ↔ session mappings |
| `$TELEGRAM_AGENT_BOT_DIR/monitor_state.json` | Monitor byte offsets per session |
| `$TELEGRAM_AGENT_BOT_DIR/pending_topic_deletions.json` | Deferred topic deletions after local cleanup |
| `~/.codex/` | Codex transcript root (read-only) |
| `~/.claude/projects/` | Claude Code transcript root (read-only, Claude mode) |
| `~/.telegram-agent-bot/accounts/` | Optional saved account homes and snapshots |

## File structure

```text
src/telegram_agent_bot/
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
Bundled fonts keep their own license files under `src/telegram_agent_bot/fonts/`.
