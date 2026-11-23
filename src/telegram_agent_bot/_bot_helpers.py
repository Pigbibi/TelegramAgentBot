"""Extracted bot runtime helpers (ADR 0001 Phase D)."""

from __future__ import annotations

from telegram_agent_bot._bot_shared import (
    AgentBrowser,
    AgentProfile,
    Any,
    ApplicationHandlerStop,
    BROWSE_DIRS_KEY,
    BROWSE_PAGE_KEY,
    BROWSE_PATH_KEY,
    BadRequest,
    CB_SESSION_CANCEL,
    CB_SESSION_NEW,
    CC_COMMANDS,
    Chat,
    ChatAction,
    CodexSession,
    ContextTypes,
    GroupChatRouteConflict,
    HTTPXRequest,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    NetworkError,
    PENDING_THREAD_MEDIA_KEY,
    PROFILE_MODELS_KEY,
    Path,
    ROOTS_KEY,
    SESSIONS_KEY,
    STATE_BROWSING_DIRECTORY,
    STATE_KEY,
    STATE_SELECTING_AGENT,
    STATE_SELECTING_PROFILE,
    STATE_SELECTING_ROOT,
    STATE_SELECTING_SESSION,
    STATE_SELECTING_WINDOW,
    TelegramError,
    TimedOut,
    TypedDict,
    UNBOUND_WINDOWS_KEY,
    Update,
    _b,
    active_remote_root_browser,
    adapt_forward_command_for_agent,
    agent_backend,
    browser_for_backend_id,
    build_agent_picker,
    build_backend_directory_browser,
    build_backend_root_picker,
    build_directory_browser,
    build_profile_picker,
    build_project_root_picker,
    build_session_picker,
    build_window_picker,
    cast,
    clear_browse_state,
    clear_permanent_delivery_failure,
    clear_profile_picker_state,
    clear_session_picker_state,
    config,
    default_directory_browser_path,
    default_profile_permission,
    logger,
    normalize_forward_command_text,
    profile_effort_values,
    profile_from_context,
    profile_models,
    resolve_profile_effort,
    safe_edit,
    safe_reply,
    sanitize_forward_text,
    session_manager,
    session_monitor,
    tmux_manager,
    upload_agent_file,
)


def _call__create_and_bind_window():
    from telegram_agent_bot._bot_text_recovery import _create_and_bind_window as _fn

    return _b("_create_and_bind_window", _fn)


async def _safe_send_typing_action(chat: Chat, *, source: str) -> None:
    """Send typing action best-effort without aborting the handler."""
    try:
        await chat.send_action(ChatAction.TYPING)
    except TelegramError as exc:
        _b("logger", logger).debug("Failed to send typing action (%s): %s", source, exc)


def _normalize_forward_command_text(cmd_text: str) -> str:
    """Normalize Telegram slash commands before forwarding them to an agent."""
    return _b("normalize_forward_command_text", normalize_forward_command_text)(
        cmd_text, known_commands=_b("CC_COMMANDS", CC_COMMANDS)
    )


def _adapt_forward_command_for_agent(cmd_text: str, agent_type: str) -> str:
    """Map one stable Telegram command to the active agent's native spelling."""
    return _b("adapt_forward_command_for_agent", adapt_forward_command_for_agent)(
        cmd_text,
        agent_type,
        known_commands=_b("CC_COMMANDS", CC_COMMANDS),
    )


class _PendingThreadMedia(TypedDict):
    """Attachment retained while a new topic is choosing its first session."""

    kind: str
    path: str
    filename: str
    caption: str


def _default_directory_browser_path(root_path: str | None = None) -> str:
    """Choose a generic starting directory for the project browser."""
    return _b("default_directory_browser_path", default_directory_browser_path)(
        root_path,
        default_projects_path=_b("config", config).default_projects_path,
    )


def _active_remote_root_browser() -> AgentBrowser | None:
    """Return the configured non-local backend browser for root selection."""
    return _b("active_remote_root_browser", active_remote_root_browser)(
        _b("agent_backend", agent_backend)
    )


def _browser_for_backend_id(backend_id: str) -> AgentBrowser | None:
    """Return the active backend browser matching a cached root selection."""
    return _b("browser_for_backend_id", browser_for_backend_id)(
        backend_id, _b("agent_backend", agent_backend)
    )


async def _build_backend_directory_browser(
    *,
    backend_id: str,
    node_id: str,
    path: str,
    page: int = 0,
    root_label: str | None = None,
    root_path: str | None = None,
) -> tuple[str, InlineKeyboardMarkup, list[str], str] | None:
    """Build directory browser UI through an optional remote backend."""
    browser = _b("_browser_for_backend_id", _browser_for_backend_id)(backend_id)
    if browser is None:
        return None
    return await _b("build_backend_directory_browser", build_backend_directory_browser)(
        backend_id=backend_id,
        node_id=node_id,
        path=path,
        page=page,
        root_label=root_label,
        root_path=root_path,
        browser=browser,
    )


async def _show_root_or_directory_picker(
    target: Any,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    edit: bool = False,
) -> None:
    """Show the configured root picker or fall through to directory browsing."""

    browser = _b("_active_remote_root_browser", _active_remote_root_browser)()
    if browser is not None:
        try:
            backend_roots = await browser.list_roots()
        except Exception:
            _b("logger", logger).exception("Unable to list backend roots")
            backend_roots = []
        if backend_roots:
            msg_text, keyboard, roots = _b(
                "build_backend_root_picker", build_backend_root_picker
            )(backend_roots)
            if context.user_data is not None:
                context.user_data[_b("STATE_KEY", STATE_KEY)] = _b(
                    "STATE_SELECTING_ROOT", STATE_SELECTING_ROOT
                )
                context.user_data[_b("ROOTS_KEY", ROOTS_KEY)] = roots
            if edit:
                await _b("safe_edit", safe_edit)(
                    target, msg_text, reply_markup=keyboard
                )
            else:
                await _b("safe_reply", safe_reply)(
                    target, msg_text, reply_markup=keyboard
                )
            return

    if getattr(_b("config", config), "project_roots_configured", False):
        msg_text, keyboard, roots = _b(
            "build_project_root_picker", build_project_root_picker
        )(_b("config", config).project_roots)
        if context.user_data is not None:
            context.user_data[_b("STATE_KEY", STATE_KEY)] = _b(
                "STATE_SELECTING_ROOT", STATE_SELECTING_ROOT
            )
            context.user_data[_b("ROOTS_KEY", ROOTS_KEY)] = roots
        if edit:
            await _b("safe_edit", safe_edit)(target, msg_text, reply_markup=keyboard)
        else:
            await _b("safe_reply", safe_reply)(target, msg_text, reply_markup=keyboard)
        return

    start_path = _b(
        "_default_directory_browser_path", _default_directory_browser_path
    )()
    msg_text, keyboard, subdirs = _b(
        "build_directory_browser", build_directory_browser
    )(start_path)
    if context.user_data is not None:
        context.user_data[_b("STATE_KEY", STATE_KEY)] = _b(
            "STATE_BROWSING_DIRECTORY", STATE_BROWSING_DIRECTORY
        )
        context.user_data[_b("BROWSE_PATH_KEY", BROWSE_PATH_KEY)] = start_path
        context.user_data[_b("BROWSE_PAGE_KEY", BROWSE_PAGE_KEY)] = 0
        context.user_data[_b("BROWSE_DIRS_KEY", BROWSE_DIRS_KEY)] = subdirs
    if edit:
        await _b("safe_edit", safe_edit)(target, msg_text, reply_markup=keyboard)
    else:
        await _b("safe_reply", safe_reply)(target, msg_text, reply_markup=keyboard)


def _clear_pending_thread_input(user_data: dict | None) -> None:
    """Forget the first message retained while a topic is being configured."""
    if user_data is not None:
        user_data.pop("_pending_thread_id", None)
        user_data.pop("_pending_thread_text", None)
        user_data.pop(_b("PENDING_THREAD_MEDIA_KEY", PENDING_THREAD_MEDIA_KEY), None)


async def _start_unbound_topic_creation(
    message: Any,
    context: ContextTypes.DEFAULT_TYPE,
    user: Any,
    thread_id: int,
    *,
    text: str | None = None,
) -> None:
    """Open the normal session picker and retain the topic's first input."""
    if context.user_data is not None:
        context.user_data["_pending_thread_id"] = thread_id
        if text is not None:
            context.user_data["_pending_thread_text"] = text
            context.user_data.pop(
                _b("PENDING_THREAD_MEDIA_KEY", PENDING_THREAD_MEDIA_KEY), None
            )

    if getattr(_b("config", config), "project_roots_configured", False) or (
        _b("_active_remote_root_browser", _active_remote_root_browser)() is not None
    ):
        _b("logger", logger).info(
            "Unbound topic: showing configured project root picker (user=%d, thread=%d)",
            user.id,
            thread_id,
        )
        await _b("_show_root_or_directory_picker", _show_root_or_directory_picker)(
            message, context
        )
        return

    all_windows = await _b("tmux_manager", tmux_manager).list_windows()
    bound_ids = {
        wid
        for _, _, wid in _b("session_manager", session_manager).iter_thread_bindings()
    }
    bindable_unbound = [
        (w.window_id, w.window_name, w.cwd)
        for w in all_windows
        if w.window_id not in bound_ids
        and _b("_has_trackable_session_for_window", _has_trackable_session_for_window)(
            w.window_id
        )
    ]
    _b("logger", logger).debug(
        "Window picker check: all=%s, bound=%s, bindable_unbound=%s",
        [w.window_name for w in all_windows],
        bound_ids,
        [name for _, name, _ in bindable_unbound],
    )

    if bindable_unbound:
        _b("logger", logger).info(
            "Unbound topic: showing window picker (%d bindable windows, user=%d, thread=%d)",
            len(bindable_unbound),
            user.id,
            thread_id,
        )
        msg_text, keyboard, win_ids = _b("build_window_picker", build_window_picker)(
            bindable_unbound
        )
        if context.user_data is not None:
            context.user_data[_b("STATE_KEY", STATE_KEY)] = _b(
                "STATE_SELECTING_WINDOW", STATE_SELECTING_WINDOW
            )
            context.user_data[_b("UNBOUND_WINDOWS_KEY", UNBOUND_WINDOWS_KEY)] = win_ids
        await _b("safe_reply", safe_reply)(message, msg_text, reply_markup=keyboard)
        return

    _b("logger", logger).info(
        "Unbound topic: showing project root or directory picker (user=%d, thread=%d)",
        user.id,
        thread_id,
    )
    await _b("_show_root_or_directory_picker", _show_root_or_directory_picker)(
        message, context
    )


def _store_pending_thread_media(
    context: ContextTypes.DEFAULT_TYPE,
    *,
    kind: str,
    path: Path,
    filename: str,
    caption: str,
) -> None:
    """Keep a downloaded first attachment until the topic selects a session."""
    if context.user_data is None:
        return
    context.user_data.pop("_pending_thread_text", None)
    context.user_data[_b("PENDING_THREAD_MEDIA_KEY", PENDING_THREAD_MEDIA_KEY)] = {
        "kind": kind,
        "path": str(path),
        "filename": filename,
        "caption": caption,
    }


async def _take_pending_thread_message(
    context: ContextTypes.DEFAULT_TYPE,
    *,
    user_id: int,
    thread_id: int,
    remote: bool,
) -> tuple[str | None, str | None]:
    """Return the retained first input, uploading an attachment when needed."""
    if context.user_data is None:
        return None, None

    pending_text = context.user_data.pop("_pending_thread_text", None)
    pending_media = context.user_data.pop(
        _b("PENDING_THREAD_MEDIA_KEY", PENDING_THREAD_MEDIA_KEY), None
    )
    if pending_text:
        return _b("sanitize_forward_text", sanitize_forward_text)(pending_text), None
    if not isinstance(pending_media, dict):
        return None, None

    pending_media = cast(_PendingThreadMedia, pending_media)
    kind = pending_media.get("kind")
    source_path = pending_media.get("path")
    filename = pending_media.get("filename")
    caption = pending_media.get("caption") or ""
    if (
        kind not in {"photo", "document"}
        or not isinstance(source_path, str)
        or not source_path
        or not isinstance(filename, str)
        or not filename
    ):
        return None, "The saved attachment is invalid. Please send it again."
    if not Path(source_path).is_file():
        return (
            None,
            "The saved attachment is no longer available. Please send it again.",
        )

    agent_file_path = source_path
    if remote:
        upload = await _b("upload_agent_file", upload_agent_file)(
            user_id,
            thread_id,
            "",
            source_path,
            filename=filename,
        )
        if upload is None or not upload.ok or not upload.path:
            detail = upload.message if upload is not None else "backend unavailable"
            return None, f"File transfer failed: {detail or 'backend unavailable'}"
        agent_file_path = upload.path

    attachment_kind = "image" if kind == "photo" else "file"
    suffix = f"({attachment_kind} attached: {agent_file_path})"
    return f"{caption}\n\n{suffix}" if caption else suffix, None


def _build_request(
    *,
    connect_timeout: float,
    read_timeout: float,
    write_timeout: float,
    pool_timeout: float,
) -> HTTPXRequest:
    """Build a Telegram HTTP client with explicit timeouts."""
    return HTTPXRequest(
        connect_timeout=connect_timeout,
        read_timeout=read_timeout,
        write_timeout=write_timeout,
        pool_timeout=pool_timeout,
    )


async def application_error_handler(
    update: object,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Log Telegram polling/handler errors with lightweight update context."""
    error = context.error
    if not isinstance(update, Update) and isinstance(error, (NetworkError, TimedOut)):
        _b("logger", logger).warning(
            "Transient Telegram polling error; polling will retry automatically: %s",
            error,
        )
        return
    if isinstance(error, BadRequest):
        message = str(error).lower()
        if "query is too old" in message or "query id is invalid" in message:
            _b("logger", logger).info(
                "Ignored expired Telegram callback query: %s", error
            )
            return

    update_type = type(update).__name__
    chat_id = None
    thread_id = None

    if isinstance(update, Update):
        if update.effective_chat:
            chat_id = update.effective_chat.id
        message = update.effective_message
        if message is not None:
            thread_id = getattr(message, "message_thread_id", None)

    _b("logger", logger).exception(
        "Telegram application error: update_type=%s chat_id=%s thread_id=%s",
        update_type,
        chat_id,
        thread_id,
        exc_info=(
            type(error),
            error,
            error.__traceback__,
        )
        if error
        else None,
    )


def is_user_allowed(user_id: int | None) -> bool:
    return user_id is not None and _b("config", config).is_user_allowed(user_id)


def _get_thread_id(update: Update) -> int | None:
    """Extract thread_id from an update, returning None if not in a named topic."""
    msg = update.message or (
        update.callback_query.message if update.callback_query else None
    )
    if msg is None:
        return None
    tid = getattr(msg, "message_thread_id", None)
    if tid is None or tid == 1:
        return None
    return tid


async def inbound_group_route_guard(
    update: Update,
    _context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Claim a group route before callbacks, health updates or input side effects."""
    user = update.effective_user
    chat = update.effective_chat
    if (
        not user
        or not _b("is_user_allowed", is_user_allowed)(user.id)
        or chat is None
        or chat.type not in ("group", "supergroup")
    ):
        return
    thread_id = _b("_get_thread_id", _get_thread_id)(update)
    if thread_id is None:
        return
    try:
        # Synchronous claim: another chat cannot win between validation and use.
        _b("session_manager", session_manager).set_group_chat_id(
            user.id, thread_id, chat.id
        )
    except GroupChatRouteConflict:
        _b("logger", logger).warning("Rejected a topic route owned by another group")
        raise ApplicationHandlerStop from None


async def inbound_route_health_handler(
    update: Update,
    _context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Treat a fresh inbound topic message as proof that its route is usable."""
    user = update.effective_user
    message = update.effective_message
    chat = update.effective_chat
    if (
        not user
        or not _b("is_user_allowed", is_user_allowed)(user.id)
        or message is None
        or chat is None
        or chat.type not in ("group", "supergroup")
        or getattr(message, "forum_topic_closed", None) is not None
    ):
        return
    thread_id = _b("_get_thread_id", _get_thread_id)(update)
    if thread_id is None:
        return

    _b("session_manager", session_manager).set_group_chat_id(
        user.id, thread_id, chat.id
    )
    if _b("clear_permanent_delivery_failure", clear_permanent_delivery_failure)(
        user.id, thread_id, chat_id=chat.id
    ):
        monitor = _b("session_monitor", session_monitor)
        clear_block = getattr(monitor, "clear_permanent_delivery_block", None)
        if callable(clear_block):
            clear_block(user.id, thread_id)
        _b("logger", logger).info(
            "Cleared permanent Telegram delivery block after inbound route proof "
            "(user=%d thread=%d chat=%d)",
            user.id,
            thread_id,
            chat.id,
        )


def _filter_resumable_sessions(
    sessions: list["CodexSession"],
) -> list["CodexSession"]:
    """Hide sessions that are already active in another Telegram topic."""
    return [
        session
        for session in sessions
        if not _b("session_manager", session_manager).has_bound_thread_for_session(
            session.session_id
        )
    ]


def _has_trackable_session_for_window(window_id: str) -> bool:
    """Return whether an existing tmux window already has a known Codex session."""
    state = _b("session_manager", session_manager).window_states.get(window_id)
    return bool(state and state.session_id)


def _build_resume_conflict_keyboard() -> InlineKeyboardMarkup:
    """Offer safe next steps after blocking a duplicate resume."""
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "➕ New Session", callback_data=_b("CB_SESSION_NEW", CB_SESSION_NEW)
                ),
                InlineKeyboardButton(
                    "Cancel", callback_data=_b("CB_SESSION_CANCEL", CB_SESSION_CANCEL)
                ),
            ]
        ]
    )


def _profile_models(agent_type: str) -> tuple[str, ...]:
    """Return configured model choices for one agent type."""
    return _b("profile_models", profile_models)(agent_type, cfg=_b("config", config))


def _profile_effort_values(agent_type: str, model: str) -> tuple[str, ...]:
    """Return reasoning efforts supported by the selected agent/model."""
    return _b("profile_effort_values", profile_effort_values)(
        agent_type, model, cfg=_b("config", config)
    )


def _resolve_profile_effort(agent_type: str, model: str, requested: str) -> str:
    """Keep a supported effort or fall back to the model/provider default."""
    return _b("resolve_profile_effort", resolve_profile_effort)(
        agent_type,
        model,
        requested,
        cfg=_b("config", config),
        effort_values=_b("_profile_effort_values", _profile_effort_values),
    )


def _profile_from_context(user_data: dict | None) -> AgentProfile:
    """Build the pending topic profile, retaining legacy defaults."""
    return _b("profile_from_context", profile_from_context)(
        user_data,
        cfg=_b("config", config),
        resolve_effort=_b("_resolve_profile_effort", _resolve_profile_effort),
        default_permission_fn=_b(
            "_default_profile_permission", _default_profile_permission
        ),
    )


def _default_profile_permission(agent_type: str) -> str:
    """Return the configured default permission mode for one agent type."""
    return _b("default_profile_permission", default_profile_permission)(
        agent_type, cfg=_b("config", config)
    )


async def _show_agent_profile_picker(
    target: Any,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    edit: bool = True,
) -> None:
    """Show the agent selector before a new topic creates a session."""
    text, keyboard = _b("build_agent_picker", build_agent_picker)()
    if context.user_data is not None:
        context.user_data[_b("STATE_KEY", STATE_KEY)] = _b(
            "STATE_SELECTING_AGENT", STATE_SELECTING_AGENT
        )
    if edit:
        await _b("safe_edit", safe_edit)(target, text, reply_markup=keyboard)
    else:
        await _b("safe_reply", safe_reply)(target, text, reply_markup=keyboard)


async def _show_agent_profile_settings(
    target: Any,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    edit: bool = True,
) -> None:
    """Show model and reasoning controls for the selected agent."""
    profile = _b("_profile_from_context", _profile_from_context)(context.user_data)
    models = _b("_profile_models", _profile_models)(profile.agent_type)
    if context.user_data is not None:
        context.user_data[_b("STATE_KEY", STATE_KEY)] = _b(
            "STATE_SELECTING_PROFILE", STATE_SELECTING_PROFILE
        )
        context.user_data[_b("PROFILE_MODELS_KEY", PROFILE_MODELS_KEY)] = list(models)
    text, keyboard = _b("build_profile_picker", build_profile_picker)(
        profile,
        models,
        effort_values=_b("_profile_effort_values", _profile_effort_values)(
            profile.agent_type, profile.model
        ),
    )
    if edit:
        await _b("safe_edit", safe_edit)(target, text, reply_markup=keyboard)
    else:
        await _b("safe_reply", safe_reply)(target, text, reply_markup=keyboard)


def _clear_creation_state(user_data: dict | None) -> None:
    """Clear all transient topic-creation state."""
    _b("clear_browse_state", clear_browse_state)(user_data)
    _b("clear_session_picker_state", clear_session_picker_state)(user_data)
    _b("clear_profile_picker_state", clear_profile_picker_state)(user_data)
    if user_data is not None:
        _b("_clear_pending_thread_input", _clear_pending_thread_input)(user_data)
        for key in (
            "_selected_path",
            "_selected_backend_id",
            "_selected_node_id",
        ):
            user_data.pop(key, None)


def _creation_path(user_data: dict | None, key: str) -> str | None:
    """Return an explicit project selection, never the service's cwd."""
    path = user_data.get(key) if user_data else None
    return path if isinstance(path, str) and path.strip() else None


async def _reject_expired_project_selection(
    query: Any, context: ContextTypes.DEFAULT_TYPE
) -> None:
    _b("_clear_creation_state", _clear_creation_state)(context.user_data)
    await query.answer("Project selection expired", show_alert=True)
    await _b("safe_edit", safe_edit)(
        query,
        "⚠️ Project selection expired. Send your message again to choose a project.",
    )


async def _continue_creation_with_profile(
    query: Any,
    context: ContextTypes.DEFAULT_TYPE,
    user: Any,
) -> None:
    """Find a resumable session or create a new window for the chosen profile."""
    user_data = context.user_data
    selected_path = _b("_creation_path", _creation_path)(user_data, "_selected_path")
    if selected_path is None:
        await _b(
            "_reject_expired_project_selection", _reject_expired_project_selection
        )(query, context)
        return
    backend_id = user_data.get("_selected_backend_id", "") if user_data else ""
    node_id = user_data.get("_selected_node_id", "") if user_data else ""
    pending_thread_id = user_data.get("_pending_thread_id") if user_data else None

    profile = _b("_profile_from_context", _profile_from_context)(user_data)
    await query.answer("Looking for sessions...")
    await _b("safe_edit", safe_edit)(
        query, "⏳ Looking for existing sessions in this directory..."
    )

    if backend_id:
        browser = _b("_browser_for_backend_id", _browser_for_backend_id)(backend_id)
        if browser is None:
            await _b("safe_edit", safe_edit)(
                query, "Backend browser unavailable. Please retry."
            )
            return
        try:
            sessions = _b("_filter_resumable_sessions", _filter_resumable_sessions)(
                await browser.list_sessions(node_id, selected_path)
            )
        except Exception:
            _b("logger", logger).exception(
                "Backend session lookup failed: backend=%s node=%s path=%s",
                backend_id,
                node_id,
                selected_path,
            )
            await _b("safe_edit", safe_edit)(
                query, "Unable to read sessions from backend."
            )
            return
    else:
        sessions = _b("_filter_resumable_sessions", _filter_resumable_sessions)(
            await _b("session_manager", session_manager).list_sessions_for_directory(
                selected_path
            )
        )

    if sessions:
        if user_data is not None:
            user_data[_b("STATE_KEY", STATE_KEY)] = _b(
                "STATE_SELECTING_SESSION", STATE_SELECTING_SESSION
            )
            user_data[_b("SESSIONS_KEY", SESSIONS_KEY)] = sessions
        text, keyboard = _b("build_session_picker", build_session_picker)(sessions)
        await _b("safe_edit", safe_edit)(query, text, reply_markup=keyboard)
        return

    create_kwargs: dict[str, Any] = {
        "answer_callback": False,
        "agent_type": profile.agent_type,
        "model": profile.model,
        "reasoning_effort": profile.reasoning_effort,
        "fast_mode": profile.fast_mode,
        "permission_mode": profile.permission_mode,
    }
    if node_id:
        create_kwargs["node_id"] = node_id
    await _call__create_and_bind_window()(
        query,
        context,
        user,
        selected_path,
        pending_thread_id,
        **create_kwargs,
    )
