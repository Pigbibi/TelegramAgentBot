# TelegramAgentBot socket backend

The socket backend runs TelegramAgentBot as a center bot while Codex or Claude
Code sessions run on one or more remote agent nodes.

Use the built-in `local` backend for a single-machine installation. Socket mode
is intended for trusted multi-host setups where the center can reach each node
through loopback forwarding or a private network.

## Architecture

```text
Telegram
   │
   ▼
center bot (`socket-cluster` backend)
   │  TCP over SSH/private network
   ▼
agent node (`telegram-agent-node`)
   │
   ▼
local tmux and agent transcript files
```

The plugin supports remote directory browsing, session creation and resume,
text and control-key input, terminal capture, file upload, and transcript event
delivery.

## Install

Install the core package and plugin into the same environment from the main
repository checkout:

```bash
uv pip install -e . -e plugins/socket_backend
```

Confirm both entry points are available:

```bash
telegram-agent-bot --version
telegram-agent-node --help
```

## Start an agent node

Run the node on the machine that owns the agent CLI, tmux server, credentials,
projects, and transcript files:

```bash
telegram-agent-node \
  --node-id macbook \
  --host 127.0.0.1 \
  --port 8765
```

The node ID is stored in topic bindings. Choose a stable, unique value and do
not reuse it for another machine.

Available environment variables:

| Variable | Default | Description |
| --- | --- | --- |
| `TELEGRAM_AGENT_NODE_ID` | `local` | Stable node identifier |
| `TELEGRAM_AGENT_NODE_HOST` | `127.0.0.1` | Listener address |
| `TELEGRAM_AGENT_NODE_PORT` | `8765` | Listener port |
| `TELEGRAM_AGENT_NODE_LOG_LEVEL` | `INFO` | Python log level |
| `TELEGRAM_AGENT_NODE_MAX_MESSAGE_BYTES` | `26214400` | Maximum accepted JSON line size |

Uploaded files are written below `~/.telegram-agent-bot/uploads/` on the node.
The local path is then sent to the agent session.

## Connect the center bot

Configure the Telegram-facing process:

```ini
TELEGRAM_AGENT_BOT_BACKEND=socket-cluster
TELEGRAM_AGENT_BOT_BACKEND_PLUGINS=telegram_agent_bot_socket_backend
TELEGRAM_AGENT_BOT_SOCKET_NODES=macbook=127.0.0.1:8765
TELEGRAM_AGENT_BOT_SOCKET_TIMEOUT=20
TELEGRAM_AGENT_BOT_SOCKET_RECONNECT_DELAY=5
TELEGRAM_AGENT_BOT_SOCKET_MAX_MESSAGE_BYTES=26214400
```

Multiple nodes use comma-separated `node-id=host:port` entries:

```ini
TELEGRAM_AGENT_BOT_SOCKET_NODES=macbook=127.0.0.1:8765,workstation=127.0.0.1:8766
```

Restart the center bot only after the node listener and network path have been
verified.

## Secure the transport

The socket protocol does not provide public-internet authentication or TLS.
Do not expose the listener directly to an untrusted network.

For a node behind NAT, keep it bound to loopback and create a reverse SSH tunnel
to the center host:

```bash
ssh -N \
  -o ServerAliveInterval=30 \
  -o ExitOnForwardFailure=yes \
  -R 127.0.0.1:8765:127.0.0.1:8765 \
  user@center-host
```

The center then connects to `127.0.0.1:8765`. Use a dedicated SSH identity,
restrict its server-side permissions, and supervise the tunnel separately.

If a private overlay network is used instead, enforce host identity and firewall
rules at that layer before binding the node to a non-loopback address.

## Service examples

Templates are available under `examples/`:

- `examples/systemd/telegram-agent-bot.socket-center.service`
- `examples/systemd/telegram-agent-node.service`
- `examples/systemd/socket-center.env.example`
- `examples/launchd/io.github.telegramcodexbot.agent-node.plist`
- `examples/launchd/io.github.telegramcodexbot.center-bot.plist`

Replace every placeholder user, path, address, and environment file. Keep
environment files owner-readable only.

## Verification

1. Start the node on loopback.
2. Confirm the center can open the configured TCP address.
3. Start the center bot with one node.
4. Create a Telegram topic and browse the node's project roots.
5. Start a disposable session and test text, Escape, capture, and file upload.
6. Restart the center bot and confirm the topic still resolves to the same node.

Inspect both processes when a request fails. A center timeout can be caused by
the network path, a stopped node, an oversized payload, or a blocked local agent
session.

See [Agent backend plugins](../../docs/agent_backend_plugins.md) for the core
backend contract and plugin-development guidance.
