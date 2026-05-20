"""Backend protocol for local and distributed Codex agents."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol


MessageCallback = Callable[[Any], Awaitable[None]]


@dataclass(frozen=True)
class BackendInfo:
    """Runtime information for one agent backend."""

    backend_id: str
    display_name: str
    mode: str = "local"


@dataclass(frozen=True)
class AgentTarget:
    """Address of one agent session in a backend."""

    backend_id: str
    node_id: str
    session_id: str = ""
    window_id: str = ""


@dataclass(frozen=True)
class CreateSessionRequest:
    """Request for creating or resuming one agent session."""

    cwd: str
    window_name: str = ""
    resume_session_id: str = ""
    account_name: str = ""


@dataclass(frozen=True)
class CreateSessionResult:
    """Result of creating or resuming one agent session."""

    ok: bool
    message: str
    target: AgentTarget | None = None
    display_name: str = ""


@dataclass(frozen=True)
class SendResult:
    """Result of sending input or a control key to an agent."""

    ok: bool
    message: str = ""


class AgentBackend(Protocol):
    """Common lifecycle contract for Codex agent backends.

    The default backend is local tmux. Optional plugins can provide a backend
    that proxies work to remote agent nodes while keeping Telegram UI code
    insulated from the transport details.
    """

    backend_id: str

    def info(self) -> BackendInfo:
        """Return displayable backend information."""
        ...

    def prepare(self) -> None:
        """Prepare local runtime resources before Telegram polling starts."""
        ...

    async def start(self, message_callback: MessageCallback) -> None:
        """Start backend event delivery."""
        ...

    async def stop(self) -> None:
        """Stop backend event delivery and flush state."""
        ...

    async def create_session(
        self, request: CreateSessionRequest
    ) -> CreateSessionResult:
        """Create or resume an agent session."""
        ...

    async def send_message(self, target: AgentTarget, text: str) -> SendResult:
        """Send user text to an agent session."""
        ...

    async def send_control(self, target: AgentTarget, key: str) -> SendResult:
        """Send one control key to an agent session."""
        ...

    async def capture(
        self, target: AgentTarget, *, with_ansi: bool = False
    ) -> str | None:
        """Capture visible agent terminal output, when available."""
        ...
