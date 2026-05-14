"""Shared helpers for the per-topic Codex working timer."""

import time

from ..terminal_parser import is_codex_input_ready, parse_status_update

SYNTHETIC_WORKING_IDLE_GRACE = 2.0

_synthetic_working_starts: dict[tuple[int, int, str], float] = {}


def working_key(
    user_id: int, thread_id: int | None, window_id: str
) -> tuple[int, int, str]:
    """Return the stable key for one Telegram topic/window timer."""
    return (user_id, thread_id or 0, window_id)


def format_elapsed(seconds: int) -> str:
    """Format elapsed seconds for compact Telegram status messages."""
    if seconds < 60:
        return f"{seconds}s"
    minutes, remainder = divmod(seconds, 60)
    return f"{minutes}m {remainder:02d}s"


def is_active_working_status(status_text: str | None) -> bool:
    """Return whether a status should remain visible while the queue is busy."""
    if not status_text:
        return False
    normalized = status_text.strip().lower()
    return (
        "esc to interrupt" in normalized
        or normalized.startswith("• working")
        or normalized.startswith("◦ working")
        or normalized.startswith("working")
        or normalized.startswith("thinking")
        or normalized.startswith("💭 thinking")
    )


def format_working_status(
    started_at: float,
    *,
    now: float | None = None,
    detail: str | None = None,
) -> str:
    """Build the Telegram status text for an active Codex run.

    ``detail`` is any public terminal progress text.  The timer is kept as the
    final line so the chat always ends with a visibly changing heartbeat.
    """
    elapsed = max(0, int((now if now is not None else time.monotonic()) - started_at))
    timer = f"💭 Thinking ({format_elapsed(elapsed)}) · esc to interrupt"
    if detail and not is_active_working_status(detail):
        return f"{detail}\n\n{timer}"
    return timer


def start_working(
    user_id: int,
    thread_id: int | None,
    window_id: str,
    *,
    started_at: float | None = None,
) -> tuple[float, str]:
    """Start or replace the local timer for a just-submitted prompt."""
    start = started_at if started_at is not None else time.monotonic()
    _synthetic_working_starts[working_key(user_id, thread_id, window_id)] = start
    return start, format_working_status(start, now=start)


def clear_working(user_id: int, thread_id: int | None, window_id: str) -> None:
    """Clear the local timer for one topic/window."""
    _synthetic_working_starts.pop(working_key(user_id, thread_id, window_id), None)


def status_text_for_pane(
    user_id: int,
    thread_id: int | None,
    window_id: str,
    pane_text: str,
    *,
    now: float | None = None,
) -> str | None:
    """Return the best Telegram status text for a pane capture.

    While a local timer is active, keep it visible until Codex is clearly idle.
    Native public progress text is preserved above the timer.
    """
    status_text = parse_status_update(pane_text)
    key = working_key(user_id, thread_id, window_id)
    started_at = _synthetic_working_starts.get(key)
    if started_at is None:
        return status_text

    current_time = now if now is not None else time.monotonic()
    if (
        is_codex_input_ready(pane_text)
        and current_time - started_at >= SYNTHETIC_WORKING_IDLE_GRACE
    ):
        _synthetic_working_starts.pop(key, None)
        return status_text

    return format_working_status(started_at, now=current_time, detail=status_text)
