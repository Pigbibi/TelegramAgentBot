"""Default single-machine backend backed by tmux and local Codex transcripts."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from .base import (
    AgentTarget,
    BackendInfo,
    CreateSessionRequest,
    CreateSessionResult,
    MessageCallback,
    SendResult,
)
from .browser import BrowserRoot, DirectoryListing
from .files import FileUploadResult

if TYPE_CHECKING:
    from ..session import CodexSession

logger = logging.getLogger(__name__)


class LocalTmuxBackend:
    """Local backend preserving the existing tmux-based behavior."""

    backend_id = "local"

    def __init__(self) -> None:
        self.session_monitor = None

    def info(self) -> BackendInfo:
        return BackendInfo(
            backend_id=self.backend_id,
            display_name="Local tmux",
            mode="local",
        )

    @staticmethod
    def target_from_window(window_id: str, *, session_id: str = "") -> AgentTarget:
        """Build a local target from a tmux window ID."""
        return AgentTarget(
            backend_id="local",
            node_id="local",
            session_id=session_id,
            window_id=window_id,
        )

    def _require_window_id(self, target: AgentTarget) -> str | None:
        if target.backend_id != self.backend_id:
            return None
        return target.window_id or None

    def prepare(self) -> None:
        """Ensure the tmux session exists for the local runtime."""
        from ..config import config
        from ..tmux_manager import tmux_manager

        session = tmux_manager.get_or_create_session()
        logger.info("Tmux session '%s' ready", session.session_name)
        logger.info("Codex transcript root: %s", config.codex_projects_path)

    async def start(self, message_callback: MessageCallback) -> None:
        """Start local transcript monitoring."""
        if self.session_monitor is not None:
            logger.warning("Local tmux backend already started")
            return

        from ..session_monitor import SessionMonitor

        monitor = SessionMonitor()
        monitor.set_message_callback(message_callback)
        monitor.start()
        self.session_monitor = monitor
        logger.info("Local tmux backend monitor started")

    async def stop(self) -> None:
        """Stop local transcript monitoring."""
        if self.session_monitor is None:
            return
        await self.session_monitor.stop()
        self.session_monitor = None
        logger.info("Local tmux backend monitor stopped")

    async def create_session(
        self, request: CreateSessionRequest
    ) -> CreateSessionResult:
        """Create or resume a local tmux-backed Codex session."""
        from ..tmux_manager import tmux_manager

        if not any((request.agent_type, request.model, request.reasoning_effort)):
            success, message, window_name, window_id = await tmux_manager.create_window(
                request.cwd,
                window_name=request.window_name or None,
                resume_session_id=request.resume_session_id or None,
                account_name=request.account_name or None,
            )
        else:
            success, message, window_name, window_id = await tmux_manager.create_window(
                request.cwd,
                window_name=request.window_name or None,
                resume_session_id=request.resume_session_id or None,
                account_name=request.account_name or None,
                agent_type=request.agent_type or None,
                model=request.model or None,
                reasoning_effort=request.reasoning_effort or None,
            )
        target = self.target_from_window(window_id) if success and window_id else None
        return CreateSessionResult(
            ok=success,
            message=message,
            target=target,
            display_name=window_name,
        )

    async def list_roots(self) -> list[BrowserRoot]:
        """Return local configured project roots for browser-compatible plugins."""
        from ..config import config

        if getattr(config, "project_roots_configured", False):
            return [
                BrowserRoot(
                    label=root.label,
                    path=str(root.path),
                    backend_id=self.backend_id,
                    node_id="local",
                )
                for root in config.project_roots
            ]

        start = config.default_projects_path.expanduser()
        if not start.is_dir():
            start = Path.home()
        return [
            BrowserRoot(
                label="Local",
                path=str(start),
                backend_id=self.backend_id,
                node_id="local",
            )
        ]

    async def list_directory(
        self,
        node_id: str,
        path: str,
        *,
        root_path: str = "",
    ) -> DirectoryListing:
        """Return a local directory listing using the same rules as the UI."""
        from ..config import config

        root = Path(root_path).expanduser().resolve() if root_path else None
        current = Path(path).expanduser().resolve()

        if root is not None:
            if not root.exists() or not root.is_dir():
                root = None
            elif not (current == root or root in current.parents):
                current = root

        if not current.exists() or not current.is_dir():
            current = root if root is not None else Path.cwd()

        try:
            subdirs = sorted(
                [
                    child.name
                    for child in current.iterdir()
                    if child.is_dir()
                    and (config.show_hidden_dirs or not child.name.startswith("."))
                ]
            )
        except (PermissionError, OSError) as exc:
            return DirectoryListing(
                path=str(current),
                subdirs=[],
                root_path=str(root) if root else root_path,
                can_go_up=current != current.parent
                and (root is None or current != root),
                error=str(exc),
            )

        return DirectoryListing(
            path=str(current),
            subdirs=subdirs,
            root_path=str(root) if root else root_path,
            can_go_up=current != current.parent and (root is None or current != root),
        )

    async def list_sessions(self, node_id: str, cwd: str) -> list[CodexSession]:
        """Return local Codex sessions for browser-compatible plugins."""
        from ..session import session_manager

        return await session_manager.list_sessions_for_directory(cwd)

    async def upload_file(
        self,
        target: AgentTarget,
        local_path: str,
        *,
        filename: str = "",
    ) -> FileUploadResult:
        """Local backend already sees files by local path."""
        return FileUploadResult(ok=True, path=local_path)

    async def send_message(self, target: AgentTarget, text: str) -> SendResult:
        """Send user text to a local tmux window."""
        from ..session import session_manager

        window_id = self._require_window_id(target)
        if not window_id:
            return SendResult(False, "Invalid local target")

        ok, message = await session_manager.send_to_window(
            window_id,
            text,
            reject_busy=False,
        )
        return SendResult(ok, message)

    async def send_control(self, target: AgentTarget, key: str) -> SendResult:
        """Send one control key to a local tmux window."""
        from ..tmux_manager import tmux_manager

        window_id = self._require_window_id(target)
        if not window_id:
            return SendResult(False, "Invalid local target")

        ok = await tmux_manager.send_control_key(window_id, key)
        return SendResult(ok, "" if ok else f"Failed to send {key}")

    async def capture(
        self, target: AgentTarget, *, with_ansi: bool = False
    ) -> str | None:
        """Capture visible local tmux pane text."""
        from ..tmux_manager import tmux_manager

        window_id = self._require_window_id(target)
        if not window_id:
            return None
        return await tmux_manager.capture_pane(window_id, with_ansi=with_ansi)
