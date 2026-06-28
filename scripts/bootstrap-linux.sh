#!/usr/bin/env bash
set -euo pipefail

if [[ "$(uname -s)" != "Linux" ]]; then
  echo "This helper currently targets Linux only."
  exit 1
fi

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TELEGRAM_AGENT_BOT_DIR="${TELEGRAM_AGENT_BOT_DIR:-$HOME/.telegram-agent-bot}"
ENV_PATH="${TELEGRAM_AGENT_BOT_DIR}/.env"
BIN_DIR="${TELEGRAM_AGENT_BOT_DIR}/bin"
LOG_DIR="${TELEGRAM_AGENT_BOT_DIR}/logs"
SYSTEMD_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
SERVICE_NAME="${TELEGRAM_AGENT_BOT_SYSTEMD_SERVICE_NAME:-io.github.telegramagentbot.service}"
SERVICE_PATH="${SYSTEMD_DIR}/${SERVICE_NAME}"
LAUNCHER_PATH="${BIN_DIR}/telegram-agent-bot-launch"
PATH_VALUE="/usr/local/bin:/usr/bin:/bin:${HOME}/.local/bin"

require_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "Missing required command: $1"
    exit 1
  fi
}

require_cmd uv
require_cmd tmux
# Detect agent type from existing .env, default to codex
_agent_type=""
if [[ -f "$ENV_PATH" ]]; then
  _agent_type="$(grep -E '^TELEGRAM_AGENT_BOT_AGENT_TYPE=' "$ENV_PATH" | tail -1 | sed 's/.*=//')"
fi
if [[ "$_agent_type" == "claude" ]]; then
  require_cmd claude
else
  require_cmd codex
fi
require_cmd python3

mkdir -p "$BIN_DIR" "$LOG_DIR" "$SYSTEMD_DIR"

if [[ ! -f "$ENV_PATH" ]]; then
  cp "$REPO_DIR/.env.example" "$ENV_PATH"
  echo "Created $ENV_PATH from .env.example"
else
  echo "Keeping existing $ENV_PATH"
fi

read_env_value() {
  local key="$1"
  local default_value="$2"
  local line=""
  local value=""

  line="$(grep -E "^${key}=" "$ENV_PATH" | tail -n 1 || true)"
  if [[ -z "$line" ]]; then
    printf '%s' "$default_value"
    return
  fi

  value="${line#*=}"
  value="${value%$'\r'}"
  value="${value%\"}"
  value="${value#\"}"
  value="${value%\'}"
  value="${value#\'}"
  printf '%s' "$value"
}

normalize_path() {
  local path="$1"

  if [[ "$path" == "~" ]]; then
    path="$HOME"
  elif [[ "$path" == "~/"* ]]; then
    path="${HOME}/${path#~/}"
  elif [[ "$path" != /* ]]; then
    path="$(pwd -P)/$path"
  fi

  python3 - "$path" <<'PY'
import sys
from pathlib import Path

print(Path(sys.argv[1]).resolve(strict=False))
PY
}

is_under_or_same() {
  local child="$1"
  local parent="$2"

  [[ "$child" == "$parent" || "$child" == "$parent/"* ]]
}

truthy() {
  local normalized
  normalized="$(printf '%s' "$1" | tr '[:upper:]' '[:lower:]')"
  case "$normalized" in
    1|true|yes|on|y) return 0 ;;
    *) return 1 ;;
  esac
}

check_runtime_checkout_location() {
  local allow
  local repo_path
  local default_root
  local project_roots
  local root_entry
  local root_path
  local unsafe_root=""
  local -a root_entries=()

  allow="${TELEGRAM_AGENT_BOT_ALLOW_PROJECTS_CHECKOUT:-$(read_env_value TELEGRAM_AGENT_BOT_ALLOW_PROJECTS_CHECKOUT false)}"
  if truthy "$allow"; then
    return
  fi

  repo_path="$(normalize_path "$REPO_DIR")"
  default_root="$(read_env_value TELEGRAM_AGENT_BOT_DEFAULT_PROJECTS_PATH "$HOME/Projects")"
  project_roots="$(read_env_value TELEGRAM_AGENT_BOT_PROJECT_ROOTS "")"

  root_path="$(normalize_path "$default_root")"
  if is_under_or_same "$repo_path" "$root_path"; then
    unsafe_root="$root_path"
  fi

  if [[ -n "$project_roots" ]]; then
    IFS=',' read -ra root_entries <<<"$project_roots"
    for root_entry in "${root_entries[@]}"; do
      root_entry="${root_entry#*=}"
      [[ -z "$root_entry" ]] && continue
      root_path="$(normalize_path "$root_entry")"
      if is_under_or_same "$repo_path" "$root_path"; then
        unsafe_root="$root_path"
        break
      fi
    done
  fi

  if [[ -n "$unsafe_root" ]]; then
    cat >&2 <<EOF
Unsafe TelegramAgentBot checkout location:
  checkout:     $repo_path
  project root: $unsafe_root

The systemd launcher points to this checkout. If a Codex session cleans the
project root, the bot can keep running from deleted files and fail on restart.

Clone TelegramAgentBot into a durable runtime path outside project roots, for example:
  mkdir -p "$TELEGRAM_AGENT_BOT_DIR/app"
  git clone https://github.com/Pigbibi/TelegramAgentBot.git "$TELEGRAM_AGENT_BOT_DIR/app/TelegramAgentBot"
  cd "$TELEGRAM_AGENT_BOT_DIR/app/TelegramAgentBot"
  ./scripts/bootstrap-linux.sh

To intentionally allow this unsafe layout, set:
  TELEGRAM_AGENT_BOT_ALLOW_PROJECTS_CHECKOUT=true
EOF
    exit 1
  fi
}

check_runtime_checkout_location

cat >"$LAUNCHER_PATH" <<EOF
#!/usr/bin/env bash
export PATH="$PATH_VALUE"
export HOME="$HOME"
cd "$REPO_DIR"
exec /usr/bin/env uv run telegram-agent-bot "\$@"
EOF
chmod +x "$LAUNCHER_PATH"

cat >"$SERVICE_PATH" <<EOF
[Unit]
Description=TelegramAgentBot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$TELEGRAM_AGENT_BOT_DIR
ExecStart=$LAUNCHER_PATH
Restart=always
RestartSec=3
KillMode=process
Environment=PATH=$PATH_VALUE
Environment=HOME=$HOME
StandardOutput=append:$LOG_DIR/telegram-agent-bot.out.log
StandardError=append:$LOG_DIR/telegram-agent-bot.err.log

[Install]
WantedBy=default.target
EOF

cd "$REPO_DIR"
uv sync
uv run telegram-agent-bot hook --install

token_line="$(grep -E '^TELEGRAM_BOT_TOKEN=' "$ENV_PATH" || true)"
user_line="$(grep -E '^ALLOWED_USERS=' "$ENV_PATH" || true)"
token_ready=1
user_ready=1
started="no"

if [[ -z "$token_line" || "$token_line" == "TELEGRAM_BOT_TOKEN=your_bot_token_here" ]]; then
  token_ready=0
fi

if [[ -z "$user_line" || "$user_line" == "ALLOWED_USERS=123456789,987654321" ]]; then
  user_ready=0
fi

if command -v systemctl >/dev/null 2>&1; then
  if [[ "$token_ready" -eq 1 && "$user_ready" -eq 1 ]]; then
    systemctl --user daemon-reload
    systemctl --user enable --now "$SERVICE_NAME"
    started="yes"
  fi
else
  echo "systemctl not found; service file was created but not started."
fi

cat <<EOF

Bootstrap complete.

Paths:
  env:        $ENV_PATH
  launcher:   $LAUNCHER_PATH
  service:    $SERVICE_PATH

Next steps:
  1. Edit $ENV_PATH
     - TELEGRAM_BOT_TOKEN
     - ALLOWED_USERS
     - optional OPENAI_API_KEY / OPENAI_BASE_URL
  2. Run: codex login
  3. Optional Telegram recovery commands:
     /codexlogin
     /codexlogin backup
     /codexaccount list
     /codexaccount use backup

Service started automatically: $started
EOF

if [[ "$started" == "no" ]]; then
  cat <<EOF

If the service is not running yet, use:
  systemctl --user daemon-reload
  systemctl --user enable --now "$SERVICE_NAME"

If you want the user service to survive reboot on a VPS, run once:
  sudo loginctl enable-linger "$USER"
EOF
fi
