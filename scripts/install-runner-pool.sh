#!/usr/bin/env bash
set -euo pipefail

if [[ "$(uname -s)" != "Linux" ]]; then
  echo "This helper targets Linux only."
  exit 1
fi

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_RUNTIME_DIR="${TELEGRAM_AGENT_BOT_DIR:-$HOME/.telegram-agent-bot}"
BIN_DIR="${APP_RUNTIME_DIR}/bin"
LOG_DIR="${APP_RUNTIME_DIR}/logs"
ROOTS_FILE="${APP_RUNTIME_DIR}/runner_pool_roots.conf"
STATE_FILE="${APP_RUNTIME_DIR}/runner_pool_state.json"
SYSTEMD_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
SERVICE_NAME="${TELEGRAM_AGENT_BOT_RUNNER_POOL_SERVICE_NAME:-io.github.telegramagentbot.runner-pool.service}"
TIMER_NAME="${SERVICE_NAME%.service}.timer"
SERVICE_PATH="${SYSTEMD_DIR}/${SERVICE_NAME}"
TIMER_PATH="${SYSTEMD_DIR}/${TIMER_NAME}"
LAUNCHER_PATH="${BIN_DIR}/telegram-agent-runner-pool"
PATH_VALUE="/usr/local/bin:/usr/bin:/bin:${HOME}/.local/bin"
POLL_SECONDS="${TELEGRAM_AGENT_BOT_RUNNER_POOL_POLL_SECONDS:-180}"
IDLE_SECONDS="${TELEGRAM_AGENT_BOT_RUNNER_IDLE_TIMEOUT_SECONDS:-600}"

require_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "Missing required command: $1"
    exit 1
  fi
}

require_cmd gh
require_cmd jq
require_cmd sudo
require_cmd systemctl
require_cmd uv
sudo -n true
gh auth status >/dev/null

mkdir -p "$BIN_DIR" "$LOG_DIR" "$SYSTEMD_DIR"

runner_roots=()
if [[ -n "${TELEGRAM_AGENT_BOT_RUNNER_POOL_ROOTS:-}" ]]; then
  IFS=',' read -r -a runner_roots <<<"$TELEGRAM_AGENT_BOT_RUNNER_POOL_ROOTS"
elif [[ -f "$ROOTS_FILE" ]]; then
  mapfile -t runner_roots <"$ROOTS_FILE"
else
  for service_file in "$HOME"/actions-runner-*/.service; do
    [[ -f "$service_file" ]] || continue
    service_name="$(tr -d '\r\n' <"$service_file")"
    if systemctl is-enabled --quiet "$service_name"; then
      runner_roots+=("$(dirname "$service_file")")
    fi
  done
fi

if [[ "${#runner_roots[@]}" -eq 0 ]]; then
  echo "No enabled GitHub Actions runner roots found."
  exit 1
fi

roots_tmp="$(mktemp)"
trap 'rm -f "$roots_tmp"' EXIT
for root in "${runner_roots[@]}"; do
  root="${root/#\~/$HOME}"
  [[ -d "$root" ]] || {
    echo "Runner root does not exist: $root"
    exit 1
  }
  root="$(cd "$root" && pwd)"
  repository_url="$(jq -r '.gitHubUrl // empty' "$root/.runner")"
  repository="${repository_url#https://github.com/}"
  if gh api --method GET "repos/$repository/actions/runs" \
    -f status=queued -f per_page=1 --jq '.total_count' >/dev/null 2>&1; then
    printf '%s\n' "$root" >>"$roots_tmp"
  else
    echo "Leaving runner always-on because gh cannot inspect its queue: $repository"
  fi
done
if [[ ! -s "$roots_tmp" ]]; then
  echo "No runner repositories are visible to the active gh account; nothing changed."
  exit 1
fi
install -m 0600 "$roots_tmp" "$ROOTS_FILE"

launcher_tmp="$(mktemp)"
cat >"$launcher_tmp" <<EOF
#!/usr/bin/env bash
set -euo pipefail
export PATH="$PATH_VALUE"
export TELEGRAM_AGENT_BOT_DIR="$APP_RUNTIME_DIR"
runner_args=()
while IFS= read -r runner_root; do
  [[ -n "\$runner_root" ]] || continue
  runner_args+=(--runner-root "\$runner_root")
done <"$ROOTS_FILE"
cd "$REPO_DIR"
exec /usr/bin/env uv run python -m telegram_agent_bot.runner_pool \\
  --once \\
  --idle-timeout-seconds "$IDLE_SECONDS" \\
  --state-file "$STATE_FILE" \\
  "\${runner_args[@]}" "\$@"
EOF
install -m 0755 "$launcher_tmp" "$LAUNCHER_PATH"
rm -f "$launcher_tmp"

service_tmp="$(mktemp)"
cat >"$service_tmp" <<EOF
[Unit]
Description=On-demand GitHub Actions runner pool for TelegramAgentBot host
Documentation=https://github.com/Pigbibi/TelegramAgentBot
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
WorkingDirectory=$REPO_DIR
ExecStart=$LAUNCHER_PATH
Environment=PATH=$PATH_VALUE
Nice=10
IOSchedulingClass=best-effort
IOSchedulingPriority=7
StandardOutput=append:$LOG_DIR/runner-pool.out.log
StandardError=append:$LOG_DIR/runner-pool.err.log
EOF
install -m 0644 "$service_tmp" "$SERVICE_PATH"
rm -f "$service_tmp"

timer_tmp="$(mktemp)"
cat >"$timer_tmp" <<EOF
[Unit]
Description=Poll GitHub queues for on-demand self-hosted runners

[Timer]
OnBootSec=2m
OnUnitActiveSec=${POLL_SECONDS}s
RandomizedDelaySec=15s
Persistent=true
Unit=$SERVICE_NAME

[Install]
WantedBy=timers.target
EOF
install -m 0644 "$timer_tmp" "$TIMER_PATH"
rm -f "$timer_tmp"

"$LAUNCHER_PATH" --dry-run
"$LAUNCHER_PATH"

systemctl --user daemon-reload
systemctl --user enable --now "$TIMER_NAME"

while IFS= read -r runner_root; do
  service_name="$(tr -d '\r\n' <"$runner_root/.service")"
  sudo -n systemctl disable "$service_name"
done <"$ROOTS_FILE"

cat <<EOF
On-demand runner pool installed.

Managed roots: $ROOTS_FILE
Timer:         $TIMER_NAME (every ${POLL_SECONDS}s)
Idle grace:    ${IDLE_SECONDS}s
Logs:          $LOG_DIR/runner-pool.*.log

Runner listeners remain online for the idle grace, then stop. Queued GitHub
workflow runs wake the matching repository runner on the next timer tick.

Rollback:
  systemctl --user disable --now "$TIMER_NAME"
  while read -r root; do sudo systemctl enable --now "\$(cat "\$root/.service")"; done < "$ROOTS_FILE"
EOF
