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

## Recommended First Real Plugin

Use a separate package, for example:

```text
telegram-codex-bot-socket-backend
```

It should provide:

- a center-side backend registered as `socket-cluster`;
- an agent-node CLI, for example `telegram-codex-agent-node`;
- a tiny newline-delimited JSON protocol over TCP.

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

## Protocol Shape

Use one request per TCP connection for commands:

```json
{"id":"...","op":"create_session","node_id":"macbook","cwd":"/Users/me/Projects/app","window_name":"app","resume_session_id":""}
{"id":"...","op":"send_message","target":{"backend_id":"socket-cluster","node_id":"macbook","session_id":"..."},"text":"hello"}
{"id":"...","op":"send_control","target":{"backend_id":"socket-cluster","node_id":"macbook","session_id":"..."},"key":"Escape"}
{"id":"...","op":"capture","target":{"backend_id":"socket-cluster","node_id":"macbook","session_id":"..."},"with_ansi":true}
```

Use a long-lived subscription connection for agent events:

```json
{"op":"subscribe","node_id":"macbook"}
```

Agent nodes stream parsed `NewMessage`-compatible payloads back to the center:

```json
{"op":"message","session_id":"...","text":"done","is_complete":true,"content_type":"text","role":"assistant"}
```

The agent node can reuse the existing `LocalTmuxBackend` internally. That keeps
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

## Missing Core Capabilities For A Polished Plugin

The backend interface is enough for bound sessions, but two user-facing flows
still assume the center bot can see the local VPS filesystem:

- directory browsing before creating a session;
- resume-session browsing for a selected directory.

To make remote nodes first-class, add a small optional capability interface:

```python
class AgentBrowser(Protocol):
    async def list_roots(self) -> list[RemoteRoot]: ...
    async def list_directory(self, node_id: str, path: str) -> DirectoryListing: ...
    async def list_sessions(self, node_id: str, cwd: str) -> list[CodexSession]: ...
```

The default `local` backend can implement this by delegating to the existing
filesystem and session-manager code. A socket plugin can proxy these calls to
the agent node.

Photo forwarding also needs an optional file-transfer capability. Until that
exists, the core intentionally keeps photo forwarding local-only for non-local
targets.

## Implementation Order

1. Create the separate socket backend package with entry point registration and
   a no-op health check.
2. Add the optional browser capability to core and keep the local backend as the
   default implementation.
3. Implement the agent-node CLI using `LocalTmuxBackend`.
4. Implement the center-side `socket-cluster` backend request/response methods.
5. Add event subscription so node transcript messages call the center
   `message_callback`.
6. Add file-transfer support for photos and attachments.
7. Add systemd/LaunchAgent examples for the VPS center bot and Mac agent node.
