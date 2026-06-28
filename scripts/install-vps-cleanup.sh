#!/usr/bin/env bash
set -euo pipefail

if [[ "$(uname -s)" != "Linux" ]]; then
  echo "This helper targets Linux only."
  exit 1
fi

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TELEGRAM_AGENT_BOT_DIR="${TELEGRAM_AGENT_BOT_DIR:-$HOME/.telegram-agent-bot}"
BIN_DIR="${TELEGRAM_AGENT_BOT_DIR}/bin"
LOG_DIR="${TELEGRAM_AGENT_BOT_DIR}/logs"
SYSTEMD_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
SERVICE_NAME="${TELEGRAM_AGENT_BOT_CLEANUP_SYSTEMD_SERVICE_NAME:-io.github.telegramagentbot.cleanup.service}"
TIMER_NAME="${SERVICE_NAME%.service}.timer"
SERVICE_PATH="${SYSTEMD_DIR}/${SERVICE_NAME}"
TIMER_PATH="${SYSTEMD_DIR}/${TIMER_NAME}"
LAUNCHER_PATH="${BIN_DIR}/telegram-agent-cleanup"
PATH_VALUE="/usr/local/bin:/usr/bin:/bin:${HOME}/.local/bin"

CLEANUP_ON_CALENDAR="${TELEGRAM_AGENT_BOT_CLEANUP_ON_CALENDAR:-*-*-* 04:20:00}"
CLEANUP_RANDOMIZED_DELAY="${TELEGRAM_AGENT_BOT_CLEANUP_RANDOMIZED_DELAY:-30m}"
CLEANUP_MAX_USED_PERCENT="${TELEGRAM_AGENT_BOT_CLEANUP_MAX_USED_PERCENT:-80}"
CLEANUP_MIN_FREE_GB="${TELEGRAM_AGENT_BOT_CLEANUP_MIN_FREE_GB:-6}"
CLEANUP_TMP_RETENTION_DAYS="${TELEGRAM_AGENT_BOT_CLEANUP_TMP_RETENTION_DAYS:-2}"
CLEANUP_CODEX_SESSION_RETENTION_DAYS="${TELEGRAM_AGENT_BOT_CLEANUP_CODEX_SESSION_RETENTION_DAYS:-30}"
CLEANUP_RUNNER_DIAG_RETENTION_DAYS="${TELEGRAM_AGENT_BOT_CLEANUP_RUNNER_DIAG_RETENTION_DAYS:-7}"

require_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "Missing required command: $1"
    exit 1
  fi
}

require_cmd uv
require_cmd systemctl

mkdir -p "$BIN_DIR" "$LOG_DIR" "$SYSTEMD_DIR"

cat >"$LAUNCHER_PATH" <<EOF
#!/usr/bin/env bash
set -euo pipefail
export PATH="$PATH_VALUE"
export HOME="$HOME"
export TELEGRAM_AGENT_BOT_DIR="$TELEGRAM_AGENT_BOT_DIR"
cd "$REPO_DIR"
exec /usr/bin/env uv run python -m telegram_agent_bot.maintenance_cleanup \\
  --yes \\
  --max-used-percent "$CLEANUP_MAX_USED_PERCENT" \\
  --min-free-gb "$CLEANUP_MIN_FREE_GB" \\
  --tmp-retention-days "$CLEANUP_TMP_RETENTION_DAYS" \\
  --codex-session-retention-days "$CLEANUP_CODEX_SESSION_RETENTION_DAYS" \\
  --runner-diag-retention-days "$CLEANUP_RUNNER_DIAG_RETENTION_DAYS" \\
  "\$@"
EOF
chmod +x "$LAUNCHER_PATH"

cat >"$SERVICE_PATH" <<EOF
[Unit]
Description=TelegramAgentBot VPS cleanup
Documentation=https://github.com/Pigbibi/TelegramAgentBot

[Service]
Type=oneshot
WorkingDirectory=$REPO_DIR
ExecStart=$LAUNCHER_PATH
Environment=PATH=$PATH_VALUE
Environment=HOME=$HOME
Nice=10
IOSchedulingClass=best-effort
IOSchedulingPriority=7
StandardOutput=append:$LOG_DIR/cleanup.out.log
StandardError=append:$LOG_DIR/cleanup.err.log
EOF

cat >"$TIMER_PATH" <<EOF
[Unit]
Description=Run TelegramAgentBot VPS cleanup

[Timer]
OnCalendar=$CLEANUP_ON_CALENDAR
RandomizedDelaySec=$CLEANUP_RANDOMIZED_DELAY
Persistent=true
Unit=$SERVICE_NAME

[Install]
WantedBy=timers.target
EOF

systemctl --user daemon-reload
systemctl --user enable --now "$TIMER_NAME"

cat <<EOF
Cleanup timer installed.

Paths:
  launcher: $LAUNCHER_PATH
  service:  $SERVICE_PATH
  timer:    $TIMER_PATH
  logs:     $LOG_DIR/cleanup.*.log

Schedule:
  OnCalendar=$CLEANUP_ON_CALENDAR
  RandomizedDelaySec=$CLEANUP_RANDOMIZED_DELAY

Thresholds:
  max used percent: $CLEANUP_MAX_USED_PERCENT
  min free GB:      $CLEANUP_MIN_FREE_GB

Useful commands:
  systemctl --user list-timers "$TIMER_NAME"
  systemctl --user start "$SERVICE_NAME"
  "$LAUNCHER_PATH" --dry-run --force
EOF
