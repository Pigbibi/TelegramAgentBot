"""Optional file transfer capability for agent backends."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from .base import AgentTarget


@dataclass(frozen=True)
class FileUploadResult:
    """Result of copying a local file to an agent backend."""

    ok: bool
    path: str = ""
    message: str = ""


class AgentFileTransfer(Protocol):
    """Optional backend capability for copying files to an agent node."""

    async def upload_file(
        self,
        target: AgentTarget,
        local_path: str,
        *,
        filename: str = "",
    ) -> FileUploadResult:
        """Copy a local file to the target's node and return its node-local path."""
        ...
