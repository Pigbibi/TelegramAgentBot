# VPS cleanup timer

TelegramAgentBot includes an optional Linux user timer for reclaiming selected
rebuildable files on small hosts. It is conservative by default and runs only
when disk pressure crosses a configured threshold.

The cleanup helper is not a general disk cleaner. It operates on an explicit
set of caches, temporary files, transcripts, and GitHub Actions runner
artifacts.

## Install

From a source checkout:

```bash
./scripts/install-vps-cleanup.sh
```

The installer writes:

- `~/.telegram-agent-bot/bin/telegram-agent-cleanup`;
- `~/.config/systemd/user/io.github.telegramagentbot.cleanup.service`;
- `~/.config/systemd/user/io.github.telegramagentbot.cleanup.timer`.

The default schedule is daily at 04:20 with up to 30 minutes of randomized
delay.

Check the timer:

```bash
systemctl --user list-timers io.github.telegramagentbot.cleanup.timer
systemctl --user status io.github.telegramagentbot.cleanup.timer --no-pager
```

## Preview and run

The cleanup command requires explicit confirmation for destructive work.
Preview the exact targets first:

```bash
~/.telegram-agent-bot/bin/telegram-agent-cleanup --dry-run --force
```

Run even when disk thresholds are healthy:

```bash
~/.telegram-agent-bot/bin/telegram-agent-cleanup --force
```

Run the installed systemd service and inspect its log:

```bash
systemctl --user start io.github.telegramagentbot.cleanup.service
journalctl --user -u io.github.telegramagentbot.cleanup.service \
  -n 100 --no-pager
```

## Disk thresholds

Cleanup is triggered when either threshold is crossed:

```ini
TELEGRAM_AGENT_BOT_CLEANUP_MAX_USED_PERCENT=80
TELEGRAM_AGENT_BOT_CLEANUP_MIN_FREE_GB=6
```

Use `--force` only for an operator-requested cleanup or a tested maintenance
procedure.

## Protected paths

The helper refuses to remove:

- `$TELEGRAM_AGENT_BOT_DIR`;
- `$TELEGRAM_AGENT_BOT_DIR/app/TelegramAgentBot`;
- `~/Projects/TelegramAgentBot`;
- paths listed in `TELEGRAM_AGENT_BOT_CLEANUP_PROTECTED_PATHS`.

Add every nonstandard checkout, state directory, mount point, and irreplaceable
workspace that may overlap a cleanup root:

```ini
TELEGRAM_AGENT_BOT_CLEANUP_PROTECTED_PATHS=/srv/agentbot,/mnt/archive
```

Protection checks resolve paths before comparing them. Do not depend on naming
alone to protect valuable data.

## GitHub Actions runners

Runner roots are discovered below `~/actions-runner-*` or provided explicitly:

```ini
TELEGRAM_AGENT_BOT_CLEANUP_RUNNER_ROOTS=/srv/runner-one,/srv/runner-two
```

The runner installation and registration files are retained. Cleanup is limited
to:

- `_work/*` when no `Runner.Worker` process is active;
- old `_diag` files;
- inactive self-update `bin.*` and `externals.*` directories that are not the
  current symlink targets.

The helper does not unregister or remove a runner installation. Use GitHub's
runner removal procedure before deleting an obsolete runner directory.

## Cache and temporary-file scope

The cleanup may process:

- npm download cache, npx cache, and old npm logs;
- selected Gradle `caches`, `daemon`, `native`, and `.tmp` paths;
- Playwright browser cache;
- unreachable uv cache objects through `uv cache prune`;
- Codex temporary files and transcripts past the configured retention period;
- old `/tmp` artifacts outside protected system and AgentBot paths.

The uv cache is left unchanged when a running process references a runtime below
that cache. The prune command is bounded to 120 seconds.

System cache commands are best effort:

```text
sudo -n apt-get clean
sudo -n journalctl --vacuum-size=100M
```

They are skipped when passwordless sudo is not available.

## Before enabling the timer

1. Run `--dry-run --force`.
2. Confirm every discovered runner root belongs to this host.
3. Add custom protected paths.
4. Verify transcript retention matches your backup policy.
5. Run one manual cleanup and inspect the journal.
6. Enable the timer only after the manual result is understood.

Keep source repositories, credentials, and persistent application state in
backups. Rebuildable does not mean automatically recoverable when an external
package or runner release becomes unavailable.
