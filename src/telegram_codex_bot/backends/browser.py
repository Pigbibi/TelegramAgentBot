"""Optional browser capability for agent backends."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from ..session import CodexSession


@dataclass(frozen=True)
class BrowserRoot:
    """One browsable project root exposed by an agent backend."""

    label: str
    path: str
    backend_id: str = ""
    node_id: str = ""


@dataclass(frozen=True)
class DirectoryListing:
    """Resolved directory listing returned by an agent backend."""

    path: str
    subdirs: list[str]
    root_label: str = ""
    root_path: str = ""
    can_go_up: bool = True
    error: str = ""


class AgentBrowser(Protocol):
    """Optional directory/session browser implemented by remote-capable backends."""

    async def list_roots(self) -> list[BrowserRoot]:
        """Return browsable roots for this backend."""
        ...

    async def list_directory(
        self,
        node_id: str,
        path: str,
        *,
        root_path: str = "",
    ) -> DirectoryListing:
        """Return a directory listing for one backend node."""
        ...

    async def list_sessions(
        self,
        node_id: str,
        cwd: str,
    ) -> list[CodexSession]:
        """Return resumable Codex sessions visible to one backend node."""
        ...
