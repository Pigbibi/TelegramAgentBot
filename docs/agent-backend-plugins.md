# Agent backend plugins

TelegramAgentBot routes session operations through an agent backend. The
built-in `local` backend controls tmux and transcript files on the same machine
as the Telegram bot. A plugin can route those operations to another transport
or host.

Most installations should use `local`. Install a backend plugin only when the
Telegram-facing process and agent sessions must run on different machines.

## Loading a backend

Select a backend by ID:

```ini
TELEGRAM_AGENT_BOT_BACKEND=local
```

Plugins can register backends through the Python entry point group:

```toml
[project.entry-points."telegram_agent_bot.backends"]
my-backend = "my_package.backend:MyBackend"
```

During development, modules can also be imported explicitly:

```ini
TELEGRAM_AGENT_BOT_BACKEND=my-backend
TELEGRAM_AGENT_BOT_BACKEND_PLUGINS=my_package.backend
```

The plugin package must be installed in the same Python environment as
TelegramAgentBot.

## Backend contract

A backend implements `telegram_agent_bot.backends.base.AgentBackend`:

```python
class AgentBackend(Protocol):
    backend_id: str

    def info(self) -> BackendInfo: ...
    def prepare(self) -> None: ...
    async def start(self, message_callback: MessageCallback) -> None: ...
    async def stop(self) -> None: ...
    async def create_session(
        self, request: CreateSessionRequest
    ) -> CreateSessionResult: ...
    async def send_message(
        self, target: AgentTarget, text: str
    ) -> SendResult: ...
    async def send_control(
        self, target: AgentTarget, key: str
    ) -> SendResult: ...
    async def capture(
        self, target: AgentTarget, *, with_ansi: bool = False
    ) -> str | None: ...
```

`AgentTarget` identifies a session with `backend_id`, `node_id`, `session_id`,
and, when applicable, `window_id`. Backend IDs and node IDs must remain stable
because topic bindings persist across process restarts.

`start()` receives an asynchronous callback for transcript events. Backends
must preserve event ordering within one session and should return clear failure
messages instead of raising transport details into Telegram handlers.

## Optional browser capability

A backend may implement `AgentBrowser` to support the normal Telegram project
picker and resume flow:

- `list_roots()`
- `list_directory(node_id, path, root_path="")`
- `list_sessions(node_id, cwd)`

Without this capability, the plugin is responsible for providing enough target
information to create or bind a session through its own workflow.

Directory listings must enforce the selected root on the backend side. Do not
rely only on Telegram callback data for path authorization.

## Optional active-turn input capability

A backend may implement `AgentInputRouter.send_input(target, text, mode=...)` to
support native active-turn behavior. The local backend maps `steer` to Enter and
Codex `queue` to Tab; Claude Code next-turn text stays in the durable AgentBot
FIFO because its CLI has no equivalent ordinary-text queue key. The base
`AgentBackend.send_message()` signature remains unchanged. Backends that do not
implement this optional capability continue to use the durable AgentBot FIFO
and are not offered controls that would imply unsupported semantics.

## Included socket backend

The repository contains an optional package at
[`plugins/socket_backend`](../plugins/socket_backend/README.md). It registers
the `socket-cluster` backend and provides the `telegram-agent-node` command.

The center bot sends commands to one or more nodes, and nodes stream transcript
events back to the center. Supported operations include:

- root and directory browsing;
- session lookup, creation, and resume;
- text and control-key input;
- terminal capture;
- photo and file upload;
- transcript event delivery.

Install it from the repository checkout:

```bash
uv pip install -e plugins/socket_backend
```

Center configuration:

```ini
TELEGRAM_AGENT_BOT_BACKEND=socket-cluster
TELEGRAM_AGENT_BOT_BACKEND_PLUGINS=telegram_agent_bot_socket_backend
TELEGRAM_AGENT_BOT_SOCKET_NODES=macbook=127.0.0.1:8765
TELEGRAM_AGENT_BOT_SOCKET_MAX_MESSAGE_BYTES=26214400
```

## Transport security

The included socket protocol is intended for a trusted loopback or private
network path. It does not provide public-internet authentication or TLS.

- Bind agent nodes to `127.0.0.1` unless a private network policy protects the
  listener.
- Use an SSH tunnel or a private overlay network between hosts.
- Apply firewall rules before binding to a non-loopback address.
- Keep maximum message size bounded.
- Treat uploaded files, project paths, terminal output, and transcript events as
  sensitive data.

Example reverse SSH tunnel from an agent node to a center host:

```bash
ssh -N -R 127.0.0.1:8765:127.0.0.1:8765 user@center-host
```

## Plugin design guidelines

- Keep transport-specific code outside the core bot package.
- Make `prepare()` idempotent and fail before Telegram polling starts when the
  configuration is invalid.
- Bound connection, request, and shutdown timeouts.
- Avoid storing credentials in `AgentTarget` or Telegram callback payloads.
- Validate node IDs, paths, file names, and message sizes at the receiving end.
- Make retries explicit and safe for operations that create sessions or upload
  files.
- Provide unit tests for target serialization, path authorization, error
  mapping, and event ordering.
- Document service ownership, network boundaries, and recovery behavior.

## Service examples

The socket package includes systemd and launchd examples under
`plugins/socket_backend/examples/`. Replace all placeholder users, paths,
addresses, and environment files before installing them.
