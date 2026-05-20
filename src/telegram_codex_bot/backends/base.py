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
