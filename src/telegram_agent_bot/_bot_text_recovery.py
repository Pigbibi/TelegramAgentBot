"""Extracted bot runtime helpers (ADR 0001 Phase D)."""

from __future__ import annotations

from telegram import User

from telegram_agent_bot._bot_shared import (
    AGENT_CLAUDE,
    AGENT_CLAUDE_OFFICIAL,
    AGENT_CODEX,
    AGENT_CURSOR,
    AgentProfile,
    AgentTarget,
    Any,
    Bot,
    ContextTypes,
    PRODUCT_NAME,
    STATE_BROWSING_DIRECTORY,
    STATE_KEY,
    STATE_SELECTING_ROOT,
    STATE_SELECTING_SESSION,
    STATE_SELECTING_WINDOW,
    Update,
    _b,
    _bash_capture_tasks,
    _safe_send_typing_action,
    asyncio,
    capture_agent_output,
    clear_browse_state,
    clear_root_picker_state,
    clear_session_picker_state,
    clear_status_msg_info,
    clear_window_picker_state,
    config,
    copy,
    enqueue_status_update,
    extract_auth_error_message,
    forget_missing_bound_window,
    get_default_account_name,
    get_interactive_window,
    handle_interactive_ui,
    is_codex_input_ready,
    is_interactive_ui,
    is_shell_pane_command,
    logger,
    mark_window_working,
    normalize_agent_type,
    remember_current_account,
    safe_edit,
    safe_reply,
    safe_send,
    sanitize_forward_text,
    session_manager,
    tmux_manager,
)
from telegram_agent_bot._bot_helpers import (
    _clear_pending_thread_input,
    _get_thread_id,
    _start_unbound_topic_creation,
    _take_pending_thread_message,
    is_user_allowed,
)
from telegram_agent_bot._bot_input_routing import (
    _offer_input_route_choice,
    _reset_transient_agent_retry,
    _send_message_to_agent,
    _should_offer_input_route_choice,
)
from telegram_agent_bot._bot_login import (
    _codex_auth_recovery_message,
)
from telegram_agent_bot._bot_media_topics import (
    _create_agent_local_window,
    _create_agent_target,
    _format_remote_target,
    _remote_target_for_thread,
)
from telegram_agent_bot._bot_queue_drain import (
    _discard_queued_agent_input,
    _send_or_queue_agent_input,
)
from telegram_agent_bot._bot_window_startup import (
    _cancel_bash_capture,
    _capture_bash_output,
    _enable_fast_mode_if_requested,
    _rotate_thread_after_usage_limit,
    _send_or_queue_to_starting_window,
)


async def _allocate_non_colliding_recovered_window(
    *,
    selected_path: str,
    requested_window_name: str,
    resume_session_id: str,
    account_name: str | None,
    agent_type: str,
    state: Any,
    user_id: int,
    thread_id: int,
    old_window_id: str,
    persisted_window_ids: set[str],
    persisted_bindings: list[tuple[int, int, str]],
) -> tuple[bool, str, str, str, AgentTarget | None]:
    max_create_attempts = min(len(persisted_window_ids) + 1, 16)
    for create_attempt in range(1, max_create_attempts + 1):
        (
            success,
            message,
            created_wname,
            created_wid,
            created_target,
        ) = await _b("_create_agent_local_window", _create_agent_local_window)(
            cwd=selected_path,
            window_name=requested_window_name,
            resume_session_id=resume_session_id,
            account_name=account_name or "",
            agent_type=agent_type,
            model=getattr(state, "model", ""),
            reasoning_effort=getattr(state, "reasoning_effort", ""),
            fast_mode=getattr(state, "fast_mode", False),
        )
        if not success:
            return False, message, created_wname, created_wid, created_target
        conflicting_topics = [
            (bound_user_id, bound_thread_id)
            for bound_user_id, bound_thread_id, bound_window_id in persisted_bindings
            if bound_window_id == created_wid
            and (bound_user_id, bound_thread_id) != (user_id, thread_id)
        ]
        collides_with_other_state = (
            created_wid in persisted_window_ids and created_wid != old_window_id
        )
        if not collides_with_other_state and not conflicting_topics:
            return True, message, created_wname, created_wid, created_target
        await _b("tmux_manager", tmux_manager).kill_window(created_wid)
        _b("logger", logger).warning(
            "Discarded recovered window id collision: old=%s created=%s "
            "topics=%s attempt=%d/%d",
            old_window_id,
            created_wid,
            conflicting_topics,
            create_attempt,
            max_create_attempts,
        )
    return (
        False,
        "tmux could not allocate a window id that is not reserved by another "
        "saved topic. Temporary windows were stopped without changing any "
        "bindings; restart the bot and retry.",
        requested_window_name,
        "",
        None,
    )


async def _discard_failed_recovered_window(
    created_wid: str,
    old_window_id: str,
    saved_state: Any,
    resume_session_id: str,
) -> None:
    await _b("tmux_manager", tmux_manager).kill_window(created_wid)
    sm = _b("session_manager", session_manager)
    if created_wid == old_window_id:
        await sm.remove_session_map_entry(old_window_id)
        sm.window_states[old_window_id] = saved_state
        sm._set_local_target_session(old_window_id, resume_session_id)
        sm._save_state()
        return
    await sm.remove_session_map_entry(created_wid)
    sm.remove_window_state(created_wid)


def _migrate_recovered_window_offsets(
    old_window_id: str,
    created_wid: str,
    old_offsets: dict[int, int],
    old_offset_sessions: dict[int, str],
) -> None:
    sm = _b("session_manager", session_manager)
    if created_wid != old_window_id:
        for offset_user_id, offset in old_offsets.items():
            offsets = sm.user_window_offsets.setdefault(offset_user_id, {})
            offsets[created_wid] = offset
            offsets.pop(old_window_id, None)
        for offset_user_id, session_id in old_offset_sessions.items():
            sessions = sm.user_window_offset_sessions.setdefault(offset_user_id, {})
            sessions[created_wid] = session_id
            sessions.pop(old_window_id, None)
        if old_offsets or old_offset_sessions:
            sm._save_state()
        sm.remove_window_state(old_window_id)


async def _recover_missing_bound_window(
    *,
    bot: Bot,
    user_id: int,
    thread_id: int,
    old_window_id: str,
    text: str,
) -> tuple[bool, str]:
    """Transactionally recreate a missing window and forward pending text."""
    sm = _b("session_manager", session_manager)
    state = sm.window_states.get(old_window_id)
    if not state or not state.session_id or not state.cwd:
        return False, "missing window has no resumable session state"

    resume_session_id = state.session_id
    saved_state = _b("copy", copy).copy(state)
    selected_path = state.cwd
    agent_type = _b("normalize_agent_type", normalize_agent_type)(
        getattr(state, "agent_type", ""), _b("config", config).agent_type
    )
    resume_offset: int | None = None
    if agent_type == _b("AGENT_CODEX", AGENT_CODEX):
        _session_id, resume_offset = await sm.transcript_confirmation_baseline(
            old_window_id
        )
    requested_window_name = state.window_name or sm.get_display_name(old_window_id)
    account_name = state.account_name or None
    old_offsets = {
        uid: offsets[old_window_id]
        for uid, offsets in sm.user_window_offsets.items()
        if old_window_id in offsets
    }
    old_offset_sessions = {
        uid: sessions[old_window_id]
        for uid, sessions in sm.user_window_offset_sessions.items()
        if old_window_id in sessions
    }
    persisted_window_ids = set(sm.window_states)
    persisted_bindings = list(sm.iter_thread_bindings())

    if not await sm.remove_session_map_entry(old_window_id):
        return (
            False,
            "could not clear the previous session mapping; message was not sent",
        )

    (
        success,
        message,
        created_wname,
        created_wid,
        created_target,
    ) = await _allocate_non_colliding_recovered_window(
        selected_path=selected_path,
        requested_window_name=requested_window_name,
        resume_session_id=resume_session_id,
        account_name=account_name,
        agent_type=agent_type,
        state=state,
        user_id=user_id,
        thread_id=thread_id,
        old_window_id=old_window_id,
        persisted_window_ids=persisted_window_ids,
        persisted_bindings=persisted_bindings,
    )
    if not success:
        return False, message

    started, startup_message = await _b(
        "_wait_for_recovered_agent_process", _wait_for_recovered_agent_process
    )(created_wid)
    if not started:
        await _discard_failed_recovered_window(
            created_wid, old_window_id, saved_state, resume_session_id
        )
        return False, startup_message

    sm.prepare_window_launch(
        created_wid,
        cwd=str(selected_path),
        window_name=created_wname,
        account_name=account_name or "",
        agent_type=agent_type,
        model=getattr(state, "model", ""),
        reasoning_effort=getattr(state, "reasoning_effort", ""),
        fast_mode=getattr(state, "fast_mode", False),
    )
    hook_ok = await sm.wait_for_session_map_entry(created_wid, timeout=15.0)
    healthy, health_message = await _b(
        "_recovered_agent_process_status", _recovered_agent_process_status
    )(created_wid)
    if not healthy:
        await _discard_failed_recovered_window(
            created_wid, old_window_id, saved_state, resume_session_id
        )
        return False, health_message

    if not hook_ok:
        if agent_type == _b(
            "AGENT_CODEX", AGENT_CODEX
        ) and not await sm.wait_for_transcript_resume_ready(
            resume_session_id,
            str(selected_path),
            account_name=account_name or "",
            after_offset=resume_offset,
        ):
            await _discard_failed_recovered_window(
                created_wid, old_window_id, saved_state, resume_session_id
            )
            return (
                False,
                "the resumed Codex session did not finish restoring. Your message "
                "was not sent; try again or create a new session.",
            )
        _b("logger", logger).info(
            "Recovered missing window %s as %s without a hook entry; "
            "falling back to resumed session_id=%s",
            old_window_id,
            created_wid,
            resume_session_id,
        )
        sm.register_session_to_window(
            created_wid,
            resume_session_id,
            str(selected_path),
            window_name=created_wname,
            persist_session_map=True,
        )
    ws = sm.get_window_state(created_wid)
    ws.account_name = account_name or ""
    sm._save_state()

    sm.unhide_session(resume_session_id)
    if created_target:
        sm.bind_thread_target(
            user_id, thread_id, created_target, window_name=created_wname
        )
    else:
        sm.bind_thread(user_id, thread_id, created_wid, window_name=created_wname)

    _migrate_recovered_window_offsets(
        old_window_id, created_wid, old_offsets, old_offset_sessions
    )
    _b("forget_missing_bound_window", forget_missing_bound_window)(
        user_id, thread_id, old_window_id
    )

    send_ok, send_msg, queued = await _b(
        "_send_or_queue_agent_input", _send_or_queue_agent_input
    )(bot, user_id, thread_id, created_wid, text)
    if send_ok:
        if not queued:
            await _b("mark_window_working", mark_window_working)(
                bot, user_id, created_wid, thread_id
            )
        delivery = "queued" if queued else "forwarded"
        return True, f"Recovered window `{created_wname}` and {delivery} your message."
    return (
        False,
        f"recovered the tmux window, but forwarding failed: {send_msg}. Please send the message again.",
    )


async def _handle_missing_bound_window_input(
    *,
    update_message: Any,
    user_id: int,
    thread_id: int,
    window_id: str,
    text: str,
    success_reply: str | None = None,
) -> None:
    """Recover a missing bound window and forward the triggering input."""
    _b("_reset_transient_agent_retry", _reset_transient_agent_retry)(window_id)
    display = _b("session_manager", session_manager).get_display_name(window_id)
    state = _b("session_manager", session_manager).window_states.get(window_id)
    if state and state.session_id and state.cwd:
        _b("logger", logger).info(
            "Recovering missing bound window %s (user=%d, thread=%d)",
            display,
            user_id,
            thread_id,
        )
        await _b("safe_reply", safe_reply)(
            update_message,
            (
                f"💤 Session `{display}` was paused while idle. Waking it and "
                "resuming your previous context..."
                if state.hibernated_at
                else f"♻️ Window `{display}` disappeared. Recreating it and "
                "resuming the previous session..."
            ),
        )
        recovered, recovery_message = await _b(
            "_recover_missing_bound_window", _recover_missing_bound_window
        )(
            bot=update_message.get_bot(),
            user_id=user_id,
            thread_id=thread_id,
            old_window_id=window_id,
            text=text,
        )
        if not recovered:
            await _b("safe_reply", safe_reply)(update_message, f"❌ {recovery_message}")
        elif success_reply:
            await _b("safe_reply", safe_reply)(update_message, success_reply)
        return

    _b("logger", logger).info(
        "Stale binding: window %s gone, unbinding (user=%d, thread=%d)",
        display,
        user_id,
        thread_id,
    )
    await _b("_discard_queued_agent_input", _discard_queued_agent_input)(
        user_id, thread_id, window_id
    )
    _b("session_manager", session_manager).unbind_thread(user_id, thread_id)
    await _b("safe_reply", safe_reply)(
        update_message,
        f"❌ Window '{display}' no longer exists. Binding removed.\n"
        "Send a message to start a new session.",
    )


async def _recovered_agent_process_status(window_id: str) -> tuple[bool, str]:
    """Return whether a recovery window still owns a live agent process."""
    window = await _b("tmux_manager", tmux_manager).find_window_by_id(window_id)
    if not window:
        return False, "the recovery window exited before it became ready"

    pane_command = (window.pane_current_command or "").strip()
    if not _b("is_shell_pane_command", is_shell_pane_command)(pane_command):
        return True, ""

    pane_text = await _b("tmux_manager", tmux_manager).capture_pane(window_id) or ""
    if "already has an active writer" in pane_text.lower():
        return (
            False,
            "the previous session is still active in another Codex process. "
            "Recovery was stopped without changing this topic's saved binding; "
            "restore the original tmux socket or stop the original writer before "
            "trying again.",
        )
    return (
        False,
        f"Codex exited during recovery (current command: {pane_command or 'unknown'}). "
        "The saved topic binding was left unchanged.",
    )


async def _wait_for_recovered_agent_process(
    window_id: str,
    *,
    timeout: float = 10.0,
    interval: float = 0.5,
) -> tuple[bool, str]:
    """Wait for a recovery command to leave the shell, detecting fast failures."""
    deadline = _b("asyncio", asyncio).get_running_loop().time() + timeout
    last_message = "Codex did not start before the recovery timeout"
    while _b("asyncio", asyncio).get_running_loop().time() < deadline:
        healthy, message = await _b(
            "_recovered_agent_process_status", _recovered_agent_process_status
        )(window_id)
        if healthy:
            return True, ""
        last_message = message
        if "active in another Codex process" in message:
            return False, message
        await _b("asyncio", asyncio).sleep(interval)
    return False, last_message


async def _handle_non_codex_bound_window(
    *,
    update_message: Any,
    user_id: int,
    thread_id: int,
    window_id: str,
    pane_command: str,
    text: str,
    success_reply: str | None = None,
) -> bool:
    """Recover or unbind a topic whose tmux window has fallen back to a shell."""
    if not _b("is_shell_pane_command", is_shell_pane_command)(pane_command):
        return False

    _b("_reset_transient_agent_retry", _reset_transient_agent_retry)(window_id)
    display = _b("session_manager", session_manager).get_display_name(window_id)
    state = _b("session_manager", session_manager).window_states.get(window_id)
    if state and state.session_id and state.cwd:
        _b("logger", logger).info(
            "Recovering non-Codex bound window %s (command=%s, user=%d, thread=%d)",
            display,
            pane_command,
            user_id,
            thread_id,
        )
        await _b("safe_reply", safe_reply)(
            update_message,
            f"♻️ Window `{display}` is no longer running {_b('PRODUCT_NAME', PRODUCT_NAME)} "
            f"(current command: `{pane_command}`). Recreating it and resuming "
            "the previous session...",
        )
        await _b("tmux_manager", tmux_manager).kill_window(window_id)
        recovered, recovery_message = await _b(
            "_recover_missing_bound_window", _recover_missing_bound_window
        )(
            bot=update_message.get_bot(),
            user_id=user_id,
            thread_id=thread_id,
            old_window_id=window_id,
            text=text,
        )
        if not recovered:
            await _b("safe_reply", safe_reply)(update_message, f"❌ {recovery_message}")
        elif success_reply:
            await _b("safe_reply", safe_reply)(update_message, success_reply)
        return True

    _b("logger", logger).info(
        "Stale non-Codex binding: window %s command=%s, unbinding (user=%d, thread=%d)",
        display,
        pane_command,
        user_id,
        thread_id,
    )
    await _b("_discard_queued_agent_input", _discard_queued_agent_input)(
        user_id, thread_id, window_id
    )
    _b("session_manager", session_manager).unbind_thread(user_id, thread_id)
    await _b("safe_reply", safe_reply)(
        update_message,
        f"❌ Window '{display}' is not running {_b('PRODUCT_NAME', PRODUCT_NAME)} "
        f"(current command: {pane_command}). Binding removed.\n"
        "Send a message to start a new session.",
    )
    return True


async def _handle_auth_error_bound_window(
    *,
    update_message: Any,
    user_id: int,
    thread_id: int,
    window_id: str,
    pane_text: str,
    text: str,
    success_reply: str | None = None,
) -> bool:
    """Recover a bound window whose Codex TUI is blocked by expired auth."""
    auth_error = _b("extract_auth_error_message", extract_auth_error_message)(pane_text)
    if not auth_error:
        return False

    display = _b("session_manager", session_manager).get_display_name(window_id)
    state = _b("session_manager", session_manager).window_states.get(window_id)
    if state and state.session_id and state.cwd:
        _b("logger", logger).info(
            "Recovering auth-blocked Codex window %s (user=%d, thread=%d)",
            display,
            user_id,
            thread_id,
        )
        await _b("safe_reply", safe_reply)(
            update_message,
            f"♻️ Window `{display}` is blocked by an expired Codex login. "
            "Recreating it with the current login and resuming the previous session...",
        )
        await _b("tmux_manager", tmux_manager).kill_window(window_id)
        recovered, recovery_message = await _b(
            "_recover_missing_bound_window", _recover_missing_bound_window
        )(
            bot=update_message.get_bot(),
            user_id=user_id,
            thread_id=thread_id,
            old_window_id=window_id,
            text=text,
        )
        if not recovered:
            await _b("safe_reply", safe_reply)(update_message, f"❌ {recovery_message}")
        elif success_reply:
            await _b("safe_reply", safe_reply)(update_message, success_reply)
        return True

    await _b("safe_reply", safe_reply)(
        update_message,
        f"⚠️ {_b('_codex_auth_recovery_message', _codex_auth_recovery_message)(auth_error)}",
    )
    return True


async def _handle_pending_picker_state_for_text(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    thread_id: int | None,
) -> bool:
    """Ignore or clear stale picker state across threads before handling text."""
    assert update.message is not None
    if not context.user_data:
        return False
    picker_specs = (
        (
            _b("STATE_SELECTING_WINDOW", STATE_SELECTING_WINDOW),
            "Please use the window picker above, or tap Cancel.",
            lambda ud: _b("clear_window_picker_state", clear_window_picker_state)(ud),
            False,
        ),
        (
            _b("STATE_SELECTING_ROOT", STATE_SELECTING_ROOT),
            "Please choose a computer/VPS above, or tap Cancel.",
            lambda ud: _b("clear_root_picker_state", clear_root_picker_state)(ud),
            False,
        ),
        (
            _b("STATE_BROWSING_DIRECTORY", STATE_BROWSING_DIRECTORY),
            "Please use the directory browser above, or tap Cancel.",
            lambda ud: _b("clear_browse_state", clear_browse_state)(ud),
            False,
        ),
        (
            _b("STATE_SELECTING_SESSION", STATE_SELECTING_SESSION),
            "Please use the session picker above, or tap Cancel.",
            lambda ud: _b("clear_session_picker_state", clear_session_picker_state)(ud),
            True,
        ),
    )
    current_state = context.user_data.get(_b("STATE_KEY", STATE_KEY))
    for state_val, prompt_msg, clearer, pop_path in picker_specs:
        if current_state != state_val:
            continue
        if context.user_data.get("_pending_thread_id") == thread_id:
            await _b("safe_reply", safe_reply)(update.message, prompt_msg)
            return True
        clearer(context.user_data)
        _b("_clear_pending_thread_input", _clear_pending_thread_input)(
            context.user_data
        )
        if pop_path:
            context.user_data.pop("_selected_path", None)
    return False


async def _deliver_text_to_bound_window(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    user: User,
    thread_id: int,
    wid: str,
    w: Any,
    text: str,
) -> None:
    assert update.message is not None
    pane_cmd = (getattr(w, "pane_current_command", "") or "").strip()
    if await _b("_handle_non_codex_bound_window", _handle_non_codex_bound_window)(
        update_message=update.message,
        user_id=user.id,
        thread_id=thread_id,
        window_id=wid,
        pane_command=pane_cmd,
        text=text,
    ):
        return
    await _b("_safe_send_typing_action", _safe_send_typing_action)(
        update.message.chat, source="text_handler"
    )
    await _b("enqueue_status_update", enqueue_status_update)(
        context.bot, user.id, wid, None, thread_id=thread_id
    )
    _b("_cancel_bash_capture", _cancel_bash_capture)(user.id, thread_id)
    capture = await _b("capture_agent_output", capture_agent_output)(
        user.id, thread_id, wid
    )
    pane_text = capture.text if capture and not capture.missing else None
    if pane_text and await _b(
        "_handle_auth_error_bound_window", _handle_auth_error_bound_window
    )(
        update_message=update.message,
        user_id=user.id,
        thread_id=thread_id,
        window_id=wid,
        pane_text=pane_text,
        text=text,
    ):
        return
    input_was_ready = _b("is_codex_input_ready", is_codex_input_ready)(pane_text or "")
    if pane_text and _b("is_interactive_ui", is_interactive_ui)(pane_text):
        _b("logger", logger).info(
            "Detected pending interactive UI before sending text (user=%d, thread=%s)",
            user.id,
            thread_id,
        )
        await _b("handle_interactive_ui", handle_interactive_ui)(
            context.bot, user.id, wid, thread_id
        )
        await _b("asyncio", asyncio).sleep(0.3)
    if await _b("session_manager", session_manager).window_has_usage_limit_exceeded(
        wid
    ):
        if await _b(
            "_rotate_thread_after_usage_limit", _rotate_thread_after_usage_limit
        )(
            context=context,
            user_id=user.id,
            thread_id=thread_id,
            current_window_id=wid,
            current_window_cwd=w.cwd,
            text=text,
        ):
            return
    if _b("_should_offer_input_route_choice", _should_offer_input_route_choice)(
        user_id=user.id, thread_id=thread_id, window_id=wid, pane_text=pane_text
    ):
        await _b("_offer_input_route_choice", _offer_input_route_choice)(
            update.message,
            user_id=user.id,
            thread_id=thread_id,
            window_id=wid,
            text=text,
        )
        return
    success, message, queued = await _b(
        "_send_or_queue_agent_input", _send_or_queue_agent_input
    )(context.bot, user.id, thread_id, wid, text)
    if not success:
        await _b("safe_reply", safe_reply)(update.message, f"❌ {message}")
        return
    if queued:
        await _b("safe_reply", safe_reply)(update.message, f"⏳ {message}")
    else:
        await _b("mark_window_working", mark_window_working)(
            context.bot, user.id, wid, thread_id
        )
    if not queued and input_was_ready and text.startswith("!") and len(text) > 1:
        task = _b("asyncio", asyncio).create_task(
            _b("_capture_bash_output", _capture_bash_output)(
                context.bot, user.id, thread_id, wid, text[1:]
            )
        )
        _b("_bash_capture_tasks", _bash_capture_tasks)[(user.id, thread_id)] = task
    interactive_window = _b("get_interactive_window", get_interactive_window)(
        user.id, thread_id
    )
    if interactive_window and interactive_window == wid:
        await _b("asyncio", asyncio).sleep(0.2)
        await _b("handle_interactive_ui", handle_interactive_ui)(
            context.bot, user.id, wid, thread_id
        )


async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user or not _b("is_user_allowed", is_user_allowed)(user.id):
        if update.message:
            await _b("safe_reply", safe_reply)(
                update.message, "You are not authorized to use this bot."
            )
        return
    if not update.message or not update.message.text:
        return
    thread_id = _b("_get_thread_id", _get_thread_id)(update)
    chat = update.effective_chat
    sm = _b("session_manager", session_manager)
    if chat and chat.type in ("group", "supergroup"):
        sm.set_group_chat_id(user.id, thread_id, chat.id)
    text = _b("sanitize_forward_text", sanitize_forward_text)(update.message.text)
    if not text:
        await _b("safe_reply", safe_reply)(
            update.message,
            "❌ This message only contained wrapper metadata and no forwardable text.",
        )
        return
    if await _handle_pending_picker_state_for_text(update, context, thread_id):
        return
    if thread_id is None:
        await _b("safe_reply", safe_reply)(
            update.message,
            "❌ Please use a named topic. Create a new topic to start a session.",
        )
        return
    ambiguous_wid = sm.get_ambiguous_window_for_thread(user.id, thread_id)
    if isinstance(ambiguous_wid, str) and ambiguous_wid:
        await _b("safe_reply", safe_reply)(
            update.message,
            "❌ This topic shares an agent window with another topic. Messages are "
            "paused to prevent cross-topic delivery; use /unbind here first.",
        )
        return
    wid = sm.get_window_for_thread(user.id, thread_id)
    if wid is None:
        if _b("_remote_target_for_thread", _remote_target_for_thread)(
            user.id, thread_id
        ):
            await _b("_safe_send_typing_action", _safe_send_typing_action)(
                update.message.chat, source="text_handler"
            )
            _b("clear_status_msg_info", clear_status_msg_info)(user.id, thread_id)
            _b("_cancel_bash_capture", _cancel_bash_capture)(user.id, thread_id)
            success, message = await _b(
                "_send_message_to_agent", _send_message_to_agent
            )(user.id, thread_id, "", text)
            if not success:
                await _b("safe_reply", safe_reply)(update.message, f"❌ {message}")
            return
        await _b("_start_unbound_topic_creation", _start_unbound_topic_creation)(
            update.message, context, user, thread_id, text=text
        )
        return
    w = await _b("tmux_manager", tmux_manager).find_window_by_id(wid)
    if not w:
        await _b(
            "_handle_missing_bound_window_input", _handle_missing_bound_window_input
        )(
            update_message=update.message,
            user_id=user.id,
            thread_id=thread_id,
            window_id=wid,
            text=text,
        )
        return
    await _deliver_text_to_bound_window(update, context, user, thread_id, wid, w, text)


async def _bind_and_forward_remote_created_target(
    query: Any,
    context: ContextTypes.DEFAULT_TYPE,
    user: User,
    selected_path: str,
    pending_thread_id: int | None,
    resume_session_id: str | None,
    profile: AgentProfile,
    message: str,
    created_wname: str,
    created_target: AgentTarget,
    answer_callback: bool,
) -> None:
    created_wname = created_wname or _b("_format_remote_target", _format_remote_target)(
        created_target
    )
    _b("logger", logger).info(
        "Remote agent target created: %s/%s session=%s at %s "
        "(user=%d, thread=%s, resume=%s)",
        created_target.backend_id,
        created_target.node_id,
        created_target.session_id,
        selected_path,
        user.id,
        pending_thread_id,
        resume_session_id,
    )
    if pending_thread_id is not None:
        sm = _b("session_manager", session_manager)
        sm.bind_thread_target(
            user.id, pending_thread_id, created_target, window_name=created_wname
        )
        resolved_chat = sm.resolve_chat_id(user.id, pending_thread_id)
        status = "Resumed" if resume_session_id else "Created"
        await _b("safe_edit", safe_edit)(
            query, f"✅ {message}\n\n{status}. Send messages here."
        )
        await _b("_enable_fast_mode_if_requested", _enable_fast_mode_if_requested)(
            context.bot,
            query.from_user.id,
            pending_thread_id,
            "",
            profile,
            resolved_chat,
        )
        pending_text, pending_error = await _b(
            "_take_pending_thread_message", _take_pending_thread_message
        )(context, user_id=query.from_user.id, thread_id=pending_thread_id, remote=True)
        if context.user_data is not None:
            context.user_data.pop("_pending_thread_id", None)
        if pending_error:
            await _b("safe_send", safe_send)(
                context.bot,
                resolved_chat,
                f"❌ Failed to send pending attachment: {pending_error}",
                message_thread_id=pending_thread_id,
            )
        elif pending_text:
            send_ok, send_msg = await _b(
                "_send_message_to_agent", _send_message_to_agent
            )(query.from_user.id, pending_thread_id, "", pending_text)
            if not send_ok:
                _b("logger", logger).warning(
                    "Failed to forward pending text to remote target: %s", send_msg
                )
                await _b("safe_send", safe_send)(
                    context.bot,
                    resolved_chat,
                    f"❌ Failed to send pending message: {send_msg}",
                    message_thread_id=pending_thread_id,
                )
    else:
        await _b("safe_edit", safe_edit)(query, f"✅ {message}")
    if answer_callback:
        try:
            await query.answer("Created")
        except Exception:
            _b("logger", logger).debug("Callback query answer skipped: query expired")


async def _bind_and_forward_local_created_window(
    query: Any,
    context: ContextTypes.DEFAULT_TYPE,
    user: User,
    selected_path: str,
    pending_thread_id: int | None,
    resume_session_id: str | None,
    launch_account: str | None,
    profile: AgentProfile,
    message: str,
    created_wname: str,
    created_wid: str,
    created_target: AgentTarget | None,
) -> None:
    sm = _b("session_manager", session_manager)
    sm.prepare_window_launch(
        created_wid,
        cwd=str(selected_path),
        window_name=created_wname,
        account_name=launch_account or "",
        agent_type=profile.agent_type,
        model=profile.model,
        reasoning_effort=profile.reasoning_effort,
        fast_mode=profile.fast_mode,
    )
    _b("logger", logger).info(
        "Window created: %s (id=%s) at %s (user=%d, thread=%s, resume=%s, account=%s)",
        created_wname,
        created_wid,
        selected_path,
        user.id,
        pending_thread_id,
        resume_session_id,
        launch_account,
    )
    if resume_session_id and not await sm.wait_for_session_map_entry(
        created_wid, timeout=15.0
    ):
        _b("logger", logger).warning(
            "Hook timed out for resume window %s, falling back to session_id=%s cwd=%s",
            created_wid,
            resume_session_id,
            selected_path,
        )
        sm.register_session_to_window(
            created_wid,
            resume_session_id,
            str(selected_path),
            window_name=created_wname,
            persist_session_map=True,
        )
    if pending_thread_id is None:
        await _b("safe_edit", safe_edit)(query, f"✅ {message}")
        return
    if created_target:
        sm.bind_thread_target(
            user.id, pending_thread_id, created_target, window_name=created_wname
        )
    else:
        sm.bind_thread(
            user.id, pending_thread_id, created_wid, window_name=created_wname
        )
    resolved_chat = sm.resolve_chat_id(user.id, pending_thread_id)
    status = "Resumed" if resume_session_id else "Created"
    await _b("safe_edit", safe_edit)(
        query, f"✅ {message}\n\n{status}. Send messages here."
    )
    await _b("_enable_fast_mode_if_requested", _enable_fast_mode_if_requested)(
        context.bot,
        query.from_user.id,
        pending_thread_id,
        created_wid,
        profile,
        resolved_chat,
    )
    pending_text, pending_error = await _b(
        "_take_pending_thread_message", _take_pending_thread_message
    )(context, user_id=query.from_user.id, thread_id=pending_thread_id, remote=False)
    if context.user_data is not None:
        context.user_data.pop("_pending_thread_id", None)
    if pending_error:
        await _b("safe_send", safe_send)(
            context.bot,
            resolved_chat,
            f"❌ Failed to send pending attachment: {pending_error}",
            message_thread_id=pending_thread_id,
        )
    elif pending_text:
        _b("logger", logger).debug(
            "Forwarding pending topic input to window %s (len=%d)",
            created_wname,
            len(pending_text),
        )
        send_ok, send_msg, queued = await _b(
            "_send_or_queue_to_starting_window", _send_or_queue_to_starting_window
        )(
            context.bot,
            query.from_user.id,
            pending_thread_id,
            created_wid,
            pending_text,
            auto_confirm_startup_trust=True,
        )
        if send_ok and not queued:
            await _b("mark_window_working", mark_window_working)(
                context.bot, query.from_user.id, created_wid, pending_thread_id
            )
        elif queued:
            await _b("safe_send", safe_send)(
                context.bot,
                resolved_chat,
                f"⏳ {send_msg}",
                message_thread_id=pending_thread_id,
            )
        if not send_ok:
            _b("logger", logger).warning("Failed to forward pending text: %s", send_msg)
            await _b("safe_send", safe_send)(
                context.bot,
                resolved_chat,
                f"❌ Failed to send pending message: {send_msg}",
                message_thread_id=pending_thread_id,
            )


async def _create_and_bind_window(
    query: object,
    context: ContextTypes.DEFAULT_TYPE,
    user: object,
    selected_path: str,
    pending_thread_id: int | None,
    resume_session_id: str | None = None,
    account_name: str | None = None,
    agent_type: str = "",
    model: str = "",
    reasoning_effort: str = "",
    fast_mode: bool = False,
    permission_mode: str = "ask",
    node_id: str = "",
    answer_callback: bool = True,
) -> None:
    """Create a tmux window, bind it to a topic, and forward pending text."""
    from telegram import CallbackQuery, User

    assert isinstance(query, CallbackQuery)
    assert isinstance(user, User)

    normalized_agent = _b("normalize_agent_type", normalize_agent_type)(
        agent_type or _b("config", config).agent_type
    )
    default_effort = (
        _b("config", config).claude_reasoning_effort
        if normalized_agent
        in {
            _b("AGENT_CLAUDE", AGENT_CLAUDE),
            _b("AGENT_CLAUDE_OFFICIAL", AGENT_CLAUDE_OFFICIAL),
        }
        else _b("config", config).codex_reasoning_effort
        if normalized_agent == _b("AGENT_CODEX", AGENT_CODEX)
        else ""
    )
    profile = _b("AgentProfile", AgentProfile)(
        agent_type=normalized_agent,
        model=model,
        reasoning_effort=reasoning_effort or default_effort,
        fast_mode=fast_mode,
        permission_mode=permission_mode,
    )
    launch_account = (
        None
        if profile.agent_type == _b("AGENT_CURSOR", AGENT_CURSOR)
        else account_name
        or _b("get_default_account_name", get_default_account_name)(profile.agent_type)
    )
    (
        success,
        message,
        created_wname,
        created_wid,
        created_target,
    ) = await _b("_create_agent_target", _create_agent_target)(
        cwd=selected_path,
        node_id=node_id,
        resume_session_id=resume_session_id or "",
        account_name=launch_account or "",
        agent_type=profile.agent_type,
        model=profile.model,
        reasoning_effort=profile.reasoning_effort,
        fast_mode=profile.fast_mode,
        permission_mode=profile.permission_mode,
    )
    if success:
        if launch_account:
            _b("remember_current_account", remember_current_account)(
                launch_account, profile.agent_type
            )
        if created_target and created_target.backend_id != "local":
            await _bind_and_forward_remote_created_target(
                query,
                context,
                user,
                selected_path,
                pending_thread_id,
                resume_session_id,
                profile,
                message,
                created_wname,
                created_target,
                answer_callback,
            )
            return
        if not created_wid:
            await _b("safe_edit", safe_edit)(
                query, "❌ Agent backend did not return a local window id"
            )
            if pending_thread_id is not None and context.user_data is not None:
                _b("_clear_pending_thread_input", _clear_pending_thread_input)(
                    context.user_data
                )
            if answer_callback:
                try:
                    await query.answer("Failed")
                except Exception:
                    _b("logger", logger).debug(
                        "Callback query answer skipped: query expired"
                    )
            return
        await _bind_and_forward_local_created_window(
            query,
            context,
            user,
            selected_path,
            pending_thread_id,
            resume_session_id,
            launch_account,
            profile,
            message,
            created_wname,
            created_wid,
            created_target,
        )
    else:
        await _b("safe_edit", safe_edit)(query, f"❌ {message}")
        if pending_thread_id is not None and context.user_data is not None:
            _b("_clear_pending_thread_input", _clear_pending_thread_input)(
                context.user_data
            )
    if answer_callback:
        try:
            await query.answer("Created" if success else "Failed")
        except Exception:
            _b("logger", logger).debug("Callback query answer skipped: query expired")
