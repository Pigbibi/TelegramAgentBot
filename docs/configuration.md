# Configuration

TelegramAgentBot reads environment variables from the current directory's
`.env` first, then from `$TELEGRAM_AGENT_BOT_DIR/.env`. The application directory
defaults to `~/.telegram-agent-bot`.

For a service installation, keep the configuration at:

```text
~/.telegram-agent-bot/.env
```

Start from the repository's [`.env.example`](../.env.example). The template
contains a safe baseline; this page groups supported settings by purpose.

## Required settings

| Variable | Description |
| --- | --- |
| `TELEGRAM_BOT_TOKEN` | Token issued by Telegram's @BotFather |
| `ALLOWED_USERS` | Comma-separated numeric Telegram user IDs allowed to control the bot |

Example:

```ini
TELEGRAM_BOT_TOKEN=123456:replace_me
ALLOWED_USERS=123456789,987654321
```

Do not quote or log the token. Keep the file readable only by the service user:

```bash
chmod 600 ~/.telegram-agent-bot/.env
```

## Agent selection

| Variable | Default | Description |
| --- | --- | --- |
| `TELEGRAM_AGENT_BOT_AGENT_TYPE` | `codex` | Default agent: `codex` or `claude` |
| `TELEGRAM_AGENT_BOT_CODEX_COMMAND` | `codex` | Command used to start Codex |
| `TELEGRAM_AGENT_BOT_CLAUDE_COMMAND` | `claude` | Command used to start Claude Code |
| `TELEGRAM_AGENT_BOT_CODEX_MODEL` | `gpt-5.4-mini` | Default Codex model shown for new topics |
| `TELEGRAM_AGENT_BOT_CLAUDE_MODEL` | `deepseek-v4-flash` | Default Claude Code model shown for new topics |
| `TELEGRAM_AGENT_BOT_CODEX_MODELS` | automatic | Comma-separated model picker override |
| `TELEGRAM_AGENT_BOT_CLAUDE_MODELS` | automatic | Comma-separated model picker override |
| `TELEGRAM_AGENT_BOT_MODEL_DISCOVERY` | `true` | Refresh available models from the configured CLI or provider |
| `TELEGRAM_AGENT_BOT_CODEX_REASONING_EFFORT` | `medium` | Default Codex reasoning level |
| `TELEGRAM_AGENT_BOT_CLAUDE_REASONING_EFFORT` | `high` | Default Claude Code reasoning level |
| `TELEGRAM_AGENT_BOT_CLAUDE_ENV_FILE` | `$TELEGRAM_AGENT_BOT_DIR/claude.env` | Optional owner-only environment file for Claude Code providers |

Each new topic can choose an agent independently. `TELEGRAM_AGENT_BOT_AGENT_TYPE`
sets the initial default and provider-neutral account command behavior.

Automatic model discovery keeps the configured default available if discovery
fails. Set an explicit comma-separated list when a gateway uses custom aliases
or when operators need a fixed picker.

## tmux and project paths

| Variable | Default | Description |
| --- | --- | --- |
| `TELEGRAM_AGENT_BOT_TMUX_SESSION_NAME` | `telegram-agent-bot` | tmux session managed by the local backend |
| `TELEGRAM_AGENT_BOT_TMUX_SOCKET_NAME` | unset | Private socket name stored below the application directory |
| `TELEGRAM_AGENT_BOT_TMUX_SOCKET_PATH` | derived | Absolute private socket path override |
| `TELEGRAM_AGENT_BOT_DEFAULT_PROJECTS_PATH` | `~/Projects` | Initial directory picker root |
| `TELEGRAM_AGENT_BOT_PROJECT_ROOTS` | unset | Named roots in `Label=/path` form |
| `TELEGRAM_AGENT_BOT_CODEX_PROJECTS_PATH` | agent-specific | Transcript root override |
| `TELEGRAM_AGENT_BOT_SHOW_HIDDEN_DIRS` | `false` | Include dot-directories in the picker |
| `TELEGRAM_AGENT_BOT_SHOW_EXTERNAL_RESUME_SESSIONS` | `false` | Include untracked CLI sessions in the resume picker |

Use a private tmux socket for service isolation:

```ini
TELEGRAM_AGENT_BOT_TMUX_SOCKET_NAME=telegram-agent-bot
```

Use named roots when one bot can access several mounted workspaces:

```ini
TELEGRAM_AGENT_BOT_PROJECT_ROOTS=Local=~/Projects,BuildHost=/mnt/build-host
```

Paths must be visible on the machine running the selected backend. A mounted
remote filesystem does not run the agent on that remote machine; use a backend
plugin for remote execution.

## Queue and concurrency limits

| Variable | Default | Description |
| --- | --- | --- |
| `TELEGRAM_AGENT_BOT_MAX_CONCURRENT_UPDATES` | `4` | Telegram topics that may be processed concurrently |
| `TELEGRAM_AGENT_BOT_MAX_ACTIVE_TURNS` | `2` | Local agent turns allowed to work simultaneously; `0` is unlimited |
| `TELEGRAM_AGENT_BOT_AGENT_INPUT_QUEUE_MAX_SIZE` | `20` | Maximum durable queued inputs per session |
| `TELEGRAM_AGENT_BOT_AGENT_INPUT_QUEUE_MAX_WAIT_SECONDS` | `1800` | Queue expiry; `0` disables expiry |
| `TELEGRAM_AGENT_BOT_TRANSCRIPT_CONFIRM_TIMEOUT_SECONDS` | `15` | Wait for Codex to persist a submitted prompt before reporting unconfirmed delivery; minimum `5` |
| `TELEGRAM_AGENT_BOT_AGENT_STARTUP_TIMEOUT_SECONDS` | `180` | Maximum wait for a newly started agent UI |
| `TELEGRAM_AGENT_BOT_IDLE_SESSION_TIMEOUT_SECONDS` | `1800` | Stop an idle resumable process; `0` disables hibernation |
| `TELEGRAM_AGENT_BOT_MONITOR_POLL_INTERVAL` | `2.0` | Transcript polling interval in seconds |
| `TELEGRAM_AGENT_BOT_STATUS_POLL_INTERVAL` | `1.0` | Terminal status polling interval in seconds |
| `TELEGRAM_AGENT_BOT_STATUS_REPOST_INTERVAL` | `60.0` | Re-send long-running status messages; `0` edits in place only |

For a host with about 2 GB of RAM, keep `MAX_ACTIVE_TURNS` at `1` or `2` and
avoid raising Telegram update concurrency without measuring memory usage.

## Output and transcription

| Variable | Default | Description |
| --- | --- | --- |
| `TELEGRAM_AGENT_BOT_OUTPUT_MODE` | `clean` | Default topic output: `clean` or `trace` |
| `TELEGRAM_AGENT_BOT_SHOW_USER_MESSAGES` | `true` | Include user messages in forwarded history |
| `TELEGRAM_AGENT_BOT_SHOW_COMMENTARY_MESSAGES` | `false` | Forward intermediary public commentary |
| `TELEGRAM_AGENT_BOT_SHOW_TOOL_CALLS` | `true` | Permit tool notifications; topic output mode still applies |
| `TELEGRAM_AGENT_BOT_SHOW_BASH_TOOL_CALLS` | `true` | Permit local command notifications |
| `AI_TRANSCRIPTION_PROVIDERS` | `openai` | Provider order, for example `openai,google` |
| `AI_TRANSCRIPTION_OPENAI_API_KEY` | unset | OpenAI-compatible transcription credential |
| `AI_TRANSCRIPTION_OPENAI_BASE_URL` | OpenAI API | OpenAI-compatible API base URL |
| `AI_TRANSCRIPTION_OPENAI_MODEL` | `gpt-4o-transcribe` | OpenAI-compatible transcription model |
| `AI_TRANSCRIPTION_GOOGLE_API_KEY` | unset | Google Gemini credential |
| `AI_TRANSCRIPTION_GOOGLE_MODEL` | `gemini-2.0-flash-lite` | Gemini transcription model |

Provider API keys are removed from the process environment after configuration
is loaded so child agent processes do not inherit them.

## Health alerts

| Variable | Default | Description |
| --- | --- | --- |
| `TELEGRAM_AGENT_BOT_HEALTH_ALERTS_ENABLED` | `true` | Enable private operator alerts |
| `TELEGRAM_AGENT_BOT_HEALTH_NOTIFICATION_LANGUAGE` | `en` | Alert language: `en` or `zh` |
| `TELEGRAM_AGENT_BOT_HEALTH_CHECK_INTERVAL_SECONDS` | `60` | Health sampling interval; minimum 30 seconds |
| `TELEGRAM_AGENT_BOT_HEALTH_ALERT_COOLDOWN_SECONDS` | `86400` | Minimum repeat interval for an unchanged issue |
| `TELEGRAM_AGENT_BOT_HEALTH_RECOVERY_STABLE_SECONDS` | `300` | Stable period before a recovery notice |
| `TELEGRAM_AGENT_BOT_HEALTH_MEMORY_AVAILABLE_MB` | `256` | Low-memory threshold |
| `TELEGRAM_AGENT_BOT_HEALTH_SWAP_USED_PERCENT` | `75` | Swap alert threshold |
| `TELEGRAM_AGENT_BOT_HEALTH_DISK_USED_PERCENT` | `85` | Disk alert threshold |
| `TELEGRAM_AGENT_BOT_HEALTH_QUEUE_OLDEST_SECONDS` | `600` | Oldest-input alert threshold |
| `TELEGRAM_AGENT_BOT_HEALTH_TRANSCRIPT_LAG_SECONDS` | `300` | Transcript-delivery lag threshold |

Alert state is durable. Restarting the service does not reset the cooldown.
Use `/health` for an on-demand snapshot without changing alert state.

## Accounts

`/codexlogin`, `/claudelogin`, and `/agentlogin` start authentication for the
corresponding CLI. Supplying a name creates a reusable account snapshot:

```text
/codexlogin backup
/codexaccount list
/codexaccount use backup
```

Named accounts live under `$TELEGRAM_AGENT_BOT_DIR/accounts/`. Selection applies
to new topics only. Set `TELEGRAM_AGENT_BOT_ENABLE_ACCOUNT_ROTATION=true` only
after manual switching works on the target host.

## Hooks

Install the session tracking hook after selecting the default agent:

```bash
uv run telegram-agent-bot hook --install
```

The hook records tmux window and session associations in
`$TELEGRAM_AGENT_BOT_DIR/session_map.json`. On unattended Codex hosts,
`TELEGRAM_AGENT_BOT_CODEX_BYPASS_HOOK_TRUST=true` bypasses the CLI trust prompt
for the configured hook. Enable it only after reviewing the installed hook
configuration.

## Updates

Source-checkout installations support bounded update checks:

| Variable | Default | Description |
| --- | --- | --- |
| `TELEGRAM_AGENT_BOT_AUTO_UPDATE` | `false` | Check and fast-forward the source checkout |
| `TELEGRAM_AGENT_BOT_UPDATE_REQUIRE_IDLE` | `true` | Apply updates only when queues and agent panes are idle |
| `TELEGRAM_AGENT_BOT_UPDATE_INTERVAL_SECONDS` | `86400` | Minimum interval between checks |
| `TELEGRAM_AGENT_BOT_UPDATE_BUSY_RETRY_SECONDS` | `300` | Retry delay while work is active |
| `TELEGRAM_AGENT_BOT_UPDATE_RUN_UV_SYNC` | `true` | Sync dependencies after a source update |
| `TELEGRAM_AGENT_BOT_CODEX_UPDATE_CHECK` | `false` | Check the selected agent CLI package for updates |
| `TELEGRAM_AGENT_BOT_CODEX_AUTO_UPDATE` | `false` | Install an available agent CLI package update |
| `TELEGRAM_AGENT_BOT_CODEX_UPDATE_NPM` | `npm` | npm command used for CLI package updates |

Self-update requires a clean Git checkout and a fast-forwardable branch. It is
skipped for `pipx` and `uv tool` installations.

Manual commands:

```bash
telegram-agent-bot update --check
telegram-agent-bot update
telegram-agent-bot codex-update --check
telegram-agent-bot codex-update
telegram-agent-bot --version
```

## Optional backends

The built-in backend requires no extra settings:

```ini
TELEGRAM_AGENT_BOT_BACKEND=local
```

Plugins may register additional backend IDs through Python entry points or
modules listed in `TELEGRAM_AGENT_BOT_BACKEND_PLUGINS`. See
[Agent backend plugins](agent_backend_plugins.md).
