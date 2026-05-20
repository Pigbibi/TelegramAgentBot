# TelegramCodexBot Socket Backend

Optional backend plugin for running TelegramCodexBot as a center bot with one or
more remote agent nodes.

The default TelegramCodexBot install still uses local tmux. Install this plugin
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
```

Then restart `telegram-codex-bot`.

## Notes

- The plugin proxies directory browsing, session creation, text sends, control
  keys, capture, and transcript events.
- Photo/file transfer is not implemented yet. Non-local photo forwarding still
  needs a dedicated file-transfer capability.
- The node id should stay stable. Current routing uses `node_id:tmux_window_id`
  as the center-visible session key.
