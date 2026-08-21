"""Shared helpers for the per-topic Codex working timer."""

import re
import time

from ..terminal_parser import codex_input_text, parse_status_update

SYNTHETIC_WORKING_IDLE_GRACE = 2.0
SYNTHETIC_WORKING_NO_OUTPUT_MAX = 10 * 60.0
LONG_RUNNING_WARNING_SECONDS = 30 * 60.0

_NATIVE_INTERRUPT_MARKER = "esc to interrupt"
_LONG_RUNNING_WARNING = (
    "⚠️ This task has been running for a long time. If progress has stopped, "
    "use /interrupt, then retry with a bounded timeout."
)

_synthetic_working_starts: dict[tuple[int, int, str], float] = {}
_synthetic_working_output_seen: set[tuple[int, int, str]] = set()


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
        _NATIVE_INTERRUPT_MARKER in normalized
        or "/interrupt" in normalized
        or normalized.startswith("• working")
        or normalized.startswith("◦ working")
        or normalized.startswith("working")
        or normalized.startswith("thinking")
        or normalized.startswith("💭 thinking")
    )


def _active_status_public_detail(status_text: str) -> str | None:
    """Return extra public detail from a native Working/Thinking status line."""
    marker_match = re.search(
        re.escape(_NATIVE_INTERRUPT_MARKER), status_text, flags=re.IGNORECASE
    )
    if marker_match is None:
        return None
    tail = status_text[marker_match.end() :]
    tail = re.sub(r"^[\s\])）·•:;\-–—]+", "", tail).strip()
    if not tail:
        return None
    return f"◦ {tail}"


def _native_elapsed_seconds(status_text: str) -> int | None:
    """Parse elapsed time from a native Codex active-status line."""
    marker_index = status_text.lower().find(_NATIVE_INTERRUPT_MARKER)
    if marker_index < 0:
        return None
    prefix = status_text[:marker_index]
    open_paren = prefix.rfind("(")
    if open_paren >= 0:
        prefix = prefix[open_paren + 1 :]
    parts = re.findall(r"(\d+)\s*([hms])", prefix, flags=re.IGNORECASE)
    if not parts:
        return None
    multipliers = {"h": 3600, "m": 60, "s": 1}
    return sum(int(value) * multipliers[unit.lower()] for value, unit in parts)


def _without_terminal_interrupt_hint(status_text: str) -> str:
    """Hide Codex's terminal-only Escape hint from Telegram users."""
    public_text = re.sub(
        rf"\s*[•·]\s*{re.escape(_NATIVE_INTERRUPT_MARKER)}",
        "",
        status_text,
        flags=re.IGNORECASE,
    )
    return re.sub(r"\s+\)", ")", public_text)


def _with_long_running_warning(status_text: str, elapsed: float) -> str:
    """Add a recoverable warning after a status has run unusually long."""
    if elapsed < LONG_RUNNING_WARNING_SECONDS:
        return status_text
    return f"{status_text}\n\n{_LONG_RUNNING_WARNING}"


def format_native_working_status(status_text: str | None) -> str | None:
    """Make native Codex working text actionable from Telegram."""
    if not is_active_working_status(status_text):
        return status_text
    assert status_text is not None
    elapsed = _native_elapsed_seconds(status_text)
    public_text = _without_terminal_interrupt_hint(status_text)
    if elapsed is None:
        return public_text
    return _with_long_running_warning(public_text, elapsed)


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
    timer = f"💭 Thinking ({format_elapsed(elapsed)})"
    timer = _with_long_running_warning(timer, elapsed)
    if detail:
        public_detail = (
            _active_status_public_detail(detail)
            if is_active_working_status(detail)
            else detail
        )
        if public_detail:
            return f"{public_detail}\n\n{timer}"
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
    key = working_key(user_id, thread_id, window_id)
    _synthetic_working_starts[key] = start
    _synthetic_working_output_seen.discard(key)
    return start, format_working_status(start, now=start)


def working_started_at_epoch(
    user_id: int,
    thread_id: int | None,
    window_id: str,
    *,
    now: float | None = None,
    now_epoch: float | None = None,
) -> float | None:
    """Return a restart-safe wall-clock start time for an active local timer."""
    started_at = _synthetic_working_starts.get(
        working_key(user_id, thread_id, window_id)
    )
    if started_at is None:
        return None
    current = now if now is not None else time.monotonic()
    current_epoch = now_epoch if now_epoch is not None else time.time()
    return current_epoch - max(0.0, current - started_at)


def restore_working(
    user_id: int,
    thread_id: int | None,
    window_id: str,
    *,
    started_at_epoch: float,
    now: float | None = None,
    now_epoch: float | None = None,
) -> float:
    """Restore an active local timer without resetting its elapsed duration."""
    current = now if now is not None else time.monotonic()
    current_epoch = now_epoch if now_epoch is not None else time.time()
    restored_start = current - max(0.0, current_epoch - started_at_epoch)
    key = working_key(user_id, thread_id, window_id)
    existing_start = _synthetic_working_starts.get(key)
    if existing_start is None or restored_start < existing_start:
        _synthetic_working_starts[key] = restored_start
    return _synthetic_working_starts[key]


def clear_working(user_id: int, thread_id: int | None, window_id: str) -> None:
    """Clear the local timer for one topic/window."""
    key = working_key(user_id, thread_id, window_id)
    _synthetic_working_starts.pop(key, None)
    _synthetic_working_output_seen.discard(key)


def mark_output_seen(user_id: int, thread_id: int | None, window_id: str) -> None:
    """Record that a visible assistant message has arrived for this run."""
    key = working_key(user_id, thread_id, window_id)
    if key in _synthetic_working_starts:
        _synthetic_working_output_seen.add(key)


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
        return format_native_working_status(status_text)

    current_time = now if now is not None else time.monotonic()
    elapsed = current_time - started_at
    input_text = codex_input_text(pane_text)
    if input_text is not None and elapsed >= SYNTHETIC_WORKING_IDLE_GRACE:
        output_seen = key in _synthetic_working_output_seen
        no_output_timed_out = elapsed >= SYNTHETIC_WORKING_NO_OUTPUT_MAX
        if output_seen or input_text == "" or no_output_timed_out:
            _synthetic_working_starts.pop(key, None)
            _synthetic_working_output_seen.discard(key)
            return status_text

    if (
        key not in _synthetic_working_output_seen
        and elapsed < SYNTHETIC_WORKING_NO_OUTPUT_MAX
    ):
        return format_working_status(started_at, now=current_time, detail=status_text)

    return format_working_status(started_at, now=current_time, detail=status_text)
