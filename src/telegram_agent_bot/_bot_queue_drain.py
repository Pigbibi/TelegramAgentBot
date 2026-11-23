"""Extracted bot runtime helpers (ADR 0001 Phase D)."""

from __future__ import annotations

from telegram_agent_bot._bot_shared import (
    AGENT_CODEX,
    AGENT_INPUT_MODE_QUEUE,
    AGENT_INPUT_MODE_STEER,
    AGENT_INPUT_MODE_TURN,
    Any,
    Bot,
    TRANSIENT_RETRY_SCHEDULED,
    TransientAgentRetry,
    _AGENT_INPUT_PERSIST_RETRY_SECONDS,
    _AGENT_INPUT_POLL_INTERVAL_SECONDS,
    _QueuedAgentInput,
    _TRANSIENT_AGENT_RETRY_DELAYS_SECONDS,
    _TRANSIENT_AGENT_RETRY_PROMPT,
    _agent_input_confirmation_modes,
    _agent_input_confirmation_targets,
    _agent_input_confirmation_tasks,
    _agent_input_locks,
    _agent_input_queues,
    _agent_input_tasks,
    _b,
    _runtime_store,
    _transient_agent_retry_attempts,
    _transient_agent_retry_tasks,
    agent_capabilities,
    agent_display_name,
    asyncio,
    capture_agent_output,
    contextlib,
    extract_auth_error_message,
    extract_interactive_content,
    idle_session_hibernator,
    is_agent_cli_startup_error,
    is_claude_agent,
    is_codex_input_ready,
    is_interactive_ui,
    is_shell_pane_command,
    logger,
    mark_window_working,
    safe_send,
    session_manager,
    time,
    turn_admission,
)
from telegram_agent_bot._bot_input_routing import (
    _agent_input_key,
    _agent_input_lock,
    _agent_input_queue_max_size,
    _agent_max_active_turns,
    _cancel_agent_input_confirmations_for_target,
    _clear_persisted_agent_input_route,
    _clear_persisted_agent_input_target,
    _confirm_persisted_agent_input,
    _delete_persisted_agent_inputs,
    _drop_expired_agent_input,
    _ensure_agent_input_confirmation_task,
    _ensure_agent_input_drain_task,
    _mark_persisted_agent_input_submitted,
    _queue_agent_input,
    _reset_transient_agent_retry,
    _route_can_use_native_input,
    _route_has_agent_input_drain,
    _route_has_agent_input_queue,
    _same_agent_input_route,
    _send_control_to_agent,
    _send_message_to_agent,
    _send_native_input_to_agent,
    _target_has_agent_input_confirmation,
    _target_has_persisted_agent_input_confirmation,
    _window_agent_type,
)
from telegram_agent_bot._bot_login import (
    _codex_auth_recovery_message,
)


def _call__maybe_confirm_startup_trust_prompt():
    from telegram_agent_bot._bot_window_startup import (
        _maybe_confirm_startup_trust_prompt as _fn,
    )

    return _b("_maybe_confirm_startup_trust_prompt", _fn)


def _call__refresh_session_map_after_first_prompt():
    from telegram_agent_bot._bot_window_startup import (
        _refresh_session_map_after_first_prompt as _fn,
    )

    return _b("_refresh_session_map_after_first_prompt", _fn)


async def _check_native_agent_pane_state(
    user_id: int,
    thread_id: int,
    window_id: str,
    display_name: str,
    agent_type: str,
) -> tuple[bool, str, str]:
    """Validate pane state before persisting native active-turn input.

    Returns ``(ok, detail_or_error, pane_text)`` where ``detail_or_error == "idle"``
    means the agent is already ready for a normal turn.
    """
    capture = await _b("capture_agent_output", capture_agent_output)(
        user_id, thread_id, window_id
    )
    if capture is None:
        return False, "No session bound", ""
    if capture.missing:
        return False, "Window not found (may have been closed)", ""
    pane_text = capture.text or ""
    auth_error = _b("extract_auth_error_message", extract_auth_error_message)(pane_text)
    if auth_error:
        msg = (
            _b("_codex_auth_recovery_message", _codex_auth_recovery_message)(auth_error)
            if agent_type == _b("AGENT_CODEX", AGENT_CODEX)
            else auth_error
        )
        return False, msg, pane_text
    if _b("is_interactive_ui", is_interactive_ui)(pane_text):
        return (
            False,
            f"{display_name} is showing an interactive prompt; handle it first.",
            pane_text,
        )
    if not pane_text.strip():
        return (
            False,
            f"{display_name} state is temporarily unavailable; retry shortly.",
            pane_text,
        )
    if _b("is_codex_input_ready", is_codex_input_ready)(pane_text):
        return True, "idle", pane_text
    return True, "busy", pane_text


async def _send_native_agent_input(
    bot: Bot,
    user_id: int,
    thread_id: int,
    window_id: str,
    text: str,
    *,
    mode: str,
) -> tuple[bool, str]:
    """Durably submit native active-turn input without replaying its text."""
    agent_type = _b("_window_agent_type", _window_agent_type)(window_id)
    display_name = _b("agent_display_name", agent_display_name)(agent_type)
    if (
        mode == _b("AGENT_INPUT_MODE_QUEUE", AGENT_INPUT_MODE_QUEUE)
        and not _b("agent_capabilities", agent_capabilities)(
            agent_type
        ).supports_native_queue
    ):
        return (
            False,
            f"{display_name} has no native next-turn key; use the AgentBot queue.",
        )
    key = _b("_agent_input_key", _agent_input_key)(user_id, thread_id, window_id)
    _b("_reset_transient_agent_retry", _reset_transient_agent_retry)(window_id)

    ok, detail, _pane = await _check_native_agent_pane_state(
        user_id, thread_id, window_id, display_name, agent_type
    )
    if not ok:
        return False, detail
    if detail == "idle":
        success, message, queued = await _b(
            "_send_or_queue_agent_input", _send_or_queue_agent_input
        )(bot, user_id, thread_id, window_id, text)
        if not success:
            return False, message
        return (
            True,
            message
            if queued
            else f"{display_name} became idle, so the message started a new turn (Enter).",
        )

    async with _b("_agent_input_lock", _agent_input_lock)(key):
        if not _b("_route_can_use_native_input", _route_can_use_native_input)(key):
            return (
                False,
                "This topic still has bot-managed input pending; wait for it to "
                "finish before using native input.",
            )
        try:
            record = _b("_runtime_store", _runtime_store).enqueue_agent_input(
                user_id=user_id,
                thread_id=thread_id,
                window_id=window_id,
                text=text,
                max_pending=_b(
                    "_agent_input_queue_max_size", _agent_input_queue_max_size
                )(),
                submission_mode=mode,
            )
        except Exception:
            _b("logger", logger).exception(
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

        item = _QueuedAgentInput(
            text=text,
            created_at_epoch=record.created_at_epoch,
            store_id=record.id,
            submission_mode=mode,
        )
        if not await _b(
            "_mark_persisted_agent_input_submitted",
            _mark_persisted_agent_input_submitted,
        )(item, window_id):
            _b("_delete_persisted_agent_inputs", _delete_persisted_agent_inputs)([item])
            return False, "The input could not enter a safe submission state."

        try:
            success, message = await _b(
                "_send_native_input_to_agent", _send_native_input_to_agent
            )(user_id, thread_id, window_id, text, mode=mode)
        except BaseException:
            if item.store_id is not None:
                _b(
                    "_ensure_agent_input_confirmation_task",
                    _ensure_agent_input_confirmation_task,
                )(
                    bot,
                    item.store_id,
                    key,
                    recover_submission=True,
                    submission_mode=mode,
                )
            raise
        if not success:
            _b("_delete_persisted_agent_inputs", _delete_persisted_agent_inputs)([item])
            return False, message

        _b("idle_session_hibernator", idle_session_hibernator).forget(window_id)
        if item.store_id is not None:
            _b(
                "_ensure_agent_input_confirmation_task",
                _ensure_agent_input_confirmation_task,
            )(
                bot,
                item.store_id,
                key,
                recover_submission=True,
                initial_delay_seconds=0.0,
                notify_on_first_delay=mode
                == _b("AGENT_INPUT_MODE_STEER", AGENT_INPUT_MODE_STEER),
                submission_mode=mode,
            )
        if mode == _b("AGENT_INPUT_MODE_QUEUE", AGENT_INPUT_MODE_QUEUE):
            return True, f"Queued in {display_name} for the next turn (Tab)."
        if _b("is_claude_agent", is_claude_agent)(
            agent_type
        ) and text.lstrip().startswith("/"):
            return (
                True,
                "Command submitted; Claude Code will run it after the current turn "
                "(Enter).",
            )
        return True, f"Guidance sent to the current {display_name} turn (Enter)."


def _agent_cli_startup_failure(capture: Any) -> str | None:
    """Return a safe notice when a local shell shows a rejected CLI launch."""
    if (
        capture is not None
        and _b("is_shell_pane_command", is_shell_pane_command)(
            getattr(capture, "pane_command", "")
        )
        and _b("is_agent_cli_startup_error", is_agent_cli_startup_error)(
            getattr(capture, "text", "") or ""
        )
    ):
        return (
            "The agent CLI failed to start. This message was not sent; fix the "
            "launch command and resend it."
        )
    return None


def _has_active_queue_or_confirmation(key: tuple[int, int, str]) -> bool:
    return bool(
        _b("_route_has_agent_input_queue", _route_has_agent_input_queue)(key)
        or _b("_route_has_agent_input_drain", _route_has_agent_input_drain)(key)
        or _b(
            "_target_has_agent_input_confirmation", _target_has_agent_input_confirmation
        )(key)
        or _b(
            "_target_has_persisted_agent_input_confirmation",
            _target_has_persisted_agent_input_confirmation,
        )(key)
    )


async def _submit_ready_queue_item(
    bot: Bot,
    user_id: int,
    thread_id: int | None,
    window_id: str,
    text: str,
    key: tuple[int, int, str],
    queue: Any,
    item: Any,
    depth: int,
    limit: int,
) -> tuple[bool, str, bool]:
    _b("turn_admission", turn_admission).observe(window_id, active=False)
    admitted = await _b("turn_admission", turn_admission).try_acquire(
        window_id, limit=_b("_agent_max_active_turns", _agent_max_active_turns)()
    )
    if not admitted:
        _b("_ensure_agent_input_drain_task", _ensure_agent_input_drain_task)(bot, key)
        return (
            True,
            f"Server is at its active-task limit; queued for the next available slot ({depth}/{limit})",
            True,
        )
    if not await _b(
        "_mark_persisted_agent_input_submitted", _mark_persisted_agent_input_submitted
    )(item, window_id):
        _b("turn_admission", turn_admission).release(window_id)
        _b("_ensure_agent_input_drain_task", _ensure_agent_input_drain_task)(bot, key)
        return (
            True,
            f"Saved safely; waiting for the delivery state to become available ({depth}/{limit})",
            True,
        )
    try:
        success, message = await _b("_send_message_to_agent", _send_message_to_agent)(
            user_id, thread_id, window_id, text
        )
    except BaseException:
        _b("turn_admission", turn_admission).release(window_id)
        if queue and queue[0] is item:
            queue.popleft()
        if not queue:
            _b("_agent_input_queues", _agent_input_queues).pop(key, None)
        if item.store_id is not None:
            _b(
                "_ensure_agent_input_confirmation_task",
                _ensure_agent_input_confirmation_task,
            )(bot, item.store_id, key, recover_submission=True)
        raise
    if success:
        _b("turn_admission", turn_admission).mark_submitted(window_id)
        _b("idle_session_hibernator", idle_session_hibernator).forget(window_id)
        queue.popleft()
        if not queue:
            _b("_agent_input_queues", _agent_input_queues).pop(key, None)
        _b("turn_admission", turn_admission).set_pending(window_id, pending=False)
        if item.store_id is not None:
            _b(
                "_ensure_agent_input_confirmation_task",
                _ensure_agent_input_confirmation_task,
            )(
                bot,
                item.store_id,
                key,
                recover_submission=True,
                initial_delay_seconds=0.0,
                notify_on_first_delay=True,
            )
        return success, message, False
    _b("turn_admission", turn_admission).release(window_id)
    queue.popleft()
    _b("_delete_persisted_agent_inputs", _delete_persisted_agent_inputs)([item])
    _b("turn_admission", turn_admission).set_pending(window_id, pending=False)
    return success, message, False


async def _dispatch_captured_queue_item(
    bot: Bot,
    user_id: int,
    thread_id: int | None,
    window_id: str,
    text: str,
    key: tuple[int, int, str],
    queue: Any,
    item: Any,
    depth: int,
    limit: int,
    capture: Any,
) -> tuple[bool, str, bool]:
    startup_failure = _b("_agent_cli_startup_failure", _agent_cli_startup_failure)(
        capture
    )
    if capture is None:
        return False, "No session bound", False
    if capture.missing:
        return False, "Window not found (may have been closed)", False
    if startup_failure:
        return False, startup_failure, False
    pane_text = capture.text or ""
    auth_error = _b("extract_auth_error_message", extract_auth_error_message)(pane_text)
    if auth_error:
        return (
            False,
            _b("_codex_auth_recovery_message", _codex_auth_recovery_message)(
                auth_error
            ),
            False,
        )
    if _b("is_interactive_ui", is_interactive_ui)(pane_text):
        interactive = _b("extract_interactive_content", extract_interactive_content)(
            pane_text
        )
        if interactive is not None and interactive.name == "DirectoryTrust":
            ok, control_message = await _call__maybe_confirm_startup_trust_prompt()(
                window_id, pane_text
            )
        else:
            ok, control_message = await _b(
                "_send_control_to_agent", _send_control_to_agent
            )(user_id, thread_id, window_id, "Escape")
        if not ok:
            _b("logger", logger).warning(
                "Failed to interrupt Codex interactive prompt before "
                "queuing input (user=%d thread=%s window=%s): %s",
                user_id,
                thread_id,
                window_id,
                control_message,
            )
        _b("_ensure_agent_input_drain_task", _ensure_agent_input_drain_task)(bot, key)
        return (
            True,
            f"Interrupted agent prompt and queued until the agent is ready ({depth}/{limit})",
            True,
        )
    if not _b("is_codex_input_ready", is_codex_input_ready)(pane_text):
        _b("turn_admission", turn_admission).observe(window_id, active=True)
        _b("_ensure_agent_input_drain_task", _ensure_agent_input_drain_task)(bot, key)
        return True, f"Agent is busy; queued until ready ({depth}/{limit})", True
    return await _submit_ready_queue_item(
        bot, user_id, thread_id, window_id, text, key, queue, item, depth, limit
    )


async def _send_or_queue_agent_input(
    bot: Bot,
    user_id: int,
    thread_id: int | None,
    window_id: str,
    text: str,
    *,
    transient_retry: bool = False,
) -> tuple[bool, str, bool]:
    """Persist one input, then send its queue head when the target is ready."""
    key = _b("_agent_input_key", _agent_input_key)(user_id, thread_id, window_id)
    if not transient_retry:
        _b("_reset_transient_agent_retry", _reset_transient_agent_retry)(window_id)
    async with _b("_agent_input_lock", _agent_input_lock)(key):
        if _has_active_queue_or_confirmation(key):
            queued, depth, limit = _b("_queue_agent_input", _queue_agent_input)(
                key, text
            )
            _b("_ensure_agent_input_drain_task", _ensure_agent_input_drain_task)(
                bot, key
            )
            if not queued:
                return (
                    False,
                    f"Agent is still busy and the input queue is full ({limit} pending). "
                    "Wait for it to finish or use /interrupt.",
                    False,
                )
            return True, f"Queued until the agent is ready ({depth}/{limit})", True

        queued, depth, limit = _b("_queue_agent_input", _queue_agent_input)(key, text)
        if not queued:
            return (
                False,
                "The input could not be saved safely. Please try again; it was not sent to the agent.",
                False,
            )
        queue = _b("_agent_input_queues", _agent_input_queues)[key]
        item = queue[0]
        try:
            capture = await _b("capture_agent_output", capture_agent_output)(
                user_id, thread_id, window_id
            )
        except BaseException:
            _b("_ensure_agent_input_drain_task", _ensure_agent_input_drain_task)(
                bot, key
            )
            raise
        result = await _dispatch_captured_queue_item(
            bot,
            user_id,
            thread_id,
            window_id,
            text,
            key,
            queue,
            item,
            depth,
            limit,
            capture,
        )
        if not result[0]:
            if queue and queue[0] is item:
                queue.popleft()
                _b("_delete_persisted_agent_inputs", _delete_persisted_agent_inputs)(
                    [item]
                )
            if not queue:
                _b("_agent_input_queues", _agent_input_queues).pop(key, None)
                _b("turn_admission", turn_admission).set_pending(
                    window_id, pending=False
                )
    return result


async def _run_transient_agent_retry(
    bot: Bot,
    key: tuple[int, int, str],
    *,
    attempt: int,
    delay_seconds: float,
) -> None:
    """Resume an unfinished turn after a bounded transient-capacity backoff."""
    user_id, thread_key, window_id = key
    thread_id = thread_key or None
    current_task = _b("asyncio", asyncio).current_task()
    try:
        await _b("asyncio", asyncio).sleep(delay_seconds)
        try:
            persisted = _b("_runtime_store", _runtime_store).get_transient_agent_retry(
                window_id
            )
            if persisted is None or (
                persisted.attempt != attempt
                or persisted.state
                != _b("TRANSIENT_RETRY_SCHEDULED", TRANSIENT_RETRY_SCHEDULED)
            ):
                return
            changed = _b(
                "_runtime_store", _runtime_store
            ).mark_transient_agent_retry_waiting(
                window_id,
                attempt,
            )
            if not changed:
                return
        except Exception:
            _b("logger", logger).exception(
                "Failed to persist due transient retry state (window=%s attempt=%d)",
                window_id,
                attempt,
            )
            return
        success, message, _queued = await _b(
            "_send_or_queue_agent_input", _send_or_queue_agent_input
        )(
            bot,
            user_id,
            thread_id,
            window_id,
            _b("_TRANSIENT_AGENT_RETRY_PROMPT", _TRANSIENT_AGENT_RETRY_PROMPT),
            transient_retry=True,
        )
        if not success:
            _b("logger", logger).warning(
                "Automatic agent retry could not be submitted "
                "(user=%d thread=%s window=%s): %s",
                user_id,
                thread_id,
                window_id,
                message,
            )
            await _b("safe_send", safe_send)(
                bot,
                _b("session_manager", session_manager).resolve_chat_id(
                    user_id, thread_id
                ),
                f"❌ The unfinished task could not be retried automatically: {message}",
                message_thread_id=thread_id,
            )
            return
        await _b("mark_window_working", mark_window_working)(
            bot, user_id, window_id, thread_id
        )
        _b("logger", logger).info(
            "Automatically retried transient agent failure "
            "(user=%d thread=%s window=%s)",
            user_id,
            thread_id,
            window_id,
        )
    except _b("asyncio", asyncio).CancelledError:
        raise
    except Exception:
        _b("logger", logger).exception(
            "Automatic agent retry failed (user=%d thread=%s window=%s)",
            user_id,
            thread_id,
            window_id,
        )
    finally:
        if (
            _b("_transient_agent_retry_tasks", _transient_agent_retry_tasks).get(
                window_id
            )
            is current_task
        ):
            _b("_transient_agent_retry_tasks", _transient_agent_retry_tasks).pop(
                window_id, None
            )


def _schedule_transient_agent_retry(
    bot: Bot,
    user_id: int,
    thread_id: int | None,
    window_id: str,
) -> tuple[int, float] | None:
    """Schedule one bounded retry, returning its attempt number and delay."""
    key = _b("_agent_input_key", _agent_input_key)(user_id, thread_id, window_id)
    existing = _b("_transient_agent_retry_tasks", _transient_agent_retry_tasks).get(
        window_id
    )
    if existing and not existing.done():
        attempt = _b(
            "_transient_agent_retry_attempts", _transient_agent_retry_attempts
        ).get(window_id, 1)
        return attempt, 0.0

    persisted: TransientAgentRetry | None = None
    try:
        persisted = _b("_runtime_store", _runtime_store).get_transient_agent_retry(
            window_id
        )
    except Exception:
        _b("logger", logger).exception(
            "Failed to load persisted transient retry for window %s", window_id
        )

    if persisted is not None and persisted.state == _b(
        "TRANSIENT_RETRY_SCHEDULED", TRANSIENT_RETRY_SCHEDULED
    ):
        attempt = persisted.attempt
        delay_seconds = max(0.0, persisted.not_before_epoch - _b("time", time).time())
        _b("_transient_agent_retry_attempts", _transient_agent_retry_attempts)[
            window_id
        ] = attempt
        task = _b("asyncio", asyncio).create_task(
            _b("_run_transient_agent_retry", _run_transient_agent_retry)(
                bot,
                _b("_agent_input_key", _agent_input_key)(
                    persisted.user_id,
                    persisted.thread_id or None,
                    window_id,
                ),
                attempt=attempt,
                delay_seconds=delay_seconds,
            )
        )
        _b("_transient_agent_retry_tasks", _transient_agent_retry_tasks)[window_id] = (
            task
        )
        return attempt, delay_seconds

    previous_attempt = _b(
        "_transient_agent_retry_attempts", _transient_agent_retry_attempts
    ).get(window_id, 0)
    if persisted is not None:
        previous_attempt = max(previous_attempt, persisted.attempt)
    attempt = previous_attempt + 1
    if attempt > len(
        _b(
            "_TRANSIENT_AGENT_RETRY_DELAYS_SECONDS",
            _TRANSIENT_AGENT_RETRY_DELAYS_SECONDS,
        )
    ):
        return None

    delay_seconds = _b(
        "_TRANSIENT_AGENT_RETRY_DELAYS_SECONDS", _TRANSIENT_AGENT_RETRY_DELAYS_SECONDS
    )[attempt - 1]
    _b("_transient_agent_retry_attempts", _transient_agent_retry_attempts)[
        window_id
    ] = attempt
    try:
        _b("_runtime_store", _runtime_store).save_transient_agent_retry(
            user_id=user_id,
            thread_id=thread_id or 0,
            window_id=window_id,
            attempt=attempt,
            state=_b("TRANSIENT_RETRY_SCHEDULED", TRANSIENT_RETRY_SCHEDULED),
            not_before_epoch=_b("time", time).time() + delay_seconds,
            created_at_epoch=(
                persisted.created_at_epoch if persisted is not None else None
            ),
        )
    except Exception:
        _b("logger", logger).exception(
            "Failed to persist transient retry (window=%s attempt=%d)",
            window_id,
            attempt,
        )
        _b("_transient_agent_retry_attempts", _transient_agent_retry_attempts).pop(
            window_id, None
        )
        return None
    task = _b("asyncio", asyncio).create_task(
        _b("_run_transient_agent_retry", _run_transient_agent_retry)(
            bot,
            key,
            attempt=attempt,
            delay_seconds=delay_seconds,
        )
    )
    _b("_transient_agent_retry_tasks", _transient_agent_retry_tasks)[window_id] = task
    return attempt, delay_seconds


def _restore_transient_agent_retries(bot: Bot) -> int:
    """Restore delayed retries without replaying already-submitted continuations."""
    try:
        records = _b("_runtime_store", _runtime_store).list_transient_agent_retries()
    except Exception:
        _b("logger", logger).exception(
            "Failed to restore persisted transient agent retries"
        )
        return 0

    restored = 0
    for record in records:
        window_id = record.window_id
        resolved_window_id = _b(
            "session_manager", session_manager
        ).resolve_window_for_thread(
            record.user_id,
            record.thread_id or None,
        )
        if resolved_window_id and resolved_window_id != window_id:
            try:
                _b("_runtime_store", _runtime_store).delete_transient_agent_retry(
                    window_id
                )
                record = _b(
                    "_runtime_store", _runtime_store
                ).save_transient_agent_retry(
                    user_id=record.user_id,
                    thread_id=record.thread_id,
                    window_id=resolved_window_id,
                    attempt=record.attempt,
                    state=record.state,
                    not_before_epoch=record.not_before_epoch,
                    created_at_epoch=record.created_at_epoch,
                )
            except Exception:
                _b("logger", logger).exception(
                    "Failed to retarget transient retry from %s to %s",
                    window_id,
                    resolved_window_id,
                )
                continue
            window_id = resolved_window_id

        _b("_transient_agent_retry_attempts", _transient_agent_retry_attempts)[
            window_id
        ] = record.attempt
        if record.state == _b("TRANSIENT_RETRY_SCHEDULED", TRANSIENT_RETRY_SCHEDULED):
            delay_seconds = max(0.0, record.not_before_epoch - _b("time", time).time())
            key = _b("_agent_input_key", _agent_input_key)(
                record.user_id,
                record.thread_id or None,
                window_id,
            )
            _b("_transient_agent_retry_tasks", _transient_agent_retry_tasks)[
                window_id
            ] = _b("asyncio", asyncio).create_task(
                _b("_run_transient_agent_retry", _run_transient_agent_retry)(
                    bot,
                    key,
                    attempt=record.attempt,
                    delay_seconds=delay_seconds,
                )
            )
        restored += 1
    if restored:
        _b("logger", logger).info(
            "Restored %d transient agent retry state(s)", restored
        )
    return restored


async def _cancel_transient_agent_retry_tasks() -> None:
    """Cancel delayed retries while preserving their restart-safe records."""
    tasks = [
        task
        for task in _b(
            "_transient_agent_retry_tasks", _transient_agent_retry_tasks
        ).values()
        if not task.done()
    ]
    for task in tasks:
        task.cancel()
    for task in tasks:
        with _b("contextlib", contextlib).suppress(
            _b("asyncio", asyncio).CancelledError
        ):
            await task
    _b("_transient_agent_retry_tasks", _transient_agent_retry_tasks).clear()
    _b("_transient_agent_retry_attempts", _transient_agent_retry_attempts).clear()


async def _notify_queued_input_failure(
    bot: Bot,
    user_id: int,
    thread_id: int | None,
    message: str,
) -> None:
    _b("logger", logger).warning(
        "Queued agent input delivery failed (user=%d thread=%s): %s",
        user_id,
        thread_id,
        message,
    )
    try:
        await _b("safe_send", safe_send)(
            bot,
            _b("session_manager", session_manager).resolve_chat_id(user_id, thread_id),
            f"❌ Queued message failed: {message}",
            message_thread_id=thread_id,
        )
    except Exception:
        _b("logger", logger).exception(
            "Failed to notify queued input failure (user=%d thread=%s)",
            user_id,
            thread_id,
        )


async def _notify_agent_input_confirmation_delayed(
    bot: Bot,
    user_id: int,
    thread_id: int | None,
) -> None:
    """Report a recoverable confirmation delay without calling it a failure."""
    _b("logger", logger).warning(
        "Agent input transcript confirmation delayed (user=%d thread=%s)",
        user_id,
        thread_id,
    )
    try:
        await _b("safe_send", safe_send)(
            bot,
            _b("session_manager", session_manager).resolve_chat_id(user_id, thread_id),
            "⏳ Message was submitted, but transcript confirmation is delayed. "
            "The bot is still tracking it and will not resend automatically, "
            "avoiding duplicate input.",
            message_thread_id=thread_id,
        )
    except Exception:
        _b("logger", logger).exception(
            "Failed to notify delayed agent input confirmation (user=%d thread=%s)",
            user_id,
            thread_id,
        )


async def _notify_agent_input_confirmation_expired(
    bot: Bot,
    user_id: int,
    thread_id: int | None,
) -> None:
    """Explain why an unconfirmed input was retired without a replay."""
    try:
        await _b("safe_send", safe_send)(
            bot,
            _b("session_manager", session_manager).resolve_chat_id(user_id, thread_id),
            "⚠️ The earlier message was not confirmed in the agent transcript "
            "within 5 minutes. It was not resent, avoiding duplicate input. "
            "Later queued messages can continue now; resend the earlier message "
            "only if the agent did not receive it.",
            message_thread_id=thread_id,
        )
    except Exception:
        _b("logger", logger).exception(
            "Failed to notify expired agent input confirmation (user=%d thread=%s)",
            user_id,
            thread_id,
        )


async def _notify_rebound_agent_input_unconfirmed(
    bot: Bot,
    user_id: int,
    thread_id: int | None,
) -> None:
    """Explain why an old-window confirmation was retired without a replay."""
    try:
        await _b("safe_send", safe_send)(
            bot,
            _b("session_manager", session_manager).resolve_chat_id(user_id, thread_id),
            "⚠️ A message submitted to the previous agent window never appeared "
            "in its transcript. It was not resent after the session moved, avoiding "
            "duplicate input. Later messages will continue normally; resend the "
            "earlier message if you still need it.",
            message_thread_id=thread_id,
        )
    except Exception:
        _b("logger", logger).exception(
            "Failed to notify retired rebound input (user=%d thread=%s)",
            user_id,
            thread_id,
        )


async def _fail_drain_queue_target(
    bot: Bot,
    key: tuple[int, int, str],
    queue: Any,
    reason: str,
    *,
    forget_admission: bool = False,
) -> None:
    user_id, thread_key, window_id = key
    await _b("_notify_queued_input_failure", _notify_queued_input_failure)(
        bot, user_id, thread_key or None, reason
    )
    queue.clear()
    _b("_clear_persisted_agent_input_target", _clear_persisted_agent_input_target)(key)
    if forget_admission:
        _b("turn_admission", turn_admission).forget(window_id)
    else:
        _b("turn_admission", turn_admission).set_pending(window_id, pending=False)


async def _validate_drain_pane(
    bot: Bot,
    key: tuple[int, int, str],
    queue: Any,
    capture: Any,
) -> tuple[bool, str]:
    """Return ``(ok, pane_text)``; notifies and clears queue on hard failures."""
    if capture is None:
        await _fail_drain_queue_target(bot, key, queue, "No session bound")
        return False, ""
    if capture.missing:
        await _fail_drain_queue_target(
            bot,
            key,
            queue,
            "Window not found (may have been closed)",
            forget_admission=True,
        )
        return False, ""
    startup_failure = _b("_agent_cli_startup_failure", _agent_cli_startup_failure)(
        capture
    )
    if startup_failure:
        await _fail_drain_queue_target(bot, key, queue, startup_failure)
        return False, ""
    pane_text = capture.text or ""
    auth_error = _b("extract_auth_error_message", extract_auth_error_message)(pane_text)
    if auth_error:
        await _fail_drain_queue_target(
            bot,
            key,
            queue,
            _b("_codex_auth_recovery_message", _codex_auth_recovery_message)(
                auth_error
            ),
        )
        return False, ""
    return True, pane_text


async def _drain_native_queued_item(
    bot: Bot,
    key: tuple[int, int, str],
    queue: Any,
    item: Any,
) -> None:
    user_id, thread_key, window_id = key
    thread_id = thread_key or None
    _b("turn_admission", turn_admission).observe(window_id, active=True)
    if not await _b(
        "_mark_persisted_agent_input_submitted", _mark_persisted_agent_input_submitted
    )(item, window_id):
        await _b("asyncio", asyncio).sleep(
            _b("_AGENT_INPUT_PERSIST_RETRY_SECONDS", _AGENT_INPUT_PERSIST_RETRY_SECONDS)
        )
        return
    try:
        success, message = await _b(
            "_send_native_input_to_agent", _send_native_input_to_agent
        )(user_id, thread_id, window_id, item.text, mode=item.submission_mode)
    except BaseException:
        if queue and queue[0] is item:
            queue.popleft()
        if item.store_id is not None:
            _b(
                "_ensure_agent_input_confirmation_task",
                _ensure_agent_input_confirmation_task,
            )(
                bot,
                item.store_id,
                key,
                recover_submission=True,
                submission_mode=item.submission_mode,
            )
        raise
    if not success:
        failed_item = queue.popleft()
        _b("_delete_persisted_agent_inputs", _delete_persisted_agent_inputs)(
            [failed_item]
        )
        await _b("_notify_queued_input_failure", _notify_queued_input_failure)(
            bot, user_id, thread_id, message
        )
        return
    _b("idle_session_hibernator", idle_session_hibernator).forget(window_id)
    submitted_item = queue.popleft()
    if not queue:
        _b("turn_admission", turn_admission).set_pending(window_id, pending=False)
    await _b("mark_window_working", mark_window_working)(
        bot, user_id, window_id, thread_id
    )
    if submitted_item.store_id is not None:
        _b(
            "_ensure_agent_input_confirmation_task",
            _ensure_agent_input_confirmation_task,
        )(
            bot,
            submitted_item.store_id,
            key,
            recover_submission=True,
            initial_delay_seconds=0.0,
            submission_mode=submitted_item.submission_mode,
        )
    await _b("asyncio", asyncio).sleep(
        _b("_AGENT_INPUT_POLL_INTERVAL_SECONDS", _AGENT_INPUT_POLL_INTERVAL_SECONDS)
    )


async def _ensure_item_turn_mode(
    item: Any,
    window_id: str,
) -> bool:
    if item.submission_mode == _b("AGENT_INPUT_MODE_TURN", AGENT_INPUT_MODE_TURN):
        return True
    try:
        changed = item.store_id is None or _b(
            "_runtime_store", _runtime_store
        ).set_agent_input_submission_mode(
            item.store_id, _b("AGENT_INPUT_MODE_TURN", AGENT_INPUT_MODE_TURN)
        )
    except Exception:
        _b("logger", logger).exception(
            "Failed to downgrade restored native input to a new turn (record=%s window=%s)",
            item.store_id,
            window_id,
        )
        changed = False
    if not changed:
        await _b("asyncio", asyncio).sleep(
            _b("_AGENT_INPUT_PERSIST_RETRY_SECONDS", _AGENT_INPUT_PERSIST_RETRY_SECONDS)
        )
        return False
    item.submission_mode = _b("AGENT_INPUT_MODE_TURN", AGENT_INPUT_MODE_TURN)
    return True


async def _drain_turn_queued_item(
    bot: Bot,
    key: tuple[int, int, str],
    queue: Any,
    item: Any,
) -> bool:
    """Send one new-turn item from queue; return True if confirmation delayed."""
    user_id, thread_key, window_id = key
    thread_id = thread_key or None
    if not await _ensure_item_turn_mode(item, window_id):
        return False
    _b("turn_admission", turn_admission).observe(window_id, active=False)
    if not await _b("turn_admission", turn_admission).try_acquire(
        window_id, limit=_b("_agent_max_active_turns", _agent_max_active_turns)()
    ):
        await _b("asyncio", asyncio).sleep(
            _b("_AGENT_INPUT_POLL_INTERVAL_SECONDS", _AGENT_INPUT_POLL_INTERVAL_SECONDS)
        )
        return False
    if not await _b(
        "_mark_persisted_agent_input_submitted", _mark_persisted_agent_input_submitted
    )(item, window_id):
        _b("turn_admission", turn_admission).release(window_id)
        await _b("asyncio", asyncio).sleep(
            _b("_AGENT_INPUT_PERSIST_RETRY_SECONDS", _AGENT_INPUT_PERSIST_RETRY_SECONDS)
        )
        return False
    try:
        success, message = await _b("_send_message_to_agent", _send_message_to_agent)(
            user_id, thread_id, window_id, item.text
        )
    except BaseException:
        _b("turn_admission", turn_admission).release(window_id)
        if queue and queue[0] is item:
            queue.popleft()
        if item.store_id is not None:
            _b(
                "_ensure_agent_input_confirmation_task",
                _ensure_agent_input_confirmation_task,
            )(bot, item.store_id, key, recover_submission=True)
        raise
    if not success:
        _b("turn_admission", turn_admission).release(window_id)
        failed_item = queue.popleft()
        _b("_delete_persisted_agent_inputs", _delete_persisted_agent_inputs)(
            [failed_item]
        )
        await _b("_notify_queued_input_failure", _notify_queued_input_failure)(
            bot, user_id, thread_id, message
        )
        return False

    _b("turn_admission", turn_admission).mark_submitted(window_id)
    _b("idle_session_hibernator", idle_session_hibernator).forget(window_id)
    submitted_item = queue.popleft()
    if not queue:
        _b("turn_admission", turn_admission).set_pending(window_id, pending=False)
    await _b("mark_window_working", mark_window_working)(
        bot, user_id, window_id, thread_id
    )
    confirmed = await _call__refresh_session_map_after_first_prompt()(
        window_id,
        text=item.text,
        confirm_existing_session=True,
        transcript_session_id=item.transcript_session_id,
        transcript_after_offset=item.transcript_offset,
    )
    if confirmed:
        _b("_confirm_persisted_agent_input", _confirm_persisted_agent_input)(
            submitted_item
        )
        await _b("asyncio", asyncio).sleep(
            _b("_AGENT_INPUT_POLL_INTERVAL_SECONDS", _AGENT_INPUT_POLL_INTERVAL_SECONDS)
        )
        return False
    if submitted_item.store_id is not None:
        _b(
            "_ensure_agent_input_confirmation_task",
            _ensure_agent_input_confirmation_task,
        )(bot, submitted_item.store_id, key, recover_submission=False)
    await _b(
        "_notify_agent_input_confirmation_delayed",
        _notify_agent_input_confirmation_delayed,
    )(bot, user_id, thread_id)
    return True


async def _step_drain_agent_input_queue(
    bot: Bot,
    key: tuple[int, int, str],
    poll_delay: float,
) -> bool:
    """Process one queue iteration; return True when the drain loop should continue."""
    user_id, thread_key, window_id = key
    thread_id = thread_key or None
    queue = _b("_agent_input_queues", _agent_input_queues).get(key)
    if not queue:
        return False
    await _b("_drop_expired_agent_input", _drop_expired_agent_input)(bot, key, queue)
    if not queue:
        return False
    capture = await _b("capture_agent_output", capture_agent_output)(
        user_id, thread_id, window_id
    )
    ok, pane_text = await _validate_drain_pane(bot, key, queue, capture)
    if not ok:
        return False
    item = queue[0]
    if not pane_text.strip():
        await _b("asyncio", asyncio).sleep(poll_delay)
        return True
    if _b("is_interactive_ui", is_interactive_ui)(pane_text):
        ic = _b("extract_interactive_content", extract_interactive_content)(pane_text)
        if ic is not None and ic.name == "DirectoryTrust":
            await _call__maybe_confirm_startup_trust_prompt()(window_id, pane_text)
        _b("turn_admission", turn_admission).observe(window_id, active=True)
        await _b("asyncio", asyncio).sleep(poll_delay)
        return True
    input_ready = _b("is_codex_input_ready", is_codex_input_ready)(pane_text)
    if (
        item.submission_mode
        in {
            _b("AGENT_INPUT_MODE_STEER", AGENT_INPUT_MODE_STEER),
            _b("AGENT_INPUT_MODE_QUEUE", AGENT_INPUT_MODE_QUEUE),
        }
        and not input_ready
    ):
        await _drain_native_queued_item(bot, key, queue, item)
        return True
    if not input_ready:
        _b("turn_admission", turn_admission).observe(window_id, active=True)
        await _b("asyncio", asyncio).sleep(poll_delay)
        return True
    return not await _drain_turn_queued_item(bot, key, queue, item)


def _finalize_drain_agent_input_queue(bot: Bot, key: tuple[int, int, str]) -> None:
    _user_id, _thread_key, window_id = key
    queue = _b("_agent_input_queues", _agent_input_queues).get(key)
    if not queue:
        _b("_agent_input_queues", _agent_input_queues).pop(key, None)
        _b("turn_admission", turn_admission).set_pending(window_id, pending=False)
    if (
        _b("_agent_input_tasks", _agent_input_tasks).get(key)
        is _b("asyncio", asyncio).current_task()
    ):
        _b("_agent_input_tasks", _agent_input_tasks).pop(key, None)
    if not _b(
        "_target_has_agent_input_confirmation", _target_has_agent_input_confirmation
    )(key):
        for queued_key, queued in list(
            _b("_agent_input_queues", _agent_input_queues).items()
        ):
            if queued and _b("_same_agent_input_route", _same_agent_input_route)(
                queued_key, key
            ):
                _b("_ensure_agent_input_drain_task", _ensure_agent_input_drain_task)(
                    bot, queued_key
                )
                break


async def _drain_agent_input_queue(
    bot: Bot,
    key: tuple[int, int, str],
) -> None:
    """Forward queued user prompts one at a time when Codex returns to input."""
    user_id, thread_key, window_id = key
    thread_id = thread_key or None
    poll_delay = _b(
        "_AGENT_INPUT_POLL_INTERVAL_SECONDS", _AGENT_INPUT_POLL_INTERVAL_SECONDS
    )
    try:
        while await _step_drain_agent_input_queue(bot, key, poll_delay):
            pass
    except _b("asyncio", asyncio).CancelledError:
        raise
    except Exception:
        _b("logger", logger).exception(
            "Agent input queue failed (user=%d thread=%s window=%s)",
            user_id,
            thread_id,
            window_id,
        )
    finally:
        _finalize_drain_agent_input_queue(bot, key)


async def _cancel_agent_input_drain_tasks() -> None:
    """Stop in-memory drainers while leaving the durable spool intact."""
    tasks = [
        task
        for task in _b("_agent_input_tasks", _agent_input_tasks).values()
        if not task.done()
    ]
    for task in tasks:
        task.cancel()
    for task in tasks:
        with _b("contextlib", contextlib).suppress(
            _b("asyncio", asyncio).CancelledError
        ):
            await task
    _b("_agent_input_tasks", _agent_input_tasks).clear()
    _b("_agent_input_queues", _agent_input_queues).clear()
    _b("_agent_input_locks", _agent_input_locks).clear()
    _b("turn_admission", turn_admission).reset()


async def _cancel_agent_input_confirmation_tasks() -> None:
    """Stop confirmation probes while preserving their durable records."""
    tasks = [
        task
        for task in _b(
            "_agent_input_confirmation_tasks", _agent_input_confirmation_tasks
        ).values()
        if not task.done()
    ]
    for task in tasks:
        task.cancel()
    for task in tasks:
        with _b("contextlib", contextlib).suppress(
            _b("asyncio", asyncio).CancelledError
        ):
            await task
    _b("_agent_input_confirmation_tasks", _agent_input_confirmation_tasks).clear()
    _b("_agent_input_confirmation_targets", _agent_input_confirmation_targets).clear()
    _b("_agent_input_confirmation_modes", _agent_input_confirmation_modes).clear()


async def _discard_queued_agent_input(
    user_id: int,
    thread_id: int | None,
    window_id: str,
) -> int:
    """Drop pending user prompts for a target before an explicit interrupt."""
    key = _b("_agent_input_key", _agent_input_key)(user_id, thread_id, window_id)
    async with _b("_agent_input_lock", _agent_input_lock)(key):
        await _b(
            "_cancel_agent_input_confirmations_for_target",
            _cancel_agent_input_confirmations_for_target,
        )(key)
        route_keys = {
            candidate
            for candidate in set(_b("_agent_input_queues", _agent_input_queues))
            | set(_b("_agent_input_tasks", _agent_input_tasks))
            if _b("_same_agent_input_route", _same_agent_input_route)(candidate, key)
        }
        route_keys.add(key)
        dropped = 0
        for route_key in route_keys:
            task = _b("_agent_input_tasks", _agent_input_tasks).pop(route_key, None)
            if (
                task
                and not task.done()
                and task is not _b("asyncio", asyncio).current_task()
            ):
                task.cancel()
                with _b("contextlib", contextlib).suppress(
                    _b("asyncio", asyncio).CancelledError
                ):
                    await task
            queue = _b("_agent_input_queues", _agent_input_queues).pop(route_key, None)
            dropped += len(queue) if queue else 0
            if queue:
                _b("_delete_persisted_agent_inputs", _delete_persisted_agent_inputs)(
                    list(queue)
                )
            _b("turn_admission", turn_admission).set_pending(
                route_key[2], pending=False
            )
        _b("_clear_persisted_agent_input_route", _clear_persisted_agent_input_route)(
            key
        )
        try:
            dropped += _b(
                "_runtime_store", _runtime_store
            ).delete_pending_input_choices_for_route(
                key[0],
                key[1],
            )
        except Exception:
            _b("logger", logger).exception(
                "Failed to clear pending input choices (user=%d thread=%d)",
                key[0],
                key[1],
            )
        _b("_reset_transient_agent_retry", _reset_transient_agent_retry)(window_id)

    return dropped
