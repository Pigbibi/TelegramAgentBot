# VPS Cleanup Timer

TelegramAgentBot hosts often accumulate GitHub Actions runner workspaces,
language package caches, browser test artifacts, and old temporary files. This
cleanup helper keeps the bot runtime intact while reclaiming rebuildable files.

## Safety Model

The cleanup command refuses destructive work unless `--yes` is passed. Use
`--dry-run --force` to preview removals.

Protected paths are never removed:

- `$TELEGRAM_AGENT_BOT_DIR` or `~/.telegram-agent-bot`
- `$TELEGRAM_AGENT_BOT_DIR/app/TelegramAgentBot`
- `~/Projects/TelegramAgentBot`
- comma-separated paths in `TELEGRAM_AGENT_BOT_CLEANUP_PROTECTED_PATHS`

GitHub Actions runner directories are discovered from `~/actions-runner-*` or
from `TELEGRAM_AGENT_BOT_CLEANUP_RUNNER_ROOTS`. Runner service binaries are kept.
The helper only removes:

- `_work/*`, but only when no `Runner.Worker` process is active
- old `_diag` files
- old self-update `bin.*` / `externals.*` directories that are not current
  symlink targets

Common cache cleanup includes:

- `~/.npm/_cacache`, `~/.npm/_npx`, npm logs
- selected Gradle caches (`caches`, `daemon`, `native`, `.tmp`)
- `~/.cache/ms-playwright`, `~/.cache/uv`
- `~/.codex/.tmp`
- old Codex session files past the configured retention window
- old `/tmp` artifacts, excluding system sockets, systemd-private dirs, and bot
  lock directories

System cache cleanup is best-effort:

- `sudo -n apt-get clean`
- `sudo -n journalctl --vacuum-size=100M`

If passwordless sudo is unavailable, those steps are skipped.

## Thresholds

By default the timer runs cleanup only when disk pressure is high:

- `TELEGRAM_AGENT_BOT_CLEANUP_MAX_USED_PERCENT=80`
- `TELEGRAM_AGENT_BOT_CLEANUP_MIN_FREE_GB=6`

The command can be forced manually:

```bash
~/.telegram-agent-bot/bin/telegram-agent-cleanup --dry-run --force
~/.telegram-agent-bot/bin/telegram-agent-cleanup --force
```

## Install

From the TelegramAgentBot checkout:

```bash
./scripts/install-vps-cleanup.sh
```

This writes a user systemd service and timer:

- `~/.config/systemd/user/io.github.telegramagentbot.cleanup.service`
- `~/.config/systemd/user/io.github.telegramagentbot.cleanup.timer`
- `~/.telegram-agent-bot/bin/telegram-agent-cleanup`

The default schedule is daily at `04:20` with up to `30m` randomized delay.

Useful commands:

```bash
systemctl --user list-timers io.github.telegramagentbot.cleanup.timer
systemctl --user start io.github.telegramagentbot.cleanup.service
journalctl --user -u io.github.telegramagentbot.cleanup.service -n 100
```
