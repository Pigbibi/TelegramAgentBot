# Deployment

The supported service layouts keep runtime configuration and state under
`~/.telegram-agent-bot` and keep the source checkout outside agent workspaces.

## Linux and VPS

Install from a dedicated checkout:

```bash
mkdir -p ~/.telegram-agent-bot/app
git clone https://github.com/Pigbibi/TelegramAgentBot.git \
  ~/.telegram-agent-bot/app/TelegramAgentBot
cd ~/.telegram-agent-bot/app/TelegramAgentBot
./scripts/bootstrap-linux.sh
```

The script:

- verifies `uv`, tmux, Python, and the selected agent CLI;
- runs `uv sync`;
- creates `~/.telegram-agent-bot/.env` when it is absent;
- installs the session tracking hook;
- writes `~/.telegram-agent-bot/bin/telegram-agent-bot-launch`;
- writes `~/.config/systemd/user/io.github.telegramagentbot.service`.

Edit the configuration and start the user service:

```bash
chmod 600 ~/.telegram-agent-bot/.env
systemctl --user daemon-reload
systemctl --user enable --now io.github.telegramagentbot.service
```

Allow the service to remain active without an interactive login session:

```bash
sudo loginctl enable-linger "$USER"
```

### Status and logs

```bash
systemctl --user status io.github.telegramagentbot.service --no-pager
journalctl --user -u io.github.telegramagentbot.service -n 100 --no-pager
tail -n 100 ~/.telegram-agent-bot/logs/telegram-agent-bot.err.log
```

The service should run as the user who owns the agent CLI credentials, project
files, tmux session, and transcript directory.

## macOS

```bash
git clone https://github.com/Pigbibi/TelegramAgentBot.git
cd TelegramAgentBot
./scripts/bootstrap-macos.sh
```

The script creates the application configuration, a launch wrapper, and:

```text
~/Library/LaunchAgents/io.github.telegramagentbot.plist
```

After configuring `.env`, load and start the service:

```bash
launchctl bootstrap "gui/$(id -u)" \
  ~/Library/LaunchAgents/io.github.telegramagentbot.plist
launchctl kickstart -k "gui/$(id -u)/io.github.telegramagentbot"
```

Inspect it with:

```bash
launchctl print "gui/$(id -u)/io.github.telegramagentbot"
tail -n 100 ~/.telegram-agent-bot/logs/telegram-agent-bot.err.log
```

## Manual installation

For interactive use without the service helpers:

```bash
git clone https://github.com/Pigbibi/TelegramAgentBot.git
cd TelegramAgentBot
uv sync
cp .env.example ~/.telegram-agent-bot/.env
uv run telegram-agent-bot hook --install
uv run telegram-agent-bot
```

The package can also be installed as a tool:

```bash
uv tool install git+https://github.com/Pigbibi/TelegramAgentBot.git
```

Tool installs are suitable when another supervisor owns the service definition.
The repository bootstrap and source self-update commands require a Git checkout.

## Upgrades

Before upgrading, confirm that the checkout is clean and that no important
agent turn is waiting on an interactive prompt.

Manual source upgrade:

```bash
cd ~/.telegram-agent-bot/app/TelegramAgentBot
git pull --ff-only
uv sync
```

Restart the bot only when active queues and tmux-backed tasks are safe to
interrupt at the controller layer:

```bash
systemctl --user restart io.github.telegramagentbot.service
```

The tmux windows continue running across an ordinary bot-service restart, but
messages in flight should still be allowed to settle first.

Source installations may enable idle-only updates in `.env`:

```ini
TELEGRAM_AGENT_BOT_AUTO_UPDATE=true
TELEGRAM_AGENT_BOT_UPDATE_REQUIRE_IDLE=true
TELEGRAM_AGENT_BOT_UPDATE_MAX_BUSY_DEFERRAL_SECONDS=1800
```

Use `telegram-agent-bot update --check` to inspect availability without applying
an update. A positive busy-deferral limit allows the AgentBot source checkout to
update and restart after the deadline while preserving active tmux agents and
durable message state. Agent CLI package updates still wait for full idleness.
See [Configuration](configuration.md#updates) for all update options.

## Small VPS settings

For a host with about 2 GB RAM and 2 vCPUs:

```ini
TELEGRAM_AGENT_BOT_MAX_CONCURRENT_UPDATES=4
TELEGRAM_AGENT_BOT_MAX_ACTIVE_TURNS=2
TELEGRAM_AGENT_BOT_AGENT_INPUT_QUEUE_MAX_SIZE=20
TELEGRAM_AGENT_BOT_IDLE_SESSION_TIMEOUT_SECONDS=1800
```

Keep builds and test suites on GitHub-hosted runners or another development
machine. The bot's health monitor reports memory, swap, disk, queue age, and
transcript lag; `/health` provides a compact snapshot.

The optional [VPS cleanup timer](vps_cleanup.md) reclaims selected rebuildable
artifacts without removing the AgentBot checkout or active uv runtimes.

## Troubleshooting

### The service exits immediately

Check the journal and confirm:

- `TELEGRAM_BOT_TOKEN` is not a placeholder;
- every value in `ALLOWED_USERS` is numeric;
- the selected agent command is available in the service `PATH`;
- the service user can read `.env` and write `$TELEGRAM_AGENT_BOT_DIR`;
- no second TelegramAgentBot process holds the instance lock.

### A new topic never reaches the agent

1. Open `/health` and inspect queue age and transcript lag.
2. Attach to the managed tmux session and look for an approval, login, hook
   trust, or model-selection prompt.
3. Confirm the agent CLI starts normally as the service user.
4. Confirm the selected project path is below an allowed project root.
5. Check the service log for startup timeout or tmux socket errors.

For a private socket:

```bash
tmux -S ~/.telegram-agent-bot/tmux/telegram-agent-bot \
  attach -t telegram-agent-bot
```

### Telegram shows repeated status updates

`TELEGRAM_AGENT_BOT_STATUS_REPOST_INTERVAL` controls how often a long-running
status is sent as a new message. It defaults to `0`, which edits the existing
status only. Choose a larger interval only when fresh topic bumps are worth the
risk of leaving an older status visible after an ambiguous Telegram response.

Host alerts have a separate cooldown controlled by
`TELEGRAM_AGENT_BOT_HEALTH_ALERT_COOLDOWN_SECONDS`. Alert state survives service
restarts.

### Voice transcription fails

Confirm that at least one configured provider has a valid API key and that the
service can reach its API endpoint. Providers are tried in the order listed in
`AI_TRANSCRIPTION_PROVIDERS`.

### Sessions do not appear in the resume picker

Run the hook installer as the service user:

```bash
uv run telegram-agent-bot hook --install
```

Then start or resume the CLI session inside the managed tmux server. Sessions
created outside AgentBot are hidden unless
`TELEGRAM_AGENT_BOT_SHOW_EXTERNAL_RESUME_SESSIONS=true` is set.

## Backup

Stop the controller before taking a consistent application-state backup. Back
up `$TELEGRAM_AGENT_BOT_DIR` with owner-only permissions. The directory can
contain secrets and account material.

Agent transcripts remain under the selected Codex or Claude Code home and must
be backed up separately when needed.
