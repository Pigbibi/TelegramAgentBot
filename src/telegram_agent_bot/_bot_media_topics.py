"""Extracted bot runtime helpers (ADR 0001 Phase D)."""

from __future__ import annotations

from telegram import User

from telegram_agent_bot._bot_shared import (
    AGENT_CODEX,
    AGENT_INPUT_MODE_STEER,
    AgentTarget,
    Any,
    BadRequest,
    Bot,
    ContextTypes,
    FILE_CONFIRMATION_MESSAGE,
    FILE_QUEUED_MESSAGE,
    MEDIA_DOWNLOAD_ATTEMPTS,
    MEDIA_DOWNLOAD_CONNECT_TIMEOUT_SECONDS,
    MEDIA_DOWNLOAD_POOL_TIMEOUT_SECONDS,
    MEDIA_DOWNLOAD_READ_TIMEOUT_SECONDS,
    MEDIA_DOWNLOAD_RETRY_DELAY_SECONDS,
    MEDIA_DOWNLOAD_WRITE_TIMEOUT_SECONDS,
    NetworkError,
    PHOTO_CONFIRMATION_MESSAGE,
    PHOTO_QUEUED_MESSAGE,
    Path,
    TelegramError,
    TimedOut,
    TranscriptionError,
    UNSUPPORTED_CONTENT_MESSAGE,
    Update,
    _FILES_DIR,
    _IMAGES_DIR,
    _PENDING_TOPIC_DELETIONS_FILE,
    _SAFE_UPLOAD_FILENAME_RE,
    _b,
    _safe_send_typing_action,
    asyncio,
    atomic_write_json,
    capture_agent_output,
    clear_status_msg_info,
    clear_topic_state,
    config,
    contextlib,
    create_agent_session,
    enqueue_status_update,
    is_codex_input_ready,
    is_interactive_ui,
    json,
    logger,
    mark_window_working,
    safe_reply,
    send_agent_message,
    session_manager,
    session_monitor,
    time,
    tmux_manager,
    transcribe_voice,
    upload_agent_file,
    validate_voice_duration,
)
from telegram_agent_bot._bot_helpers import (
    _adapt_forward_command_for_agent,
    _get_thread_id,
    _normalize_forward_command_text,
    _start_unbound_topic_creation,
    _store_pending_thread_media,
    is_user_allowed,
)
from telegram_agent_bot._bot_input_routing import (
    _agent_input_key,
    _offer_input_route_choice,
    _route_can_use_native_input,
    _send_message_to_agent,
    _should_offer_input_route_choice,
    _window_agent_type,
    _window_supports_native_input_routing,
)
from telegram_agent_bot._bot_queue_drain import (
    _discard_queued_agent_input,
    _send_native_agent_input,
    _send_or_queue_agent_input,
)
from telegram_agent_bot._bot_window_startup import (
    _rotate_thread_after_usage_limit,
)


def _call__handle_auth_error_bound_window():
    from telegram_agent_bot._bot_text_recovery import (
        _handle_auth_error_bound_window as _fn,
    )

    return _b("_handle_auth_error_bound_window", _fn)


def _call__handle_missing_bound_window_input():
    from telegram_agent_bot._bot_text_recovery import (
        _handle_missing_bound_window_input as _fn,
    )

    return _b("_handle_missing_bound_window_input", _fn)


def _call__handle_non_codex_bound_window():
    from telegram_agent_bot._bot_text_recovery import (
        _handle_non_codex_bound_window as _fn,
    )

    return _b("_handle_non_codex_bound_window", _fn)


def _remote_target_for_thread(
    user_id: int,
    thread_id: int | None,
) -> AgentTarget | None:
    """Return a non-local target bound to this thread, if any."""
    target = _b("session_manager", session_manager).resolve_target_for_thread(
        user_id, thread_id
    )
    if (
        isinstance(target, _b("AgentTarget", AgentTarget))
        and target.backend_id != "local"
    ):
        return target
    return None


def _format_remote_target(target: AgentTarget) -> str:
    """Build a compact user-facing label for a remote target."""
    label = f"{target.backend_id}/{target.node_id}"
    if target.session_id:
        label = f"{label}:{target.session_id}"
    return label


async def _create_agent_local_window(
    *,
    cwd: str,
    window_name: str = "",
    resume_session_id: str = "",
    account_name: str = "",
    agent_type: str = "",
    model: str = "",
    reasoning_effort: str = "",
    fast_mode: bool = False,
    permission_mode: str = "ask",
) -> tuple[bool, str, str, str, AgentTarget | None]:
    success, message, display_name, window_id, target = await _b(
        "_create_agent_target", _create_agent_target
    )(
        cwd=cwd,
        window_name=window_name,
        resume_session_id=resume_session_id,
        account_name=account_name,
        agent_type=agent_type,
        model=model,
        reasoning_effort=reasoning_effort,
        fast_mode=fast_mode,
        permission_mode=permission_mode,
    )
    if success and not window_id:
        return (
            False,
            "Agent backend did not return a local window id",
            display_name,
            "",
            target,
        )
    return success, message, display_name, window_id, target


async def _create_agent_target(
    *,
    cwd: str,
    node_id: str = "",
    window_name: str = "",
    resume_session_id: str = "",
    account_name: str = "",
    agent_type: str = "",
    model: str = "",
    reasoning_effort: str = "",
    fast_mode: bool = False,
    permission_mode: str = "ask",
) -> tuple[bool, str, str, str, AgentTarget | None]:
    create_kwargs: dict[str, Any] = {
        "cwd": cwd,
        "window_name": window_name,
        "resume_session_id": resume_session_id,
        "account_name": account_name,
    }
    if agent_type:
        create_kwargs["agent_type"] = agent_type
    if model:
        create_kwargs["model"] = model
    if reasoning_effort:
        create_kwargs["reasoning_effort"] = reasoning_effort
    if fast_mode:
        create_kwargs["fast_mode"] = True
    create_kwargs["permission_mode"] = permission_mode
    if node_id:
        create_kwargs["node_id"] = node_id
    result = await _b("create_agent_session", create_agent_session)(**create_kwargs)
    target = result.target
    window_id = target.window_id if target else ""
    if result.ok and target is None:
        return (
            False,
            "Agent backend did not return a target",
            result.display_name,
            "",
            target,
        )
    return result.ok, result.message, result.display_name, window_id, target


async def topic_closed_handler(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle topic closure — kill the associated tmux window and clean up state."""
    user = update.effective_user
    if not user or not _b("is_user_allowed", is_user_allowed)(user.id):
        return

    thread_id = _b("_get_thread_id", _get_thread_id)(update)
    if thread_id is None:
        return

    chat = update.effective_chat
    chat_id = (
        chat.id
        if chat
        else _b("session_manager", session_manager).resolve_chat_id(user.id, thread_id)
    )
    wid = _b("session_manager", session_manager).get_window_for_thread(
        user.id, thread_id
    )
    if wid:
        display = _b("session_manager", session_manager).get_display_name(wid)
        state = _b("session_manager", session_manager).window_states.get(wid)
        session_id = state.session_id if state else ""
        w = await _b("tmux_manager", tmux_manager).find_window_by_id(wid)
        if w:
            await _b("tmux_manager", tmux_manager).kill_window(w.window_id)
            _b("logger", logger).info(
                "Topic closed: killed window %s (user=%d, thread=%d)",
                display,
                user.id,
                thread_id,
            )
        else:
            _b("logger", logger).info(
                "Topic closed: window %s already gone (user=%d, thread=%d)",
                display,
                user.id,
                thread_id,
            )
        if session_id:
            _b("session_manager", session_manager).hide_session(session_id)
        await _b("_discard_queued_agent_input", _discard_queued_agent_input)(
            user.id, thread_id, wid
        )
        _b("session_manager", session_manager).unbind_thread(user.id, thread_id)
        await _b("session_manager", session_manager).remove_session_map_entry(wid)
        _b("session_manager", session_manager).remove_window_state(wid)
        _sm = _b("session_monitor", session_monitor)
        if session_id and _sm is not None:
            _sm.state.remove_session(session_id)
            _sm.state.save_if_dirty()
        # Clean up all memory state for this topic
        await _b("clear_topic_state", clear_topic_state)(
            user.id, thread_id, context.bot, context.user_data
        )
        try:
            await context.bot.delete_forum_topic(
                chat_id=chat_id,
                message_thread_id=thread_id,
            )
            _b("logger", logger).info(
                "Topic closed: deleted topic (chat_id=%s, thread=%d)",
                chat_id,
                thread_id,
            )
        except BadRequest as exc:
            message = str(exc)
            if "Topic_id_invalid" in message or "message thread not found" in message:
                _b("logger", logger).info(
                    "Topic closed: topic already deleted (chat_id=%s, thread=%d)",
                    chat_id,
                    thread_id,
                )
            else:
                _b("logger", logger).warning(
                    "Topic closed: failed to delete topic (chat_id=%s, thread=%d): %s",
                    chat_id,
                    thread_id,
                    exc,
                )
        except TelegramError as exc:
            _b("logger", logger).warning(
                "Topic closed: failed to delete topic (chat_id=%s, thread=%d): %s",
                chat_id,
                thread_id,
                exc,
            )
    else:
        await _b("clear_topic_state", clear_topic_state)(
            user.id, thread_id, context.bot, context.user_data
        )
        try:
            await context.bot.delete_forum_topic(
                chat_id=chat_id,
                message_thread_id=thread_id,
            )
            _b("logger", logger).info(
                "Topic closed: deleted unbound topic (chat_id=%s, thread=%d)",
                chat_id,
                thread_id,
            )
        except BadRequest as exc:
            message = str(exc)
            if "Topic_id_invalid" in message or "message thread not found" in message:
                _b("logger", logger).info(
                    "Topic closed: unbound topic already deleted (chat_id=%s, thread=%d)",
                    chat_id,
                    thread_id,
                )
            else:
                _b("logger", logger).debug(
                    "Topic closed: no binding and delete failed (user=%d, thread=%d): %s",
                    user.id,
                    thread_id,
                    exc,
                )
        except TelegramError as exc:
            _b("logger", logger).debug(
                "Topic closed: no binding and delete failed (user=%d, thread=%d): %s",
                user.id,
                thread_id,
                exc,
            )


async def topic_edited_handler(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle topic rename — sync new name to tmux window and internal state."""
    user = update.effective_user
    if not user or not _b("is_user_allowed", is_user_allowed)(user.id):
        return

    msg = update.message
    if not msg or not msg.forum_topic_edited:
        return

    new_name = msg.forum_topic_edited.name
    if new_name is None:
        # Icon-only change, no rename needed
        return

    thread_id = _b("_get_thread_id", _get_thread_id)(update)
    if thread_id is None:
        return

    wid = _b("session_manager", session_manager).get_window_for_thread(
        user.id, thread_id
    )
    if not wid:
        _b("logger", logger).debug(
            "Topic edited: no binding (user=%d, thread=%d)", user.id, thread_id
        )
        return

    old_name = _b("session_manager", session_manager).get_display_name(wid)
    await _b("tmux_manager", tmux_manager).rename_window(wid, new_name)
    _b("session_manager", session_manager).update_display_name(wid, new_name)
    _b("logger", logger).info(
        "Topic renamed: '%s' -> '%s' (window=%s, user=%d, thread=%d)",
        old_name,
        new_name,
        wid,
        user.id,
        thread_id,
    )


async def _send_local_forwarded_command(
    bot: Bot,
    message: Any,
    *,
    user_id: int,
    thread_id: int | None,
    window_id: str,
    command_text: str,
) -> tuple[bool, str, str]:
    """Send a local slash command with the active agent's busy-turn semantics."""
    agent_type = _b("_window_agent_type", _window_agent_type)(window_id)
    capture = await _b("capture_agent_output", capture_agent_output)(
        user_id, thread_id, window_id
    )
    pane_text = (
        capture.text
        if capture is not None and not capture.missing and capture.text
        else ""
    )
    key = _b("_agent_input_key", _agent_input_key)(user_id, thread_id, window_id)
    busy = (
        bool(pane_text.strip())
        and not _b("is_codex_input_ready", is_codex_input_ready)(pane_text)
        and not _b("is_interactive_ui", is_interactive_ui)(pane_text)
    )
    if (
        busy
        and thread_id is not None
        and _b(
            "_window_supports_native_input_routing",
            _window_supports_native_input_routing,
        )(window_id)
        and _b("_route_can_use_native_input", _route_can_use_native_input)(key)
    ):
        if agent_type == _b("AGENT_CODEX", AGENT_CODEX):
            offered = await _b("_offer_input_route_choice", _offer_input_route_choice)(
                message,
                user_id=user_id,
                thread_id=thread_id,
                window_id=window_id,
                text=command_text,
            )
            return offered, "", "choice"

        # Claude Code natively queues slash commands entered while it responds.
        success, result_message = await _b(
            "_send_native_agent_input", _send_native_agent_input
        )(
            bot,
            user_id,
            thread_id,
            window_id,
            command_text,
            mode=_b("AGENT_INPUT_MODE_STEER", AGENT_INPUT_MODE_STEER),
        )
        if success:
            await _b("mark_window_working", mark_window_working)(
                bot, user_id, window_id, thread_id
            )
        return success, result_message, "native"

    success, result_message, queued = await _b(
        "_send_or_queue_agent_input", _send_or_queue_agent_input
    )(
        bot,
        user_id,
        thread_id,
        window_id,
        command_text,
    )
    return success, result_message, "queued" if queued else "sent"


async def forward_command_handler(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Forward any non-bot command to the active Codex or Claude session."""
    user = update.effective_user
    if not user or not _b("is_user_allowed", is_user_allowed)(user.id):
        return
    if not update.message:
        return

    thread_id = _b("_get_thread_id", _get_thread_id)(update)

    # Capture group chat_id for supergroup forum topic routing.
    # Required: Telegram Bot API needs group chat_id (not user_id) to send
    # messages with message_thread_id. Do NOT remove — see session.py docs.
    chat = update.effective_chat
    if chat and chat.type in ("group", "supergroup"):
        _b("session_manager", session_manager).set_group_chat_id(
            user.id, thread_id, chat.id
        )

    cmd_text = update.message.text or ""
    # The full text is already a slash command like "/clear" or "/compact foo".
    # Strip only a Telegram bot mention in the command token; keep @mentions
    # in command arguments intact.
    normalized_slash = _b(
        "_normalize_forward_command_text", _normalize_forward_command_text
    )(cmd_text)
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

    agent_type = (
        _b("_window_agent_type", _window_agent_type)(wid)
        if wid
        else _b("config", config).agent_type
    )
    cc_slash = _b("_adapt_forward_command_for_agent", _adapt_forward_command_for_agent)(
        normalized_slash, agent_type
    )
    target_window_id = target.window_id if target else ""
    display = _b("session_manager", session_manager).get_display_name(
        wid or target_window_id
    )
    _b("logger", logger).info(
        "Forwarding command %s to window %s (user=%d)", cc_slash, display, user.id
    )
    await _b("_safe_send_typing_action", _safe_send_typing_action)(
        update.message.chat, source="history_command"
    )
    is_clear_command = cc_slash.strip().lower() == "/clear"
    if wid and not is_clear_command:
        success, message, delivery = await _b(
            "_send_local_forwarded_command", _send_local_forwarded_command
        )(
            context.bot,
            update.message,
            user_id=user.id,
            thread_id=thread_id,
            window_id=wid,
            command_text=cc_slash,
        )
        if delivery == "choice":
            return
        if not success:
            await _b("safe_reply", safe_reply)(update.message, f"❌ {message}")
            return
        if delivery == "native":
            await _b("safe_reply", safe_reply)(
                update.message, f"⚡ [{display}] {message}"
            )
            return
        action = "Queued" if delivery == "queued" else "Sent"
        await _b("safe_reply", safe_reply)(
            update.message, f"⚡ [{display}] {action}: {cc_slash}"
        )
        return

    result = await _b("send_agent_message", send_agent_message)(
        user.id, thread_id, wid or "", cc_slash
    )
    if result is None:
        await _b("safe_reply", safe_reply)(
            update.message, "❌ No session bound to this topic."
        )
        return
    if result.missing:
        await _b("safe_reply", safe_reply)(
            update.message, f"❌ Window '{display}' no longer exists."
        )
        return

    if result.ok:
        await _b("safe_reply", safe_reply)(
            update.message, f"⚡ [{display}] Sent: {cc_slash}"
        )
        # If /clear command was sent, clear the session association
        # so we can detect the new session after first message.  Keep /clear
        # out of the bot-side queue so this state update remains synchronous.
        if wid and is_clear_command:
            _b("logger", logger).info(
                "Clearing session for window %s after /clear", display
            )
            _b("session_manager", session_manager).clear_window_session(wid)

        # Interactive commands (e.g. /model) render a terminal-based UI
        # with no JSONL tool_use entry.  The status poller already detects
        # interactive UIs every 1s (status_polling.py), so no
        # proactive detection needed here — the poller handles it.
    else:
        await _b("safe_reply", safe_reply)(update.message, f"❌ {result.message}")


async def agent_command_mode(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Forward an arbitrary agent slash command through one stable bot command.

    Examples: ``/agentcmd /review`` and ``/cmd compact``.
    """
    user = update.effective_user
    message = update.message
    if not user or not _b("is_user_allowed", is_user_allowed)(user.id) or not message:
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        await _b("safe_reply", safe_reply)(
            message, "Usage: /agentcmd /command [arguments]"
        )
        return

    command_text = parts[1].strip()
    if not command_text.startswith("/"):
        command_text = "/" + command_text
    command_text = _b(
        "_normalize_forward_command_text", _normalize_forward_command_text
    )(command_text)
    thread_id = _b("_get_thread_id", _get_thread_id)(update)
    wid = _b("session_manager", session_manager).resolve_window_for_thread(
        user.id, thread_id
    )
    target = _b("session_manager", session_manager).resolve_target_for_thread(
        user.id, thread_id
    )
    if not wid and not target:
        await _b("safe_reply", safe_reply)(
            message, "❌ No session bound to this topic."
        )
        return
    if wid:
        command_text = _b(
            "_adapt_forward_command_for_agent", _adapt_forward_command_for_agent
        )(
            command_text,
            _b("_window_agent_type", _window_agent_type)(wid),
        )
        success, result_message, delivery = await _b(
            "_send_local_forwarded_command", _send_local_forwarded_command
        )(
            context.bot,
            message,
            user_id=user.id,
            thread_id=thread_id,
            window_id=wid,
            command_text=command_text,
        )
        if delivery == "choice":
            return
        if not success:
            await _b("safe_reply", safe_reply)(message, f"❌ {result_message}")
            return
        if delivery == "native":
            await _b("safe_reply", safe_reply)(message, f"⚡ {result_message}")
            return
        action = "Queued" if delivery == "queued" else "Sent"
        await _b("safe_reply", safe_reply)(message, f"⚡ {action}: {command_text}")
        return

    command_text = _b(
        "_adapt_forward_command_for_agent", _adapt_forward_command_for_agent
    )(command_text, _b("config", config).agent_type)
    result = await _b("send_agent_message", send_agent_message)(
        user.id, thread_id, "", command_text
    )
    if result is None or not result.ok:
        await _b("safe_reply", safe_reply)(
            message, f"❌ {result.message if result else 'No session bound'}"
        )
        return
    await _b("safe_reply", safe_reply)(message, f"⚡ Sent: {command_text}")


async def unsupported_content_handler(
    update: Update,
    _context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Reply to non-text messages (stickers, video, etc.)."""
    if not update.message:
        return
    user = update.effective_user
    if not user or not _b("is_user_allowed", is_user_allowed)(user.id):
        return
    _b("logger", logger).debug("Unsupported content from user %d", user.id)
    await _b("safe_reply", safe_reply)(
        update.message,
        _b("UNSUPPORTED_CONTENT_MESSAGE", UNSUPPORTED_CONTENT_MESSAGE),
    )


def _safe_upload_filename(filename: str, *, fallback: str = "upload.bin") -> str:
    """Return a path-safe ASCII filename for local and remote uploads."""
    name = Path(filename).name or fallback
    safe = (
        _b("_SAFE_UPLOAD_FILENAME_RE", _SAFE_UPLOAD_FILENAME_RE)
        .sub("_", name)
        .strip("._")
    )
    return safe or fallback


async def _download_telegram_media(media: Any, file_path: Path, *, label: str) -> None:
    """Download Telegram media with media-specific timeouts and transient retries."""
    timeout_kwargs = {
        "connect_timeout": _b(
            "MEDIA_DOWNLOAD_CONNECT_TIMEOUT_SECONDS",
            MEDIA_DOWNLOAD_CONNECT_TIMEOUT_SECONDS,
        ),
        "read_timeout": _b(
            "MEDIA_DOWNLOAD_READ_TIMEOUT_SECONDS", MEDIA_DOWNLOAD_READ_TIMEOUT_SECONDS
        ),
        "write_timeout": _b(
            "MEDIA_DOWNLOAD_WRITE_TIMEOUT_SECONDS", MEDIA_DOWNLOAD_WRITE_TIMEOUT_SECONDS
        ),
        "pool_timeout": _b(
            "MEDIA_DOWNLOAD_POOL_TIMEOUT_SECONDS", MEDIA_DOWNLOAD_POOL_TIMEOUT_SECONDS
        ),
    }

    for attempt in range(1, _b("MEDIA_DOWNLOAD_ATTEMPTS", MEDIA_DOWNLOAD_ATTEMPTS) + 1):
        try:
            tg_file = await media.get_file(**timeout_kwargs)
            await tg_file.download_to_drive(file_path, **timeout_kwargs)
            return
        except (TimedOut, NetworkError) as exc:
            if attempt >= _b("MEDIA_DOWNLOAD_ATTEMPTS", MEDIA_DOWNLOAD_ATTEMPTS):
                raise
            with _b("contextlib", contextlib).suppress(OSError):
                file_path.unlink()
            _b("logger", logger).warning(
                "Telegram %s download failed with transient network error; "
                "retrying: attempt=%d/%d path=%s error=%s",
                label,
                attempt,
                _b("MEDIA_DOWNLOAD_ATTEMPTS", MEDIA_DOWNLOAD_ATTEMPTS),
                file_path,
                exc,
            )
            await _b("asyncio", asyncio).sleep(
                _b(
                    "MEDIA_DOWNLOAD_RETRY_DELAY_SECONDS",
                    MEDIA_DOWNLOAD_RETRY_DELAY_SECONDS,
                )
            )


async def process_pending_topic_deletions(bot: Bot) -> None:
    """Delete queued forum topics from a previous local cleanup request."""
    if not _b("_PENDING_TOPIC_DELETIONS_FILE", _PENDING_TOPIC_DELETIONS_FILE).exists():
        return

    try:
        payload = _b("json", json).loads(
            _b(
                "_PENDING_TOPIC_DELETIONS_FILE", _PENDING_TOPIC_DELETIONS_FILE
            ).read_text()
        )
    except (_b("json", json).JSONDecodeError, OSError) as exc:
        _b("logger", logger).warning(
            "Failed to load pending topic deletions file %s: %s",
            _b("_PENDING_TOPIC_DELETIONS_FILE", _PENDING_TOPIC_DELETIONS_FILE),
            exc,
        )
        return

    if not isinstance(payload, list):
        _b("logger", logger).warning(
            "Invalid pending topic deletions payload in %s",
            _b("_PENDING_TOPIC_DELETIONS_FILE", _PENDING_TOPIC_DELETIONS_FILE),
        )
        return

    failed_entries: list[dict[str, int]] = []
    deleted_count = 0
    skipped_count = 0

    for entry in payload:
        if not isinstance(entry, dict):
            skipped_count += 1
            continue

        try:
            chat_id = int(entry["chat_id"])
            thread_id = int(entry["thread_id"])
            user_id = int(entry["user_id"])
        except (KeyError, TypeError, ValueError):
            skipped_count += 1
            _b("logger", logger).warning(
                "Skipping invalid pending topic deletion entry: %s", entry
            )
            continue

        try:
            await bot.delete_forum_topic(
                chat_id=chat_id,
                message_thread_id=thread_id,
            )
            deleted_count += 1
            _b("session_manager", session_manager).clear_group_chat_id(
                user_id, thread_id
            )
            _b("logger", logger).info(
                "Deleted queued topic (chat_id=%s, thread_id=%d)",
                chat_id,
                thread_id,
            )
        except BadRequest as exc:
            message = str(exc)
            if "Topic_id_invalid" in message or "message thread not found" in message:
                deleted_count += 1
                _b("session_manager", session_manager).clear_group_chat_id(
                    user_id, thread_id
                )
                _b("logger", logger).info(
                    "Queued topic already gone (chat_id=%s, thread_id=%d)",
                    chat_id,
                    thread_id,
                )
            else:
                failed_entries.append(entry)
                _b("logger", logger).warning(
                    "Failed to delete queued topic (chat_id=%s, thread_id=%d): %s",
                    chat_id,
                    thread_id,
                    exc,
                )
        except TelegramError as exc:
            failed_entries.append(entry)
            _b("logger", logger).warning(
                "Failed to delete queued topic (chat_id=%s, thread_id=%d): %s",
                chat_id,
                thread_id,
                exc,
            )

    if failed_entries:
        _b("atomic_write_json", atomic_write_json)(
            _b("_PENDING_TOPIC_DELETIONS_FILE", _PENDING_TOPIC_DELETIONS_FILE),
            failed_entries,
        )
    else:
        _b("_PENDING_TOPIC_DELETIONS_FILE", _PENDING_TOPIC_DELETIONS_FILE).unlink(
            missing_ok=True
        )

    _b("logger", logger).info(
        "Processed pending topic deletions: total=%d deleted=%d failed=%d skipped=%d",
        len(payload),
        deleted_count,
        len(failed_entries),
        skipped_count,
    )


async def _validate_media_topic_route(
    update: Update,
    *,
    require_bound: bool = False,
) -> tuple[User, int, str | None, Any | None, Any | None] | None:
    """Validate authorization, topic binding, and group chat id for media handlers."""
    user = update.effective_user
    if not user or not _b("is_user_allowed", is_user_allowed)(user.id):
        if update.message:
            await _b("safe_reply", safe_reply)(
                update.message, "You are not authorized to use this bot."
            )
        return None
    msg = update.message
    if not msg:
        return None
    chat = msg.chat
    thread_id = _b("_get_thread_id", _get_thread_id)(update)
    sm = _b("session_manager", session_manager)
    if chat.type in ("group", "supergroup") and thread_id is not None:
        sm.set_group_chat_id(user.id, thread_id, chat.id)
    if thread_id is None:
        await _b("safe_reply", safe_reply)(
            msg, "❌ Please use a named topic. Create a new topic to start a session."
        )
        return None
    ambiguous_wid = sm.get_ambiguous_window_for_thread(user.id, thread_id)
    if isinstance(ambiguous_wid, str) and ambiguous_wid:
        await _b("safe_reply", safe_reply)(
            msg,
            "❌ This topic shares an agent window with another topic. Messages are "
            "paused to prevent cross-topic delivery; use /unbind here first.",
        )
        return None
    wid = sm.get_window_for_thread(user.id, thread_id)
    remote_target = (
        None
        if wid is not None
        else _b("_remote_target_for_thread", _remote_target_for_thread)(
            user.id, thread_id
        )
    )
    if require_bound and wid is None and remote_target is None:
        await _b("safe_reply", safe_reply)(
            msg,
            "❌ No session bound to this topic. Send a text message first to create one.",
        )
        return None
    w = (
        await _b("tmux_manager", tmux_manager).find_window_by_id(wid)
        if wid is not None
        else None
    )
    return user, thread_id, wid, remote_target, w


async def _prepare_attachment_path(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    user: User,
    thread_id: int,
    wid: str | None,
    remote_target: Any,
    *,
    kind: str,
    file_path: Path,
    filename: str,
    caption: str,
) -> str | None:
    """Stage an unbound topic attachment or upload a remote attachment."""
    assert update.message is not None
    if wid is None and remote_target is None:
        _b("_store_pending_thread_media", _store_pending_thread_media)(
            context, kind=kind, path=file_path, filename=filename, caption=caption
        )
        await _b("safe_reply", safe_reply)(
            update.message,
            "📎 Attachment received. Choose a project to start a session; it will be sent automatically.",
        )
        await _b("_start_unbound_topic_creation", _start_unbound_topic_creation)(
            update.message, context, user, thread_id
        )
        return None
    if remote_target is None:
        return str(file_path)
    upload = await _b("upload_agent_file", upload_agent_file)(
        user.id, thread_id, "", str(file_path), filename=filename
    )
    if upload is None:
        await _b("safe_reply", safe_reply)(update.message, "❌ No session bound")
        return None
    if not upload.ok or not upload.path:
        await _b("safe_reply", safe_reply)(
            update.message,
            f"❌ File transfer failed: {upload.message or 'backend unavailable'}",
        )
        return None
    return upload.path


async def _check_bound_window_for_media(
    update: Update,
    user: User,
    thread_id: int,
    wid: str,
    w: Any,
    *,
    text_to_send: str,
    success_reply: str,
    check_non_codex: bool,
) -> tuple[bool, str | None]:
    """Handle missing/non-codex/auth-error states; return (handled, pane_text)."""
    assert update.message is not None
    if not w:
        await _call__handle_missing_bound_window_input()(
            update_message=update.message,
            user_id=user.id,
            thread_id=thread_id,
            window_id=wid,
            text=text_to_send,
            success_reply=success_reply,
        )
        return True, None
    if check_non_codex:
        pane_cmd = (getattr(w, "pane_current_command", "") or "").strip()
        if await _call__handle_non_codex_bound_window()(
            update_message=update.message,
            user_id=user.id,
            thread_id=thread_id,
            window_id=wid,
            pane_command=pane_cmd,
            text=text_to_send,
            success_reply=success_reply,
        ):
            return True, None
    capture = await _b("capture_agent_output", capture_agent_output)(
        user.id, thread_id, wid
    )
    pane_text = capture.text if capture and not capture.missing else None
    if pane_text and await _call__handle_auth_error_bound_window()(
        update_message=update.message,
        user_id=user.id,
        thread_id=thread_id,
        window_id=wid,
        pane_text=pane_text,
        text=text_to_send,
        success_reply=success_reply,
    ):
        return True, pane_text
    return False, pane_text


async def _deliver_media_or_voice_text(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    user: User,
    thread_id: int,
    wid: str | None,
    w: Any,
    *,
    source: str,
    text_to_send: str,
    success_reply: str,
    queued_reply: str,
    check_non_codex: bool = True,
    offer_route_choice: bool = True,
) -> None:
    """Forward a resolved media attachment prompt or transcribed voice message."""
    assert update.message is not None
    pane_text: str | None = None
    if wid:
        handled, pane_text = await _check_bound_window_for_media(
            update,
            user,
            thread_id,
            wid,
            w,
            text_to_send=text_to_send,
            success_reply=success_reply,
            check_non_codex=check_non_codex,
        )
        if handled:
            return
    await _b("_safe_send_typing_action", _safe_send_typing_action)(
        update.message.chat, source=source
    )
    if wid:
        await _b("enqueue_status_update", enqueue_status_update)(
            context.bot, user.id, wid, None, thread_id=thread_id
        )
    else:
        _b("clear_status_msg_info", clear_status_msg_info)(user.id, thread_id)
    if (
        wid
        and w
        and await _b(
            "session_manager", session_manager
        ).window_has_usage_limit_exceeded(wid)
    ):
        await _b("_rotate_thread_after_usage_limit", _rotate_thread_after_usage_limit)(
            context=context,
            user_id=user.id,
            thread_id=thread_id,
            current_window_id=wid,
            current_window_cwd=w.cwd,
            text=text_to_send,
        )
        return
    if (
        offer_route_choice
        and wid
        and _b("_should_offer_input_route_choice", _should_offer_input_route_choice)(
            user_id=user.id,
            thread_id=thread_id,
            window_id=wid,
            pane_text=pane_text,
        )
    ):
        await _b("_offer_input_route_choice", _offer_input_route_choice)(
            update.message,
            user_id=user.id,
            thread_id=thread_id,
            window_id=wid,
            text=text_to_send,
        )
        return
    if wid:
        success, message, queued = await _b(
            "_send_or_queue_agent_input", _send_or_queue_agent_input
        )(context.bot, user.id, thread_id, wid, text_to_send)
    else:
        success, message = await _b("_send_message_to_agent", _send_message_to_agent)(
            user.id, thread_id, "", text_to_send
        )
        queued = False
    if not success:
        await _b("safe_reply", safe_reply)(update.message, f"❌ {message}")
        return
    await _b("safe_reply", safe_reply)(
        update.message, queued_reply if queued else success_reply
    )
    if wid and not queued:
        await _b("mark_window_working", mark_window_working)(
            context.bot, user.id, wid, thread_id
        )


async def photo_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle photos sent by the user: download and forward path to Codex."""
    if not update.message or not update.message.photo:
        return
    resolved = await _validate_media_topic_route(update)
    if resolved is None:
        return
    user, thread_id, wid, remote_target, w = resolved
    photo = update.message.photo[-1]
    filename = f"{_b('time', time).time_ns()}_{photo.file_unique_id}.jpg"
    file_path = _b("_IMAGES_DIR", _IMAGES_DIR) / filename
    try:
        await _b("_download_telegram_media", _download_telegram_media)(
            photo, file_path, label="photo"
        )
    except (TelegramError, OSError) as exc:
        _b("logger", logger).exception("Failed to download Telegram photo")
        await _b("safe_reply", safe_reply)(
            update.message, f"❌ Failed to download image: {exc}"
        )
        return
    caption = update.message.caption or ""
    agent_file_path = await _prepare_attachment_path(
        update,
        context,
        user,
        thread_id,
        wid,
        remote_target,
        kind="photo",
        file_path=file_path,
        filename=filename,
        caption=caption,
    )
    if agent_file_path is None:
        return
    text_to_send = (
        f"{caption}\n\n(image attached: {agent_file_path})"
        if caption
        else f"(image attached: {agent_file_path})"
    )
    await _deliver_media_or_voice_text(
        update,
        context,
        user,
        thread_id,
        wid,
        w,
        source="photo_handler",
        text_to_send=text_to_send,
        success_reply=_b("PHOTO_CONFIRMATION_MESSAGE", PHOTO_CONFIRMATION_MESSAGE),
        queued_reply=_b("PHOTO_QUEUED_MESSAGE", PHOTO_QUEUED_MESSAGE),
    )


async def document_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle files sent by the user: download and forward path to Codex."""
    if not update.message or not update.message.document:
        return
    resolved = await _validate_media_topic_route(update)
    if resolved is None:
        return
    user, thread_id, wid, remote_target, w = resolved
    document = update.message.document
    document_name = _b("_safe_upload_filename", _safe_upload_filename)(
        document.file_name or f"{document.file_unique_id}.bin"
    )
    unique_id = _b("_safe_upload_filename", _safe_upload_filename)(
        document.file_unique_id, fallback="file"
    )
    file_path = (
        _b("_FILES_DIR", _FILES_DIR)
        / f"{_b('time', time).time_ns()}_{unique_id}_{document_name}"
    )
    try:
        await _b("_download_telegram_media", _download_telegram_media)(
            document, file_path, label="document"
        )
    except (TelegramError, OSError) as exc:
        _b("logger", logger).exception("Failed to download Telegram document")
        await _b("safe_reply", safe_reply)(
            update.message, f"❌ Failed to download file: {exc}"
        )
        return
    caption = update.message.caption or ""
    agent_file_path = await _prepare_attachment_path(
        update,
        context,
        user,
        thread_id,
        wid,
        remote_target,
        kind="document",
        file_path=file_path,
        filename=document_name,
        caption=caption,
    )
    if agent_file_path is None:
        return
    text_to_send = (
        f"{caption}\n\n(file attached: {agent_file_path})"
        if caption
        else f"(file attached: {agent_file_path})"
    )
    await _deliver_media_or_voice_text(
        update,
        context,
        user,
        thread_id,
        wid,
        w,
        source="document_handler",
        text_to_send=text_to_send,
        success_reply=_b("FILE_CONFIRMATION_MESSAGE", FILE_CONFIRMATION_MESSAGE),
        queued_reply=_b("FILE_QUEUED_MESSAGE", FILE_QUEUED_MESSAGE),
    )


async def voice_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle voice messages: transcribe and forward text to Codex/Claude."""
    if not update.message or not update.message.voice:
        return
    user = update.effective_user
    if not user or not _b("is_user_allowed", is_user_allowed)(user.id):
        await _b("safe_reply", safe_reply)(
            update.message, "You are not authorized to use this bot."
        )
        return
    duration_seconds = getattr(update.message.voice, "duration", None)
    try:
        _b("validate_voice_duration", validate_voice_duration)(duration_seconds)
    except TranscriptionError as exc:
        await _b("safe_reply", safe_reply)(update.message, f"⚠ {exc}")
        return
    cfg = _b("config", config)
    if not cfg.transcription_openai_api_key and not cfg.transcription_google_api_key:
        await _b("safe_reply", safe_reply)(
            update.message,
            "⚠ Voice transcription requires an API key.\n"
            "Set `AI_TRANSCRIPTION_OPENAI_API_KEY` or `AI_TRANSCRIPTION_GOOGLE_API_KEY` "
            "in your `.env` file and restart the bot.",
        )
        return
    resolved = await _validate_media_topic_route(update, require_bound=True)
    if resolved is None:
        return
    user, thread_id, wid, _remote_target, w = resolved
    voice_file = await update.message.voice.get_file()
    ogg_data = bytes(await voice_file.download_as_bytearray())
    try:
        text = await _b("transcribe_voice", transcribe_voice)(
            ogg_data, duration_seconds=duration_seconds
        )
    except TranscriptionError as e:
        await _b("safe_reply", safe_reply)(update.message, f"⚠ {e}")
        return
    except Exception as e:
        _b("logger", logger).error("Voice transcription failed: %s", e)
        await _b("safe_reply", safe_reply)(
            update.message, f"⚠ Transcription failed: {e}"
        )
        return
    await _deliver_media_or_voice_text(
        update,
        context,
        user,
        thread_id,
        wid,
        w,
        source="voice_handler",
        text_to_send=text,
        success_reply=f'🎤 "{text}"',
        queued_reply=f'🎤 Queued: "{text}"',
        check_non_codex=False,
        offer_route_choice=False,
    )
