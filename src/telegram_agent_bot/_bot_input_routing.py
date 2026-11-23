"""Extracted bot runtime helpers (ADR 0001 Phase D)."""

from __future__ import annotations

from telegram_agent_bot._bot_shared import (
    AGENT_INPUT_MODE_QUEUE,
    AGENT_INPUT_MODE_STEER,
    AGENT_INPUT_MODE_TURN,
    AGENT_INPUT_QUEUED,
    AGENT_INPUT_SUBMITTED_UNCONFIRMED,
    AgentInputMode,
    Any,
    Bot,
    CB_KEYS_PREFIX,
    CB_SCREENSHOT_REFRESH,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    PendingAgentInput,
    _AGENT_INPUT_CONFIRM_MAX_WAIT_SECONDS,
    _AGENT_INPUT_CONFIRM_RETRY_SECONDS,
    _INPUT_ROUTE_CHOICE_MAX_AGE_SECONDS,
    _QueuedAgentInput,
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
    build_input_routing_keyboard,
    cast,
    config,
    contextlib,
    deque,
    is_codex_input_ready,
    is_interactive_ui,
    logger,
    normalize_agent_type,
    safe_reply,
    send_agent_control,
    send_agent_input,
    send_agent_message,
    session_manager,
    time,
    turn_admission,
)


def _call__drain_agent_input_queue():
    from telegram_agent_bot._bot_queue_drain import _drain_agent_input_queue as _fn

    return _b("_drain_agent_input_queue", _fn)


def _call__notify_agent_input_confirmation_delayed():
    from telegram_agent_bot._bot_queue_drain import (
        _notify_agent_input_confirmation_delayed as _fn,
    )

    return _b("_notify_agent_input_confirmation_delayed", _fn)


def _call__notify_agent_input_confirmation_expired():
    from telegram_agent_bot._bot_queue_drain import (
        _notify_agent_input_confirmation_expired as _fn,
    )

    return _b("_notify_agent_input_confirmation_expired", _fn)


def _call__notify_queued_input_failure():
    from telegram_agent_bot._bot_queue_drain import _notify_queued_input_failure as _fn

    return _b("_notify_queued_input_failure", _fn)


def _call__notify_rebound_agent_input_unconfirmed():
    from telegram_agent_bot._bot_queue_drain import (
        _notify_rebound_agent_input_unconfirmed as _fn,
    )

    return _b("_notify_rebound_agent_input_unconfirmed", _fn)


def _call__refresh_session_map_after_first_prompt():
    from telegram_agent_bot._bot_window_startup import (
        _refresh_session_map_after_first_prompt as _fn,
    )

    return _b("_refresh_session_map_after_first_prompt", _fn)


def _build_screenshot_keyboard(window_id: str) -> InlineKeyboardMarkup:
    """Build inline keyboard for screenshot: control keys + refresh."""

    def btn(label: str, key_id: str) -> InlineKeyboardButton:
        return InlineKeyboardButton(
            label,
            callback_data=f"{_b('CB_KEYS_PREFIX', CB_KEYS_PREFIX)}{key_id}:{window_id}"[
                :64
            ],
        )

    return InlineKeyboardMarkup(
        [
            [btn("␣ Space", "spc"), btn("↑", "up"), btn("⇥ Tab", "tab")],
            [btn("←", "lt"), btn("↓", "dn"), btn("→", "rt")],
            [btn("⎋ Esc", "esc"), btn("^C", "cc"), btn("⏎ Enter", "ent")],
            [
                InlineKeyboardButton(
                    "🔄 Refresh",
                    callback_data=f"{_b('CB_SCREENSHOT_REFRESH', CB_SCREENSHOT_REFRESH)}{window_id}"[
                        :64
                    ],
                )
            ],
        ]
    )


async def _send_control_to_agent(
    user_id: int,
    thread_id: int | None,
    window_id: str,
    key: str,
) -> tuple[bool, str]:
    result = await _b("send_agent_control", send_agent_control)(
        user_id, thread_id, window_id, key
    )
    if result is None:
        return False, "No session bound"
    if result.missing:
        return False, "Window not found"
    if not result.ok:
        return False, result.message or f"Failed to send {key}"
    return True, ""


async def _send_message_to_agent(
    user_id: int,
    thread_id: int | None,
    window_id: str,
    text: str,
) -> tuple[bool, str]:
    result = await _b("send_agent_message", send_agent_message)(
        user_id, thread_id, window_id, text
    )
    if result is None:
        return False, "No session bound"
    if result.missing:
        return False, "Window not found (may have been closed)"
    return result.ok, result.message


async def _send_native_input_to_agent(
    user_id: int,
    thread_id: int | None,
    window_id: str,
    text: str,
    *,
    mode: str,
) -> tuple[bool, str]:
    if mode not in {
        _b("AGENT_INPUT_MODE_STEER", AGENT_INPUT_MODE_STEER),
        _b("AGENT_INPUT_MODE_QUEUE", AGENT_INPUT_MODE_QUEUE),
    }:
        return False, f"Unsupported native input mode: {mode}"
    result = await _b("send_agent_input", send_agent_input)(
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


def _window_agent_type(window_id: str) -> str:
    """Return the persisted agent type for a local window."""
    state = _b("session_manager", session_manager).window_states.get(window_id)
    return _b("normalize_agent_type", normalize_agent_type)(
        state.agent_type
        if state and state.agent_type
        else _b("config", config).agent_type
    )


def _window_supports_native_input_routing(window_id: str) -> bool:
    """Return whether this window supports an Enter-based active-turn steer."""
    return _b("agent_capabilities", agent_capabilities)(
        _b("_window_agent_type", _window_agent_type)(window_id)
    ).supports_native_steer


def _route_can_use_native_input(key: tuple[int, int, str]) -> bool:
    """Avoid reordering native input around the legacy bot-side FIFO."""
    return not (
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


async def _offer_input_route_choice(
    message: Any,
    *,
    user_id: int,
    thread_id: int,
    window_id: str,
    text: str,
) -> bool:
    """Persist a busy-turn message and attach its native routing controls."""
    agent_type = _b("_window_agent_type", _window_agent_type)(window_id)
    display_name = _b("agent_display_name", agent_display_name)(agent_type)
    try:
        _b("_runtime_store", _runtime_store).delete_expired_input_choices(
            _b("time", time).time()
            - _b(
                "_INPUT_ROUTE_CHOICE_MAX_AGE_SECONDS",
                _INPUT_ROUTE_CHOICE_MAX_AGE_SECONDS,
            )
        )
        record = _b("_runtime_store", _runtime_store).create_pending_input_choice(
            user_id=user_id,
            thread_id=thread_id,
            window_id=window_id,
            text=text,
            max_pending=_b(
                "_agent_input_queue_max_size", _agent_input_queue_max_size
            )(),
        )
    except Exception:
        _b("logger", logger).exception(
            "Failed to persist input route choice (user=%d thread=%d window=%s)",
            user_id,
            thread_id,
            window_id,
        )
        record = None
    if record is None:
        await _b("safe_reply", safe_reply)(
            message,
            "❌ 无法安全保存这条输入，或待选择消息已满；请稍后重试。",
        )
        return False
    await _b("safe_reply", safe_reply)(
        message,
        f"{display_name} 正在工作。请选择这条消息的处理方式：",
        reply_markup=_b("build_input_routing_keyboard", build_input_routing_keyboard)(
            record.id,
            native_queue=_b("agent_capabilities", agent_capabilities)(
                agent_type
            ).supports_native_queue,
        ),
    )
    return True


def _agent_input_key(
    user_id: int,
    thread_id: int | None,
    window_id: str,
) -> tuple[int, int, str]:
    owner_id = _b("session_manager", session_manager).conversation_owner_id(
        user_id, thread_id
    )
    return (owner_id, thread_id or 0, window_id)


def _should_offer_input_route_choice(
    *,
    user_id: int,
    thread_id: int,
    window_id: str,
    pane_text: str | None,
) -> bool:
    """Return whether a busy local agent can route this input natively."""
    current_output = pane_text or ""
    return (
        not _b("is_codex_input_ready", is_codex_input_ready)(current_output)
        and bool(current_output.strip())
        and not _b("is_interactive_ui", is_interactive_ui)(current_output)
        and _b(
            "_window_supports_native_input_routing",
            _window_supports_native_input_routing,
        )(window_id)
        and _b("_route_can_use_native_input", _route_can_use_native_input)(
            _b("_agent_input_key", _agent_input_key)(user_id, thread_id, window_id)
        )
    )


def _agent_input_lock(key: tuple[int, int, str]) -> asyncio.Lock:
    route = key[:2]
    lock = _b("_agent_input_locks", _agent_input_locks).get(route)
    if lock is None:
        lock = _b("asyncio", asyncio).Lock()
        _b("_agent_input_locks", _agent_input_locks)[route] = lock
    return lock


def _reset_transient_agent_retry(window_id: str) -> None:
    """Cancel a pending automatic retry when a real user sends new input."""
    task = _b("_transient_agent_retry_tasks", _transient_agent_retry_tasks).pop(
        window_id, None
    )
    if task and task is not _b("asyncio", asyncio).current_task() and not task.done():
        task.cancel()
    _b("_transient_agent_retry_attempts", _transient_agent_retry_attempts).pop(
        window_id, None
    )
    try:
        _b("_runtime_store", _runtime_store).delete_transient_agent_retry(window_id)
    except Exception:
        _b("logger", logger).exception(
            "Failed to clear persisted transient retry for window %s", window_id
        )


def _agent_input_queue_max_size() -> int:
    return max(1, int(getattr(_b("config", config), "agent_input_queue_max_size", 20)))


def _agent_input_queue_max_wait_seconds() -> float:
    return max(
        0.0,
        float(getattr(_b("config", config), "agent_input_queue_max_wait_seconds", 0.0)),
    )


def _agent_max_active_turns() -> int:
    return max(0, int(getattr(_b("config", config), "agent_max_active_turns", 2)))


def _queue_agent_input(
    key: tuple[int, int, str],
    text: str,
    *,
    submission_mode: str = _b("AGENT_INPUT_MODE_TURN", AGENT_INPUT_MODE_TURN),
) -> tuple[bool, int, int]:
    queue = _b("_agent_input_queues", _agent_input_queues).setdefault(
        key, _b("deque", deque)()
    )
    limit = _b("_agent_input_queue_max_size", _agent_input_queue_max_size)()
    if len(queue) >= limit:
        return False, len(queue), limit
    try:
        record = _b("_runtime_store", _runtime_store).enqueue_agent_input(
            user_id=key[0],
            thread_id=key[1],
            window_id=key[2],
            text=text,
            max_pending=limit,
            submission_mode=submission_mode,
        )
    except Exception:
        _b("logger", logger).exception(
            "Failed to persist queued agent input (user=%d thread=%d window=%s)",
            key[0],
            key[1],
            key[2],
        )
        return False, len(queue), limit
    if record is None:
        return False, len(queue), limit
    queue.append(
        _QueuedAgentInput(
            text=text,
            created_at_epoch=record.created_at_epoch,
            store_id=record.id,
            submission_mode=record.submission_mode,
        )
    )
    _b("turn_admission", turn_admission).set_pending(key[2], pending=True)
    return True, len(queue), limit


def _delete_persisted_agent_inputs(items: list[_QueuedAgentInput]) -> None:
    record_ids = [item.store_id for item in items if item.store_id is not None]
    if not record_ids:
        return
    try:
        _b("_runtime_store", _runtime_store).delete_agent_inputs(record_ids)
    except Exception:
        _b("logger", logger).exception(
            "Failed to remove %d persisted agent input(s)", len(record_ids)
        )


async def _mark_persisted_agent_input_submitted(
    item: _QueuedAgentInput,
    window_id: str,
) -> bool:
    """Durably enter the no-auto-resend state before touching tmux."""
    try:
        (
            session_id,
            transcript_offset,
        ) = await _b(
            "session_manager", session_manager
        ).transcript_confirmation_baseline(window_id)
    except Exception:
        _b("logger", logger).exception(
            "Failed to capture transcript confirmation baseline (window=%s)",
            window_id,
        )
        return False
    if item.store_id is None:
        item.state = _b(
            "AGENT_INPUT_SUBMITTED_UNCONFIRMED", AGENT_INPUT_SUBMITTED_UNCONFIRMED
        )
        item.transcript_session_id = session_id
        item.transcript_offset = transcript_offset
        return True
    try:
        changed = _b("_runtime_store", _runtime_store).mark_agent_input_submitted(
            item.store_id,
            transcript_session_id=session_id,
            transcript_offset=transcript_offset,
        )
    except Exception:
        _b("logger", logger).exception(
            "Failed to persist agent input submission state (record=%d)",
            item.store_id,
        )
        return False
    if not changed:
        _b("logger", logger).error(
            "Queued agent input was not in a dispatchable state (record=%d)",
            item.store_id,
        )
        return False
    item.state = _b(
        "AGENT_INPUT_SUBMITTED_UNCONFIRMED", AGENT_INPUT_SUBMITTED_UNCONFIRMED
    )
    item.transcript_session_id = session_id
    item.transcript_offset = transcript_offset
    return True


def _confirm_persisted_agent_input(item: _QueuedAgentInput) -> None:
    """Remove one lifecycle record after transcript confirmation."""
    if item.store_id is None:
        return
    try:
        _b("_runtime_store", _runtime_store).confirm_agent_input(item.store_id)
    except Exception:
        _b("logger", logger).exception(
            "Failed to confirm persisted agent input (record=%d)", item.store_id
        )


def _clear_persisted_agent_input_target(key: tuple[int, int, str]) -> None:
    try:
        _b("_runtime_store", _runtime_store).delete_agent_inputs_for_target(*key)
    except Exception:
        _b("logger", logger).exception(
            "Failed to clear persisted agent input target "
            "(user=%d thread=%d window=%s)",
            key[0],
            key[1],
            key[2],
        )


def _clear_persisted_agent_input_route(key: tuple[int, int, str]) -> None:
    try:
        _b("_runtime_store", _runtime_store).delete_agent_inputs_for_route(
            key[0], key[1]
        )
    except Exception:
        _b("logger", logger).exception(
            "Failed to clear persisted agent input route (user=%d thread=%d)",
            key[0],
            key[1],
        )


def _same_agent_input_route(
    left: tuple[int, int, str],
    right: tuple[int, int, str],
) -> bool:
    return left[:2] == right[:2]


def _is_current_bound_agent_input_target(key: tuple[int, int, str]) -> bool:
    """Return whether the topic authoritatively points at this exact window."""
    return (
        _b("session_manager", session_manager).resolve_window_for_thread(
            key[0], key[1] or None
        )
        == key[2]
    )


def _target_has_agent_input_confirmation(key: tuple[int, int, str]) -> bool:
    exact_target_only = _b(
        "_is_current_bound_agent_input_target", _is_current_bound_agent_input_target
    )(key)
    return any(
        _b("_same_agent_input_route", _same_agent_input_route)(target, key)
        and (target == key or not exact_target_only)
        and _b("_agent_input_confirmation_modes", _agent_input_confirmation_modes).get(
            record_id
        )
        != _b("AGENT_INPUT_MODE_QUEUE", AGENT_INPUT_MODE_QUEUE)
        and not task.done()
        for record_id, task in _b(
            "_agent_input_confirmation_tasks", _agent_input_confirmation_tasks
        ).items()
        if (
            target := _b(
                "_agent_input_confirmation_targets", _agent_input_confirmation_targets
            ).get(record_id)
        )
        is not None
    )


def _route_has_agent_input_queue(key: tuple[int, int, str]) -> bool:
    return any(
        queue
        and _b("_same_agent_input_route", _same_agent_input_route)(queued_key, key)
        for queued_key, queue in _b("_agent_input_queues", _agent_input_queues).items()
    )


def _route_has_agent_input_drain(key: tuple[int, int, str]) -> bool:
    return any(
        _b("_same_agent_input_route", _same_agent_input_route)(task_key, key)
        and not task.done()
        for task_key, task in _b("_agent_input_tasks", _agent_input_tasks).items()
    )


def _target_has_persisted_agent_input_confirmation(
    key: tuple[int, int, str],
) -> bool:
    try:
        return _b("_runtime_store", _runtime_store).has_unconfirmed_agent_input(
            key[0],
            key[1],
            window_id=(
                key[2]
                if _b(
                    "_is_current_bound_agent_input_target",
                    _is_current_bound_agent_input_target,
                )(key)
                else None
            ),
        )
    except Exception:
        _b("logger", logger).exception(
            "Failed to inspect persisted agent input confirmations (user=%d thread=%d)",
            key[0],
            key[1],
        )
        return True


def _agent_input_confirmation_expired(record: PendingAgentInput) -> bool:
    """Return whether an unconfirmed submission may no longer block this topic."""
    if record.submitted_at_epoch is None:
        return False
    return _b("time", time).time() - record.submitted_at_epoch >= _b(
        "_AGENT_INPUT_CONFIRM_MAX_WAIT_SECONDS", _AGENT_INPUT_CONFIRM_MAX_WAIT_SECONDS
    )


async def _confirm_rebound_agent_input(
    bot: Bot,
    record_id: int,
    record: Any,
    bound_window_id: str,
) -> bool:
    """Confirm or retire a pending submission when its topic rebound."""
    rebound_confirmed = False
    sm = _b("session_manager", session_manager)
    if record.transcript_session_id and sm.window_matches_session(
        bound_window_id, record.transcript_session_id
    ):
        rebound_confirmed = await sm.wait_for_transcript_user_message(
            bound_window_id,
            record.text,
            timeout=_b("config", config).transcript_confirm_timeout_seconds,
            expected_session_id=record.transcript_session_id,
            after_offset=record.transcript_offset,
        )
    _b("_runtime_store", _runtime_store).confirm_agent_input(record_id)
    if rebound_confirmed:
        _b("logger", logger).info(
            "Confirmed rebound agent input from the continued "
            "transcript (record=%d old_window=%s new_window=%s)",
            record_id,
            record.window_id,
            bound_window_id,
        )
        return True
    _b("logger", logger).warning(
        "Retired unconfirmed agent input after topic rebound "
        "(record=%d old_window=%s new_window=%s)",
        record_id,
        record.window_id,
        bound_window_id,
    )
    await _call__notify_rebound_agent_input_unconfirmed()(
        bot, record.user_id, record.thread_id or None
    )
    return True


async def _poll_agent_input_confirmation_once(
    bot: Bot,
    record_id: int,
    key: tuple[int, int, str],
    recover_submission: bool,
    notify_on_first_delay: bool,
) -> tuple[bool, bool, bool, bool]:
    """Run one confirmation poll; return (done, confirmed, recover_submission, notify)."""
    record = _b("_runtime_store", _runtime_store).get_agent_input(record_id)
    if record is None or record.state != _b(
        "AGENT_INPUT_SUBMITTED_UNCONFIRMED", AGENT_INPUT_SUBMITTED_UNCONFIRMED
    ):
        return True, True, recover_submission, notify_on_first_delay
    if _b("_agent_input_confirmation_expired", _agent_input_confirmation_expired)(
        record
    ):
        _b("_runtime_store", _runtime_store).confirm_agent_input(record_id)
        _b("logger", logger).warning(
            "Retired unconfirmed agent input after confirmation deadline "
            "(record=%d window=%s)",
            record_id,
            record.window_id,
        )
        await _call__notify_agent_input_confirmation_expired()(
            bot, record.user_id, record.thread_id or None
        )
        return True, True, recover_submission, notify_on_first_delay

    bound_window_id = _b("session_manager", session_manager).resolve_window_for_thread(
        record.user_id, record.thread_id or None
    )
    if bound_window_id and bound_window_id != record.window_id:
        done = await _confirm_rebound_agent_input(
            bot, record_id, record, bound_window_id
        )
        return done, True, recover_submission, notify_on_first_delay

    if recover_submission:
        confirmed_now = await _call__refresh_session_map_after_first_prompt()(
            record.window_id,
            text=record.text,
            confirm_existing_session=True,
            transcript_session_id=record.transcript_session_id,
            transcript_after_offset=record.transcript_offset,
            submit_key=(
                "Tab"
                if record.submission_mode
                == _b("AGENT_INPUT_MODE_QUEUE", AGENT_INPUT_MODE_QUEUE)
                else "Enter"
            ),
        )
        recover_submission = False
    else:
        confirmed_now = await _b(
            "session_manager", session_manager
        ).wait_for_transcript_user_message(
            record.window_id,
            record.text,
            timeout=_b("config", config).transcript_confirm_timeout_seconds,
            expected_session_id=record.transcript_session_id,
            after_offset=record.transcript_offset,
        )
    if confirmed_now:
        _b("_runtime_store", _runtime_store).confirm_agent_input(record_id)
        _b("logger", logger).info(
            "Confirmed previously submitted agent input after delay "
            "(record=%d window=%s)",
            record_id,
            record.window_id,
        )
        return True, True, recover_submission, notify_on_first_delay
    if notify_on_first_delay:
        await _call__notify_agent_input_confirmation_delayed()(
            bot, key[0], key[1] or None
        )
        notify_on_first_delay = False
    return False, False, recover_submission, notify_on_first_delay


def _finish_agent_input_confirmation_cleanup(
    bot: Bot,
    record_id: int,
    key: tuple[int, int, str],
    current_task: Any,
    confirmed: bool,
) -> None:
    if (
        _b("_agent_input_confirmation_tasks", _agent_input_confirmation_tasks).get(
            record_id
        )
        is current_task
    ):
        _b("_agent_input_confirmation_tasks", _agent_input_confirmation_tasks).pop(
            record_id, None
        )
        _b("_agent_input_confirmation_targets", _agent_input_confirmation_targets).pop(
            record_id, None
        )
        _b("_agent_input_confirmation_modes", _agent_input_confirmation_modes).pop(
            record_id, None
        )
    if confirmed and not _b(
        "_target_has_agent_input_confirmation", _target_has_agent_input_confirmation
    )(key):
        for queued_key, queue in list(
            _b("_agent_input_queues", _agent_input_queues).items()
        ):
            if queue and _b("_same_agent_input_route", _same_agent_input_route)(
                queued_key, key
            ):
                _b("_ensure_agent_input_drain_task", _ensure_agent_input_drain_task)(
                    bot, queued_key
                )


async def _run_agent_input_confirmation(
    bot: Bot,
    record_id: int,
    key: tuple[int, int, str],
    *,
    recover_submission: bool,
    initial_delay_seconds: float | None = None,
    notify_on_first_delay: bool = False,
) -> None:
    """Confirm a submitted prompt without ever replaying its text."""
    current_task = _b("asyncio", asyncio).current_task()
    confirmed = False
    try:
        delay = initial_delay_seconds
        if delay is None and not recover_submission:
            delay = _b(
                "_AGENT_INPUT_CONFIRM_RETRY_SECONDS", _AGENT_INPUT_CONFIRM_RETRY_SECONDS
            )
        if delay is not None and delay > 0:
            await _b("asyncio", asyncio).sleep(delay)
        while True:
            try:
                (
                    done,
                    confirmed,
                    recover_submission,
                    notify_on_first_delay,
                ) = await _poll_agent_input_confirmation_once(
                    bot,
                    record_id,
                    key,
                    recover_submission,
                    notify_on_first_delay,
                )
                if done:
                    return
            except _b("asyncio", asyncio).CancelledError:
                raise
            except Exception:
                _b("logger", logger).exception(
                    "Deferred agent input confirmation failed "
                    "(record=%d user=%d thread=%s window=%s); retrying",
                    record_id,
                    key[0],
                    key[1] or None,
                    key[2],
                )
            await _b("asyncio", asyncio).sleep(
                _b(
                    "_AGENT_INPUT_CONFIRM_RETRY_SECONDS",
                    _AGENT_INPUT_CONFIRM_RETRY_SECONDS,
                )
            )
    except _b("asyncio", asyncio).CancelledError:
        raise
    finally:
        _finish_agent_input_confirmation_cleanup(
            bot, record_id, key, current_task, confirmed
        )


def _ensure_agent_input_confirmation_task(
    bot: Bot,
    record_id: int,
    key: tuple[int, int, str],
    *,
    recover_submission: bool,
    initial_delay_seconds: float | None = None,
    notify_on_first_delay: bool = False,
    submission_mode: str = _b("AGENT_INPUT_MODE_TURN", AGENT_INPUT_MODE_TURN),
) -> None:
    task = _b("_agent_input_confirmation_tasks", _agent_input_confirmation_tasks).get(
        record_id
    )
    if task and not task.done():
        return
    _b("_agent_input_confirmation_targets", _agent_input_confirmation_targets)[
        record_id
    ] = key
    _b("_agent_input_confirmation_modes", _agent_input_confirmation_modes)[
        record_id
    ] = submission_mode
    _b("_agent_input_confirmation_tasks", _agent_input_confirmation_tasks)[
        record_id
    ] = _b("asyncio", asyncio).create_task(
        _b("_run_agent_input_confirmation", _run_agent_input_confirmation)(
            bot,
            record_id,
            key,
            recover_submission=recover_submission,
            initial_delay_seconds=initial_delay_seconds,
            notify_on_first_delay=notify_on_first_delay,
        )
    )


async def _cancel_agent_input_confirmations_for_target(
    key: tuple[int, int, str],
) -> None:
    record_ids = [
        record_id
        for record_id, target in _b(
            "_agent_input_confirmation_targets", _agent_input_confirmation_targets
        ).items()
        if _b("_same_agent_input_route", _same_agent_input_route)(target, key)
    ]
    tasks = [
        task
        for record_id in record_ids
        if (
            task := _b(
                "_agent_input_confirmation_tasks", _agent_input_confirmation_tasks
            ).get(record_id)
        )
        is not None
        and not task.done()
    ]
    for task in tasks:
        task.cancel()
    for task in tasks:
        with _b("contextlib", contextlib).suppress(
            _b("asyncio", asyncio).CancelledError
        ):
            await task
    for record_id in record_ids:
        _b("_agent_input_confirmation_tasks", _agent_input_confirmation_tasks).pop(
            record_id, None
        )
        _b("_agent_input_confirmation_targets", _agent_input_confirmation_targets).pop(
            record_id, None
        )
        _b("_agent_input_confirmation_modes", _agent_input_confirmation_modes).pop(
            record_id, None
        )


def _restore_agent_input_queues(bot: Bot) -> int:
    """Reload prompts accepted before a process restart and restart drainers."""
    try:
        records = _b("_runtime_store", _runtime_store).list_pending_agent_inputs()
    except Exception:
        _b("logger", logger).exception("Failed to restore persisted agent input queue")
        return 0

    restored = 0
    existing_ids = {
        item.store_id
        for queue in _b("_agent_input_queues", _agent_input_queues).values()
        for item in queue
        if item.store_id is not None
    } | set(_b("_agent_input_confirmation_tasks", _agent_input_confirmation_tasks))
    for record in records:
        if record.id in existing_ids:
            continue
        window_id = record.window_id
        resolved_window_id = _b(
            "session_manager", session_manager
        ).resolve_window_for_thread(
            record.user_id,
            record.thread_id or None,
        )
        if (
            record.state == _b("AGENT_INPUT_QUEUED", AGENT_INPUT_QUEUED)
            and resolved_window_id
            and resolved_window_id != window_id
        ):
            window_id = resolved_window_id
            try:
                _b("_runtime_store", _runtime_store).retarget_agent_input(
                    record.id, window_id
                )
            except Exception:
                _b("logger", logger).exception(
                    "Failed to retarget restored agent input %d to window %s",
                    record.id,
                    window_id,
                )
                continue

        age = max(0.0, _b("time", time).time() - record.created_at_epoch)
        created_at = _b("time", time).monotonic() - age
        key = _b("_agent_input_key", _agent_input_key)(
            record.user_id,
            record.thread_id or None,
            window_id,
        )
        if record.state == _b(
            "AGENT_INPUT_SUBMITTED_UNCONFIRMED", AGENT_INPUT_SUBMITTED_UNCONFIRMED
        ):
            confirmation_kwargs: dict[str, Any] = {"recover_submission": True}
            if record.submission_mode != _b(
                "AGENT_INPUT_MODE_TURN", AGENT_INPUT_MODE_TURN
            ):
                confirmation_kwargs["submission_mode"] = record.submission_mode
            _b(
                "_ensure_agent_input_confirmation_task",
                _ensure_agent_input_confirmation_task,
            )(
                bot,
                record.id,
                key,
                **confirmation_kwargs,
            )
            restored += 1
            continue
        _b("_agent_input_queues", _agent_input_queues).setdefault(
            key, _b("deque", deque)()
        ).append(
            _QueuedAgentInput(
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
        _b("turn_admission", turn_admission).set_pending(window_id, pending=True)
        restored += 1

    for key in list(_b("_agent_input_queues", _agent_input_queues)):
        if not _b(
            "_target_has_agent_input_confirmation", _target_has_agent_input_confirmation
        )(key):
            _b("_ensure_agent_input_drain_task", _ensure_agent_input_drain_task)(
                bot, key
            )
    if restored:
        _b("logger", logger).info(
            "Restored %d agent input lifecycle record(s)", restored
        )
    return restored


async def _drop_expired_agent_input(
    bot: Bot,
    key: tuple[int, int, str],
    queue: deque[_QueuedAgentInput],
) -> None:
    max_wait = _b(
        "_agent_input_queue_max_wait_seconds", _agent_input_queue_max_wait_seconds
    )()
    if max_wait <= 0:
        return

    now = _b("time", time).monotonic()
    expired = 0
    expired_items: list[_QueuedAgentInput] = []
    while queue and now - queue[0].created_at >= max_wait:
        expired_items.append(queue.popleft())
        expired += 1

    if expired:
        _b("_delete_persisted_agent_inputs", _delete_persisted_agent_inputs)(
            expired_items
        )
        user_id, thread_key, _window_id = key
        thread_id = thread_key or None
        wait_display = int(max_wait)
        await _call__notify_queued_input_failure()(
            bot,
            user_id,
            thread_id,
            f"{expired} queued input(s) expired after waiting {wait_display}s "
            "for the agent to become ready",
        )
    if not queue:
        _b("turn_admission", turn_admission).set_pending(key[2], pending=False)


def _ensure_agent_input_drain_task(
    bot: Bot,
    key: tuple[int, int, str],
) -> None:
    if _b("_target_has_agent_input_confirmation", _target_has_agent_input_confirmation)(
        key
    ) or _b(
        "_target_has_persisted_agent_input_confirmation",
        _target_has_persisted_agent_input_confirmation,
    )(key):
        return
    if _b("_route_has_agent_input_drain", _route_has_agent_input_drain)(key):
        return
    _b("_agent_input_tasks", _agent_input_tasks)[key] = _b(
        "asyncio", asyncio
    ).create_task(_call__drain_agent_input_queue()(bot, key))


async def _queue_agent_input_after_interrupt(
    bot: Bot,
    user_id: int,
    thread_id: int | None,
    window_id: str,
    text: str,
) -> tuple[bool, str]:
    """Queue replacement text after an explicit interrupt request."""
    key = _b("_agent_input_key", _agent_input_key)(user_id, thread_id, window_id)
    async with _b("_agent_input_lock", _agent_input_lock)(key):
        queued, depth, limit = _b("_queue_agent_input", _queue_agent_input)(key, text)
        if not queued:
            _b("_ensure_agent_input_drain_task", _ensure_agent_input_drain_task)(
                bot, key
            )
            return (
                False,
                "Agent is still handling the interrupt and the input queue is full "
                f"({limit} pending). Wait for it to finish or use /esc first.",
            )
        _b("_ensure_agent_input_drain_task", _ensure_agent_input_drain_task)(bot, key)
        return (
            True,
            f"Interrupt requested; queued message until the agent is ready ({depth}/{limit})",
        )
