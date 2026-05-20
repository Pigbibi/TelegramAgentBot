"""Per-user message queue management for ordered message delivery.

Provides a queue-based message processing system that ensures:
  - Messages are sent in receive order (FIFO)
  - Status messages always follow content messages
  - Consecutive content messages can be merged for efficiency
  - Thread-aware sending: each MessageTask carries an optional thread_id
    for Telegram topic support

Rate limiting is handled globally by AIORateLimiter on the Application.

Key components:
  - MessageTask: Dataclass representing a queued message task (with thread_id)
  - get_or_create_queue: Get or create queue and worker for a user
  - Message queue worker: Background task processing user's queue
  - Content task processing with tool_use/tool_result handling
  - Status message tracking and conversion (keyed by (user_id, thread_id))
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Literal

from telegram import Bot
from telegram.constants import ChatAction
from telegram.error import BadRequest, RetryAfter

from ..markdown_v2 import convert_markdown
from ..session import session_manager
from ..tmux_manager import tmux_manager
from .message_sender import (
    NO_LINK_PREVIEW,
    PARSE_MODE,
    send_photo,
    send_with_fallback,
    strip_sentinels,
)
from .working_status import mark_output_seen, status_text_for_pane

logger = logging.getLogger(__name__)


def _ensure_formatted(text: str) -> str:
    """Convert markdown to MarkdownV2."""
    return convert_markdown(text)


def _is_message_not_modified_error(exc: Exception) -> bool:
    """Return whether Telegram rejected an edit because content is unchanged."""
    return isinstance(exc, BadRequest) and "message is not modified" in str(exc).lower()


# Merge limit for content messages
MERGE_MAX_LENGTH = 3800  # Leave room for markdown conversion overhead


@dataclass
class MessageTask:
    """Message task for queue processing."""

    task_type: Literal["content", "status_update", "status_clear"]
    text: str | None = None
    window_id: str | None = None
    # content type fields
    parts: list[str] = field(default_factory=list)
    tool_use_id: str | None = None
    content_type: str = "text"
    role: str = "assistant"
    thread_id: int | None = None  # Telegram topic thread_id for targeted send
    image_data: list[tuple[str, bytes]] | None = None  # From tool_result images


# Per-target message queues and worker tasks.
# Keyed by (user_id, thread_id_or_0) so different Telegram topics/windows for
# the same user can be delivered independently while preserving ordering within
# each topic.
QueueKey = tuple[int, int]
_message_queues: dict[QueueKey, asyncio.Queue[MessageTask]] = {}
_queue_workers: dict[QueueKey, asyncio.Task[None]] = {}
_queue_locks: dict[QueueKey, asyncio.Lock] = {}  # Protect drain/refill operations
_active_queue_keys: set[QueueKey] = set()

# Map (tool_use_id, user_id, thread_id_or_0) -> telegram message_id
# for editing tool_use messages with results
_tool_msg_ids: dict[tuple[str, int, int], int] = {}

# Status message tracking: (user_id, thread_id_or_0) -> (message_id, window_id, last_text)
_status_msg_info: dict[tuple[int, int], tuple[int, str, str]] = {}

# Flood control: user_id -> monotonic time when ban expires
_flood_until: dict[int, float] = {}

# Max seconds to wait for flood control before dropping tasks
FLOOD_CONTROL_MAX_WAIT = 10


def _queue_key(user_id: int, thread_id: int | None = None) -> QueueKey:
    """Return the queue key for a user/topic target."""
    return (user_id, thread_id or 0)


def get_message_queue(
    user_id: int, thread_id: int | None = None
) -> asyncio.Queue[MessageTask] | None:
    """Get the message queue for a user/topic target (if exists)."""
    return _message_queues.get(_queue_key(user_id, thread_id))


def has_pending_message_work() -> bool:
    """Return whether Telegram message delivery has queued or active work."""
    return bool(_active_queue_keys) or any(
        not queue.empty() for queue in _message_queues.values()
    )


def get_or_create_queue(
    bot: Bot, user_id: int, thread_id: int | None = None
) -> asyncio.Queue[MessageTask]:
    """Get or create message queue and worker for a user/topic target."""
    key = _queue_key(user_id, thread_id)
    if key not in _message_queues:
        _message_queues[key] = asyncio.Queue()
        _queue_locks[key] = asyncio.Lock()
        # Start worker task for this user/topic target
        _queue_workers[key] = asyncio.create_task(
            _message_queue_worker(bot, user_id, key[1])
        )
    return _message_queues[key]


def _inspect_queue(queue: asyncio.Queue[MessageTask]) -> list[MessageTask]:
    """Non-destructively inspect all items in queue.

    Drains the queue and returns all items. Caller must refill.
    """
    items: list[MessageTask] = []
    while not queue.empty():
        try:
            item = queue.get_nowait()
            items.append(item)
        except asyncio.QueueEmpty:
            break
    return items


def _can_merge_tasks(base: MessageTask, candidate: MessageTask) -> bool:
    """Check if two content tasks can be merged."""
    if base.window_id != candidate.window_id:
        return False
    if candidate.task_type != "content":
        return False
    # tool_use/tool_result break merge chain
    # - tool_use: will be edited later by tool_result
    # - tool_result: edits previous message, merging would cause order issues
    if base.content_type in ("tool_use", "tool_result"):
        return False
    if candidate.content_type in ("tool_use", "tool_result"):
        return False
    return True


async def _merge_content_tasks(
    queue: asyncio.Queue[MessageTask],
    first: MessageTask,
    lock: asyncio.Lock,
) -> tuple[MessageTask, int]:
    """Merge consecutive content tasks from queue.

    Returns: (merged_task, merge_count) where merge_count is the number of
    additional tasks merged (0 if no merging occurred).

    Note on queue counter management:
        When we put items back, we call task_done() to compensate for the
        internal counter increment caused by put_nowait(). This is necessary
        because the items were already counted when originally enqueued.
        Without this compensation, queue.join() would wait indefinitely.
    """
    merged_parts = list(first.parts)
    current_length = sum(len(p) for p in merged_parts)
    merge_count = 0

    async with lock:
        items = _inspect_queue(queue)
        remaining: list[MessageTask] = []

        for i, task in enumerate(items):
            if not _can_merge_tasks(first, task):
                # Can't merge, keep this and all remaining items
                remaining = items[i:]
                break

            # Check length before merging
            task_length = sum(len(p) for p in task.parts)
            if current_length + task_length > MERGE_MAX_LENGTH:
                # Too long, stop merging
                remaining = items[i:]
                break

            merged_parts.extend(task.parts)
            current_length += task_length
            merge_count += 1

        # Put remaining items back into the queue
        for item in remaining:
            queue.put_nowait(item)
            # Compensate: this item was already counted when first enqueued,
            # put_nowait adds a duplicate count that must be removed
            queue.task_done()

    if merge_count == 0:
        return first, 0

    return (
        MessageTask(
            task_type="content",
            window_id=first.window_id,
            parts=merged_parts,
            tool_use_id=first.tool_use_id,
            content_type=first.content_type,
            role=first.role,
            thread_id=first.thread_id,
        ),
        merge_count,
    )


async def _enqueue_coalesced_status_task(
    queue: asyncio.Queue[MessageTask],
    task: MessageTask,
    lock: asyncio.Lock,
) -> int:
    """Enqueue the latest status task, dropping stale pending status tasks.

    Status updates are ephemeral. Older pending status updates can bury actual
    Codex output behind a long queue of Working timer edits, so preserve content
    tasks and keep only the newest pending status task for the target topic.
    A pending status_clear is different when the new task is status_update: it
    intentionally deletes the old Telegram bubble before a fresh Working bubble
    is sent below the user's latest message.
    """
    dropped = 0

    async with lock:
        items = _inspect_queue(queue)
        kept_status_clear = False
        for item in items:
            if item.task_type == "status_update":
                queue.task_done()
                dropped += 1
                continue

            if item.task_type == "status_clear":
                if task.task_type == "status_update" and not kept_status_clear:
                    queue.put_nowait(item)
                    queue.task_done()
                    kept_status_clear = True
                    continue

                queue.task_done()
                dropped += 1
                continue

            queue.put_nowait(item)
            # Compensate for put_nowait increment. This task was already counted
            # when originally enqueued.
            queue.task_done()

        queue.put_nowait(task)

    return dropped


async def _message_queue_worker(bot: Bot, user_id: int, thread_id_or_0: int) -> None:
    """Process message tasks for a user/topic target sequentially."""
    key = _queue_key(user_id, thread_id_or_0)
    queue = _message_queues[key]
    lock = _queue_locks[key]
    logger.info(
        "Message queue worker started for user %d thread %d",
        user_id,
        thread_id_or_0,
    )

    while True:
        try:
            task = await queue.get()
            _active_queue_keys.add(key)
            try:
                # Flood control: drop status, wait for content
                flood_end = _flood_until.get(user_id, 0)
                if flood_end > 0:
                    remaining = flood_end - time.monotonic()
                    if remaining > 0:
                        if task.task_type != "content":
                            # Status is ephemeral — safe to drop
                            continue
                        # Content is actual Codex output — wait then send
                        logger.debug(
                            "Flood controlled: waiting %.0fs for content (user %d)",
                            remaining,
                            user_id,
                        )
                        await asyncio.sleep(remaining)
                    # Ban expired
                    _flood_until.pop(user_id, None)
                    logger.info("Flood control lifted for user %d", user_id)

                if task.task_type == "content":
                    # Try to merge consecutive content tasks
                    merged_task, merge_count = await _merge_content_tasks(
                        queue, task, lock
                    )
                    if merge_count > 0:
                        logger.debug(f"Merged {merge_count} tasks for user {user_id}")
                        # Mark merged tasks as done
                        for _ in range(merge_count):
                            queue.task_done()
                    await _process_content_task(bot, user_id, merged_task)
                elif task.task_type == "status_update":
                    await _process_status_update_task(bot, user_id, task)
                elif task.task_type == "status_clear":
                    await _do_clear_status_message(bot, user_id, task.thread_id or 0)
            except RetryAfter as e:
                retry_secs = (
                    e.retry_after
                    if isinstance(e.retry_after, int)
                    else int(e.retry_after.total_seconds())
                )
                if retry_secs > FLOOD_CONTROL_MAX_WAIT:
                    _flood_until[user_id] = time.monotonic() + retry_secs
                    logger.warning(
                        "Flood control for user %d: retry_after=%ds, "
                        "pausing queue until ban expires",
                        user_id,
                        retry_secs,
                    )
                else:
                    logger.warning(
                        "Flood control for user %d: waiting %ds",
                        user_id,
                        retry_secs,
                    )
                    await asyncio.sleep(retry_secs)
            except Exception as e:
                logger.error(f"Error processing message task for user {user_id}: {e}")
            finally:
                _active_queue_keys.discard(key)
                queue.task_done()
        except asyncio.CancelledError:
            logger.info(
                "Message queue worker cancelled for user %d thread %d",
                user_id,
                thread_id_or_0,
            )
            break
        except Exception as e:
            logger.error(
                "Unexpected error in queue worker for user %d thread %d: %s",
                user_id,
                thread_id_or_0,
                e,
            )


def _send_kwargs(thread_id: int | None) -> dict[str, int]:
    """Build message_thread_id kwargs for bot.send_message()."""
    if thread_id is not None:
        return {"message_thread_id": thread_id}
    return {}


async def _send_task_images(bot: Bot, chat_id: int, task: MessageTask) -> None:
    """Send images attached to a task, if any."""
    if not task.image_data:
        return
    logger.info(
        "Sending %d image(s) in thread %s",
        len(task.image_data),
        task.thread_id,
    )
    await send_photo(
        bot,
        chat_id,
        task.image_data,
        **_send_kwargs(task.thread_id),  # type: ignore[arg-type]
    )


async def _process_content_task(bot: Bot, user_id: int, task: MessageTask) -> None:
    """Process a content message task."""
    wid = task.window_id or ""
    tid = task.thread_id or 0
    chat_id = session_manager.resolve_chat_id(user_id, task.thread_id)
    logger.debug(
        "Process content: user=%d, thread=%d, window_id=%s, content_type=%s, parts=%d",
        user_id,
        tid,
        wid,
        task.content_type,
        len(task.parts),
    )

    # 1. Handle tool_result editing (merged parts are edited together)
    if task.content_type == "tool_result" and task.tool_use_id:
        _tkey = (task.tool_use_id, user_id, tid)
        edit_msg_id = _tool_msg_ids.pop(_tkey, None)
        if edit_msg_id is not None:
            # Clear status message first
            await _do_clear_status_message(bot, user_id, tid)
            # Join all parts for editing (merged content goes together)
            full_text = "\n\n".join(task.parts)
            try:
                await bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=edit_msg_id,
                    text=_ensure_formatted(full_text),
                    parse_mode=PARSE_MODE,
                    link_preview_options=NO_LINK_PREVIEW,
                )
                logger.info(
                    "Delivered tool result by editing message: user=%d thread=%d "
                    "message_id=%d",
                    user_id,
                    tid,
                    edit_msg_id,
                )
                await _send_task_images(bot, chat_id, task)
                await _check_and_send_status(bot, user_id, wid, task.thread_id)
                return
            except RetryAfter:
                raise
            except Exception:
                try:
                    # Fallback: plain text with sentinels stripped
                    plain_text = strip_sentinels(task.text or full_text)
                    await bot.edit_message_text(
                        chat_id=chat_id,
                        message_id=edit_msg_id,
                        text=plain_text,
                        link_preview_options=NO_LINK_PREVIEW,
                    )
                    logger.info(
                        "Delivered tool result by plain edit: user=%d thread=%d "
                        "message_id=%d",
                        user_id,
                        tid,
                        edit_msg_id,
                    )
                    await _send_task_images(bot, chat_id, task)
                    await _check_and_send_status(bot, user_id, wid, task.thread_id)
                    return
                except RetryAfter:
                    raise
                except Exception:
                    logger.debug(f"Failed to edit tool msg {edit_msg_id}, sending new")
                    # Fall through to send as new message

    # 2. Send content messages, converting status message to first content part
    first_part = True
    last_msg_id: int | None = None
    for part in task.parts:
        sent = None

        # For first part, try to convert status message to content (edit instead of delete)
        if first_part:
            first_part = False
            converted_msg_id = await _convert_status_to_content(
                bot,
                user_id,
                tid,
                wid,
                part,
            )
            if converted_msg_id is not None:
                logger.info(
                    "Delivered content by editing status message: user=%d thread=%d "
                    "message_id=%d",
                    user_id,
                    tid,
                    converted_msg_id,
                )
                last_msg_id = converted_msg_id
                continue

        sent = await send_with_fallback(
            bot,
            chat_id,
            part,
            **_send_kwargs(task.thread_id),  # type: ignore[arg-type]
        )

        if sent:
            logger.info(
                "Delivered content message: user=%d thread=%d message_id=%d",
                user_id,
                tid,
                sent.message_id,
            )
            last_msg_id = sent.message_id
        else:
            logger.warning(
                "Content send returned no Telegram message: user=%d thread=%d",
                user_id,
                tid,
            )

    # 3. Record tool_use message ID for later editing
    if last_msg_id and task.tool_use_id and task.content_type == "tool_use":
        _tool_msg_ids[(task.tool_use_id, user_id, tid)] = last_msg_id

    # 4. Send images if present (from tool_result with base64 image blocks)
    await _send_task_images(bot, chat_id, task)

    # 5. After content, check and send status
    if task.role == "assistant":
        mark_output_seen(user_id, task.thread_id, wid)
    await _check_and_send_status(bot, user_id, wid, task.thread_id)


async def _convert_status_to_content(
    bot: Bot,
    user_id: int,
    thread_id_or_0: int,
    window_id: str,
    content_text: str,
) -> int | None:
    """Convert status message to content message by editing it.

    Returns the message_id if converted successfully, None otherwise.
    """
    skey = (user_id, thread_id_or_0)
    info = _status_msg_info.pop(skey, None)
    if not info:
        return None

    msg_id, stored_wid, _ = info
    chat_id = session_manager.resolve_chat_id(user_id, thread_id_or_0 or None)
    if stored_wid != window_id:
        # Different window, just delete the old status
        try:
            await bot.delete_message(chat_id=chat_id, message_id=msg_id)
        except Exception:
            pass
        return None

    # Edit status message to show content
    try:
        await bot.edit_message_text(
            chat_id=chat_id,
            message_id=msg_id,
            text=_ensure_formatted(content_text),
            parse_mode=PARSE_MODE,
            link_preview_options=NO_LINK_PREVIEW,
        )
        return msg_id
    except RetryAfter:
        raise
    except Exception:
        try:
            # Fallback to plain text with sentinels stripped
            plain = strip_sentinels(content_text)
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=msg_id,
                text=plain,
                link_preview_options=NO_LINK_PREVIEW,
            )
            return msg_id
        except RetryAfter:
            raise
        except Exception as e:
            logger.debug(f"Failed to convert status to content: {e}")
            # The caller will send content as a fresh message. Delete the old
            # status bubble so it does not remain above the content while a new
            # Working bubble is sent below it.
            try:
                await bot.delete_message(chat_id=chat_id, message_id=msg_id)
            except Exception as delete_exc:
                logger.debug(
                    "Failed to delete unconverted status message %s: %s",
                    msg_id,
                    delete_exc,
                )
            return None


async def _process_status_update_task(
    bot: Bot, user_id: int, task: MessageTask
) -> None:
    """Process a status update task."""
    wid = task.window_id or ""
    tid = task.thread_id or 0
    chat_id = session_manager.resolve_chat_id(user_id, task.thread_id)
    skey = (user_id, tid)
    status_text = task.text or ""

    if not status_text:
        # No status text means clear status
        await _do_clear_status_message(bot, user_id, tid)
        return

    current_info = _status_msg_info.get(skey)

    if current_info:
        msg_id, stored_wid, last_text = current_info

        if stored_wid != wid:
            # Window changed - delete old and send new
            await _do_clear_status_message(bot, user_id, tid)
            await _do_send_status_message(bot, user_id, tid, wid, status_text)
        elif status_text == last_text:
            # Same content, skip edit
            return
        else:
            # Same window, text changed - edit in place
            # Send typing indicator when Codex is working
            if "esc to interrupt" in status_text.lower():
                try:
                    await bot.send_chat_action(
                        chat_id=chat_id, action=ChatAction.TYPING
                    )
                except RetryAfter:
                    raise
                except Exception:
                    pass
            try:
                await bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=msg_id,
                    text=_ensure_formatted(status_text),
                    parse_mode=PARSE_MODE,
                    link_preview_options=NO_LINK_PREVIEW,
                )
                _status_msg_info[skey] = (msg_id, wid, status_text)
            except RetryAfter:
                raise
            except Exception as e:
                if _is_message_not_modified_error(e):
                    _status_msg_info[skey] = (msg_id, wid, status_text)
                    return
                try:
                    await bot.edit_message_text(
                        chat_id=chat_id,
                        message_id=msg_id,
                        text=status_text,
                        link_preview_options=NO_LINK_PREVIEW,
                    )
                    _status_msg_info[skey] = (msg_id, wid, status_text)
                except RetryAfter:
                    raise
                except Exception as e:
                    if _is_message_not_modified_error(e):
                        _status_msg_info[skey] = (msg_id, wid, status_text)
                        return
                    logger.debug(f"Failed to edit status message: {e}")
                    _status_msg_info.pop(skey, None)
                    await _do_send_status_message(bot, user_id, tid, wid, status_text)
    else:
        # No existing status message, send new
        await _do_send_status_message(bot, user_id, tid, wid, status_text)


async def _do_send_status_message(
    bot: Bot,
    user_id: int,
    thread_id_or_0: int,
    window_id: str,
    text: str,
) -> None:
    """Send a new status message and track it (internal, called from worker)."""
    skey = (user_id, thread_id_or_0)
    thread_id: int | None = thread_id_or_0 if thread_id_or_0 != 0 else None
    chat_id = session_manager.resolve_chat_id(user_id, thread_id)
    # Safety net: delete any orphaned status message before sending a new one.
    # This catches edge cases where tracking was cleared without deleting the message.
    old = _status_msg_info.pop(skey, None)
    if old:
        try:
            await bot.delete_message(chat_id=chat_id, message_id=old[0])
        except Exception:
            pass
    # Send typing indicator when Codex is working
    if "esc to interrupt" in text.lower():
        try:
            await bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
        except RetryAfter:
            raise
        except Exception:
            pass
    sent = await send_with_fallback(
        bot,
        chat_id,
        text,
        **_send_kwargs(thread_id),  # type: ignore[arg-type]
    )
    if sent:
        _status_msg_info[skey] = (sent.message_id, window_id, text)


async def _do_clear_status_message(
    bot: Bot,
    user_id: int,
    thread_id_or_0: int = 0,
) -> None:
    """Delete the status message for a user (internal, called from worker)."""
    skey = (user_id, thread_id_or_0)
    info = _status_msg_info.pop(skey, None)
    if info:
        msg_id = info[0]
        chat_id = session_manager.resolve_chat_id(user_id, thread_id_or_0 or None)
        try:
            await bot.delete_message(chat_id=chat_id, message_id=msg_id)
        except Exception as e:
            logger.debug(f"Failed to delete status message {msg_id}: {e}")


async def _check_and_send_status(
    bot: Bot,
    user_id: int,
    window_id: str,
    thread_id: int | None = None,
) -> None:
    """Check terminal for status line and send status message if present."""
    # Skip if there are more messages pending in the queue
    queue = get_message_queue(user_id, thread_id)
    if queue and not queue.empty():
        return
    w = await tmux_manager.find_window_by_id(window_id)
    if not w:
        return

    pane_text = await tmux_manager.capture_pane(w.window_id)
    if not pane_text:
        return

    tid = thread_id or 0
    status_text = status_text_for_pane(user_id, thread_id, window_id, pane_text)
    if status_text:
        await _do_send_status_message(bot, user_id, tid, window_id, status_text)


async def enqueue_content_message(
    bot: Bot,
    user_id: int,
    window_id: str,
    parts: list[str],
    tool_use_id: str | None = None,
    content_type: str = "text",
    role: str = "assistant",
    text: str | None = None,
    thread_id: int | None = None,
    image_data: list[tuple[str, bytes]] | None = None,
    wait_until_sent: bool = False,
) -> None:
    """Enqueue a content message task."""
    logger.debug(
        "Enqueue content: user=%d, thread=%s, window_id=%s, content_type=%s",
        user_id,
        thread_id,
        window_id,
        content_type,
    )
    queue = get_or_create_queue(bot, user_id, thread_id)

    task = MessageTask(
        task_type="content",
        text=text,
        window_id=window_id,
        parts=parts,
        tool_use_id=tool_use_id,
        content_type=content_type,
        role=role,
        thread_id=thread_id,
        image_data=image_data,
    )
    queue.put_nowait(task)
    if wait_until_sent:
        await queue.join()


async def enqueue_status_update(
    bot: Bot,
    user_id: int,
    window_id: str,
    status_text: str | None,
    thread_id: int | None = None,
) -> None:
    """Enqueue status update. Skipped if text unchanged or during flood control."""
    # Don't enqueue during flood control — they'd just be dropped
    flood_end = _flood_until.get(user_id, 0)
    if flood_end > time.monotonic():
        return

    tid = thread_id or 0

    # Deduplicate: skip if text matches what's already displayed
    if status_text:
        skey = (user_id, tid)
        info = _status_msg_info.get(skey)
        if info and info[1] == window_id and info[2] == status_text:
            return

    key = _queue_key(user_id, thread_id)
    queue = get_or_create_queue(bot, user_id, thread_id)

    if status_text:
        task = MessageTask(
            task_type="status_update",
            text=status_text,
            window_id=window_id,
            thread_id=thread_id,
        )
    else:
        task = MessageTask(task_type="status_clear", thread_id=thread_id)

    dropped = await _enqueue_coalesced_status_task(queue, task, _queue_locks[key])
    if dropped:
        logger.debug(
            "Coalesced %d queued status tasks for user=%d thread=%d",
            dropped,
            user_id,
            tid,
        )


def clear_status_msg_info(user_id: int, thread_id: int | None = None) -> None:
    """Clear status message tracking for a user (and optionally a specific thread)."""
    skey = (user_id, thread_id or 0)
    _status_msg_info.pop(skey, None)


def clear_tool_msg_ids_for_topic(user_id: int, thread_id: int | None = None) -> None:
    """Clear tool message ID tracking for a specific topic.

    Removes all entries in _tool_msg_ids that match the given user and thread.
    """
    tid = thread_id or 0
    # Find and remove all matching keys
    keys_to_remove = [
        key for key in _tool_msg_ids if key[1] == user_id and key[2] == tid
    ]
    for key in keys_to_remove:
        _tool_msg_ids.pop(key, None)


async def shutdown_workers() -> None:
    """Stop all queue workers (called during bot shutdown)."""
    queues = list(_message_queues.items())
    if queues:
        try:
            await asyncio.wait_for(
                asyncio.gather(*(queue.join() for _, queue in queues)),
                timeout=10.0,
            )
        except TimeoutError:
            pending = {
                key: queue.qsize()
                for key, queue in queues
                if not queue.empty() or key in _active_queue_keys
            }
            logger.warning(
                "Timed out waiting for message queues to drain before shutdown: %s",
                pending,
            )

    for _, worker in list(_queue_workers.items()):
        worker.cancel()
        try:
            await worker
        except asyncio.CancelledError:
            pass
    _queue_workers.clear()
    _message_queues.clear()
    _queue_locks.clear()
    _active_queue_keys.clear()
    logger.info("Message queue workers stopped")
