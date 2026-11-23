"""Extracted bot runtime helpers (ADR 0001 Phase D)."""

from __future__ import annotations

from telegram_agent_bot._bot_shared import (
    AGENT_CODEX,
    AGENT_INPUT_MODE_QUEUE,
    AGENT_INPUT_MODE_STEER,
    Any,
    CB_OUTPUT_MODE,
    ContextTypes,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    OUTPUT_MODE_CLEAN,
    OUTPUT_MODE_TRACE,
    SESSION_STILL_RUNNING_MESSAGE,
    Update,
    WELCOME_MESSAGE,
    _b,
    _health_monitor,
    _safe_send_typing_action,
    agent_capabilities,
    agent_display_name,
    asyncio,
    capture_agent_output,
    clear_browse_state,
    clear_status_msg_info,
    clear_topic_state,
    clear_window_working,
    config,
    enqueue_status_update,
    extract_auth_error_message,
    format_health_snapshot,
    handle_interactive_ui,
    io,
    is_codex_input_ready,
    is_interactive_ui,
    is_shell_pane_command,
    logger,
    mark_window_working,
    normalize_output_mode,
    output_mode_label,
    safe_reply,
    sanitize_forward_text,
    send_agent_control,
    send_history,
    session_manager,
    session_monitor,
    text_to_image,
    tmux_manager,
)
from telegram_agent_bot._bot_helpers import (
    _get_thread_id,
    is_user_allowed,
)
from telegram_agent_bot._bot_input_routing import (
    _build_screenshot_keyboard,
    _send_control_to_agent,
    _send_message_to_agent,
    _window_agent_type,
    _window_supports_native_input_routing,
)
from telegram_agent_bot._bot_login import (
    _codex_auth_recovery_message,
)
from telegram_agent_bot._bot_queue_drain import (
    _discard_queued_agent_input,
    _send_native_agent_input,
    _send_or_queue_agent_input,
)
from telegram_agent_bot._bot_window_startup import (
    _cancel_bash_capture,
    _rotate_thread_after_usage_limit,
)


def _call__format_remote_target():
    from telegram_agent_bot._bot_media_topics import _format_remote_target as _fn

    return _b("_format_remote_target", _fn)


def _call__remote_target_for_thread():
    from telegram_agent_bot._bot_media_topics import _remote_target_for_thread as _fn

    return _b("_remote_target_for_thread", _fn)


def _output_mode_keyboard(mode: str) -> InlineKeyboardMarkup:
    normalized = _b("normalize_output_mode", normalize_output_mode)(mode)
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    f"{'✅ ' if normalized == _b('OUTPUT_MODE_CLEAN', OUTPUT_MODE_CLEAN) else ''}Clean",
                    callback_data=f"{_b('CB_OUTPUT_MODE', CB_OUTPUT_MODE)}{_b('OUTPUT_MODE_CLEAN', OUTPUT_MODE_CLEAN)}",
                ),
                InlineKeyboardButton(
                    f"{'✅ ' if normalized == _b('OUTPUT_MODE_TRACE', OUTPUT_MODE_TRACE) else ''}Trace",
                    callback_data=f"{_b('CB_OUTPUT_MODE', CB_OUTPUT_MODE)}{_b('OUTPUT_MODE_TRACE', OUTPUT_MODE_TRACE)}",
                ),
            ]
        ]
    )


def _output_mode_text(mode: str) -> str:
    normalized = _b("normalize_output_mode", normalize_output_mode)(mode)
    if normalized == _b("OUTPUT_MODE_CLEAN", OUTPUT_MODE_CLEAN):
        detail = "只显示最终回答、错误和必要的交互确认。"
    else:
        detail = "额外显示公开工具摘要；模型 reasoning 始终隐藏。"
    return f"Output mode: *{_b('output_mode_label', output_mode_label)(normalized)}*\n\n{detail}"


async def output_mode_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Show or update the per-topic Telegram output mode."""
    del context
    user = update.effective_user
    if (
        not user
        or not _b("is_user_allowed", is_user_allowed)(user.id)
        or not update.message
    ):
        return

    thread_id = _b("_get_thread_id", _get_thread_id)(update)
    parts = (update.message.text or "").split(maxsplit=1)
    if len(parts) > 1 and parts[1].strip().lower() in {"clean", "trace"}:
        mode = _b("session_manager", session_manager).set_output_mode(
            user.id, thread_id, parts[1].strip().lower()
        )
        await _b("safe_reply", safe_reply)(
            update.message, f"✅ {_b('_output_mode_text', _output_mode_text)(mode)}"
        )
        return

    mode = _b("session_manager", session_manager).get_output_mode(user.id, thread_id)
    await _b("safe_reply", safe_reply)(
        update.message,
        _b("_output_mode_text", _output_mode_text)(mode),
        reply_markup=_b("_output_mode_keyboard", _output_mode_keyboard)(mode),
    )


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user or not _b("is_user_allowed", is_user_allowed)(user.id):
        if update.message:
            await _b("safe_reply", safe_reply)(
                update.message, "You are not authorized to use this bot."
            )
        return

    _b("clear_browse_state", clear_browse_state)(context.user_data)

    if update.message:
        await _b("safe_reply", safe_reply)(
            update.message,
            _b("WELCOME_MESSAGE", WELCOME_MESSAGE),
        )


async def history_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show message history for the active session or bound thread."""
    user = update.effective_user
    if not user or not _b("is_user_allowed", is_user_allowed)(user.id):
        return
    if not update.message:
        return

    thread_id = _b("_get_thread_id", _get_thread_id)(update)
    wid = _b("session_manager", session_manager).resolve_window_for_thread(
        user.id, thread_id
    )
    if not wid:
        target = _call__remote_target_for_thread()(user.id, thread_id)
        if target:
            await _b("safe_reply", safe_reply)(
                update.message,
                "❌ History is only available for local tmux sessions.",
            )
            return
        await _b("safe_reply", safe_reply)(
            update.message, "❌ No session bound to this topic."
        )
        return

    await _b("send_history", send_history)(
        update.message,
        wid,
        output_mode=_b("session_manager", session_manager).get_output_mode(
            user.id, thread_id
        ),
    )


async def screenshot_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Capture the current tmux pane and send it as an image."""
    user = update.effective_user
    if not user or not _b("is_user_allowed", is_user_allowed)(user.id):
        return
    if not update.message:
        return

    thread_id = _b("_get_thread_id", _get_thread_id)(update)
    wid = _b("session_manager", session_manager).resolve_window_for_thread(
        user.id, thread_id
    )
    target = _b("session_manager", session_manager).resolve_target_for_thread(
        user.id, thread_id
    )
    if not wid and not target:
        await _b("safe_reply", safe_reply)(
            update.message, "❌ No session bound to this topic."
        )
        return

    capture = await _b("capture_agent_output", capture_agent_output)(
        user.id,
        thread_id,
        wid or "",
        with_ansi=True,
    )
    if capture is None:
        await _b("safe_reply", safe_reply)(
            update.message, "❌ No session bound to this topic."
        )
        return
    if capture.missing:
        display = _b("session_manager", session_manager).get_display_name(
            wid or capture.target.window_id
        )
        await _b("safe_reply", safe_reply)(
            update.message, f"❌ Window '{display}' no longer exists."
        )
        return

    text = capture.text
    if not text:
        await _b("safe_reply", safe_reply)(
            update.message, "❌ Failed to capture pane content."
        )
        return

    png_bytes = await _b("text_to_image", text_to_image)(text, with_ansi=True)
    keyboard = (
        _b("_build_screenshot_keyboard", _build_screenshot_keyboard)(
            capture.target.window_id
        )
        if capture.target.window_id
        else None
    )
    await update.message.reply_document(
        document=_b("io", io).BytesIO(png_bytes),
        filename="screenshot.png",
        reply_markup=keyboard,
    )


async def unbind_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Unbind this topic from its Codex session without killing the window."""
    user = update.effective_user
    if not user or not _b("is_user_allowed", is_user_allowed)(user.id):
        return
    if not update.message:
        return

    thread_id = _b("_get_thread_id", _get_thread_id)(update)
    if thread_id is None:
        await _b("safe_reply", safe_reply)(
            update.message, "❌ This command only works in a topic."
        )
        return

    ambiguous_wid = _b(
        "session_manager", session_manager
    ).get_ambiguous_window_for_thread(user.id, thread_id)
    if isinstance(ambiguous_wid, str) and ambiguous_wid:
        await _b("_discard_queued_agent_input", _discard_queued_agent_input)(
            user.id, thread_id, ambiguous_wid
        )
        _b("session_manager", session_manager).unbind_thread(user.id, thread_id)
        await _b("clear_topic_state", clear_topic_state)(
            user.id, thread_id, context.bot, context.user_data
        )
        await _b("safe_reply", safe_reply)(
            update.message,
            "✅ Removed the ambiguous topic binding. Send a message to create or "
            "resume a dedicated session.",
        )
        return

    wid = _b("session_manager", session_manager).get_window_for_thread(
        user.id, thread_id
    )
    if not wid:
        target = _call__remote_target_for_thread()(user.id, thread_id)
        if target:
            _b("session_manager", session_manager).unbind_thread(user.id, thread_id)
            await _b("clear_topic_state", clear_topic_state)(
                user.id, thread_id, context.bot, context.user_data
            )
            await _b("safe_reply", safe_reply)(
                update.message,
                f"✅ Topic unbound from `{_call__format_remote_target()(target)}`.\n"
                "Send a message to bind to a new session.",
            )
            return
        await _b("safe_reply", safe_reply)(
            update.message, "❌ No session bound to this topic."
        )
        return

    display = _b("session_manager", session_manager).get_display_name(wid)
    await _b("_discard_queued_agent_input", _discard_queued_agent_input)(
        user.id, thread_id, wid
    )
    _b("session_manager", session_manager).unbind_thread(user.id, thread_id)
    await _b("clear_topic_state", clear_topic_state)(
        user.id, thread_id, context.bot, context.user_data
    )

    await _b("safe_reply", safe_reply)(
        update.message,
        f"✅ Topic unbound from window '{display}'.\n"
        f"{_b('SESSION_STILL_RUNNING_MESSAGE', SESSION_STILL_RUNNING_MESSAGE)}\n"
        "Send a message to bind to a new session.",
    )


async def esc_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send Escape key to interrupt Codex."""
    user = update.effective_user
    if not user or not _b("is_user_allowed", is_user_allowed)(user.id):
        return
    if not update.message:
        return

    thread_id = _b("_get_thread_id", _get_thread_id)(update)
    ambiguous_wid = (
        _b("session_manager", session_manager).get_ambiguous_window_for_thread(
            user.id, thread_id
        )
        if thread_id is not None
        else None
    )
    if isinstance(ambiguous_wid, str) and ambiguous_wid:
        await _b("safe_reply", safe_reply)(
            update.message,
            "❌ This topic shares an agent window with another topic. Control is "
            "paused to prevent affecting the wrong session; use /unbind here first.",
        )
        return
    wid = _b("session_manager", session_manager).resolve_window_for_thread(
        user.id, thread_id
    )
    target = _b("session_manager", session_manager).resolve_target_for_thread(
        user.id, thread_id
    )
    if not wid and not target:
        await _b("safe_reply", safe_reply)(
            update.message, "❌ No session bound to this topic."
        )
        return

    dropped = await _b("_discard_queued_agent_input", _discard_queued_agent_input)(
        user.id, thread_id, wid or ""
    )
    if dropped:
        _b("logger", logger).info(
            "Discarded %d queued input(s) before Escape (user=%d thread=%s window=%s)",
            dropped,
            user.id,
            thread_id,
            wid or "",
        )

    result = await _b("send_agent_control", send_agent_control)(
        user.id, thread_id, wid or "", "Escape"
    )
    if result is None:
        await _b("safe_reply", safe_reply)(
            update.message, "❌ No session bound to this topic."
        )
        return
    if result.missing:
        display = _b("session_manager", session_manager).get_display_name(
            wid or result.target.window_id
        )
        await _b("safe_reply", safe_reply)(
            update.message, f"❌ Window '{display}' no longer exists."
        )
        return

    if not result.ok:
        await _b("safe_reply", safe_reply)(update.message, "❌ Failed to send Escape.")
        return
    if wid:
        _b("clear_window_working", clear_window_working)(user.id, wid, thread_id)
        await _b("enqueue_status_update", enqueue_status_update)(
            context.bot, user.id, wid, None, thread_id=thread_id
        )
    else:
        _b("clear_status_msg_info", clear_status_msg_info)(user.id, thread_id)
    await _b("safe_reply", safe_reply)(update.message, "⎋ Sent Escape")


async def _resolve_interrupt_target(
    update: Update, user_id: int, payload_raw: str
) -> tuple[int, str, str, Any] | None:
    assert update.message is not None
    thread_id = _b("_get_thread_id", _get_thread_id)(update)
    if thread_id is None:
        await _b("safe_reply", safe_reply)(
            update.message, "❌ Please use a named topic."
        )
        return None
    sm = _b("session_manager", session_manager)
    ambiguous_wid = sm.get_ambiguous_window_for_thread(user_id, thread_id)
    if isinstance(ambiguous_wid, str) and ambiguous_wid:
        await _b("safe_reply", safe_reply)(
            update.message,
            "❌ This topic shares an agent window with another topic. Interrupt is "
            "paused to prevent affecting the wrong session; use /unbind here first.",
        )
        return None
    chat = update.effective_chat
    if chat and chat.type in ("group", "supergroup"):
        sm.set_group_chat_id(user_id, thread_id, chat.id)
    payload = _b("sanitize_forward_text", sanitize_forward_text)(payload_raw)
    if not payload:
        await _b("safe_reply", safe_reply)(
            update.message,
            "❌ This message only contained wrapper metadata and no forwardable text.",
        )
        return None
    wid = sm.resolve_window_for_thread(user_id, thread_id)
    target = sm.resolve_target_for_thread(user_id, thread_id)
    if not wid and not target:
        await _b("safe_reply", safe_reply)(
            update.message, "❌ No session bound to this topic."
        )
        return None
    window = None
    if wid:
        window = await _b("tmux_manager", tmux_manager).find_window_by_id(wid)
        if not window:
            display = sm.get_display_name(wid)
            await _b("safe_reply", safe_reply)(
                update.message, f"❌ Window '{display}' no longer exists."
            )
            return None
    return thread_id, payload, wid or "", window


async def _send_interrupt_and_replace(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
    thread_id: int,
    wid: str,
    pane_text: str | None,
    payload: str,
) -> None:
    assert update.message is not None
    queued_replacement = False
    if wid:
        if not _b("is_codex_input_ready", is_codex_input_ready)(pane_text or ""):
            ok, control_message = await _b(
                "_send_control_to_agent", _send_control_to_agent
            )(user_id, thread_id, wid, "Escape")
            if not ok:
                await _b("safe_reply", safe_reply)(
                    update.message, f"❌ {control_message}"
                )
                return
            await _b("asyncio", asyncio).sleep(0.3)
        success, message, queued_replacement = await _b(
            "_send_or_queue_agent_input", _send_or_queue_agent_input
        )(context.bot, user_id, thread_id, wid, payload)
    else:
        ok, control_message = await _b(
            "_send_control_to_agent", _send_control_to_agent
        )(user_id, thread_id, "", "Escape")
        if not ok:
            await _b("safe_reply", safe_reply)(update.message, f"❌ {control_message}")
            return
        await _b("asyncio", asyncio).sleep(0.3)
        success, message = await _b("_send_message_to_agent", _send_message_to_agent)(
            user_id, thread_id, "", payload
        )
    if not success:
        await _b("safe_reply", safe_reply)(update.message, f"❌ {message}")
        return
    if wid:
        await _b("mark_window_working", mark_window_working)(
            context.bot, user_id, wid, thread_id
        )
        if queued_replacement:
            await _b("safe_reply", safe_reply)(update.message, f"⎋ {message}")


async def interrupt_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send Escape, then submit an optional replacement message."""
    user = update.effective_user
    if not user or not _b("is_user_allowed", is_user_allowed)(user.id):
        return
    if not update.message:
        return
    parts = (update.message.text or "").split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        await _b("esc_command", esc_command)(update, context)
        return
    resolved = await _resolve_interrupt_target(update, user.id, parts[1])
    if resolved is None:
        return
    thread_id, payload, wid, w = resolved

    await _b("_safe_send_typing_action", _safe_send_typing_action)(
        update.message.chat, source="interrupt_command"
    )
    if wid:
        await _b("enqueue_status_update", enqueue_status_update)(
            context.bot, user.id, wid, None, thread_id=thread_id
        )
    else:
        _b("clear_status_msg_info", clear_status_msg_info)(user.id, thread_id)
    _b("_cancel_bash_capture", _cancel_bash_capture)(user.id, thread_id)
    dropped = await _b("_discard_queued_agent_input", _discard_queued_agent_input)(
        user.id, thread_id, wid or ""
    )
    if dropped:
        _b("logger", logger).info(
            "Discarded %d queued input(s) before interrupt replacement "
            "(user=%d thread=%s window=%s)",
            dropped,
            user.id,
            thread_id,
            wid or "",
        )

    pane_text = None
    if wid:
        capture = await _b("capture_agent_output", capture_agent_output)(
            user.id, thread_id, wid
        )
        pane_text = capture.text if capture and not capture.missing else None
        if pane_text and _b("is_interactive_ui", is_interactive_ui)(pane_text):
            await _b("handle_interactive_ui", handle_interactive_ui)(
                context.bot, user.id, wid, thread_id
            )
            await _b("asyncio", asyncio).sleep(0.3)
        if w and await _b(
            "session_manager", session_manager
        ).window_has_usage_limit_exceeded(wid):
            if await _b(
                "_rotate_thread_after_usage_limit", _rotate_thread_after_usage_limit
            )(
                context=context,
                user_id=user.id,
                thread_id=thread_id,
                current_window_id=wid,
                current_window_cwd=w.cwd,
                text=payload,
            ):
                return

    await _send_interrupt_and_replace(
        update, context, user.id, thread_id, wid, pane_text, payload
    )


async def _validate_native_input_context(
    update: Update,
    user_id: int,
    command_name: str,
) -> tuple[str, int, str, Any, str, str] | None:
    assert update.message is not None
    parts = (update.message.text or "").split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        await _b("safe_reply", safe_reply)(
            update.message, f"Usage: /{command_name} <message>"
        )
        return None
    text = _b("sanitize_forward_text", sanitize_forward_text)(parts[1])
    if not text:
        await _b("safe_reply", safe_reply)(
            update.message, "❌ No forwardable text was provided."
        )
        return None
    thread_id = _b("_get_thread_id", _get_thread_id)(update)
    if thread_id is None:
        await _b("safe_reply", safe_reply)(
            update.message, "❌ Please use a named topic."
        )
        return None
    sm = _b("session_manager", session_manager)
    ambiguous_wid = sm.get_ambiguous_window_for_thread(user_id, thread_id)
    if isinstance(ambiguous_wid, str) and ambiguous_wid:
        await _b("safe_reply", safe_reply)(
            update.message,
            "❌ This topic shares an agent window with another topic. Use /unbind first.",
        )
        return None
    chat = update.effective_chat
    if chat and chat.type in ("group", "supergroup"):
        sm.set_group_chat_id(user_id, thread_id, chat.id)
    window_id = sm.resolve_window_for_thread(user_id, thread_id)
    if not window_id:
        await _b("safe_reply", safe_reply)(
            update.message,
            "❌ Native steer/queue requires a local Codex or Claude Code session.",
        )
        return None
    window = await _b("tmux_manager", tmux_manager).find_window_by_id(window_id)
    if not window:
        display = sm.get_display_name(window_id)
        await _b("safe_reply", safe_reply)(
            update.message, f"❌ Window '{display}' no longer exists."
        )
        return None
    agent_type = _b("_window_agent_type", _window_agent_type)(window_id)
    display_name = _b("agent_display_name", agent_display_name)(agent_type)
    pane_cmd = (window.pane_current_command or "").strip()
    if _b("is_shell_pane_command", is_shell_pane_command)(pane_cmd):
        await _b("safe_reply", safe_reply)(
            update.message,
            f"❌ Window is not running {display_name} (current command: {pane_cmd}).",
        )
        return None
    if not _b(
        "_window_supports_native_input_routing", _window_supports_native_input_routing
    )(window_id):
        await _b("safe_reply", safe_reply)(
            update.message,
            "❌ This command is unavailable for the current agent backend.",
        )
        return None
    return text, thread_id, window_id, window, agent_type, display_name


async def _fallback_queue_native_input(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
    thread_id: int,
    window_id: str,
    text: str,
    display_name: str,
) -> bool:
    assert update.message is not None
    success, message, queued = await _b(
        "_send_or_queue_agent_input", _send_or_queue_agent_input
    )(context.bot, user_id, thread_id, window_id, text)
    if not success:
        await _b("safe_reply", safe_reply)(update.message, f"❌ {message}")
        return True
    if queued:
        await _b("safe_reply", safe_reply)(update.message, f"⏳ {message}")
        return True
    await _b("mark_window_working", mark_window_working)(
        context.bot, user_id, window_id, thread_id
    )
    await _b("safe_reply", safe_reply)(
        update.message,
        f"✅ {display_name} was idle, so the message started a new turn.",
    )
    return True


async def _native_input_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    mode: str,
) -> None:
    """Handle explicit /steer and /queue active-turn input."""
    user = update.effective_user
    if (
        not user
        or not _b("is_user_allowed", is_user_allowed)(user.id)
        or not update.message
    ):
        return
    command_name = (
        "steer"
        if mode == _b("AGENT_INPUT_MODE_STEER", AGENT_INPUT_MODE_STEER)
        else "queue"
    )
    validated = await _validate_native_input_context(update, user.id, command_name)
    if validated is None:
        return
    text, thread_id, window_id, window, agent_type, display_name = validated

    await _b("_safe_send_typing_action", _safe_send_typing_action)(
        update.message.chat, source=f"{command_name}_command"
    )
    await _b("enqueue_status_update", enqueue_status_update)(
        context.bot, user.id, window_id, None, thread_id=thread_id
    )
    _b("_cancel_bash_capture", _cancel_bash_capture)(user.id, thread_id)
    capture = await _b("capture_agent_output", capture_agent_output)(
        user.id, thread_id, window_id
    )
    if capture is None or capture.missing:
        await _b("safe_reply", safe_reply)(
            update.message, f"❌ {display_name} window is no longer available."
        )
        return
    pane_text = capture.text or ""
    auth_error = _b("extract_auth_error_message", extract_auth_error_message)(pane_text)
    if auth_error:
        recovery_message = (
            _b("_codex_auth_recovery_message", _codex_auth_recovery_message)(auth_error)
            if agent_type == _b("AGENT_CODEX", AGENT_CODEX)
            else auth_error
        )
        await _b("safe_reply", safe_reply)(update.message, f"❌ {recovery_message}")
        return

    if (
        mode == _b("AGENT_INPUT_MODE_QUEUE", AGENT_INPUT_MODE_QUEUE)
        and not _b("agent_capabilities", agent_capabilities)(
            agent_type
        ).supports_native_queue
    ):
        await _fallback_queue_native_input(
            update, context, user.id, thread_id, window_id, text, display_name
        )
        return

    if not pane_text.strip() or _b("is_interactive_ui", is_interactive_ui)(pane_text):
        if mode == _b("AGENT_INPUT_MODE_QUEUE", AGENT_INPUT_MODE_QUEUE):
            success, message, _queued = await _b(
                "_send_or_queue_agent_input", _send_or_queue_agent_input
            )(context.bot, user.id, thread_id, window_id, text)
            if success:
                await _b("safe_reply", safe_reply)(update.message, f"⏳ {message}")
                return
        err = (
            f"❌ {display_name} is showing an interactive prompt. Handle it first, then retry."
            if pane_text.strip()
            else f"❌ {display_name} state is temporarily unavailable. Retry shortly."
        )
        await _b("safe_reply", safe_reply)(update.message, err)
        return

    if await _b("session_manager", session_manager).window_has_usage_limit_exceeded(
        window_id
    ):
        if await _b(
            "_rotate_thread_after_usage_limit", _rotate_thread_after_usage_limit
        )(
            context=context,
            user_id=user.id,
            thread_id=thread_id,
            current_window_id=window_id,
            current_window_cwd=window.cwd,
            text=text,
        ):
            return

    if _b("is_codex_input_ready", is_codex_input_ready)(pane_text):
        await _fallback_queue_native_input(
            update, context, user.id, thread_id, window_id, text, display_name
        )
        return

    success, message = await _b("_send_native_agent_input", _send_native_agent_input)(
        context.bot, user.id, thread_id, window_id, text, mode=mode
    )
    if not success:
        await _b("safe_reply", safe_reply)(update.message, f"❌ {message}")
        return
    await _b("mark_window_working", mark_window_working)(
        context.bot, user.id, window_id, thread_id
    )
    await _b("safe_reply", safe_reply)(update.message, f"✅ {message}")


async def steer_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Inject guidance into the current agent turn."""
    await _b("_native_input_command", _native_input_command)(
        update, context, mode=_b("AGENT_INPUT_MODE_STEER", AGENT_INPUT_MODE_STEER)
    )


async def queue_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Queue input for the next agent turn."""
    await _b("_native_input_command", _native_input_command)(
        update, context, mode=_b("AGENT_INPUT_MODE_QUEUE", AGENT_INPUT_MODE_QUEUE)
    )


async def health_command(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show one bounded host/runtime health snapshot on demand."""
    user = update.effective_user
    if (
        not user
        or not _b("is_user_allowed", is_user_allowed)(user.id)
        or not update.message
    ):
        return
    try:
        snapshot = await _b("_health_monitor", _health_monitor).snapshot(
            _b("session_monitor", session_monitor)
        )
        issues = _b("_health_monitor", _health_monitor).issues(
            snapshot,
            _b("config", config),
            active_keys=_b("_health_monitor", _health_monitor).active_issue_keys(),
        )
        await _b("safe_reply", safe_reply)(
            update.message,
            _b("format_health_snapshot", format_health_snapshot)(
                snapshot,
                issues,
                language=_b("config", config).health_notification_language,
            ),
        )
    except Exception:
        _b("logger", logger).exception("Failed to build on-demand host health snapshot")
        await _b("safe_reply", safe_reply)(
            update.message, "❌ Host health snapshot is unavailable."
        )


async def usage_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Fetch Codex usage stats from TUI and send to Telegram."""
    user = update.effective_user
    if not user or not _b("is_user_allowed", is_user_allowed)(user.id):
        return
    if not update.message:
        return

    thread_id = _b("_get_thread_id", _get_thread_id)(update)
    wid = _b("session_manager", session_manager).resolve_window_for_thread(
        user.id, thread_id
    )
    if not wid:
        await _b("safe_reply", safe_reply)(
            update.message, "No session bound to this topic."
        )
        return

    w = await _b("tmux_manager", tmux_manager).find_window_by_id(wid)
    if not w:
        await _b("safe_reply", safe_reply)(
            update.message, f"Window '{wid}' no longer exists."
        )
        return

    # Send /usage command to Codex TUI
    await _b("tmux_manager", tmux_manager).send_keys(w.window_id, "/usage")
    # Wait for the modal to render
    await _b("asyncio", asyncio).sleep(2.0)
    # Capture the pane content
    pane_text = await _b("tmux_manager", tmux_manager).capture_pane(w.window_id)
    # Dismiss the modal
    await _b("tmux_manager", tmux_manager).send_keys(
        w.window_id, "Escape", enter=False, literal=False
    )

    if not pane_text:
        await _b("safe_reply", safe_reply)(
            update.message, "Failed to capture usage info."
        )
        return

    # Try to parse structured usage info
    from .terminal_parser import parse_usage_output

    usage = parse_usage_output(pane_text)
    if usage and usage.parsed_lines:
        text = "\n".join(usage.parsed_lines)
        await _b("safe_reply", safe_reply)(update.message, f"```\n{text}\n```")
    else:
        # Fallback: send raw pane capture trimmed
        trimmed = pane_text.strip()
        if len(trimmed) > 3000:
            trimmed = trimmed[:3000] + "\n... (truncated)"
        await _b("safe_reply", safe_reply)(update.message, f"```\n{trimmed}\n```")
