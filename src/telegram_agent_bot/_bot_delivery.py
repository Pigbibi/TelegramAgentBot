"""Extracted bot runtime helpers (ADR 0001 Phase D)."""

from __future__ import annotations

from telegram_agent_bot._bot_shared import (
    BACKGROUND_WAIT_TOOL_STATUS_TEXT,
    Bot,
    INTERACTIVE_TOOL_NAMES,
    NewMessage,
    TranscriptParser,
    _TRANSIENT_AGENT_ERROR_CODES,
    _TRANSIENT_AGENT_RETRY_DELAYS_SECONDS,
    _b,
    asyncio,
    build_response_parts,
    clear_interactive_mode,
    clear_interactive_msg,
    config,
    enqueue_content_message,
    enqueue_status_update,
    get_interactive_msg_id,
    get_message_queue,
    get_next_account_name,
    handle_interactive_ui,
    logger,
    safe_send,
    session_manager,
    set_interactive_mode,
    turn_admission,
)
from telegram_agent_bot._bot_callbacks import (
    _mark_transcript_message_delivered,
    _transcript_message_already_delivered,
)
from telegram_agent_bot._bot_input_routing import (
    _reset_transient_agent_retry,
)
from telegram_agent_bot._bot_queue_drain import (
    _schedule_transient_agent_retry,
)


async def _handle_delivery_agent_error(
    msg: NewMessage,
    bot: Bot,
    user_id: int,
    wid: str,
    thread_id: int | None,
    *,
    is_remote_target: bool,
) -> tuple[bool, str | None]:
    """Handle transient agent errors; return (handled_and_done, error_notice)."""
    if not is_remote_target:
        _b("turn_admission", turn_admission).observe(wid, active=False)
    if is_remote_target or msg.error_code not in _b(
        "_TRANSIENT_AGENT_ERROR_CODES", _TRANSIENT_AGENT_ERROR_CODES
    ):
        return False, None
    scheduled = _b("_schedule_transient_agent_retry", _schedule_transient_agent_retry)(
        bot, user_id, thread_id, wid
    )
    delays = _b(
        "_TRANSIENT_AGENT_RETRY_DELAYS_SECONDS", _TRANSIENT_AGENT_RETRY_DELAYS_SECONDS
    )
    if scheduled is not None:
        attempt, delay_seconds = scheduled
        await _b("enqueue_status_update", enqueue_status_update)(
            bot,
            user_id,
            wid,
            "⚠️ Agent stopped before finishing because the selected "
            "model is temporarily at capacity.\n"
            f"Automatic retry {attempt}/{len(delays)} in {int(delay_seconds)}s.",
            thread_id=thread_id,
        )
        if msg.source_offset > 0:
            await _b(
                "_mark_transcript_message_delivered",
                _mark_transcript_message_delivered,
            )(user_id, wid, msg)
        return True, None
    return False, (
        f"{msg.text}\n\nAutomatic retries were exhausted "
        f"({len(delays)} attempts). Send the request again or switch models."
    )


async def _send_usage_limit_notice(
    bot: Bot,
    user_id: int,
    wid: str,
    thread_id: int | None,
    *,
    is_remote_target: bool,
) -> None:
    sm = _b("session_manager", session_manager)
    if is_remote_target:
        await _b("safe_send", safe_send)(
            bot,
            sm.resolve_chat_id(user_id, thread_id),
            "⚠️ This remote session has hit its usage limit.",
            message_thread_id=thread_id,
        )
        return
    changed = sm.mark_window_usage_limit_exceeded(wid, True)
    current_state = sm.get_window_state(wid)
    next_account = _b("get_next_account_name", get_next_account_name)(
        current_state.account_name
    )
    status_text = (
        "now marked as exhausted" if changed else "already marked as exhausted"
    )
    note = f"⚠️ This session has hit its usage limit.\nThe window is {status_text}."
    if _b("config", config).enable_account_rotation and next_account:
        note += (
            " On your next message, TelegramAgentBot will open a new "
            f"`{next_account}` session automatically."
        )
    elif _b("config", config).enable_account_rotation:
        note += (
            "\nAutomatic rotation is enabled, but no backup account is "
            "selected. Use /agentlogin <name> and /agentaccount use <name>."
        )
    else:
        note += (
            "\nAutomatic account rotation is disabled. Use /agentlogin to "
            "refresh the current login, or /agentaccount to choose a saved account."
        )
    await _b("safe_send", safe_send)(
        bot, sm.resolve_chat_id(user_id, thread_id), note, message_thread_id=thread_id
    )


async def _handle_special_message_event(
    msg: NewMessage,
    bot: Bot,
    user_id: int,
    wid: str,
    thread_id: int | None,
    *,
    is_remote_target: bool,
) -> bool:
    """Handle interactive UI, thinking, Wait, and filtered tool messages."""
    if (
        not is_remote_target
        and msg.tool_name in _b("INTERACTIVE_TOOL_NAMES", INTERACTIVE_TOOL_NAMES)
        and msg.content_type == "tool_use"
    ):
        _b("set_interactive_mode", set_interactive_mode)(user_id, wid, thread_id)
        queue = _b("get_message_queue", get_message_queue)(user_id, thread_id)
        if queue:
            await queue.join()
        await _b("asyncio", asyncio).sleep(0.3)
        if await _b("handle_interactive_ui", handle_interactive_ui)(
            bot, user_id, wid, thread_id
        ):
            await _b(
                "_mark_transcript_message_delivered", _mark_transcript_message_delivered
            )(user_id, wid, msg)
            return True
        _b("clear_interactive_mode", clear_interactive_mode)(user_id, thread_id)

    if not is_remote_target and _b("get_interactive_msg_id", get_interactive_msg_id)(
        user_id, thread_id
    ):
        await _b("clear_interactive_msg", clear_interactive_msg)(
            user_id, bot, thread_id
        )

    if msg.content_type == "thinking":
        if TranscriptParser.is_encrypted_reasoning_placeholder(msg.text):
            await _b("enqueue_status_update", enqueue_status_update)(
                bot, user_id, wid, "💭 Thinking…\n◦ Working on it…", thread_id=thread_id
            )
        if not is_remote_target and msg.source_offset > 0:
            await _b(
                "_mark_transcript_message_delivered", _mark_transcript_message_delivered
            )(user_id, wid, msg)
        return True

    if msg.tool_name == "Wait" and msg.content_type in ("tool_use", "tool_result"):
        if msg.content_type == "tool_use":
            await _b("enqueue_status_update", enqueue_status_update)(
                bot,
                user_id,
                wid,
                _b(
                    "BACKGROUND_WAIT_TOOL_STATUS_TEXT",
                    BACKGROUND_WAIT_TOOL_STATUS_TEXT,
                ),
                thread_id=thread_id,
            )
        if not is_remote_target:
            await _b(
                "_mark_transcript_message_delivered", _mark_transcript_message_delivered
            )(user_id, wid, msg)
        return True

    if not _b("session_manager", session_manager).is_trace_mode(
        user_id, thread_id
    ) and msg.content_type in ("tool_use", "tool_result", "local_command"):
        if not is_remote_target and msg.source_offset > 0:
            await _b(
                "_mark_transcript_message_delivered", _mark_transcript_message_delivered
            )(user_id, wid, msg)
        return True

    return not _b("config", config).show_tool_calls and msg.content_type in (
        "tool_use",
        "tool_result",
    )


async def _deliver_new_message_to_user(
    msg: NewMessage,
    bot: Bot,
    user_id: int,
    wid: str,
    thread_id: int | None,
) -> bool:
    """Deliver one message to one user target; return True when delivery failed."""
    is_remote_target = not wid
    if not is_remote_target and _b(
        "_transcript_message_already_delivered", _transcript_message_already_delivered
    )(user_id, wid, msg):
        _b("logger", logger).info(
            "Skipping already delivered transcript message: "
            "session=%s user=%d thread=%s window_id=%s source_offset=%d",
            msg.session_id,
            user_id,
            thread_id,
            wid,
            msg.source_offset,
        )
        return False

    agent_error_notice: str | None = None
    if msg.content_type == "agent_error":
        done, agent_error_notice = await _handle_delivery_agent_error(
            msg, bot, user_id, wid, thread_id, is_remote_target=is_remote_target
        )
        if done:
            return False
    elif (
        not is_remote_target
        and msg.is_complete
        and msg.content_type == "text"
        and msg.role == "assistant"
    ):
        _b("_reset_transient_agent_retry", _reset_transient_agent_retry)(wid)

    if msg.content_type == "usage_limit":
        await _send_usage_limit_notice(
            bot, user_id, wid, thread_id, is_remote_target=is_remote_target
        )
        return False

    if await _handle_special_message_event(
        msg, bot, user_id, wid, thread_id, is_remote_target=is_remote_target
    ):
        return False

    delivery_text = agent_error_notice or msg.text
    delivery_content_type = "text" if agent_error_notice else msg.content_type
    parts = _b("build_response_parts", build_response_parts)(
        delivery_text, msg.is_complete, delivery_content_type, msg.role
    )
    if not msg.is_complete:
        return False
    delivered = await _b("enqueue_content_message", enqueue_content_message)(
        bot=bot,
        user_id=user_id,
        window_id=wid,
        parts=parts,
        tool_use_id=msg.tool_use_id,
        tool_name=msg.tool_name,
        content_type=delivery_content_type,
        role=msg.role,
        text=delivery_text,
        thread_id=thread_id,
        image_data=msg.image_data,
        wait_until_sent=True,
    )
    if not delivered:
        _b("logger", logger).warning(
            "Transcript message was not delivered; leaving offset unchanged: "
            "session=%s user=%d thread=%s window_id=%s content_type=%s",
            msg.session_id,
            user_id,
            thread_id,
            wid,
            msg.content_type,
        )
        return True
    if not is_remote_target:
        await _b(
            "_mark_transcript_message_delivered", _mark_transcript_message_delivered
        )(user_id, wid, msg)
    return False


async def handle_new_message(msg: NewMessage, bot: Bot) -> None:
    """Handle a new assistant message — enqueue for sequential processing.

    Messages are queued per-user to ensure status messages always appear last.
    Routes via thread_bindings to deliver to the correct topic.
    """
    status = "complete" if msg.is_complete else "streaming"
    _b("logger", logger).info(
        f"handle_new_message [{status}]: session={msg.session_id}, "
        f"text_len={len(msg.text)}"
    )

    sm = _b("session_manager", session_manager)
    active_users = await sm.find_users_for_session(msg.session_id)
    remote_users = sm.find_users_for_target_session(msg.session_id)
    seen_targets = {(user_id, thread_id) for user_id, _wid, thread_id in active_users}
    active_users.extend(
        (user_id, wid, thread_id)
        for user_id, wid, thread_id in remote_users
        if (user_id, thread_id) not in seen_targets
    )
    if not active_users:
        _b("logger", logger).info(f"No active users for session {msg.session_id}")
        return

    delivery_failed = False
    for user_id, wid, thread_id in active_users:
        if await _deliver_new_message_to_user(msg, bot, user_id, wid, thread_id):
            delivery_failed = True
    if delivery_failed:
        raise RuntimeError("One or more Telegram content deliveries failed")
