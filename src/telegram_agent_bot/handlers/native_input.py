"""Native active-turn input routing, durable input spool, and confirmation."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, cast

from telegram import Bot, Update
from telegram.constants import ChatAction
from telegram.error import BadRequest, TelegramError
from telegram.ext import ContextTypes

from ..agent_io import (
    capture_agent_output,
    send_agent_control,
    send_agent_input,
    send_agent_message,
)
from ..agent_profile import (
    AGENT_CODEX,
    agent_capabilities,
    agent_display_name,
    is_claude_agent,
    normalize_agent_type,
)
from ..backends.input_routing import AgentInputMode
from ..config import config
from ..durable_state import (
    AGENT_INPUT_MODE_QUEUE,
    AGENT_INPUT_MODE_STEER,
    AGENT_INPUT_MODE_TURN,
    AGENT_INPUT_QUEUED,
    AGENT_INPUT_SUBMITTED_UNCONFIRMED,
    TRANSIENT_RETRY_SCHEDULED,
    DurableRuntimeStore,
    PendingAgentInput,
    TransientAgentRetry,
)
from ..idle_sessions import idle_session_hibernator
from ..session import is_shell_pane_command, session_manager
from ..terminal_parser import (
    extract_auth_error_message,
    extract_interactive_content,
    is_agent_cli_startup_error,
    is_codex_input_ready,
    is_interactive_ui,
)
from ..tmux_manager import tmux_manager
from ..turn_admission import turn_admission
from ..utils import sanitize_forward_text
from .agent_login import codex_auth_recovery_message
from .input_routing import (
    INPUT_ROUTE_CANCEL,
    INPUT_ROUTE_INTERRUPT,
    INPUT_ROUTE_QUEUE,
    build_input_routing_keyboard,
)
from .message_queue import enqueue_status_update
from .message_sender import safe_edit, safe_reply, safe_send
from .status_polling import mark_window_working

logger = logging.getLogger(__name__)

BOT_WRAPPER_MARKER = object()


@dataclass
class QueuedAgentInput:
    """In-memory mirror of one durable queued or unconfirmed agent prompt."""

    text: str
    created_at: float = field(default_factory=time.monotonic)
    created_at_epoch: float = field(default_factory=time.time)
    store_id: int | None = None
    state: str = AGENT_INPUT_QUEUED
    transcript_session_id: str | None = None
    transcript_offset: int | None = None
    submission_mode: str = AGENT_INPUT_MODE_TURN


AGENT_INPUT_POLL_INTERVAL_SECONDS = 1.0
AGENT_INPUT_PERSIST_RETRY_SECONDS = 5.0
AGENT_INPUT_CONFIRM_RETRY_SECONDS = 60.0
AGENT_INPUT_CONFIRM_MAX_WAIT_SECONDS = 5 * 60.0
INPUT_ROUTE_CHOICE_MAX_AGE_SECONDS = 15 * 60.0
TRANSIENT_AGENT_RETRY_DELAYS_SECONDS = (15.0, 45.0, 120.0)
TRANSIENT_AGENT_ERROR_CODES = frozenset({"server_overloaded"})
TRANSIENT_AGENT_RETRY_PROMPT = (
    "Continue the previous user request from where it stopped. The previous turn "
    "ended because the selected model was temporarily at capacity. Do not repeat "
    "completed work; finish the unfinished task and provide a final answer."
)

_agent_input_queues: dict[tuple[int, int, str], deque[QueuedAgentInput]] = {}
_agent_input_tasks: dict[tuple[int, int, str], asyncio.Task] = {}
_agent_input_locks: dict[tuple[int, int], asyncio.Lock] = {}
_agent_input_confirmation_tasks: dict[int, asyncio.Task[None]] = {}
_agent_input_confirmation_targets: dict[int, tuple[int, int, str]] = {}
_agent_input_confirmation_modes: dict[int, str] = {}
_transient_agent_retry_attempts: dict[str, int] = {}
_transient_agent_retry_tasks: dict[str, asyncio.Task[None]] = {}
_fallback_runtime_store: DurableRuntimeStore | None = None


def _dep(runtime: Any, name: str, default: Any) -> Any:
    return getattr(runtime, name, default) if runtime is not None else default


def _rt_fn(
    runtime: Any,
    attr_name: str,
    default_fn: Callable[..., Any],
) -> tuple[Callable[..., Any], bool]:
    """Return an overridden runtime callable when patched, or the default."""
    if runtime is not None:
        candidate = getattr(runtime, attr_name, None)
        if (
            candidate is not None
            and getattr(candidate, "_bot_wrapper_marker", None)
            is not BOT_WRAPPER_MARKER
        ):
            return candidate, True
    return default_fn, False


def _runtime_store(runtime: Any) -> DurableRuntimeStore:
    global _fallback_runtime_store
    if runtime is not None:
        store = getattr(runtime, "_runtime_store", None)
        if store is not None:
            return store
    if _fallback_runtime_store is None:
        _fallback_runtime_store = DurableRuntimeStore(
            config.config_dir / "runtime.sqlite3"
        )
    return _fallback_runtime_store


def _default_thread_id(update: Update) -> int | None:
    msg = update.message or (
        update.callback_query.message if update.callback_query else None
    )
    if msg is None:
        return None
    tid = getattr(msg, "message_thread_id", None)
    if tid is None or tid == 1:
        return None
    return tid


def _default_is_user_allowed(user_id: int | None, cfg: Any) -> bool:
    return user_id is not None and cfg.is_user_allowed(user_id)


async def _default_safe_send_typing_action(chat: Any, *, source: str) -> None:
    try:
        await chat.send_action(ChatAction.TYPING)
    except TelegramError as exc:
        logger.debug("Failed to send typing action (%s): %s", source, exc)


async def safe_answer_callback(query: Any, *args: Any, **kwargs: Any) -> bool:
    """Answer a callback while treating an expired query as harmless."""
    try:
        await query.answer(*args, **kwargs)
        return True
    except BadRequest as exc:
        message = str(exc).lower()
        if "query is too old" in message or "query id is invalid" in message:
            logger.debug("Callback query answer skipped because it expired")
            return False
        raise


async def send_control_to_agent(
    user_id: int,
    thread_id: int | None,
    window_id: str,
    key: str,
    *,
    runtime: Any = None,
) -> tuple[bool, str]:
    """Send a control key to the agent target bound to a topic."""
    send_ctrl = _dep(runtime, "send_agent_control", send_agent_control)
    result = await send_ctrl(user_id, thread_id, window_id, key)
    if result is None:
        return False, "No session bound"
    if result.missing:
        return False, "Window not found"
    if not result.ok:
        return False, result.message or f"Failed to send {key}"
    return True, ""


async def send_message_to_agent(
    user_id: int,
    thread_id: int | None,
    window_id: str,
    text: str,
    *,
    runtime: Any = None,
) -> tuple[bool, str]:
    """Send a plain text prompt to the agent target bound to a topic."""
    send_msg = _dep(runtime, "send_agent_message", send_agent_message)
    result = await send_msg(user_id, thread_id, window_id, text)
    if result is None:
        return False, "No session bound"
    if result.missing:
        return False, "Window not found (may have been closed)"
    return result.ok, result.message


async def send_native_input_to_agent(
    user_id: int,
    thread_id: int | None,
    window_id: str,
    text: str,
    *,
    mode: str,
    runtime: Any = None,
) -> tuple[bool, str]:
    """Submit native steer/queue input to a busy agent session."""
    if mode not in {AGENT_INPUT_MODE_STEER, AGENT_INPUT_MODE_QUEUE}:
        return False, f"Unsupported native input mode: {mode}"
    send_input = _dep(runtime, "send_agent_input", send_agent_input)
    result = await send_input(
        user_id,
        thread_id,
        window_id,
        text,
        mode=cast(AgentInputMode, mode),
    )
    if result is None:
        return False, "No session bound"
    if result.missing:
        return False, "Window not found (may have been closed)"
    return result.ok, result.message


def window_agent_type(window_id: str, *, runtime: Any = None) -> str:
    """Return the persisted agent type for a local window."""
    sm = _dep(runtime, "session_manager", session_manager)
    cfg = _dep(runtime, "config", config)
    state = sm.window_states.get(window_id)
    return normalize_agent_type(
        state.agent_type if state and state.agent_type else cfg.agent_type
    )


def window_supports_native_input_routing(
    window_id: str, *, runtime: Any = None
) -> bool:
    """Return whether this window supports an Enter-based active-turn steer."""
    fn, patched = _rt_fn(runtime, "_window_agent_type", window_agent_type)
    agent_type = fn(window_id) if patched else fn(window_id, runtime=runtime)
    return agent_capabilities(agent_type).supports_native_steer


def route_can_use_native_input(
    key: tuple[int, int, str], *, runtime: Any = None
) -> bool:
    """Avoid reordering native input around the legacy bot-side FIFO."""
    return not (
        route_has_agent_input_queue(key, runtime=runtime)
        or route_has_agent_input_drain(key, runtime=runtime)
        or target_has_agent_input_confirmation(key, runtime=runtime)
        or target_has_persisted_agent_input_confirmation(key, runtime=runtime)
    )


async def offer_input_route_choice(
    message: Any,
    *,
    user_id: int,
    thread_id: int,
    window_id: str,
    text: str,
    runtime: Any = None,
) -> bool:
    """Persist a busy-turn message and attach its native routing controls."""
    fn, patched = _rt_fn(runtime, "_window_agent_type", window_agent_type)
    agent_type = fn(window_id) if patched else fn(window_id, runtime=runtime)
    display_name = agent_display_name(agent_type)
    store = _runtime_store(runtime)
    time_mod = _dep(runtime, "time", time)
    max_age = _dep(
        runtime,
        "_INPUT_ROUTE_CHOICE_MAX_AGE_SECONDS",
        INPUT_ROUTE_CHOICE_MAX_AGE_SECONDS,
    )
    reply_fn = _dep(runtime, "safe_reply", safe_reply)
    try:
        store.delete_expired_input_choices(time_mod.time() - max_age)
        record = store.create_pending_input_choice(
            user_id=user_id,
            thread_id=thread_id,
            window_id=window_id,
            text=text,
            max_pending=agent_input_queue_max_size(runtime=runtime),
        )
    except Exception:
        logger.exception(
            "Failed to persist input route choice (user=%d thread=%d window=%s)",
            user_id,
            thread_id,
            window_id,
        )
        record = None
    if record is None:
        await reply_fn(
            message,
            "❌ 无法安全保存这条输入，或待选择消息已满；请稍后重试。",
        )
        return False
    await reply_fn(
        message,
        f"{display_name} 正在工作。请选择这条消息的处理方式：",
        reply_markup=build_input_routing_keyboard(
            record.id,
            native_queue=agent_capabilities(agent_type).supports_native_queue,
        ),
    )
    return True


def agent_input_key(
    user_id: int,
    thread_id: int | None,
    window_id: str,
    *,
    runtime: Any = None,
) -> tuple[int, int, str]:
    sm = _dep(runtime, "session_manager", session_manager)
    owner_id = sm.conversation_owner_id(user_id, thread_id)
    return (owner_id, thread_id or 0, window_id)


def should_offer_input_route_choice(
    *,
    user_id: int,
    thread_id: int,
    window_id: str,
    pane_text: str | None,
    runtime: Any = None,
) -> bool:
    """Return whether a busy local agent can route this input natively."""
    current_output = pane_text or ""
    supports_fn, supports_patched = _rt_fn(
        runtime,
        "_window_supports_native_input_routing",
        window_supports_native_input_routing,
    )
    can_use_fn, can_use_patched = _rt_fn(
        runtime,
        "_route_can_use_native_input",
        route_can_use_native_input,
    )
    key = agent_input_key(user_id, thread_id, window_id, runtime=runtime)
    supports = (
        supports_fn(window_id)
        if supports_patched
        else supports_fn(window_id, runtime=runtime)
    )
    can_use = can_use_fn(key) if can_use_patched else can_use_fn(key, runtime=runtime)
    return (
        not is_codex_input_ready(current_output)
        and bool(current_output.strip())
        and not is_interactive_ui(current_output)
        and bool(supports)
        and bool(can_use)
    )


def agent_input_lock(key: tuple[int, int, str], *, runtime: Any = None) -> asyncio.Lock:
    locks = _dep(runtime, "_agent_input_locks", _agent_input_locks)
    route = key[:2]
    lock = locks.get(route)
    if lock is None:
        lock = asyncio.Lock()
        locks[route] = lock
    return lock


def reset_transient_agent_retry(window_id: str, *, runtime: Any = None) -> None:
    """Cancel a pending automatic retry when a real user sends new input."""
    tasks = _dep(runtime, "_transient_agent_retry_tasks", _transient_agent_retry_tasks)
    attempts = _dep(
        runtime, "_transient_agent_retry_attempts", _transient_agent_retry_attempts
    )
    store = _runtime_store(runtime)
    task = tasks.pop(window_id, None)
    if task and task is not asyncio.current_task() and not task.done():
        task.cancel()
    attempts.pop(window_id, None)
    try:
        store.delete_transient_agent_retry(window_id)
    except Exception:
        logger.exception(
            "Failed to clear persisted transient retry for window %s", window_id
        )


def agent_input_queue_max_size(*, runtime: Any = None) -> int:
    cfg = _dep(runtime, "config", config)
    return max(1, int(getattr(cfg, "agent_input_queue_max_size", 20)))


def agent_input_queue_max_wait_seconds(*, runtime: Any = None) -> float:
    cfg = _dep(runtime, "config", config)
    return max(
        0.0,
        float(getattr(cfg, "agent_input_queue_max_wait_seconds", 0.0)),
    )


def agent_max_active_turns(*, runtime: Any = None) -> int:
    cfg = _dep(runtime, "config", config)
    return max(0, int(getattr(cfg, "agent_max_active_turns", 2)))


def queue_agent_input(
    key: tuple[int, int, str],
    text: str,
    *,
    submission_mode: str = AGENT_INPUT_MODE_TURN,
    runtime: Any = None,
) -> tuple[bool, int, int]:
    queues = _dep(runtime, "_agent_input_queues", _agent_input_queues)
    store = _runtime_store(runtime)
    admission = _dep(runtime, "turn_admission", turn_admission)
    queue = queues.setdefault(key, deque())
    limit = agent_input_queue_max_size(runtime=runtime)
    if len(queue) >= limit:
        return False, len(queue), limit
    try:
        record = store.enqueue_agent_input(
            user_id=key[0],
            thread_id=key[1],
            window_id=key[2],
            text=text,
            max_pending=limit,
            submission_mode=submission_mode,
        )
    except Exception:
        logger.exception(
            "Failed to persist queued agent input (user=%d thread=%d window=%s)",
            key[0],
            key[1],
            key[2],
        )
        return False, len(queue), limit
    if record is None:
        return False, len(queue), limit
    queue.append(
        QueuedAgentInput(
            text=text,
            created_at_epoch=record.created_at_epoch,
            store_id=record.id,
            submission_mode=record.submission_mode,
        )
    )
    admission.set_pending(key[2], pending=True)
    return True, len(queue), limit


def delete_persisted_agent_inputs(
    items: list[QueuedAgentInput], *, runtime: Any = None
) -> None:
    record_ids = [item.store_id for item in items if item.store_id is not None]
    if not record_ids:
        return
    store = _runtime_store(runtime)
    try:
        store.delete_agent_inputs(record_ids)
    except Exception:
        logger.exception(
            "Failed to remove %d persisted agent input(s)", len(record_ids)
        )


async def mark_persisted_agent_input_submitted(
    item: QueuedAgentInput,
    window_id: str,
    *,
    runtime: Any = None,
) -> bool:
    """Durably enter the no-auto-resend state before touching tmux."""
    sm = _dep(runtime, "session_manager", session_manager)
    store = _runtime_store(runtime)
    try:
        (
            session_id,
            transcript_offset,
        ) = await sm.transcript_confirmation_baseline(window_id)
    except Exception:
        logger.exception(
            "Failed to capture transcript confirmation baseline (window=%s)",
            window_id,
        )
        return False
    if item.store_id is None:
        item.state = AGENT_INPUT_SUBMITTED_UNCONFIRMED
        item.transcript_session_id = session_id
        item.transcript_offset = transcript_offset
        return True
    try:
        changed = store.mark_agent_input_submitted(
            item.store_id,
            transcript_session_id=session_id,
            transcript_offset=transcript_offset,
        )
    except Exception:
        logger.exception(
            "Failed to persist agent input submission state (record=%d)",
            item.store_id,
        )
        return False
    if not changed:
        logger.error(
            "Queued agent input was not in a dispatchable state (record=%d)",
            item.store_id,
        )
        return False
    item.state = AGENT_INPUT_SUBMITTED_UNCONFIRMED
    item.transcript_session_id = session_id
    item.transcript_offset = transcript_offset
    return True


def confirm_persisted_agent_input(
    item: QueuedAgentInput, *, runtime: Any = None
) -> None:
    """Remove one lifecycle record after transcript confirmation."""
    if item.store_id is None:
        return
    store = _runtime_store(runtime)
    try:
        store.confirm_agent_input(item.store_id)
    except Exception:
        logger.exception(
            "Failed to confirm persisted agent input (record=%d)", item.store_id
        )


def clear_persisted_agent_input_target(
    key: tuple[int, int, str], *, runtime: Any = None
) -> None:
    store = _runtime_store(runtime)
    try:
        store.delete_agent_inputs_for_target(*key)
    except Exception:
        logger.exception(
            "Failed to clear persisted agent input target "
            "(user=%d thread=%d window=%s)",
            key[0],
            key[1],
            key[2],
        )


def clear_persisted_agent_input_route(
    key: tuple[int, int, str], *, runtime: Any = None
) -> None:
    store = _runtime_store(runtime)
    try:
        store.delete_agent_inputs_for_route(key[0], key[1])
    except Exception:
        logger.exception(
            "Failed to clear persisted agent input route (user=%d thread=%d)",
            key[0],
            key[1],
        )


def same_agent_input_route(
    left: tuple[int, int, str],
    right: tuple[int, int, str],
) -> bool:
    return left[:2] == right[:2]


def is_current_bound_agent_input_target(
    key: tuple[int, int, str], *, runtime: Any = None
) -> bool:
    """Return whether the topic authoritatively points at this exact window."""
    sm = _dep(runtime, "session_manager", session_manager)
    return sm.resolve_window_for_thread(key[0], key[1] or None) == key[2]


def target_has_agent_input_confirmation(
    key: tuple[int, int, str], *, runtime: Any = None
) -> bool:
    conf_tasks = _dep(
        runtime, "_agent_input_confirmation_tasks", _agent_input_confirmation_tasks
    )
    conf_targets = _dep(
        runtime, "_agent_input_confirmation_targets", _agent_input_confirmation_targets
    )
    conf_modes = _dep(
        runtime, "_agent_input_confirmation_modes", _agent_input_confirmation_modes
    )
    exact_target_only = is_current_bound_agent_input_target(key, runtime=runtime)
    return any(
        same_agent_input_route(target, key)
        and (target == key or not exact_target_only)
        and conf_modes.get(record_id) != AGENT_INPUT_MODE_QUEUE
        and not task.done()
        for record_id, task in conf_tasks.items()
        if (target := conf_targets.get(record_id)) is not None
    )


def route_has_agent_input_queue(
    key: tuple[int, int, str], *, runtime: Any = None
) -> bool:
    queues = _dep(runtime, "_agent_input_queues", _agent_input_queues)
    return any(
        queue and same_agent_input_route(queued_key, key)
        for queued_key, queue in queues.items()
    )


def route_has_agent_input_drain(
    key: tuple[int, int, str], *, runtime: Any = None
) -> bool:
    tasks = _dep(runtime, "_agent_input_tasks", _agent_input_tasks)
    return any(
        same_agent_input_route(task_key, key) and not task.done()
        for task_key, task in tasks.items()
    )


def target_has_persisted_agent_input_confirmation(
    key: tuple[int, int, str], *, runtime: Any = None
) -> bool:
    store = _runtime_store(runtime)
    try:
        return store.has_unconfirmed_agent_input(
            key[0],
            key[1],
            window_id=(
                key[2]
                if is_current_bound_agent_input_target(key, runtime=runtime)
                else None
            ),
        )
    except Exception:
        logger.exception(
            "Failed to inspect persisted agent input confirmations (user=%d thread=%d)",
            key[0],
            key[1],
        )
        return True


def agent_input_confirmation_expired(
    record: PendingAgentInput, *, runtime: Any = None
) -> bool:
    """Return whether an unconfirmed submission may no longer block this topic."""
    if record.submitted_at_epoch is None:
        return False
    time_mod = _dep(runtime, "time", time)
    max_wait = _dep(
        runtime,
        "_AGENT_INPUT_CONFIRM_MAX_WAIT_SECONDS",
        AGENT_INPUT_CONFIRM_MAX_WAIT_SECONDS,
    )
    return time_mod.time() - record.submitted_at_epoch >= max_wait


async def maybe_confirm_startup_trust_prompt(
    window_id: str,
    pane_text: str,
    *,
    runtime: Any = None,
) -> tuple[bool, str]:
    """Confirm safe startup-only trust prompts before the first forwarded text."""
    tmux = _dep(runtime, "tmux_manager", tmux_manager)
    content = extract_interactive_content(pane_text)
    if content is None:
        return False, ""
    if content.name != "DirectoryTrust":
        return False, f"Agent is waiting for interactive input: {content.name}"
    logger.info("Auto-confirming Codex directory trust prompt in window %s", window_id)
    if await tmux.send_control_key(window_id, "Enter"):
        return True, "Confirmed Codex directory trust prompt"
    return False, "Failed to confirm Codex directory trust prompt"


async def confirm_first_prompt_delivery(
    window_id: str,
    text: str,
    *,
    transcript_timeout: float | None = None,
    transcript_session_id: str | None = None,
    transcript_after_offset: int | None = None,
    submit_key: str = "Enter",
    runtime: Any = None,
) -> bool:
    """Confirm that the first forwarded prompt reached the Codex transcript."""
    cfg = _dep(runtime, "config", config)
    sm = _dep(runtime, "session_manager", session_manager)
    tmux = _dep(runtime, "tmux_manager", tmux_manager)
    if transcript_timeout is None:
        transcript_timeout = cfg.transcript_confirm_timeout_seconds
    transcript_ok = await sm.wait_for_transcript_user_message(
        window_id,
        text,
        timeout=transcript_timeout,
        expected_session_id=transcript_session_id,
        after_offset=transcript_after_offset,
    )
    if transcript_ok:
        return True

    if not await tmux.prompt_still_pending(window_id, text):
        logger.warning(
            "Agent input cleared in window %s but was not recorded in the transcript",
            window_id,
        )
        return False

    logger.warning(
        "Agent input is still pending in window %s after transcript confirmation; "
        "retrying %s",
        window_id,
        submit_key,
    )
    if not await tmux.send_control_key(window_id, submit_key):
        return False

    transcript_ok = await sm.wait_for_transcript_user_message(
        window_id,
        text,
        timeout=transcript_timeout,
        expected_session_id=transcript_session_id,
        after_offset=transcript_after_offset,
    )
    if transcript_ok:
        return True

    if await tmux.prompt_still_pending(window_id, text):
        logger.warning(
            "Agent input is still pending in window %s after submit retry",
            window_id,
        )
        return False

    logger.info(
        "Agent input cleared in window %s after explicit submit retry; transcript "
        "confirmation is still pending",
        window_id,
    )
    return True


async def refresh_session_map_after_first_prompt(
    window_id: str,
    *,
    text: str | None = None,
    confirm_existing_session: bool = False,
    timeout: float = 20.0,
    transcript_session_id: str | None = None,
    transcript_after_offset: int | None = None,
    submit_key: str = "Enter",
    runtime: Any = None,
) -> bool:
    """Load the session_map entry that Codex writes after the first prompt starts."""
    sm = _dep(runtime, "session_manager", session_manager)
    tmux = _dep(runtime, "tmux_manager", tmux_manager)
    confirm_fn, confirm_patched = _rt_fn(
        runtime, "_confirm_first_prompt_delivery", confirm_first_prompt_delivery
    )

    async def _confirm(prompt_text: str) -> bool:
        kwargs: dict[str, Any] = {
            "transcript_session_id": transcript_session_id,
            "transcript_after_offset": transcript_after_offset,
            "submit_key": submit_key,
        }
        if not confirm_patched:
            kwargs["runtime"] = runtime
        return await confirm_fn(window_id, prompt_text, **kwargs)

    if transcript_session_id and not sm.window_matches_session(
        window_id,
        transcript_session_id,
    ):
        return False
    if sm.get_window_state(window_id).session_id:
        if text and confirm_existing_session:
            return await _confirm(text)
        return True
    hook_ok = await sm.wait_for_session_map_entry(window_id, timeout=timeout)
    if hook_ok:
        if text:
            return await _confirm(text)
        return True

    if text:
        if await tmux.prompt_still_pending(window_id, text):
            logger.warning(
                "Codex window %s still has the first prompt in the input row; "
                "retrying %s before waiting for session_map again",
                window_id,
                submit_key,
            )
            if await tmux.send_control_key(window_id, submit_key):
                hook_ok = await sm.wait_for_session_map_entry(
                    window_id,
                    timeout=min(timeout, 10.0),
                )
                if hook_ok:
                    return await _confirm(text)
        logger.warning(
            "Codex window %s accepted input but did not register session_map",
            window_id,
        )
    else:
        logger.warning(
            "Codex window %s did not register session_map",
            window_id,
        )
    return hook_ok


async def run_agent_input_confirmation(
    bot: Bot,
    record_id: int,
    key: tuple[int, int, str],
    *,
    recover_submission: bool,
    initial_delay_seconds: float | None = None,
    notify_on_first_delay: bool = False,
    runtime: Any = None,
) -> None:
    """Confirm a submitted prompt without ever replaying its text."""
    store = _runtime_store(runtime)
    sm = _dep(runtime, "session_manager", session_manager)
    cfg = _dep(runtime, "config", config)
    asyncio_mod = _dep(runtime, "asyncio", asyncio)
    retry_seconds = _dep(
        runtime, "_AGENT_INPUT_CONFIRM_RETRY_SECONDS", AGENT_INPUT_CONFIRM_RETRY_SECONDS
    )
    conf_tasks = _dep(
        runtime, "_agent_input_confirmation_tasks", _agent_input_confirmation_tasks
    )
    conf_targets = _dep(
        runtime, "_agent_input_confirmation_targets", _agent_input_confirmation_targets
    )
    conf_modes = _dep(
        runtime, "_agent_input_confirmation_modes", _agent_input_confirmation_modes
    )
    queues = _dep(runtime, "_agent_input_queues", _agent_input_queues)

    expired_fn, expired_patched = _rt_fn(
        runtime,
        "_notify_agent_input_confirmation_expired",
        notify_agent_input_confirmation_expired,
    )
    rebound_fn, rebound_patched = _rt_fn(
        runtime,
        "_notify_rebound_agent_input_unconfirmed",
        notify_rebound_agent_input_unconfirmed,
    )
    delayed_fn, delayed_patched = _rt_fn(
        runtime,
        "_notify_agent_input_confirmation_delayed",
        notify_agent_input_confirmation_delayed,
    )
    refresh_fn, refresh_patched = _rt_fn(
        runtime,
        "_refresh_session_map_after_first_prompt",
        refresh_session_map_after_first_prompt,
    )
    drain_fn, drain_patched = _rt_fn(
        runtime,
        "_ensure_agent_input_drain_task",
        ensure_agent_input_drain_task,
    )

    current_task = asyncio.current_task()
    confirmed = False
    try:
        if initial_delay_seconds is not None:
            if initial_delay_seconds > 0:
                await asyncio_mod.sleep(initial_delay_seconds)
        elif not recover_submission:
            await asyncio_mod.sleep(retry_seconds)
        while True:
            try:
                record = store.get_agent_input(record_id)
                if record is None or record.state != AGENT_INPUT_SUBMITTED_UNCONFIRMED:
                    confirmed = True
                    return
                if agent_input_confirmation_expired(record, runtime=runtime):
                    store.confirm_agent_input(record_id)
                    confirmed = True
                    logger.warning(
                        "Retired unconfirmed agent input after confirmation deadline "
                        "(record=%d window=%s)",
                        record_id,
                        record.window_id,
                    )
                    if expired_patched:
                        await expired_fn(bot, record.user_id, record.thread_id or None)
                    else:
                        await expired_fn(
                            bot,
                            record.user_id,
                            record.thread_id or None,
                            runtime=runtime,
                        )
                    return

                bound_window_id = sm.resolve_window_for_thread(
                    record.user_id,
                    record.thread_id or None,
                )
                if bound_window_id and bound_window_id != record.window_id:
                    if record.transcript_session_id and sm.window_matches_session(
                        bound_window_id,
                        record.transcript_session_id,
                    ):
                        confirmed = await sm.wait_for_transcript_user_message(
                            bound_window_id,
                            record.text,
                            timeout=cfg.transcript_confirm_timeout_seconds,
                            expected_session_id=record.transcript_session_id,
                            after_offset=record.transcript_offset,
                        )
                    if confirmed:
                        store.confirm_agent_input(record_id)
                        logger.info(
                            "Confirmed rebound agent input from the continued "
                            "transcript (record=%d old_window=%s new_window=%s)",
                            record_id,
                            record.window_id,
                            bound_window_id,
                        )
                        return

                    store.confirm_agent_input(record_id)
                    confirmed = True
                    logger.warning(
                        "Retired unconfirmed agent input after topic rebound "
                        "(record=%d old_window=%s new_window=%s)",
                        record_id,
                        record.window_id,
                        bound_window_id,
                    )
                    if rebound_patched:
                        await rebound_fn(bot, record.user_id, record.thread_id or None)
                    else:
                        await rebound_fn(
                            bot,
                            record.user_id,
                            record.thread_id or None,
                            runtime=runtime,
                        )
                    return

                if recover_submission:
                    refresh_kwargs: dict[str, Any] = {
                        "text": record.text,
                        "confirm_existing_session": True,
                        "transcript_session_id": record.transcript_session_id,
                        "transcript_after_offset": record.transcript_offset,
                        "submit_key": (
                            "Tab"
                            if record.submission_mode == AGENT_INPUT_MODE_QUEUE
                            else "Enter"
                        ),
                    }
                    if not refresh_patched:
                        refresh_kwargs["runtime"] = runtime
                    confirmed = await refresh_fn(record.window_id, **refresh_kwargs)
                    recover_submission = False
                else:
                    confirmed = await sm.wait_for_transcript_user_message(
                        record.window_id,
                        record.text,
                        timeout=cfg.transcript_confirm_timeout_seconds,
                        expected_session_id=record.transcript_session_id,
                        after_offset=record.transcript_offset,
                    )
                if confirmed:
                    store.confirm_agent_input(record_id)
                    logger.info(
                        "Confirmed previously submitted agent input after delay "
                        "(record=%d window=%s)",
                        record_id,
                        record.window_id,
                    )
                    return
                if notify_on_first_delay:
                    if delayed_patched:
                        await delayed_fn(bot, key[0], key[1] or None)
                    else:
                        await delayed_fn(bot, key[0], key[1] or None, runtime=runtime)
                    notify_on_first_delay = False
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "Deferred agent input confirmation failed "
                    "(record=%d user=%d thread=%s window=%s); retrying",
                    record_id,
                    key[0],
                    key[1] or None,
                    key[2],
                )
            await asyncio_mod.sleep(retry_seconds)
    except asyncio.CancelledError:
        raise
    finally:
        if conf_tasks.get(record_id) is current_task:
            conf_tasks.pop(record_id, None)
            conf_targets.pop(record_id, None)
            conf_modes.pop(record_id, None)
        if confirmed and not target_has_agent_input_confirmation(key, runtime=runtime):
            for queued_key, queue in list(queues.items()):
                if queue and same_agent_input_route(queued_key, key):
                    if drain_patched:
                        drain_fn(bot, queued_key)
                    else:
                        drain_fn(bot, queued_key, runtime=runtime)


def ensure_agent_input_confirmation_task(
    bot: Bot,
    record_id: int,
    key: tuple[int, int, str],
    *,
    recover_submission: bool,
    initial_delay_seconds: float | None = None,
    notify_on_first_delay: bool = False,
    submission_mode: str = AGENT_INPUT_MODE_TURN,
    runtime: Any = None,
) -> None:
    conf_tasks = _dep(
        runtime, "_agent_input_confirmation_tasks", _agent_input_confirmation_tasks
    )
    conf_targets = _dep(
        runtime, "_agent_input_confirmation_targets", _agent_input_confirmation_targets
    )
    conf_modes = _dep(
        runtime, "_agent_input_confirmation_modes", _agent_input_confirmation_modes
    )
    task = conf_tasks.get(record_id)
    if task and not task.done():
        return
    conf_targets[record_id] = key
    conf_modes[record_id] = submission_mode
    conf_tasks[record_id] = asyncio.create_task(
        run_agent_input_confirmation(
            bot,
            record_id,
            key,
            recover_submission=recover_submission,
            initial_delay_seconds=initial_delay_seconds,
            notify_on_first_delay=notify_on_first_delay,
            runtime=runtime,
        )
    )


async def cancel_agent_input_confirmations_for_target(
    key: tuple[int, int, str], *, runtime: Any = None
) -> None:
    conf_tasks = _dep(
        runtime, "_agent_input_confirmation_tasks", _agent_input_confirmation_tasks
    )
    conf_targets = _dep(
        runtime, "_agent_input_confirmation_targets", _agent_input_confirmation_targets
    )
    conf_modes = _dep(
        runtime, "_agent_input_confirmation_modes", _agent_input_confirmation_modes
    )
    record_ids = [
        record_id
        for record_id, target in conf_targets.items()
        if same_agent_input_route(target, key)
    ]
    tasks = [
        task
        for record_id in record_ids
        if (task := conf_tasks.get(record_id)) is not None and not task.done()
    ]
    for task in tasks:
        task.cancel()
    for task in tasks:
        with contextlib.suppress(asyncio.CancelledError):
            await task
    for record_id in record_ids:
        conf_tasks.pop(record_id, None)
        conf_targets.pop(record_id, None)
        conf_modes.pop(record_id, None)


def restore_agent_input_queues(bot: Bot, *, runtime: Any = None) -> int:
    """Reload prompts accepted before a process restart and restart drainers."""
    store = _runtime_store(runtime)
    sm = _dep(runtime, "session_manager", session_manager)
    time_mod = _dep(runtime, "time", time)
    admission = _dep(runtime, "turn_admission", turn_admission)
    queues = _dep(runtime, "_agent_input_queues", _agent_input_queues)
    conf_tasks = _dep(
        runtime, "_agent_input_confirmation_tasks", _agent_input_confirmation_tasks
    )
    conf_fn, conf_patched = _rt_fn(
        runtime,
        "_ensure_agent_input_confirmation_task",
        ensure_agent_input_confirmation_task,
    )
    drain_fn, drain_patched = _rt_fn(
        runtime, "_ensure_agent_input_drain_task", ensure_agent_input_drain_task
    )

    try:
        records = store.list_pending_agent_inputs()
    except Exception:
        logger.exception("Failed to restore persisted agent input queue")
        return 0

    restored = 0
    existing_ids = {
        item.store_id
        for queue in queues.values()
        for item in queue
        if item.store_id is not None
    } | set(conf_tasks)
    for record in records:
        if record.id in existing_ids:
            continue
        window_id = record.window_id
        resolved_window_id = sm.resolve_window_for_thread(
            record.user_id,
            record.thread_id or None,
        )
        if (
            record.state == AGENT_INPUT_QUEUED
            and resolved_window_id
            and resolved_window_id != window_id
        ):
            window_id = resolved_window_id
            try:
                store.retarget_agent_input(record.id, window_id)
            except Exception:
                logger.exception(
                    "Failed to retarget restored agent input %d to window %s",
                    record.id,
                    window_id,
                )
                continue

        age = max(0.0, time_mod.time() - record.created_at_epoch)
        created_at = time_mod.monotonic() - age
        key = agent_input_key(
            record.user_id,
            record.thread_id or None,
            window_id,
            runtime=runtime,
        )
        if record.state == AGENT_INPUT_SUBMITTED_UNCONFIRMED:
            confirmation_kwargs: dict[str, Any] = {"recover_submission": True}
            if record.submission_mode != AGENT_INPUT_MODE_TURN:
                confirmation_kwargs["submission_mode"] = record.submission_mode
            if not conf_patched:
                confirmation_kwargs["runtime"] = runtime
            conf_fn(bot, record.id, key, **confirmation_kwargs)
            restored += 1
            continue
        queues.setdefault(key, deque()).append(
            QueuedAgentInput(
                text=record.text,
                created_at=created_at,
                created_at_epoch=record.created_at_epoch,
                store_id=record.id,
                state=record.state,
                transcript_session_id=record.transcript_session_id,
                transcript_offset=record.transcript_offset,
                submission_mode=record.submission_mode,
            )
        )
        admission.set_pending(window_id, pending=True)
        restored += 1

    for key in list(queues):
        if not target_has_agent_input_confirmation(key, runtime=runtime):
            if drain_patched:
                drain_fn(bot, key)
            else:
                drain_fn(bot, key, runtime=runtime)
    if restored:
        logger.info("Restored %d agent input lifecycle record(s)", restored)
    return restored


async def drop_expired_agent_input(
    bot: Bot,
    key: tuple[int, int, str],
    queue: deque[QueuedAgentInput],
    *,
    runtime: Any = None,
) -> None:
    max_wait = agent_input_queue_max_wait_seconds(runtime=runtime)
    if max_wait <= 0:
        return

    time_mod = _dep(runtime, "time", time)
    admission = _dep(runtime, "turn_admission", turn_admission)
    notify_fn, notify_patched = _rt_fn(
        runtime, "_notify_queued_input_failure", notify_queued_input_failure
    )

    now = time_mod.monotonic()
    expired = 0
    expired_items: list[QueuedAgentInput] = []
    while queue and now - queue[0].created_at >= max_wait:
        expired_items.append(queue.popleft())
        expired += 1

    if expired:
        delete_persisted_agent_inputs(expired_items, runtime=runtime)
        user_id, thread_key, _window_id = key
        thread_id = thread_key or None
        wait_display = int(max_wait)
        message = (
            f"{expired} queued input(s) expired after waiting {wait_display}s "
            "for the agent to become ready"
        )
        if notify_patched:
            await notify_fn(bot, user_id, thread_id, message)
        else:
            await notify_fn(bot, user_id, thread_id, message, runtime=runtime)
    if not queue:
        admission.set_pending(key[2], pending=False)


def ensure_agent_input_drain_task(
    bot: Bot,
    key: tuple[int, int, str],
    *,
    runtime: Any = None,
) -> None:
    if target_has_agent_input_confirmation(
        key, runtime=runtime
    ) or target_has_persisted_agent_input_confirmation(key, runtime=runtime):
        return
    if route_has_agent_input_drain(key, runtime=runtime):
        return
    tasks = _dep(runtime, "_agent_input_tasks", _agent_input_tasks)
    tasks[key] = asyncio.create_task(drain_agent_input_queue(bot, key, runtime=runtime))


async def queue_agent_input_after_interrupt(
    bot: Bot,
    user_id: int,
    thread_id: int | None,
    window_id: str,
    text: str,
    *,
    runtime: Any = None,
) -> tuple[bool, str]:
    """Queue replacement text after an explicit interrupt request."""
    key = agent_input_key(user_id, thread_id, window_id, runtime=runtime)
    drain_fn, drain_patched = _rt_fn(
        runtime, "_ensure_agent_input_drain_task", ensure_agent_input_drain_task
    )
    async with agent_input_lock(key, runtime=runtime):
        queued, depth, limit = queue_agent_input(key, text, runtime=runtime)
        if drain_patched:
            drain_fn(bot, key)
        else:
            drain_fn(bot, key, runtime=runtime)
        if not queued:
            return (
                False,
                "Agent is still handling the interrupt and the input queue is full "
                f"({limit} pending). Wait for it to finish or use /esc first.",
            )
        return (
            True,
            f"Interrupt requested; queued message until the agent is ready ({depth}/{limit})",
        )


async def send_native_agent_input(
    bot: Bot,
    user_id: int,
    thread_id: int,
    window_id: str,
    text: str,
    *,
    mode: str,
    runtime: Any = None,
) -> tuple[bool, str]:
    """Durably submit native active-turn input without replaying its text."""
    type_fn, type_patched = _rt_fn(runtime, "_window_agent_type", window_agent_type)
    agent_type = (
        type_fn(window_id) if type_patched else type_fn(window_id, runtime=runtime)
    )
    display_name = agent_display_name(agent_type)
    if (
        mode == AGENT_INPUT_MODE_QUEUE
        and not agent_capabilities(agent_type).supports_native_queue
    ):
        return (
            False,
            f"{display_name} has no native next-turn key; use the AgentBot queue.",
        )

    key = agent_input_key(user_id, thread_id, window_id, runtime=runtime)
    reset_fn, reset_patched = _rt_fn(
        runtime, "_reset_transient_agent_retry", reset_transient_agent_retry
    )
    if reset_patched:
        reset_fn(window_id)
    else:
        reset_fn(window_id, runtime=runtime)

    capture_fn = _dep(runtime, "capture_agent_output", capture_agent_output)
    capture = await capture_fn(user_id, thread_id, window_id)
    if capture is None:
        return False, "No session bound"
    if capture.missing:
        return False, "Window not found (may have been closed)"
    pane_text = capture.text or ""
    auth_error = extract_auth_error_message(pane_text)
    if auth_error:
        if agent_type == AGENT_CODEX:
            return False, codex_auth_recovery_message(auth_error)
        return False, auth_error
    if is_interactive_ui(pane_text):
        return (
            False,
            f"{display_name} is showing an interactive prompt; handle it first.",
        )
    if not pane_text.strip():
        return False, f"{display_name} state is temporarily unavailable; retry shortly."
    if is_codex_input_ready(pane_text):
        soq_fn, soq_patched = _rt_fn(
            runtime, "_send_or_queue_agent_input", send_or_queue_agent_input
        )
        if soq_patched:
            success, message, queued = await soq_fn(
                bot, user_id, thread_id, window_id, text
            )
        else:
            success, message, queued = await soq_fn(
                bot, user_id, thread_id, window_id, text, runtime=runtime
            )
        if not success:
            return False, message
        if queued:
            return True, message
        return (
            True,
            f"{display_name} became idle, so the message started a new turn (Enter).",
        )

    store = _runtime_store(runtime)
    hibernator = _dep(runtime, "idle_session_hibernator", idle_session_hibernator)
    conf_fn, conf_patched = _rt_fn(
        runtime,
        "_ensure_agent_input_confirmation_task",
        ensure_agent_input_confirmation_task,
    )
    send_native_fn, send_native_patched = _rt_fn(
        runtime, "_send_native_input_to_agent", send_native_input_to_agent
    )

    async with agent_input_lock(key, runtime=runtime):
        if not route_can_use_native_input(key, runtime=runtime):
            return (
                False,
                "This topic still has bot-managed input pending; wait for it to "
                "finish before using native input.",
            )
        try:
            record = store.enqueue_agent_input(
                user_id=user_id,
                thread_id=thread_id,
                window_id=window_id,
                text=text,
                max_pending=agent_input_queue_max_size(runtime=runtime),
                submission_mode=mode,
            )
        except Exception:
            logger.exception(
                "Failed to persist native agent input "
                "(user=%d thread=%d window=%s mode=%s)",
                user_id,
                thread_id,
                window_id,
                mode,
            )
            record = None
        if record is None:
            return False, "The native input queue is full or could not be saved safely."

        item = QueuedAgentInput(
            text=text,
            created_at_epoch=record.created_at_epoch,
            store_id=record.id,
            submission_mode=mode,
        )
        if not await mark_persisted_agent_input_submitted(
            item, window_id, runtime=runtime
        ):
            delete_persisted_agent_inputs([item], runtime=runtime)
            return False, "The input could not enter a safe submission state."

        try:
            if send_native_patched:
                success, message = await send_native_fn(
                    user_id, thread_id, window_id, text, mode=mode
                )
            else:
                success, message = await send_native_fn(
                    user_id, thread_id, window_id, text, mode=mode, runtime=runtime
                )
        except BaseException:
            if item.store_id is not None:
                conf_kwargs: dict[str, Any] = {
                    "recover_submission": True,
                    "submission_mode": mode,
                }
                if not conf_patched:
                    conf_kwargs["runtime"] = runtime
                conf_fn(bot, item.store_id, key, **conf_kwargs)
            raise
        if not success:
            delete_persisted_agent_inputs([item], runtime=runtime)
            return False, message

        hibernator.forget(window_id)
        if item.store_id is not None:
            conf_kwargs = {
                "recover_submission": True,
                "initial_delay_seconds": 0.0,
                "notify_on_first_delay": mode == AGENT_INPUT_MODE_STEER,
                "submission_mode": mode,
            }
            if not conf_patched:
                conf_kwargs["runtime"] = runtime
            conf_fn(bot, item.store_id, key, **conf_kwargs)
        if mode == AGENT_INPUT_MODE_QUEUE:
            return True, f"Queued in {display_name} for the next turn (Tab)."
        if is_claude_agent(agent_type) and text.lstrip().startswith("/"):
            return (
                True,
                "Command submitted; Claude Code will run it after the current turn "
                "(Enter).",
            )
        return True, f"Guidance sent to the current {display_name} turn (Enter)."


def agent_cli_startup_failure(capture: Any) -> str | None:
    """Return a safe notice when a local shell shows a rejected CLI launch."""
    if (
        capture is not None
        and is_shell_pane_command(getattr(capture, "pane_command", ""))
        and is_agent_cli_startup_error(getattr(capture, "text", "") or "")
    ):
        return (
            "The agent CLI failed to start. This message was not sent; fix the "
            "launch command and resend it."
        )
    return None


async def send_or_queue_agent_input(
    bot: Bot,
    user_id: int,
    thread_id: int | None,
    window_id: str,
    text: str,
    *,
    transient_retry: bool = False,
    runtime: Any = None,
) -> tuple[bool, str, bool]:
    """Persist one input, then send its queue head when the target is ready."""
    key = agent_input_key(user_id, thread_id, window_id, runtime=runtime)
    if not transient_retry:
        reset_fn, reset_patched = _rt_fn(
            runtime, "_reset_transient_agent_retry", reset_transient_agent_retry
        )
        if reset_patched:
            reset_fn(window_id)
        else:
            reset_fn(window_id, runtime=runtime)

    queues = _dep(runtime, "_agent_input_queues", _agent_input_queues)
    capture_fn = _dep(runtime, "capture_agent_output", capture_agent_output)
    admission = _dep(runtime, "turn_admission", turn_admission)
    hibernator = _dep(runtime, "idle_session_hibernator", idle_session_hibernator)

    drain_fn, drain_patched = _rt_fn(
        runtime, "_ensure_agent_input_drain_task", ensure_agent_input_drain_task
    )
    conf_fn, conf_patched = _rt_fn(
        runtime,
        "_ensure_agent_input_confirmation_task",
        ensure_agent_input_confirmation_task,
    )
    send_msg_fn, send_msg_patched = _rt_fn(
        runtime, "_send_message_to_agent", send_message_to_agent
    )
    send_ctrl_fn, send_ctrl_patched = _rt_fn(
        runtime, "_send_control_to_agent", send_control_to_agent
    )

    def _ensure_drain() -> None:
        if drain_patched:
            drain_fn(bot, key)
        else:
            drain_fn(bot, key, runtime=runtime)

    def _ensure_conf(store_id: int, **kwargs: Any) -> None:
        if not conf_patched:
            kwargs["runtime"] = runtime
        conf_fn(bot, store_id, key, **kwargs)

    result: tuple[bool, str, bool]
    async with agent_input_lock(key, runtime=runtime):
        queue = queues.get(key)
        if (
            route_has_agent_input_queue(key, runtime=runtime)
            or route_has_agent_input_drain(key, runtime=runtime)
            or target_has_agent_input_confirmation(key, runtime=runtime)
            or target_has_persisted_agent_input_confirmation(key, runtime=runtime)
        ):
            queued, depth, limit = queue_agent_input(key, text, runtime=runtime)
            _ensure_drain()
            if not queued:
                return (
                    False,
                    "Agent is still busy and the input queue is full "
                    f"({limit} pending). Wait for it to finish or use /interrupt.",
                    False,
                )
            return (
                True,
                f"Queued until the agent is ready ({depth}/{limit})",
                True,
            )

        queued, depth, limit = queue_agent_input(key, text, runtime=runtime)
        if not queued:
            return (
                False,
                "The input could not be saved safely. Please try again; it was not "
                "sent to the agent.",
                False,
            )
        queue = queues[key]
        item = queue[0]

        try:
            capture = await capture_fn(user_id, thread_id, window_id)
        except BaseException:
            _ensure_drain()
            raise
        startup_failure = agent_cli_startup_failure(capture)
        if capture is None:
            result = False, "No session bound", False
        elif capture.missing:
            result = False, "Window not found (may have been closed)", False
        elif startup_failure:
            result = False, startup_failure, False
        else:
            pane_text = capture.text or ""
            auth_error = extract_auth_error_message(pane_text)
            if auth_error:
                result = False, codex_auth_recovery_message(auth_error), False
            elif is_interactive_ui(pane_text):
                interactive = extract_interactive_content(pane_text)
                if interactive is not None and interactive.name == "DirectoryTrust":
                    ok, control_message = await maybe_confirm_startup_trust_prompt(
                        window_id,
                        pane_text,
                        runtime=runtime,
                    )
                elif send_ctrl_patched:
                    ok, control_message = await send_ctrl_fn(
                        user_id, thread_id, window_id, "Escape"
                    )
                else:
                    ok, control_message = await send_ctrl_fn(
                        user_id, thread_id, window_id, "Escape", runtime=runtime
                    )
                if not ok:
                    logger.warning(
                        "Failed to interrupt Codex interactive prompt before "
                        "queuing input (user=%d thread=%s window=%s): %s",
                        user_id,
                        thread_id,
                        window_id,
                        control_message,
                    )
                _ensure_drain()
                result = (
                    True,
                    "Interrupted agent prompt and queued until the agent is ready "
                    f"({depth}/{limit})",
                    True,
                )
            elif not is_codex_input_ready(pane_text):
                admission.observe(window_id, active=True)
                _ensure_drain()
                result = (
                    True,
                    f"Agent is busy; queued until ready ({depth}/{limit})",
                    True,
                )
            else:
                admission.observe(window_id, active=False)
                admitted = await admission.try_acquire(
                    window_id,
                    limit=agent_max_active_turns(runtime=runtime),
                )
                if not admitted:
                    _ensure_drain()
                    result = (
                        True,
                        "Server is at its active-task limit; queued for the "
                        f"next available slot ({depth}/{limit})",
                        True,
                    )
                elif not await mark_persisted_agent_input_submitted(
                    item, window_id, runtime=runtime
                ):
                    admission.release(window_id)
                    _ensure_drain()
                    result = (
                        True,
                        "Saved safely; waiting for the delivery state to become "
                        f"available ({depth}/{limit})",
                        True,
                    )
                else:
                    try:
                        if send_msg_patched:
                            success, message = await send_msg_fn(
                                user_id, thread_id, window_id, text
                            )
                        else:
                            success, message = await send_msg_fn(
                                user_id, thread_id, window_id, text, runtime=runtime
                            )
                    except BaseException:
                        admission.release(window_id)
                        if queue and queue[0] is item:
                            queue.popleft()
                        if not queue:
                            queues.pop(key, None)
                        if item.store_id is not None:
                            _ensure_conf(item.store_id, recover_submission=True)
                        raise
                    if success:
                        admission.mark_submitted(window_id)
                        hibernator.forget(window_id)
                        queue.popleft()
                        if not queue:
                            queues.pop(key, None)
                        admission.set_pending(window_id, pending=False)
                        if item.store_id is not None:
                            _ensure_conf(
                                item.store_id,
                                recover_submission=True,
                                initial_delay_seconds=0.0,
                                notify_on_first_delay=True,
                            )
                        result = success, message, False
                    else:
                        admission.release(window_id)
                        queue.popleft()
                        delete_persisted_agent_inputs([item], runtime=runtime)
                        admission.set_pending(window_id, pending=False)
                        result = success, message, False

        if not result[0]:
            if queue and queue[0] is item:
                queue.popleft()
                delete_persisted_agent_inputs([item], runtime=runtime)
            if not queue:
                queues.pop(key, None)
                admission.set_pending(window_id, pending=False)

    return result


async def run_transient_agent_retry(
    bot: Bot,
    key: tuple[int, int, str],
    *,
    attempt: int,
    delay_seconds: float,
    runtime: Any = None,
) -> None:
    """Resume an unfinished turn after a bounded transient-capacity backoff."""
    user_id, thread_key, window_id = key
    thread_id = thread_key or None
    current_task = asyncio.current_task()
    asyncio_mod = _dep(runtime, "asyncio", asyncio)
    store = _runtime_store(runtime)
    sm = _dep(runtime, "session_manager", session_manager)
    send_fn = _dep(runtime, "safe_send", safe_send)
    mark_working_fn = _dep(runtime, "mark_window_working", mark_window_working)
    retry_prompt = _dep(
        runtime, "_TRANSIENT_AGENT_RETRY_PROMPT", TRANSIENT_AGENT_RETRY_PROMPT
    )
    retry_tasks = _dep(
        runtime, "_transient_agent_retry_tasks", _transient_agent_retry_tasks
    )
    soq_fn, soq_patched = _rt_fn(
        runtime, "_send_or_queue_agent_input", send_or_queue_agent_input
    )
    try:
        await asyncio_mod.sleep(delay_seconds)
        try:
            persisted = store.get_transient_agent_retry(window_id)
            if persisted is None or (
                persisted.attempt != attempt
                or persisted.state != TRANSIENT_RETRY_SCHEDULED
            ):
                return
            changed = store.mark_transient_agent_retry_waiting(
                window_id,
                attempt,
            )
            if not changed:
                return
        except Exception:
            logger.exception(
                "Failed to persist due transient retry state (window=%s attempt=%d)",
                window_id,
                attempt,
            )
            return
        if soq_patched:
            success, message, _queued = await soq_fn(
                bot, user_id, thread_id, window_id, retry_prompt, transient_retry=True
            )
        else:
            success, message, _queued = await soq_fn(
                bot,
                user_id,
                thread_id,
                window_id,
                retry_prompt,
                transient_retry=True,
                runtime=runtime,
            )
        if not success:
            logger.warning(
                "Automatic agent retry could not be submitted "
                "(user=%d thread=%s window=%s): %s",
                user_id,
                thread_id,
                window_id,
                message,
            )
            await send_fn(
                bot,
                sm.resolve_chat_id(user_id, thread_id),
                f"❌ The unfinished task could not be retried automatically: {message}",
                message_thread_id=thread_id,
            )
            return
        await mark_working_fn(bot, user_id, window_id, thread_id)
        logger.info(
            "Automatically retried transient agent failure "
            "(user=%d thread=%s window=%s)",
            user_id,
            thread_id,
            window_id,
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception(
            "Automatic agent retry failed (user=%d thread=%s window=%s)",
            user_id,
            thread_id,
            window_id,
        )
    finally:
        if retry_tasks.get(window_id) is current_task:
            retry_tasks.pop(window_id, None)


def schedule_transient_agent_retry(
    bot: Bot,
    user_id: int,
    thread_id: int | None,
    window_id: str,
    *,
    runtime: Any = None,
) -> tuple[int, float] | None:
    """Schedule one bounded retry, returning its attempt number and delay."""
    key = agent_input_key(user_id, thread_id, window_id, runtime=runtime)
    retry_tasks = _dep(
        runtime, "_transient_agent_retry_tasks", _transient_agent_retry_tasks
    )
    retry_attempts = _dep(
        runtime, "_transient_agent_retry_attempts", _transient_agent_retry_attempts
    )
    delays = _dep(
        runtime,
        "_TRANSIENT_AGENT_RETRY_DELAYS_SECONDS",
        TRANSIENT_AGENT_RETRY_DELAYS_SECONDS,
    )
    time_mod = _dep(runtime, "time", time)
    store = _runtime_store(runtime)

    existing = retry_tasks.get(window_id)
    if existing and not existing.done():
        attempt = retry_attempts.get(window_id, 1)
        return attempt, 0.0

    persisted: TransientAgentRetry | None = None
    try:
        persisted = store.get_transient_agent_retry(window_id)
    except Exception:
        logger.exception(
            "Failed to load persisted transient retry for window %s", window_id
        )

    if persisted is not None and persisted.state == TRANSIENT_RETRY_SCHEDULED:
        attempt = persisted.attempt
        delay_seconds = max(0.0, persisted.not_before_epoch - time_mod.time())
        retry_attempts[window_id] = attempt
        task = asyncio.create_task(
            run_transient_agent_retry(
                bot,
                agent_input_key(
                    persisted.user_id,
                    persisted.thread_id or None,
                    window_id,
                    runtime=runtime,
                ),
                attempt=attempt,
                delay_seconds=delay_seconds,
                runtime=runtime,
            )
        )
        retry_tasks[window_id] = task
        return attempt, delay_seconds

    previous_attempt = retry_attempts.get(window_id, 0)
    if persisted is not None:
        previous_attempt = max(previous_attempt, persisted.attempt)
    attempt = previous_attempt + 1
    if attempt > len(delays):
        return None

    delay_seconds = delays[attempt - 1]
    retry_attempts[window_id] = attempt
    try:
        store.save_transient_agent_retry(
            user_id=user_id,
            thread_id=thread_id or 0,
            window_id=window_id,
            attempt=attempt,
            state=TRANSIENT_RETRY_SCHEDULED,
            not_before_epoch=time_mod.time() + delay_seconds,
            created_at_epoch=(
                persisted.created_at_epoch if persisted is not None else None
            ),
        )
    except Exception:
        logger.exception(
            "Failed to persist transient retry (window=%s attempt=%d)",
            window_id,
            attempt,
        )
        retry_attempts.pop(window_id, None)
        return None
    task = asyncio.create_task(
        run_transient_agent_retry(
            bot,
            key,
            attempt=attempt,
            delay_seconds=delay_seconds,
            runtime=runtime,
        )
    )
    retry_tasks[window_id] = task
    return attempt, delay_seconds


def restore_transient_agent_retries(bot: Bot, *, runtime: Any = None) -> int:
    """Restore delayed retries without replaying already-submitted continuations."""
    store = _runtime_store(runtime)
    sm = _dep(runtime, "session_manager", session_manager)
    time_mod = _dep(runtime, "time", time)
    retry_attempts = _dep(
        runtime, "_transient_agent_retry_attempts", _transient_agent_retry_attempts
    )
    retry_tasks = _dep(
        runtime, "_transient_agent_retry_tasks", _transient_agent_retry_tasks
    )
    try:
        records = store.list_transient_agent_retries()
    except Exception:
        logger.exception("Failed to restore persisted transient agent retries")
        return 0

    restored = 0
    for record in records:
        window_id = record.window_id
        resolved_window_id = sm.resolve_window_for_thread(
            record.user_id,
            record.thread_id or None,
        )
        if resolved_window_id and resolved_window_id != window_id:
            try:
                store.delete_transient_agent_retry(window_id)
                record = store.save_transient_agent_retry(
                    user_id=record.user_id,
                    thread_id=record.thread_id,
                    window_id=resolved_window_id,
                    attempt=record.attempt,
                    state=record.state,
                    not_before_epoch=record.not_before_epoch,
                    created_at_epoch=record.created_at_epoch,
                )
            except Exception:
                logger.exception(
                    "Failed to retarget transient retry from %s to %s",
                    window_id,
                    resolved_window_id,
                )
                continue
            window_id = resolved_window_id

        retry_attempts[window_id] = record.attempt
        if record.state == TRANSIENT_RETRY_SCHEDULED:
            delay_seconds = max(0.0, record.not_before_epoch - time_mod.time())
            key = agent_input_key(
                record.user_id,
                record.thread_id or None,
                window_id,
                runtime=runtime,
            )
            retry_tasks[window_id] = asyncio.create_task(
                run_transient_agent_retry(
                    bot,
                    key,
                    attempt=record.attempt,
                    delay_seconds=delay_seconds,
                    runtime=runtime,
                )
            )
        restored += 1
    if restored:
        logger.info("Restored %d transient agent retry state(s)", restored)
    return restored


async def cancel_transient_agent_retry_tasks(*, runtime: Any = None) -> None:
    """Cancel delayed retries while preserving their restart-safe records."""
    retry_tasks = _dep(
        runtime, "_transient_agent_retry_tasks", _transient_agent_retry_tasks
    )
    retry_attempts = _dep(
        runtime, "_transient_agent_retry_attempts", _transient_agent_retry_attempts
    )
    tasks = [task for task in retry_tasks.values() if not task.done()]
    for task in tasks:
        task.cancel()
    for task in tasks:
        with contextlib.suppress(asyncio.CancelledError):
            await task
    retry_tasks.clear()
    retry_attempts.clear()


async def notify_queued_input_failure(
    bot: Bot,
    user_id: int,
    thread_id: int | None,
    message: str,
    *,
    runtime: Any = None,
) -> None:
    sm = _dep(runtime, "session_manager", session_manager)
    send_fn = _dep(runtime, "safe_send", safe_send)
    logger.warning(
        "Queued agent input delivery failed (user=%d thread=%s): %s",
        user_id,
        thread_id,
        message,
    )
    try:
        await send_fn(
            bot,
            sm.resolve_chat_id(user_id, thread_id),
            f"❌ Queued message failed: {message}",
            message_thread_id=thread_id,
        )
    except Exception:
        logger.exception(
            "Failed to notify queued input failure (user=%d thread=%s)",
            user_id,
            thread_id,
        )


async def notify_agent_input_confirmation_delayed(
    bot: Bot,
    user_id: int,
    thread_id: int | None,
    *,
    runtime: Any = None,
) -> None:
    """Report a recoverable confirmation delay without calling it a failure."""
    sm = _dep(runtime, "session_manager", session_manager)
    send_fn = _dep(runtime, "safe_send", safe_send)
    logger.warning(
        "Agent input transcript confirmation delayed (user=%d thread=%s)",
        user_id,
        thread_id,
    )
    try:
        await send_fn(
            bot,
            sm.resolve_chat_id(user_id, thread_id),
            "⏳ Message was submitted, but transcript confirmation is delayed. "
            "The bot is still tracking it and will not resend automatically, "
            "avoiding duplicate input.",
            message_thread_id=thread_id,
        )
    except Exception:
        logger.exception(
            "Failed to notify delayed agent input confirmation (user=%d thread=%s)",
            user_id,
            thread_id,
        )


async def notify_agent_input_confirmation_expired(
    bot: Bot,
    user_id: int,
    thread_id: int | None,
    *,
    runtime: Any = None,
) -> None:
    """Explain why an unconfirmed input was retired without a replay."""
    sm = _dep(runtime, "session_manager", session_manager)
    send_fn = _dep(runtime, "safe_send", safe_send)
    try:
        await send_fn(
            bot,
            sm.resolve_chat_id(user_id, thread_id),
            "⚠️ The earlier message was not confirmed in the agent transcript "
            "within 5 minutes. It was not resent, avoiding duplicate input. "
            "Later queued messages can continue now; resend the earlier message "
            "only if the agent did not receive it.",
            message_thread_id=thread_id,
        )
    except Exception:
        logger.exception(
            "Failed to notify expired agent input confirmation (user=%d thread=%s)",
            user_id,
            thread_id,
        )


async def notify_rebound_agent_input_unconfirmed(
    bot: Bot,
    user_id: int,
    thread_id: int | None,
    *,
    runtime: Any = None,
) -> None:
    """Explain why an old-window confirmation was retired without a replay."""
    sm = _dep(runtime, "session_manager", session_manager)
    send_fn = _dep(runtime, "safe_send", safe_send)
    try:
        await send_fn(
            bot,
            sm.resolve_chat_id(user_id, thread_id),
            "⚠️ A message submitted to the previous agent window never appeared "
            "in its transcript. It was not resent after the session moved, avoiding "
            "duplicate input. Later messages will continue normally; resend the "
            "earlier message if you still need it.",
            message_thread_id=thread_id,
        )
    except Exception:
        logger.exception(
            "Failed to notify retired rebound input (user=%d thread=%s)",
            user_id,
            thread_id,
        )


async def drain_agent_input_queue(
    bot: Bot,
    key: tuple[int, int, str],
    *,
    runtime: Any = None,
) -> None:
    """Forward queued user prompts one at a time when Codex returns to input."""
    user_id, thread_key, window_id = key
    thread_id = thread_key or None
    queues = _dep(runtime, "_agent_input_queues", _agent_input_queues)
    tasks = _dep(runtime, "_agent_input_tasks", _agent_input_tasks)
    capture_fn = _dep(runtime, "capture_agent_output", capture_agent_output)
    admission = _dep(runtime, "turn_admission", turn_admission)
    hibernator = _dep(runtime, "idle_session_hibernator", idle_session_hibernator)
    store = _runtime_store(runtime)
    asyncio_mod = _dep(runtime, "asyncio", asyncio)
    mark_working_fn = _dep(runtime, "mark_window_working", mark_window_working)
    poll_interval = _dep(
        runtime,
        "_AGENT_INPUT_POLL_INTERVAL_SECONDS",
        AGENT_INPUT_POLL_INTERVAL_SECONDS,
    )

    notify_fail_fn, notify_fail_patched = _rt_fn(
        runtime, "_notify_queued_input_failure", notify_queued_input_failure
    )
    notify_delay_fn, notify_delay_patched = _rt_fn(
        runtime,
        "_notify_agent_input_confirmation_delayed",
        notify_agent_input_confirmation_delayed,
    )
    send_native_fn, send_native_patched = _rt_fn(
        runtime, "_send_native_input_to_agent", send_native_input_to_agent
    )
    send_msg_fn, send_msg_patched = _rt_fn(
        runtime, "_send_message_to_agent", send_message_to_agent
    )
    refresh_fn, refresh_patched = _rt_fn(
        runtime,
        "_refresh_session_map_after_first_prompt",
        refresh_session_map_after_first_prompt,
    )
    conf_fn, conf_patched = _rt_fn(
        runtime,
        "_ensure_agent_input_confirmation_task",
        ensure_agent_input_confirmation_task,
    )
    drain_fn, drain_patched = _rt_fn(
        runtime, "_ensure_agent_input_drain_task", ensure_agent_input_drain_task
    )

    async def _notify_fail(msg: str) -> None:
        if notify_fail_patched:
            await notify_fail_fn(bot, user_id, thread_id, msg)
        else:
            await notify_fail_fn(bot, user_id, thread_id, msg, runtime=runtime)

    def _ensure_conf(store_id: int, **kwargs: Any) -> None:
        if not conf_patched:
            kwargs["runtime"] = runtime
        conf_fn(bot, store_id, key, **kwargs)

    try:
        while True:
            queue = queues.get(key)
            if not queue:
                return
            await drop_expired_agent_input(bot, key, queue, runtime=runtime)
            if not queue:
                return

            capture = await capture_fn(user_id, thread_id, window_id)
            if capture is None:
                await _notify_fail("No session bound")
                queue.clear()
                clear_persisted_agent_input_target(key, runtime=runtime)
                admission.set_pending(window_id, pending=False)
                return
            if capture.missing:
                await _notify_fail("Window not found (may have been closed)")
                queue.clear()
                clear_persisted_agent_input_target(key, runtime=runtime)
                admission.forget(window_id)
                return

            startup_failure = agent_cli_startup_failure(capture)
            if startup_failure:
                await _notify_fail(startup_failure)
                queue.clear()
                clear_persisted_agent_input_target(key, runtime=runtime)
                admission.set_pending(window_id, pending=False)
                return

            pane_text = capture.text or ""
            auth_error = extract_auth_error_message(pane_text)
            if auth_error:
                await _notify_fail(codex_auth_recovery_message(auth_error))
                queue.clear()
                clear_persisted_agent_input_target(key, runtime=runtime)
                admission.set_pending(window_id, pending=False)
                return
            item = queue[0]
            interactive = is_interactive_ui(pane_text)
            input_ready = is_codex_input_ready(pane_text)
            if not pane_text.strip():
                await asyncio_mod.sleep(poll_interval)
                continue
            if interactive:
                interactive_content = extract_interactive_content(pane_text)
                if (
                    interactive_content is not None
                    and interactive_content.name == "DirectoryTrust"
                ):
                    await maybe_confirm_startup_trust_prompt(
                        window_id, pane_text, runtime=runtime
                    )
                admission.observe(window_id, active=True)
                await asyncio_mod.sleep(poll_interval)
                continue
            if (
                item.submission_mode in {AGENT_INPUT_MODE_STEER, AGENT_INPUT_MODE_QUEUE}
                and not input_ready
            ):
                admission.observe(window_id, active=True)
                if not await mark_persisted_agent_input_submitted(
                    item, window_id, runtime=runtime
                ):
                    await asyncio_mod.sleep(AGENT_INPUT_PERSIST_RETRY_SECONDS)
                    continue
                try:
                    if send_native_patched:
                        success, message = await send_native_fn(
                            user_id,
                            thread_id,
                            window_id,
                            item.text,
                            mode=item.submission_mode,
                        )
                    else:
                        success, message = await send_native_fn(
                            user_id,
                            thread_id,
                            window_id,
                            item.text,
                            mode=item.submission_mode,
                            runtime=runtime,
                        )
                except BaseException:
                    if queue and queue[0] is item:
                        queue.popleft()
                    if item.store_id is not None:
                        _ensure_conf(
                            item.store_id,
                            recover_submission=True,
                            submission_mode=item.submission_mode,
                        )
                    raise
                if not success:
                    failed_item = queue.popleft()
                    delete_persisted_agent_inputs([failed_item], runtime=runtime)
                    await _notify_fail(message)
                    continue
                hibernator.forget(window_id)
                submitted_item = queue.popleft()
                if not queue:
                    admission.set_pending(window_id, pending=False)
                await mark_working_fn(bot, user_id, window_id, thread_id)
                if submitted_item.store_id is not None:
                    _ensure_conf(
                        submitted_item.store_id,
                        recover_submission=True,
                        initial_delay_seconds=0.0,
                        submission_mode=submitted_item.submission_mode,
                    )
                await asyncio_mod.sleep(poll_interval)
                continue
            if not input_ready:
                admission.observe(window_id, active=True)
                await asyncio_mod.sleep(poll_interval)
                continue
            if item.submission_mode != AGENT_INPUT_MODE_TURN:
                try:
                    changed = (
                        item.store_id is None
                        or store.set_agent_input_submission_mode(
                            item.store_id,
                            AGENT_INPUT_MODE_TURN,
                        )
                    )
                except Exception:
                    logger.exception(
                        "Failed to downgrade restored native input to a new turn "
                        "(record=%s window=%s)",
                        item.store_id,
                        window_id,
                    )
                    changed = False
                if not changed:
                    await asyncio_mod.sleep(AGENT_INPUT_PERSIST_RETRY_SECONDS)
                    continue
                item.submission_mode = AGENT_INPUT_MODE_TURN
            admission.observe(window_id, active=False)
            admitted = await admission.try_acquire(
                window_id,
                limit=agent_max_active_turns(runtime=runtime),
            )
            if not admitted:
                await asyncio_mod.sleep(poll_interval)
                continue
            if not await mark_persisted_agent_input_submitted(
                item, window_id, runtime=runtime
            ):
                admission.release(window_id)
                await asyncio_mod.sleep(AGENT_INPUT_PERSIST_RETRY_SECONDS)
                continue
            try:
                if send_msg_patched:
                    success, message = await send_msg_fn(
                        user_id, thread_id, window_id, item.text
                    )
                else:
                    success, message = await send_msg_fn(
                        user_id, thread_id, window_id, item.text, runtime=runtime
                    )
            except BaseException:
                admission.release(window_id)
                if queue and queue[0] is item:
                    queue.popleft()
                if item.store_id is not None:
                    _ensure_conf(item.store_id, recover_submission=True)
                raise
            if not success:
                admission.release(window_id)
                failed_item = queue.popleft()
                delete_persisted_agent_inputs([failed_item], runtime=runtime)
                await _notify_fail(message)
                continue

            admission.mark_submitted(window_id)
            hibernator.forget(window_id)
            submitted_item = queue.popleft()
            if not queue:
                admission.set_pending(window_id, pending=False)
            await mark_working_fn(bot, user_id, window_id, thread_id)
            refresh_kwargs: dict[str, Any] = {
                "text": item.text,
                "confirm_existing_session": True,
                "transcript_session_id": item.transcript_session_id,
                "transcript_after_offset": item.transcript_offset,
            }
            if not refresh_patched:
                refresh_kwargs["runtime"] = runtime
            confirmed = await refresh_fn(window_id, **refresh_kwargs)
            if confirmed:
                confirm_persisted_agent_input(submitted_item, runtime=runtime)
            else:
                if submitted_item.store_id is not None:
                    _ensure_conf(submitted_item.store_id, recover_submission=False)
                if notify_delay_patched:
                    await notify_delay_fn(bot, user_id, thread_id)
                else:
                    await notify_delay_fn(bot, user_id, thread_id, runtime=runtime)
                return
            await asyncio_mod.sleep(poll_interval)
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception(
            "Agent input queue failed (user=%d thread=%s window=%s)",
            user_id,
            thread_id,
            window_id,
        )
    finally:
        queue = queues.get(key)
        if not queue:
            queues.pop(key, None)
            admission.set_pending(window_id, pending=False)
        if tasks.get(key) is asyncio.current_task():
            tasks.pop(key, None)
        if not target_has_agent_input_confirmation(key, runtime=runtime):
            for queued_key, queued in list(queues.items()):
                if queued and same_agent_input_route(queued_key, key):
                    if drain_patched:
                        drain_fn(bot, queued_key)
                    else:
                        drain_fn(bot, queued_key, runtime=runtime)
                    break


async def cancel_agent_input_drain_tasks(*, runtime: Any = None) -> None:
    """Stop in-memory drainers while leaving the durable spool intact."""
    tasks_map = _dep(runtime, "_agent_input_tasks", _agent_input_tasks)
    queues = _dep(runtime, "_agent_input_queues", _agent_input_queues)
    locks = _dep(runtime, "_agent_input_locks", _agent_input_locks)
    admission = _dep(runtime, "turn_admission", turn_admission)
    tasks = [task for task in tasks_map.values() if not task.done()]
    for task in tasks:
        task.cancel()
    for task in tasks:
        with contextlib.suppress(asyncio.CancelledError):
            await task
    tasks_map.clear()
    queues.clear()
    locks.clear()
    admission.reset()


async def cancel_agent_input_confirmation_tasks(*, runtime: Any = None) -> None:
    """Stop confirmation probes while preserving their durable records."""
    conf_tasks = _dep(
        runtime, "_agent_input_confirmation_tasks", _agent_input_confirmation_tasks
    )
    conf_targets = _dep(
        runtime, "_agent_input_confirmation_targets", _agent_input_confirmation_targets
    )
    conf_modes = _dep(
        runtime, "_agent_input_confirmation_modes", _agent_input_confirmation_modes
    )
    tasks = [task for task in conf_tasks.values() if not task.done()]
    for task in tasks:
        task.cancel()
    for task in tasks:
        with contextlib.suppress(asyncio.CancelledError):
            await task
    conf_tasks.clear()
    conf_targets.clear()
    conf_modes.clear()


async def discard_queued_agent_input(
    user_id: int,
    thread_id: int | None,
    window_id: str,
    *,
    runtime: Any = None,
) -> int:
    """Drop pending user prompts for a target before an explicit interrupt."""
    key = agent_input_key(user_id, thread_id, window_id, runtime=runtime)
    queues = _dep(runtime, "_agent_input_queues", _agent_input_queues)
    tasks = _dep(runtime, "_agent_input_tasks", _agent_input_tasks)
    store = _runtime_store(runtime)
    admission = _dep(runtime, "turn_admission", turn_admission)
    reset_fn, reset_patched = _rt_fn(
        runtime, "_reset_transient_agent_retry", reset_transient_agent_retry
    )

    async with agent_input_lock(key, runtime=runtime):
        await cancel_agent_input_confirmations_for_target(key, runtime=runtime)
        route_keys = {
            candidate
            for candidate in set(queues) | set(tasks)
            if same_agent_input_route(candidate, key)
        }
        route_keys.add(key)
        dropped = 0
        for route_key in route_keys:
            task = tasks.pop(route_key, None)
            if task and not task.done() and task is not asyncio.current_task():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            queue = queues.pop(route_key, None)
            dropped += len(queue) if queue else 0
            if queue:
                delete_persisted_agent_inputs(list(queue), runtime=runtime)
            admission.set_pending(route_key[2], pending=False)
        clear_persisted_agent_input_route(key, runtime=runtime)
        try:
            dropped += store.delete_pending_input_choices_for_route(key[0], key[1])
        except Exception:
            logger.exception(
                "Failed to clear pending input choices (user=%d thread=%d)",
                key[0],
                key[1],
            )
        if reset_patched:
            reset_fn(window_id)
        else:
            reset_fn(window_id, runtime=runtime)

    return dropped


async def run_native_input_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    mode: str,
    runtime: Any = None,
) -> None:
    """Handle explicit /steer and /queue active-turn input."""
    cfg = _dep(runtime, "config", config)
    allowed_fn = _dep(
        runtime,
        "is_user_allowed",
        lambda uid: _default_is_user_allowed(uid, cfg),
    )
    reply_fn = _dep(runtime, "safe_reply", safe_reply)
    get_tid = _dep(runtime, "_get_thread_id", _default_thread_id)
    sm = _dep(runtime, "session_manager", session_manager)
    tmux = _dep(runtime, "tmux_manager", tmux_manager)
    capture_fn = _dep(runtime, "capture_agent_output", capture_agent_output)
    typing_fn = _dep(
        runtime, "_safe_send_typing_action", _default_safe_send_typing_action
    )
    enqueue_status_fn = _dep(runtime, "enqueue_status_update", enqueue_status_update)
    mark_working_fn = _dep(runtime, "mark_window_working", mark_window_working)
    cancel_bash_fn = _dep(runtime, "_cancel_bash_capture", lambda *_args: None)

    user = update.effective_user
    if not user or not allowed_fn(user.id) or not update.message:
        return
    command_name = "steer" if mode == AGENT_INPUT_MODE_STEER else "queue"
    parts = (update.message.text or "").split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        await reply_fn(update.message, f"Usage: /{command_name} <message>")
        return
    text = sanitize_forward_text(parts[1])
    if not text:
        await reply_fn(update.message, "❌ No forwardable text was provided.")
        return

    thread_id = get_tid(update)
    if thread_id is None:
        await reply_fn(update.message, "❌ Please use a named topic.")
        return
    ambiguous_wid = sm.get_ambiguous_window_for_thread(user.id, thread_id)
    if isinstance(ambiguous_wid, str) and ambiguous_wid:
        await reply_fn(
            update.message,
            "❌ This topic shares an agent window with another topic. Use /unbind first.",
        )
        return

    chat = update.effective_chat
    if chat and chat.type in ("group", "supergroup"):
        sm.set_group_chat_id(user.id, thread_id, chat.id)

    window_id = sm.resolve_window_for_thread(user.id, thread_id)
    if not window_id:
        await reply_fn(
            update.message,
            "❌ Native steer/queue requires a local Codex or Claude Code session.",
        )
        return
    window = await tmux.find_window_by_id(window_id)
    if not window:
        display = sm.get_display_name(window_id)
        await reply_fn(update.message, f"❌ Window '{display}' no longer exists.")
        return

    type_fn, type_patched = _rt_fn(runtime, "_window_agent_type", window_agent_type)
    agent_type = (
        type_fn(window_id) if type_patched else type_fn(window_id, runtime=runtime)
    )
    display_name = agent_display_name(agent_type)
    pane_cmd = (window.pane_current_command or "").strip()
    if is_shell_pane_command(pane_cmd):
        await reply_fn(
            update.message,
            f"❌ Window is not running {display_name} (current command: {pane_cmd}).",
        )
        return

    supports_fn, supports_patched = _rt_fn(
        runtime,
        "_window_supports_native_input_routing",
        window_supports_native_input_routing,
    )
    supports_native = (
        supports_fn(window_id)
        if supports_patched
        else supports_fn(window_id, runtime=runtime)
    )
    if not supports_native:
        await reply_fn(
            update.message,
            "❌ This command is unavailable for the current agent backend.",
        )
        return

    await typing_fn(update.message.chat, source=f"{command_name}_command")
    await enqueue_status_fn(
        context.bot,
        user.id,
        window_id,
        None,
        thread_id=thread_id,
    )
    cancel_bash_fn(user.id, thread_id)
    capture = await capture_fn(user.id, thread_id, window_id)
    if capture is None or capture.missing:
        await reply_fn(
            update.message,
            f"❌ {display_name} window is no longer available.",
        )
        return
    pane_text = capture.text or ""
    auth_error = extract_auth_error_message(pane_text)
    if auth_error:
        recovery_message = (
            codex_auth_recovery_message(auth_error)
            if agent_type == AGENT_CODEX
            else auth_error
        )
        await reply_fn(update.message, f"❌ {recovery_message}")
        return

    soq_fn, soq_patched = _rt_fn(
        runtime, "_send_or_queue_agent_input", send_or_queue_agent_input
    )

    async def _send_or_queue() -> tuple[bool, str, bool]:
        if soq_patched:
            return await soq_fn(context.bot, user.id, thread_id, window_id, text)
        return await soq_fn(
            context.bot, user.id, thread_id, window_id, text, runtime=runtime
        )

    if (
        mode == AGENT_INPUT_MODE_QUEUE
        and not agent_capabilities(agent_type).supports_native_queue
    ):
        success, message, queued = await _send_or_queue()
        if not success:
            await reply_fn(update.message, f"❌ {message}")
            return
        if queued:
            await reply_fn(update.message, f"⏳ {message}")
        else:
            await mark_working_fn(context.bot, user.id, window_id, thread_id)
            await reply_fn(
                update.message,
                f"✅ {display_name} was idle, so the message started a new turn.",
            )
        return

    if not pane_text.strip():
        if mode == AGENT_INPUT_MODE_QUEUE:
            success, message, _queued = await _send_or_queue()
            if success:
                await reply_fn(update.message, f"⏳ {message}")
                return
        await reply_fn(
            update.message,
            f"❌ {display_name} state is temporarily unavailable. Retry shortly.",
        )
        return
    if is_interactive_ui(pane_text):
        if mode == AGENT_INPUT_MODE_QUEUE:
            success, message, _queued = await _send_or_queue()
            if success:
                await reply_fn(update.message, f"⏳ {message}")
                return
        await reply_fn(
            update.message,
            f"❌ {display_name} is showing an interactive prompt. "
            "Handle it first, then retry.",
        )
        return

    if await sm.window_has_usage_limit_exceeded(window_id):
        from .window_lifecycle import rotate_thread_after_usage_limit

        rot_fn, rot_patched = _rt_fn(
            runtime,
            "_rotate_thread_after_usage_limit",
            rotate_thread_after_usage_limit,
        )
        rot_kwargs: dict[str, Any] = {
            "context": context,
            "user_id": user.id,
            "thread_id": thread_id,
            "current_window_id": window_id,
            "current_window_cwd": window.cwd,
            "text": text,
        }
        if not rot_patched:
            rot_kwargs["runtime"] = runtime
        handled = await rot_fn(**rot_kwargs)
        if handled:
            return

    if is_codex_input_ready(pane_text):
        success, message, queued = await _send_or_queue()
        if not success:
            await reply_fn(update.message, f"❌ {message}")
            return
        await mark_working_fn(context.bot, user.id, window_id, thread_id)
        if queued:
            await reply_fn(update.message, f"⏳ {message}")
        else:
            await reply_fn(
                update.message,
                f"✅ {display_name} was idle, so the message started a new turn.",
            )
        return

    native_fn, native_patched = _rt_fn(
        runtime, "_send_native_agent_input", send_native_agent_input
    )
    if native_patched:
        success, message = await native_fn(
            context.bot, user.id, thread_id, window_id, text, mode=mode
        )
    else:
        success, message = await native_fn(
            context.bot,
            user.id,
            thread_id,
            window_id,
            text,
            mode=mode,
            runtime=runtime,
        )
    if not success:
        await reply_fn(update.message, f"❌ {message}")
        return
    await mark_working_fn(context.bot, user.id, window_id, thread_id)
    await reply_fn(update.message, f"✅ {message}")


async def steer_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Inject guidance into the current agent turn."""
    await run_native_input_command(update, context, mode=AGENT_INPUT_MODE_STEER)


async def queue_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Queue input for the next agent turn."""
    await run_native_input_command(update, context, mode=AGENT_INPUT_MODE_QUEUE)


async def send_local_forwarded_command(
    bot: Bot,
    message: Any,
    *,
    user_id: int,
    thread_id: int | None,
    window_id: str,
    command_text: str,
    runtime: Any = None,
) -> tuple[bool, str, str]:
    """Send a local slash command with the active agent's busy-turn semantics."""
    type_fn, type_patched = _rt_fn(runtime, "_window_agent_type", window_agent_type)
    agent_type = (
        type_fn(window_id) if type_patched else type_fn(window_id, runtime=runtime)
    )
    capture_fn = _dep(runtime, "capture_agent_output", capture_agent_output)
    capture = await capture_fn(user_id, thread_id, window_id)
    pane_text = (
        capture.text
        if capture is not None and not capture.missing and capture.text
        else ""
    )
    key = agent_input_key(user_id, thread_id, window_id, runtime=runtime)
    busy = (
        bool(pane_text.strip())
        and not is_codex_input_ready(pane_text)
        and not is_interactive_ui(pane_text)
    )
    supports_fn, supports_patched = _rt_fn(
        runtime,
        "_window_supports_native_input_routing",
        window_supports_native_input_routing,
    )
    can_use_fn, can_use_patched = _rt_fn(
        runtime, "_route_can_use_native_input", route_can_use_native_input
    )
    supports_native = (
        supports_fn(window_id)
        if supports_patched
        else supports_fn(window_id, runtime=runtime)
    )
    can_use_native = (
        can_use_fn(key) if can_use_patched else can_use_fn(key, runtime=runtime)
    )

    if busy and thread_id is not None and supports_native and can_use_native:
        if agent_type == AGENT_CODEX:
            offer_fn, offer_patched = _rt_fn(
                runtime, "_offer_input_route_choice", offer_input_route_choice
            )
            offer_kwargs: dict[str, Any] = {
                "user_id": user_id,
                "thread_id": thread_id,
                "window_id": window_id,
                "text": command_text,
            }
            if not offer_patched:
                offer_kwargs["runtime"] = runtime
            offered = await offer_fn(message, **offer_kwargs)
            return offered, "", "choice"

        # Claude Code natively queues slash commands entered while it responds.
        native_fn, native_patched = _rt_fn(
            runtime, "_send_native_agent_input", send_native_agent_input
        )
        mark_working_fn = _dep(runtime, "mark_window_working", mark_window_working)
        if native_patched:
            success, result_message = await native_fn(
                bot,
                user_id,
                thread_id,
                window_id,
                command_text,
                mode=AGENT_INPUT_MODE_STEER,
            )
        else:
            success, result_message = await native_fn(
                bot,
                user_id,
                thread_id,
                window_id,
                command_text,
                mode=AGENT_INPUT_MODE_STEER,
                runtime=runtime,
            )
        if success:
            await mark_working_fn(bot, user_id, window_id, thread_id)
        return success, result_message, "native"

    soq_fn, soq_patched = _rt_fn(
        runtime, "_send_or_queue_agent_input", send_or_queue_agent_input
    )
    if soq_patched:
        success, result_message, queued = await soq_fn(
            bot, user_id, thread_id, window_id, command_text
        )
    else:
        success, result_message, queued = await soq_fn(
            bot, user_id, thread_id, window_id, command_text, runtime=runtime
        )
    return success, result_message, "queued" if queued else "sent"


async def handle_input_routing_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    mode: str,
    record_id: int,
    runtime: Any = None,
) -> None:
    """Consume one persisted mode choice and route it to the bound agent window."""
    get_tid = _dep(runtime, "_get_thread_id", _default_thread_id)
    store = _runtime_store(runtime)
    sm = _dep(runtime, "session_manager", session_manager)
    edit_fn = _dep(runtime, "safe_edit", safe_edit)
    answer_fn = _dep(runtime, "_safe_answer_callback", safe_answer_callback)
    capture_fn = _dep(runtime, "capture_agent_output", capture_agent_output)
    mark_working_fn = _dep(runtime, "mark_window_working", mark_window_working)
    asyncio_mod = _dep(runtime, "asyncio", asyncio)
    max_age = _dep(
        runtime,
        "_INPUT_ROUTE_CHOICE_MAX_AGE_SECONDS",
        INPUT_ROUTE_CHOICE_MAX_AGE_SECONDS,
    )

    query = update.callback_query
    user = update.effective_user
    thread_id = get_tid(update)
    if not query or not user or thread_id is None:
        if query:
            await answer_fn(query, "Invalid topic", show_alert=True)
        return

    try:
        choice = store.claim_pending_input_choice(
            record_id,
            user_id=user.id,
            thread_id=thread_id,
            max_age_seconds=max_age,
        )
    except Exception:
        logger.exception(
            "Failed to claim input routing choice (record=%d user=%d thread=%d)",
            record_id,
            user.id,
            thread_id,
        )
        await answer_fn(query, "Could not load this input", show_alert=True)
        return
    if choice is None:
        await answer_fn(
            query,
            "This input was already handled or expired",
            show_alert=True,
        )
        return

    if mode == INPUT_ROUTE_CANCEL:
        await edit_fn(query, "已取消，这条消息没有发送给 Agent。")
        await answer_fn(query, "Cancelled")
        return

    current_window_id = sm.resolve_window_for_thread(user.id, thread_id)
    if not current_window_id or current_window_id != choice.window_id:
        await edit_fn(query, "❌ 会话绑定已经变化，这条消息没有发送。")
        await answer_fn(query, "Session changed", show_alert=True)
        return

    supports_fn, supports_patched = _rt_fn(
        runtime,
        "_window_supports_native_input_routing",
        window_supports_native_input_routing,
    )
    supports_native = (
        supports_fn(current_window_id)
        if supports_patched
        else supports_fn(current_window_id, runtime=runtime)
    )
    if not supports_native:
        await edit_fn(query, "❌ 当前会话不支持原生引导/排队。")
        await answer_fn(query, "Unsupported session", show_alert=True)
        return

    type_fn, type_patched = _rt_fn(runtime, "_window_agent_type", window_agent_type)
    agent_type = (
        type_fn(current_window_id)
        if type_patched
        else type_fn(current_window_id, runtime=runtime)
    )
    display_name = agent_display_name(agent_type)

    capture = await capture_fn(user.id, thread_id, current_window_id)
    if capture is None or capture.missing:
        await edit_fn(
            query,
            f"❌ {display_name} 窗口已不存在，这条消息没有发送。",
        )
        await answer_fn(query, "Window missing", show_alert=True)
        return
    pane_text = capture.text or ""
    auth_error = extract_auth_error_message(pane_text)
    if auth_error:
        recovery_message = (
            codex_auth_recovery_message(auth_error)
            if agent_type == AGENT_CODEX
            else auth_error
        )
        await edit_fn(query, f"❌ {recovery_message}")
        await answer_fn(query, "Login required", show_alert=True)
        return

    soq_fn, soq_patched = _rt_fn(
        runtime, "_send_or_queue_agent_input", send_or_queue_agent_input
    )

    async def _send_or_queue() -> tuple[bool, str, bool]:
        if soq_patched:
            return await soq_fn(
                context.bot, user.id, thread_id, current_window_id, choice.text
            )
        return await soq_fn(
            context.bot,
            user.id,
            thread_id,
            current_window_id,
            choice.text,
            runtime=runtime,
        )

    if (
        mode == INPUT_ROUTE_QUEUE
        and not agent_capabilities(agent_type).supports_native_queue
    ):
        success, message, queued = await _send_or_queue()
        if not success:
            await edit_fn(query, f"❌ {message}")
            await answer_fn(query, "Queue failed", show_alert=True)
            return
        if not queued:
            await mark_working_fn(context.bot, user.id, current_window_id, thread_id)
        label = f"⏳ {message}" if queued else "✅ Claude Code 已空闲，消息已发送。"
        await edit_fn(query, label)
        await answer_fn(query, "Queued by AgentBot" if queued else "Sent")
        return

    if not pane_text.strip():
        if mode == INPUT_ROUTE_QUEUE:
            success, message, _queued = await _send_or_queue()
            if success:
                await edit_fn(query, f"⏳ {message}")
                await answer_fn(query, "Queued by AgentBot")
                return
        await edit_fn(
            query,
            f"❌ 暂时无法确认 {display_name} 状态；请稍后重新发送这条消息。",
        )
        await answer_fn(query, "Agent state unavailable", show_alert=True)
        return

    if mode == INPUT_ROUTE_INTERRUPT:
        discard_fn, discard_patched = _rt_fn(
            runtime, "_discard_queued_agent_input", discard_queued_agent_input
        )
        if discard_patched:
            await discard_fn(user.id, thread_id, current_window_id)
        else:
            await discard_fn(user.id, thread_id, current_window_id, runtime=runtime)
        send_ctrl_fn, send_ctrl_patched = _rt_fn(
            runtime, "_send_control_to_agent", send_control_to_agent
        )
        if send_ctrl_patched:
            ok, control_message = await send_ctrl_fn(
                user.id, thread_id, current_window_id, "Escape"
            )
        else:
            ok, control_message = await send_ctrl_fn(
                user.id, thread_id, current_window_id, "Escape", runtime=runtime
            )
        if not ok:
            await edit_fn(query, f"❌ {control_message}")
            await answer_fn(query, "Interrupt failed", show_alert=True)
            return
        await asyncio_mod.sleep(0.3)
        success, message, queued = await _send_or_queue()
        if not success:
            await edit_fn(query, f"❌ {message}")
            await answer_fn(query, "Send failed", show_alert=True)
            return
        await mark_working_fn(context.bot, user.id, current_window_id, thread_id)
        label = f"⎋ 已中断；{message}" if queued else "⎋ 已中断并发送新任务。"
        await edit_fn(query, label)
        await answer_fn(query, "Sent")
        return

    if is_interactive_ui(pane_text):
        if mode == INPUT_ROUTE_QUEUE:
            success, message, _queued = await _send_or_queue()
            if success:
                await edit_fn(query, f"⏳ {message}")
                await answer_fn(query, "Queued by AgentBot")
                return
        await edit_fn(
            query,
            f"❌ {display_name} 当前显示交互确认界面；"
            "请先处理该界面，再重新发送这条消息。",
        )
        await answer_fn(query, "Interactive prompt active", show_alert=True)
        return

    if is_codex_input_ready(pane_text):
        success, message, queued = await _send_or_queue()
        if not success:
            await edit_fn(query, f"❌ {message}")
            await answer_fn(query, "Send failed", show_alert=True)
            return
        await mark_working_fn(context.bot, user.id, current_window_id, thread_id)
        label = (
            f"⏳ {message}"
            if queued
            else f"✅ {display_name} 已空闲，消息已作为新一轮发送。"
        )
        await edit_fn(query, label)
        await answer_fn(query, "Sent")
        return

    native_mode = (
        AGENT_INPUT_MODE_QUEUE if mode == INPUT_ROUTE_QUEUE else AGENT_INPUT_MODE_STEER
    )
    native_fn, native_patched = _rt_fn(
        runtime, "_send_native_agent_input", send_native_agent_input
    )
    if native_patched:
        success, message = await native_fn(
            context.bot,
            user.id,
            thread_id,
            current_window_id,
            choice.text,
            mode=native_mode,
        )
    else:
        success, message = await native_fn(
            context.bot,
            user.id,
            thread_id,
            current_window_id,
            choice.text,
            mode=native_mode,
            runtime=runtime,
        )
    if not success:
        await edit_fn(query, f"❌ {message}")
        await answer_fn(query, "Send failed", show_alert=True)
        return
    await mark_working_fn(context.bot, user.id, current_window_id, thread_id)
    await edit_fn(query, f"✅ {message}")
    await answer_fn(query, "Sent")
