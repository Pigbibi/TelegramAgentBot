"""Small in-process admission controller for local agent turns.

The durable prompt spool remains the source of truth for queued user input.
This module only tracks short-lived runtime observations and reservations so
concurrent Telegram handlers cannot start more local Codex/Claude turns than
the VPS is configured to run at once.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class TurnAdmissionSnapshot:
    """Current local scheduler state for diagnostics."""

    active_windows: frozenset[str]
    reserved_windows: frozenset[str]
    pending_windows: frozenset[str]


class TurnAdmissionController:
    """Bound concurrent local turns without owning their durable queue."""

    def __init__(self) -> None:
        self._observed_active_windows: set[str] = set()
        self._reserved_windows: set[str] = set()
        self._pending_windows: set[str] = set()
        self._lock: asyncio.Lock | None = None

    def _get_lock(self) -> asyncio.Lock:
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    async def try_acquire(self, window_id: str, *, limit: int) -> bool:
        """Reserve one turn slot, returning False when the limit is full."""
        if limit <= 0:
            return True
        async with self._get_lock():
            occupied = self._observed_active_windows | self._reserved_windows
            if window_id in occupied:
                return True
            if len(occupied) >= limit:
                return False
            self._reserved_windows.add(window_id)
            return True

    def mark_submitted(self, window_id: str) -> None:
        """Convert a short reservation into an observed active turn."""
        self._reserved_windows.discard(window_id)
        self._observed_active_windows.add(window_id)

    def release(self, window_id: str) -> None:
        """Release a failed or abandoned reservation."""
        self._reserved_windows.discard(window_id)

    def observe(self, window_id: str, *, active: bool) -> None:
        """Record the latest terminal readiness observation for one window."""
        if active:
            self._observed_active_windows.add(window_id)
        else:
            self._observed_active_windows.discard(window_id)

    def forget(self, window_id: str) -> None:
        """Remove all runtime state for a missing or closed window."""
        self._observed_active_windows.discard(window_id)
        self._reserved_windows.discard(window_id)
        self._pending_windows.discard(window_id)

    def set_pending(self, window_id: str, *, pending: bool) -> None:
        """Protect a window with queued input from idle hibernation."""
        if pending:
            self._pending_windows.add(window_id)
        else:
            self._pending_windows.discard(window_id)

    def has_pending(self, window_id: str) -> bool:
        return window_id in self._pending_windows

    def is_protected(self, window_id: str) -> bool:
        """Return whether dispatch or queued work makes hibernation unsafe."""
        return window_id in self._pending_windows or window_id in self._reserved_windows

    def snapshot(self) -> TurnAdmissionSnapshot:
        return TurnAdmissionSnapshot(
            active_windows=frozenset(self._observed_active_windows),
            reserved_windows=frozenset(self._reserved_windows),
            pending_windows=frozenset(self._pending_windows),
        )

    def reset(self) -> None:
        """Clear process-local state during application shutdown and tests."""
        self._observed_active_windows.clear()
        self._reserved_windows.clear()
        self._pending_windows.clear()
        self._lock = None


turn_admission = TurnAdmissionController()
