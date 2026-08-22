"""Conservative idle-window hibernation decisions.

The hibernator deliberately owns no tmux or persistence behavior. It only
requires a window to remain continuously idle for the configured duration;
the status poller performs the final resumability checks and window shutdown.
"""

from __future__ import annotations

import time


class IdleSessionHibernator:
    """Track continuous idle time for local agent windows."""

    def __init__(self) -> None:
        self._idle_since: dict[str, float] = {}

    def observe(
        self,
        window_id: str,
        *,
        idle: bool,
        protected: bool,
        timeout_seconds: float,
        now: float | None = None,
    ) -> bool:
        """Return True once a resumable window is continuously idle long enough."""
        current = time.monotonic() if now is None else now
        if timeout_seconds <= 0 or not idle or protected:
            self._idle_since.pop(window_id, None)
            return False
        started_at = self._idle_since.setdefault(window_id, current)
        return current - started_at >= timeout_seconds

    def forget(self, window_id: str) -> None:
        self._idle_since.pop(window_id, None)

    def reset(self) -> None:
        self._idle_since.clear()


idle_session_hibernator = IdleSessionHibernator()
