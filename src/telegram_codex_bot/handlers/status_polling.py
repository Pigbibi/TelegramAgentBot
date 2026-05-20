"""Terminal status line polling for thread-bound windows.

Provides background polling of terminal status lines for all active users:
  - Detects Codex status (working, waiting, etc.)
  - Detects interactive UIs (permission prompts) not triggered via JSONL
  - Updates status messages in Telegram
  - Polls thread_bindings (each topic = one window)
  - Periodically probes topic existence via unpin_all_forum_topic_messages
    (silent no-op when no pins); cleans up deleted topics (kills tmux window
    + unbinds thread)

Key components:
  - STATUS_POLL_INTERVAL: Polling frequency (1 second)
  - TOPIC_CHECK_INTERVAL: Topic existence probe frequency (60 seconds)
  - status_poll_loop: Background polling task
  - update_status_message: Poll and enqueue status updates
"""

import asyncio
import logging
import time

from telegram import Bot
from telegram.error import BadRequest

from ..config import config
from ..session import session_manager
from ..agent_io import capture_agent_output
from ..terminal_parser import is_interactive_ui
from ..tmux_manager import tmux_manager
from .interactive_ui import (
    clear_interactive_msg,
    get_interactive_window,
    handle_interactive_ui,
)
from .cleanup import clear_topic_state
from .message_queue import enqueue_status_update, get_message_queue
from .working_status import (
    _synthetic_working_starts as _working_starts,
    clear_working,
    format_working_status,
    is_active_working_status,
    start_working,
    status_text_for_pane,
    working_key,
)

logger = logging.getLogger(__name__)

# Default comes from TELEGRAM_CODEX_BOT_STATUS_POLL_INTERVAL. Rate limiting remains at the send layer.

# Topic existence probe interval
TOPIC_CHECK_INTERVAL = 60.0  # seconds

# Missing tmux windows are handled lazily by the next user message.  A Codex
# pane can disappear during local restarts or tmux churn, and immediately
# deleting the topic binding loses the session id needed to resume it.
_missing_bound_windows: set[tuple[int, int, str]] = set()
_synthetic_working_starts = _working_starts


def forget_missing_bound_window(user_id: int, thread_id: int, window_id: str) -> None:
    """Clear the missing-window marker after a binding is recovered or removed."""
    _missing_bound_windows.discard((user_id, thread_id, window_id))


def _working_key(
    user_id: int, thread_id: int | None, window_id: str
) -> tuple[int, int, str]:
    return working_key(user_id, thread_id, window_id)


def _format_synthetic_working(started_at: float, now: float | None = None) -> str:
    return format_working_status(started_at, now=now)


async def mark_window_working(
    bot: Bot,
    user_id: int,
    window_id: str,
    thread_id: int | None = None,
) -> None:
    """Start an immediate local Thinking timer for a just-submitted prompt."""
    started_at = time.monotonic()
    _, status_text = start_working(
        user_id,
        thread_id,
        window_id,
        started_at=started_at,
    )
    await enqueue_status_update(
        bot,
        user_id,
        window_id,
        status_text,
        thread_id=thread_id,
    )


def clear_window_working(
    user_id: int,
    window_id: str,
    thread_id: int | None = None,
) -> None:
    """Clear the local Thinking timer for a window/topic."""
    clear_working(user_id, thread_id, window_id)


async def update_status_message(
    bot: Bot,
    user_id: int,
    window_id: str,
    thread_id: int | None = None,
    skip_status: bool = False,
) -> None:
    """Poll terminal and check for interactive UIs and status updates.

    UI detection always happens regardless of skip_status. When skip_status=True,
    only UI detection runs (used when message queue is non-empty to avoid
    flooding the queue with status updates).

    Also detects permission prompt UIs (not triggered via JSONL) and enters
    interactive mode when found.
    """
    capture = await capture_agent_output(user_id, thread_id, window_id)
    if capture is None or capture.missing:
        clear_window_working(user_id, window_id, thread_id)
        # Window gone, enqueue clear (unless skipping status)
        if not skip_status:
            await enqueue_status_update(
                bot, user_id, window_id, None, thread_id=thread_id
            )
        return

    pane_text = capture.text
    if not pane_text:
        # Transient capture failure - keep existing status message
        return

    interactive_window = get_interactive_window(user_id, thread_id)
    should_check_new_ui = True

    if interactive_window == window_id:
        # User is in interactive mode for THIS window
        if is_interactive_ui(pane_text):
            # Interactive UI still showing — skip status update (user is interacting)
            return
        # Interactive UI gone — clear interactive mode, fall through to status check.
        # Don't re-check for new UI this cycle (the old one just disappeared).
        await clear_interactive_msg(user_id, bot, thread_id)
        should_check_new_ui = False
    elif interactive_window is not None:
        # User is in interactive mode for a DIFFERENT window (window switched)
        # Clear stale interactive mode
        await clear_interactive_msg(user_id, bot, thread_id)

    # Check for permission prompt (interactive UI not triggered via JSONL)
    # ALWAYS check UI, regardless of skip_status
    if should_check_new_ui and is_interactive_ui(pane_text):
        logger.debug(
            "Interactive UI detected in polling (user=%d, window=%s, thread=%s)",
            user_id,
            window_id,
            thread_id,
        )
        await handle_interactive_ui(bot, user_id, window_id, thread_id)
        return

    status_text = status_text_for_pane(
        user_id,
        thread_id,
        window_id,
        pane_text,
        now=time.monotonic(),
    )

    # Normal status line check. If the queue is busy, still allow active timer
    # updates so the topic does not look idle.
    if skip_status:
        if is_active_working_status(status_text):
            await enqueue_status_update(
                bot,
                user_id,
                window_id,
                status_text,
                thread_id=thread_id,
            )
        return

    if status_text:
        await enqueue_status_update(
            bot,
            user_id,
            window_id,
            status_text,
            thread_id=thread_id,
        )
    else:
        await enqueue_status_update(
            bot,
            user_id,
            window_id,
            None,
            thread_id=thread_id,
        )


async def status_poll_loop(bot: Bot) -> None:
    """Background task to poll terminal status for all thread-bound windows."""
    poll_interval = config.status_poll_interval
    logger.info("Status polling started (interval: %ss)", poll_interval)
    last_topic_check = 0.0
    while True:
        try:
            # Periodic topic existence probe
            now = time.monotonic()
            if now - last_topic_check >= TOPIC_CHECK_INTERVAL:
                last_topic_check = now
                for user_id, thread_id, wid in list(
                    session_manager.iter_thread_bindings()
                ):
                    try:
                        await bot.unpin_all_forum_topic_messages(
                            chat_id=session_manager.resolve_chat_id(user_id, thread_id),
                            message_thread_id=thread_id,
                        )
                    except BadRequest as e:
                        if "Topic_id_invalid" in str(e):
                            # Topic deleted — kill window, unbind, and clean up state
                            w = await tmux_manager.find_window_by_id(wid)
                            state = session_manager.window_states.get(wid)
                            session_id = state.session_id if state else ""
                            if w:
                                await tmux_manager.kill_window(w.window_id)
                            if session_id:
                                session_manager.hide_session(session_id)
                            session_manager.unbind_thread(user_id, thread_id)
                            await session_manager.remove_session_map_entry(wid)
                            session_manager.remove_window_state(wid)
                            await clear_topic_state(user_id, thread_id, bot)
                            logger.info(
                                "Topic deleted: killed window_id '%s' and "
                                "unbound thread %d for user %d",
                                wid,
                                thread_id,
                                user_id,
                            )
                        else:
                            logger.debug(
                                "Topic probe error for %s: %s",
                                wid,
                                e,
                            )
                    except Exception as e:
                        logger.debug(
                            "Topic probe error for %s: %s",
                            wid,
                            e,
                        )

            for user_id, thread_id, wid in list(session_manager.iter_thread_bindings()):
                try:
                    # Keep stale bindings around so the next user message can
                    # recreate the tmux window and resume the previous session.
                    w = await tmux_manager.find_window_by_id(wid)
                    if not w:
                        key = (user_id, thread_id, wid)
                        if key not in _missing_bound_windows:
                            _missing_bound_windows.add(key)
                            logger.info(
                                "Bound window missing; keeping binding for recovery: "
                                "user=%d thread=%d window_id=%s",
                                user_id,
                                thread_id,
                                wid,
                            )
                        await enqueue_status_update(
                            bot, user_id, wid, None, thread_id=thread_id
                        )
                        continue
                    _missing_bound_windows.discard((user_id, thread_id, wid))

                    # UI detection happens unconditionally in update_status_message.
                    # Status enqueue is skipped inside update_status_message when
                    # interactive UI is detected (returns early) or when queue is non-empty.
                    queue = get_message_queue(user_id, thread_id)
                    skip_status = queue is not None and not queue.empty()

                    await update_status_message(
                        bot,
                        user_id,
                        wid,
                        thread_id=thread_id,
                        skip_status=skip_status,
                    )
                except Exception as e:
                    logger.debug(
                        f"Status update error for user {user_id} "
                        f"thread {thread_id}: {e}"
                    )
        except Exception as e:
            logger.error(f"Status poll loop error: {e}")

        await asyncio.sleep(poll_interval)
