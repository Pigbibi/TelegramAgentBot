"""Socket backend plugin for TelegramAgentBot."""

from .backend import SocketClusterBackend

BACKEND_ID = "socket-cluster"
BACKEND_CLASS = SocketClusterBackend


def create_backend() -> SocketClusterBackend:
    """Module-plugin entry point used by TELEGRAM_CODEX_BOT_BACKEND_PLUGINS."""
    return SocketClusterBackend()
