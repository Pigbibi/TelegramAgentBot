"""Default single-machine backend backed by tmux and local Codex transcripts."""

from __future__ import annotations

import logging

from .base import (
    AgentTarget,
    BackendInfo,
    CreateSessionRequest,
    CreateSessionResult,
    MessageCallback,
    SendResult,
)

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
        self.session_monitor.stop()
        self.session_monitor = None
        logger.info("Local tmux backend monitor stopped")

    async def create_session(
        self, request: CreateSessionRequest
    ) -> CreateSessionResult:
        """Create or resume a local tmux-backed Codex session."""
        from ..tmux_manager import tmux_manager

        success, message, window_name, window_id = await tmux_manager.create_window(
            request.cwd,
            window_name=request.window_name or None,
            resume_session_id=request.resume_session_id or None,
            account_name=request.account_name or None,
        )
        target = self.target_from_window(window_id) if success and window_id else None
        return CreateSessionResult(
            ok=success,
            message=message,
            target=target,
            display_name=window_name,
        )

    async def send_message(self, target: AgentTarget, text: str) -> SendResult:
        """Send user text to a local tmux window."""
        from ..session import session_manager

        window_id = self._require_window_id(target)
        if not window_id:
            return SendResult(False, "Invalid local target")

        ok, message = await session_manager.send_to_window(window_id, text)
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
