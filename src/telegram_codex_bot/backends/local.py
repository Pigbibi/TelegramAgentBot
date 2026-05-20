"""Default single-machine backend backed by tmux and local Codex transcripts."""

from __future__ import annotations

import logging

from .base import BackendInfo, MessageCallback

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
