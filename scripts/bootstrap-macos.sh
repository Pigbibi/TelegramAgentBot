#!/usr/bin/env bash
set -euo pipefail

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "This helper currently targets macOS only."
  exit 1
fi

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TELEGRAM_CODEX_BOT_DIR="${TELEGRAM_CODEX_BOT_DIR:-$HOME/.telegram-codex-bot}"
ENV_PATH="${TELEGRAM_CODEX_BOT_DIR}/.env"
BIN_DIR="${TELEGRAM_CODEX_BOT_DIR}/bin"
LOG_DIR="${TELEGRAM_CODEX_BOT_DIR}/logs"
LAUNCH_AGENT_LABEL="${TELEGRAM_CODEX_BOT_LAUNCH_AGENT_LABEL:-io.github.telegramcodexbot}"
PLIST_PATH="$HOME/Library/LaunchAgents/${LAUNCH_AGENT_LABEL}.plist"
LAUNCHER_PATH="${BIN_DIR}/telegram-codex-bot-launch"
PATH_VALUE="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:${HOME}/.local/bin"

require_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "Missing required command: $1"
    exit 1
  fi
}

require_cmd uv
require_cmd tmux
require_cmd codex
require_cmd plutil
require_cmd launchctl

mkdir -p "$BIN_DIR" "$LOG_DIR" "$HOME/Library/LaunchAgents"

if [[ ! -f "$ENV_PATH" ]]; then
  cp "$REPO_DIR/.env.example" "$ENV_PATH"
  echo "Created $ENV_PATH from .env.example"
else
  echo "Keeping existing $ENV_PATH"
fi

cat >"$LAUNCHER_PATH" <<EOF
#!/bin/zsh
export PATH="$PATH_VALUE"
export HOME="$HOME"
cd "$REPO_DIR"
exec /usr/bin/env uv run telegram-codex-bot "\$@"
EOF
chmod +x "$LAUNCHER_PATH"

cat >"$PLIST_PATH" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>$LAUNCH_AGENT_LABEL</string>

  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/caffeinate</string>
    <string>-s</string>
    <string>-i</string>
    <string>$LAUNCHER_PATH</string>
  </array>

  <key>RunAtLoad</key>
  <true/>

  <key>KeepAlive</key>
  <true/>

  <key>WorkingDirectory</key>
  <string>$TELEGRAM_CODEX_BOT_DIR</string>

  <key>ProcessType</key>
  <string>Background</string>

  <key>StandardOutPath</key>
  <string>$LOG_DIR/telegram-codex-bot.out.log</string>
  <key>StandardErrorPath</key>
  <string>$LOG_DIR/telegram-codex-bot.err.log</string>

  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key>
    <string>$PATH_VALUE</string>
    <key>HOME</key>
    <string>$HOME</string>
  </dict>
</dict>
</plist>
EOF

plutil -lint "$PLIST_PATH" >/dev/null

cd "$REPO_DIR"
uv sync
uv run telegram-codex-bot hook --install

token_line="$(grep -E '^TELEGRAM_BOT_TOKEN=' "$ENV_PATH" || true)"
user_line="$(grep -E '^ALLOWED_USERS=' "$ENV_PATH" || true)"
token_ready=1
user_ready=1

if [[ -z "$token_line" || "$token_line" == "TELEGRAM_BOT_TOKEN=your_bot_token_here" ]]; then
  token_ready=0
fi

if [[ -z "$user_line" || "$user_line" == "ALLOWED_USERS=123456789,987654321" ]]; then
  user_ready=0
fi

if [[ "$token_ready" -eq 1 && "$user_ready" -eq 1 ]]; then
  launchctl bootout "gui/$(id -u)" "$PLIST_PATH" >/dev/null 2>&1 || true
  launchctl bootstrap "gui/$(id -u)" "$PLIST_PATH"
  launchctl kickstart -k "gui/$(id -u)/$LAUNCH_AGENT_LABEL"
  started="yes"
else
  started="no"
fi

cat <<EOF

Bootstrap complete.

Paths:
  env:         $ENV_PATH
  launcher:    $LAUNCHER_PATH
  launchd:     $PLIST_PATH

Next steps:
  1. Edit $ENV_PATH
     - TELEGRAM_BOT_TOKEN
     - ALLOWED_USERS
     - optional OPENAI_API_KEY / OPENAI_BASE_URL
  2. Run: codex login
  3. Optional multi-account:
     ~/.telegram-codex-bot/bin/codex-account save main
     ~/.telegram-codex-bot/bin/codex-account save backup
     ~/.telegram-codex-bot/bin/codex-account use main

Service started automatically: $started
EOF

if [[ "$started" == "no" ]]; then
  cat <<EOF

Placeholder values are still present in .env, so launchd was not started.
After editing .env, run:
  launchctl bootstrap "gui/\$(id -u)" "$PLIST_PATH"
  launchctl kickstart -k "gui/\$(id -u)/$LAUNCH_AGENT_LABEL"
EOF
fi
