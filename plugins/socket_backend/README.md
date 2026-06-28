# TelegramAgentBot Socket Backend

Optional backend plugin for running TelegramAgentBot as a center bot with one or
more remote agent nodes.

The default TelegramAgentBot install still uses local tmux. Install this plugin
only on machines that need the socket cluster mode.

## Install

From the main repository checkout:

```bash
pip install -e plugins/socket_backend
```

## Agent Node

Run this on the machine that should host Codex/tmux sessions:

```bash
telegram-codex-agent-node --node-id macbook --host 127.0.0.1 --port 8765
```

For image/file uploads, both sides default to a 25 MiB JSON line limit. You can
raise or lower it with:

```bash
telegram-codex-agent-node --node-id macbook --max-message-bytes 26214400
```

If the node is behind NAT, expose it to the VPS center bot with reverse SSH:

```bash
ssh -N -R 127.0.0.1:8765:127.0.0.1:8765 ubuntu@your-vps
```

## Center Bot

Configure the VPS bot:

```ini
TELEGRAM_CODEX_BOT_BACKEND=socket-cluster
TELEGRAM_CODEX_BOT_BACKEND_PLUGINS=telegram_codex_bot_socket_backend
TELEGRAM_CODEX_BOT_SOCKET_NODES=macbook=127.0.0.1:8765
TELEGRAM_CODEX_BOT_SOCKET_MAX_MESSAGE_BYTES=26214400
```

Then restart `telegram-codex-bot`.

## Service Examples

Example service files live under `examples/`:

- `examples/systemd/telegram-codex-bot.socket-center.service`
- `examples/systemd/telegram-codex-agent-node.service`
- `examples/systemd/socket-center.env.example`
- `examples/launchd/io.github.telegramcodexbot.agent-node.plist`
- `examples/launchd/io.github.telegramcodexbot.center-bot.plist`

## Notes

- The plugin proxies directory browsing, session creation, text sends, control
  keys, capture, photo/file upload, and transcript events.
- Uploaded files are written on the agent node under
  `~/.telegram-codex-bot/uploads/` and Codex receives the node-local file path.
- The node id should stay stable. Current routing uses `node_id:tmux_window_id`
  as the center-visible session key.
