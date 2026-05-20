# Agent Backend Plugins

TelegramCodexBot keeps the default single-machine path on the built-in `local`
backend. A real multi-node setup should live in a separate installable backend
plugin so normal tmux usage stays simple and rollback-safe.

## Current Core Support

The core already provides the plugin loading surface:

- `TELEGRAM_CODEX_BOT_BACKEND=<backend-id>`
- `TELEGRAM_CODEX_BOT_BACKEND_PLUGINS=<python.module[,python.module...]>`
- Python entry point group: `telegram_codex_bot.backends`

A backend plugin implements `AgentBackend`:

- `prepare()`
- `start(message_callback)`
- `stop()`
- `create_session(request)`
- `send_message(target, text)`
- `send_control(target, key)`
- `capture(target, with_ansi=False)`

The core can now bind non-local `AgentTarget` values and route messages back by
`backend_id`, `node_id`, and `session_id`.

Backends can also implement `AgentBrowser` for first-class remote session
creation:

- `list_roots()`
- `list_directory(node_id, path, root_path="")`
- `list_sessions(node_id, cwd)`

## Included Socket Backend Plugin

The repository includes the first real plugin as a separate package:

```text
plugins/socket_backend
```

It provides:

- a center-side backend registered as `socket-cluster`;
- an agent-node CLI: `telegram-codex-agent-node`;
- a tiny newline-delimited JSON protocol over TCP;
- remote root browsing, directory browsing, resume-session lookup, session
  creation, text send, control key send, pane capture, and transcript event
  streaming.

This avoids putting cluster code into the main bot package. It also avoids
requiring a public domain: a Mac or another private machine can expose its
local agent node to the VPS through a reverse SSH tunnel.

```bash
# On the agent node machine:
telegram-codex-agent-node --node-id macbook --host 127.0.0.1 --port 8765

# Also on the agent node machine, tunnel that local port to the VPS:
ssh -N -R 127.0.0.1:8765:127.0.0.1:8765 ubuntu@your-vps
```

Center bot `.env`:

```ini
TELEGRAM_CODEX_BOT_BACKEND=socket-cluster
TELEGRAM_CODEX_BOT_BACKEND_PLUGINS=telegram_codex_bot_socket_backend
TELEGRAM_CODEX_BOT_SOCKET_NODES=macbook=127.0.0.1:8765
```

Install the plugin on machines that use socket mode:

```bash
pip install -e plugins/socket_backend
```

## Protocol Shape

Use one request per TCP connection for commands:

```json
{"id":"...","op":"create_session","node_id":"macbook","cwd":"/Users/me/Projects/app","window_name":"app","resume_session_id":""}
{"id":"...","op":"send_message","target":{"backend_id":"socket-cluster","node_id":"macbook","session_id":"..."},"text":"hello"}
{"id":"...","op":"send_control","target":{"backend_id":"socket-cluster","node_id":"macbook","session_id":"..."},"key":"Escape"}
{"id":"...","op":"capture","target":{"backend_id":"socket-cluster","node_id":"macbook","session_id":"..."},"with_ansi":true}
{"id":"...","op":"list_roots"}
{"id":"...","op":"list_directory","path":"/Users/me/Projects","root_path":"/Users/me/Projects"}
{"id":"...","op":"list_sessions","cwd":"/Users/me/Projects/app"}
```

Use a long-lived subscription connection for agent events:

```json
{"op":"subscribe","node_id":"macbook"}
```

Agent nodes stream parsed `NewMessage`-compatible payloads back to the center:

```json
{"op":"message","node_id":"macbook","window_id":"@7","message":{"session_id":"macbook:@7","text":"done","is_complete":true,"content_type":"text","role":"assistant"}}
```

The agent node reuses the existing `LocalTmuxBackend` internally. That keeps
tmux, transcript parsing, screenshots, control keys, and session creation
consistent with the default local mode.

## Why Not GitHub As The Live Transport

GitHub Issues or repository files can work as a slow control plane, but they are
not ideal for live Telegram chat:

- polling latency is noticeable;
- API rate limits and concurrent edits need careful backoff;
- secrets and transient chat payloads become harder to keep out of repository
state;
- screenshots and binary attachments need a separate storage path.

GitHub remains useful for async task orchestration, which is what
`telegram-codex-bridge` already covers. For live center-bot / agent-node chat,
a socket plugin over reverse SSH is simpler and faster.

## Remaining Gap

Photo forwarding still needs an optional file-transfer capability. Until that
exists, the core intentionally keeps photo forwarding local-only for non-local
targets.

## Implementation Order

1. Done: add optional browser capability to core and keep local mode as the
   default path.
2. Done: create the separate socket backend package with entry point
   registration.
3. Done: implement the agent-node CLI using `LocalTmuxBackend`.
4. Done: implement the center-side `socket-cluster` backend request/response
   methods.
5. Done: add event subscription so node transcript messages call the center
   `message_callback`.
6. Next: add file-transfer support for photos and attachments.
7. Next: add systemd/LaunchAgent examples for the VPS center bot and Mac agent
   node.
