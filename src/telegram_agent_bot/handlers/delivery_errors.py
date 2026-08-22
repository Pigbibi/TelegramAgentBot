"""Typed Telegram delivery failures shared by sender, queue, and monitor layers."""

from __future__ import annotations

from telegram.error import BadRequest


_PERMANENT_TOPIC_ERROR_MARKERS = (
    "message thread not found",
    "topic_closed",
    "topic closed",
    "topic was deleted",
)


def is_permanent_topic_error(exc: BaseException) -> bool:
    """Return whether retrying the same Telegram forum destination cannot help."""
    if not isinstance(exc, BadRequest):
        return False
    message = str(exc).lower()
    return any(marker in message for marker in _PERMANENT_TOPIC_ERROR_MARKERS)


class PermanentDeliveryError(RuntimeError):
    """A Telegram topic route that must be repaired before delivery can resume."""

    def __init__(
        self,
        message: str,
        *,
        user_id: int | None = None,
        thread_id: int | None = None,
        chat_id: int | None = None,
    ) -> None:
        super().__init__(message)
        self.user_id = user_id
        self.thread_id = thread_id
        self.chat_id = chat_id

    def with_target(self, *, user_id: int, thread_id: int | None) -> None:
        """Attach the owning AgentBot route before propagating to the monitor."""
        self.user_id = user_id
        self.thread_id = thread_id
