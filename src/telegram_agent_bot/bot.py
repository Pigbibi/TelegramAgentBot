"""Telegram Agent Bot runtime and compatibility facade."""

from __future__ import annotations

from telegram_agent_bot._bot_shared import (  # noqa: F401
    ACCOUNT_COMMAND_DESCRIPTION,
    AGENT_AUTH_RECOVERY_MESSAGE,
    AGENT_CLAUDE,
    AGENT_CLAUDE_OFFICIAL,
    AGENT_CODEX,
    AGENT_CURSOR,
    AGENT_INPUT_MODE_QUEUE,
    AGENT_INPUT_MODE_STEER,
    AGENT_INPUT_MODE_TURN,
    AGENT_INPUT_QUEUED,
    AGENT_INPUT_SUBMITTED_UNCONFIRMED,
    AGENT_LOGIN_COMMAND_DESCRIPTION,
    AGENT_LOGIN_TIMEOUT_SECONDS,
    AIORateLimiter,
    ASK_REFRESH_ACTIONS,
    AgentBackend,
    AgentBrowser,
    AgentInputMode,
    AgentProfile,
    AgentTarget,
    Any,
    Application,
    ApplicationHandlerStop,
    Awaitable,
    BACKGROUND_WAIT_TOOL_STATUS_TEXT,
    BROWSE_DIRS_KEY,
    BROWSE_PAGE_KEY,
    BROWSE_PATH_KEY,
    BadRequest,
    Bot,
    BotCommand,
    CB_ASK_ESC,
    CB_ASK_REFRESH,
    CB_CODEX_UPDATE_APPLY,
    CB_CODEX_UPDATE_DISMISS,
    CB_DIR_CANCEL,
    CB_DIR_CONFIRM,
    CB_DIR_PAGE,
    CB_DIR_SELECT,
    CB_DIR_UP,
    CB_HISTORY_NEXT,
    CB_HISTORY_PREV,
    CB_INPUT_ROUTE,
    CB_KEYS_PREFIX,
    CB_OUTPUT_MODE,
    CB_PROFILE_AGENT,
    CB_PROFILE_CANCEL,
    CB_PROFILE_CONFIRM,
    CB_PROFILE_EFFORT,
    CB_PROFILE_FAST,
    CB_PROFILE_MODEL,
    CB_PROFILE_PERMISSION,
    CB_ROOT_CANCEL,
    CB_ROOT_SELECT,
    CB_SCREENSHOT_REFRESH,
    CB_SESSION_CANCEL,
    CB_SESSION_NEW,
    CB_SESSION_SELECT,
    CB_WIN_BIND,
    CB_WIN_CANCEL,
    CB_WIN_NEW,
    CC_COMMANDS,
    COMMAND_ARG_SEPARATORS,
    Callable,
    CallbackContext,
    CallbackQueryHandler,
    Chat,
    ChatAction,
    CodexSession,
    CodexUpdateResult,
    CommandHandler,
    ContextTypes,
    DEFAULT_REQUEST_CONNECT_TIMEOUT_SECONDS,
    DEFAULT_REQUEST_POOL_TIMEOUT_SECONDS,
    DEFAULT_REQUEST_READ_TIMEOUT_SECONDS,
    DEFAULT_REQUEST_WRITE_TIMEOUT_SECONDS,
    DirectoryBrowserKwargs,
    DurableRuntimeStore,
    ESC_COMMAND_DESCRIPTION,
    FILE_CONFIRMATION_MESSAGE,
    FILE_QUEUED_MESSAGE,
    FORWARDED_COMMAND_RE,
    GET_UPDATES_CONNECT_TIMEOUT_SECONDS,
    GET_UPDATES_POOL_TIMEOUT_SECONDS,
    GET_UPDATES_READ_TIMEOUT_SECONDS,
    GET_UPDATES_WRITE_TIMEOUT_SECONDS,
    GroupChatRouteConflict,
    HELP_COMMAND_DESCRIPTION,
    HTTPXRequest,
    INPUT_ROUTE_CANCEL,
    INPUT_ROUTE_INTERRUPT,
    INPUT_ROUTE_QUEUE,
    INTERACTIVE_TOOL_NAMES,
    INTERRUPT_COMMAND_DESCRIPTION,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    MEDIA_DOWNLOAD_ATTEMPTS,
    MEDIA_DOWNLOAD_CONNECT_TIMEOUT_SECONDS,
    MEDIA_DOWNLOAD_POOL_TIMEOUT_SECONDS,
    MEDIA_DOWNLOAD_READ_TIMEOUT_SECONDS,
    MEDIA_DOWNLOAD_RETRY_DELAY_SECONDS,
    MEDIA_DOWNLOAD_WRITE_TIMEOUT_SECONDS,
    MessageHandler,
    NO_LINK_PREVIEW,
    NetworkError,
    NewMessage,
    OUTPUT_MODE_CLEAN,
    OUTPUT_MODE_TRACE,
    PENDING_THREAD_MEDIA_KEY,
    PHOTO_CONFIRMATION_MESSAGE,
    PHOTO_QUEUED_MESSAGE,
    POLL_TIMEOUT_SECONDS,
    PRODUCT_NAME,
    PROFILE_MODELS_KEY,
    Path,
    PendingAgentInput,
    QUEUE_COMMAND_DESCRIPTION,
    ROOTS_KEY,
    RootSelection,
    SESSIONS_KEY,
    SESSION_STILL_RUNNING_MESSAGE,
    STATE_BROWSING_DIRECTORY,
    STATE_KEY,
    STATE_SELECTING_AGENT,
    STATE_SELECTING_PROFILE,
    STATE_SELECTING_ROOT,
    STATE_SELECTING_SESSION,
    STATE_SELECTING_WINDOW,
    STEER_COMMAND_DESCRIPTION,
    TRANSIENT_RETRY_SCHEDULED,
    TelegramError,
    TimedOut,
    TopicUpdateProcessor,
    TranscriptParser,
    TranscriptionError,
    TransientAgentRetry,
    TypeHandler,
    TypedDict,
    UNBOUND_WINDOWS_KEY,
    UNSUPPORTED_CONTENT_MESSAGE,
    USAGE_COMMAND_DESCRIPTION,
    Update,
    VpsHealthMonitor,
    WELCOME_MESSAGE,
    _AGENT_INPUT_CONFIRM_MAX_WAIT_SECONDS,
    _AGENT_INPUT_CONFIRM_RETRY_SECONDS,
    _AGENT_INPUT_PERSIST_RETRY_SECONDS,
    _AGENT_INPUT_POLL_INTERVAL_SECONDS,
    _AGENT_LOGIN_DEFAULT_KEY,
    _ANSI_ESCAPE_RE,
    _CODEX_UPDATE_PROMPT_STATE_FILENAME,
    _COMMAND_ARG_SEPARATORS,
    _DEVICE_LOGIN_CODE_RE,
    _DEVICE_LOGIN_URL_RE,
    _DirectoryBrowserKwargs,
    _FILES_DIR,
    _FORWARDED_COMMAND_RE,
    _IMAGES_DIR,
    _INPUT_ROUTE_CHOICE_MAX_AGE_SECONDS,
    _KEYS_SEND_MAP,
    _KEY_LABELS,
    _PENDING_TOPIC_DELETIONS_FILE,
    _QueuedAgentInput,
    _RootSelection,
    _SAFE_UPLOAD_FILENAME_RE,
    _TRANSIENT_AGENT_ERROR_CODES,
    _TRANSIENT_AGENT_RETRY_DELAYS_SECONDS,
    _TRANSIENT_AGENT_RETRY_PROMPT,
    _agent_input_confirmation_modes,
    _agent_input_confirmation_targets,
    _agent_input_confirmation_tasks,
    _agent_input_locks,
    _agent_input_queues,
    _agent_input_tasks,
    _agent_login_tasks,
    _as_browser,
    _auto_update_task,
    _bash_capture_tasks,
    _browse_backend_context,
    _browse_root_context,
    _clamp_to_selected_root,
    _codex_update_apply_lock,
    _codex_update_prompted_versions,
    _directory_browser_kwargs,
    _directory_listing_with_root,
    _health_monitor,
    _health_task,
    _parse_root_selection,
    _remote_child_path,
    _remote_parent_path,
    _runtime_stopped,
    _runtime_store,
    _safe_send_typing_action,
    _session_ids_match,
    _status_poll_task,
    _transient_agent_retry_attempts,
    _transient_agent_retry_tasks,
    active_remote_root_browser,
    adapt_forward_command_for_agent,
    agent_backend,
    agent_capabilities,
    agent_display_name,
    app_dir,
    as_browser,
    asyncio,
    atomic_write_json,
    browse_backend_context,
    browse_root_context,
    browser_for_backend_id,
    build_agent_picker,
    build_backend_directory_browser,
    build_backend_root_picker,
    build_directory_browser,
    build_input_routing_keyboard,
    build_profile_picker,
    build_project_root_picker,
    build_response_parts,
    build_session_picker,
    build_window_picker,
    capture_agent_output,
    cast,
    check_codex_update,
    clamp_to_selected_root,
    clear_browse_state,
    clear_current_account,
    clear_interactive_mode,
    clear_interactive_msg,
    clear_permanent_delivery_failure,
    clear_profile_picker_state,
    clear_root_picker_state,
    clear_session_picker_state,
    clear_status_msg_info,
    clear_topic_state,
    clear_window_picker_state,
    clear_window_working,
    close_transcribe_client,
    config,
    contextlib,
    convert_markdown,
    copy,
    create_agent_session,
    dataclass,
    default_directory_browser_path,
    default_profile_permission,
    deque,
    directory_browser_kwargs,
    directory_listing_with_root,
    enqueue_content_message,
    enqueue_status_update,
    extract_auth_error_message,
    extract_bash_output,
    extract_interactive_content,
    field,
    filters,
    forget_missing_bound_window,
    format_health_snapshot,
    get_configured_backend,
    get_current_account_name,
    get_default_account_name,
    get_interactive_msg_id,
    get_interactive_window,
    get_message_queue,
    get_next_account_name,
    handle_ask_control,
    handle_ask_esc,
    handle_ask_refresh,
    handle_codex_update_apply,
    handle_codex_update_dismiss,
    handle_dir_cancel,
    handle_dir_confirm,
    handle_dir_page,
    handle_dir_select,
    handle_dir_up,
    handle_history_page,
    handle_input_route,
    handle_interactive_ui,
    handle_noop,
    handle_output_mode,
    handle_profile_agent,
    handle_profile_cancel,
    handle_profile_confirm,
    handle_profile_effort,
    handle_profile_fast,
    handle_profile_model,
    handle_profile_permission,
    handle_quick_keys,
    handle_root_cancel,
    handle_root_select,
    handle_screenshot_refresh,
    handle_session_cancel,
    handle_session_new,
    handle_session_select,
    handle_window_bind,
    handle_window_cancel,
    handle_window_new,
    idle_session_hibernator,
    io,
    is_agent_cli_startup_error,
    is_claude_agent,
    is_codex_input_ready,
    is_interactive_ui,
    is_shell_pane_command,
    is_valid_account_name,
    json,
    list_account_names,
    load_codex_update_settings,
    load_update_env,
    logger,
    logging,
    mark_window_working,
    normalize_agent_type,
    normalize_forward_command_text,
    normalize_output_mode,
    os,
    output_mode_label,
    parse_root_selection,
    parse_status_update,
    prepare_account_home,
    profile_effort_values,
    profile_from_context,
    profile_models,
    re,
    refresh_model_catalog,
    remember_current_account,
    remote_child_path,
    remote_parent_path,
    resolve_profile_effort,
    runtime_logger,
    safe_edit,
    safe_reply,
    safe_send,
    sanitize_forward_text,
    save_account_snapshot,
    send_agent_control,
    send_agent_input,
    send_agent_message,
    send_history,
    send_with_fallback,
    session_manager,
    session_monitor,
    set_interactive_mode,
    shlex,
    shutdown_workers,
    shutil,
    status_poll_loop,
    sys,
    text_to_image,
    time,
    tmux_manager,
    transcribe_voice,
    turn_admission,
    upload_agent_file,
    validate_voice_duration,
)
from telegram_agent_bot._bot_helpers import (  # noqa: F401
    _normalize_forward_command_text,
    _adapt_forward_command_for_agent,
    _PendingThreadMedia,
    _default_directory_browser_path,
    _active_remote_root_browser,
    _browser_for_backend_id,
    _build_backend_directory_browser,
    _show_root_or_directory_picker,
    _clear_pending_thread_input,
    _start_unbound_topic_creation,
    _store_pending_thread_media,
    _take_pending_thread_message,
    _build_request,
    application_error_handler,
    is_user_allowed,
    _get_thread_id,
    inbound_group_route_guard,
    inbound_route_health_handler,
    _filter_resumable_sessions,
    _has_trackable_session_for_window,
    _build_resume_conflict_keyboard,
    _profile_models,
    _profile_effort_values,
    _resolve_profile_effort,
    _profile_from_context,
    _default_profile_permission,
    _show_agent_profile_picker,
    _show_agent_profile_settings,
    _clear_creation_state,
    _creation_path,
    _reject_expired_project_selection,
    _continue_creation_with_profile,
)
from telegram_agent_bot._bot_login import (  # noqa: F401
    _requested_agent_type,
    _agent_command_name,
    _agent_login_executable,
    _agent_login_args,
    _extract_device_login_details,
    _login_display_name,
    _codex_auth_recovery_message,
    _account_command_usage,
    _format_account_status,
    _wait_for_agent_login_details,
    _agent_login_worker,
    _agent_login_command,
    agent_login_command,
    codex_login_command,
    claude_login_command,
    cursor_login_command,
    _agent_account_command,
    agent_account_command,
    codex_account_command,
    claude_account_command,
)
from telegram_agent_bot._bot_input_routing import (  # noqa: F401
    _build_screenshot_keyboard,
    _send_control_to_agent,
    _send_message_to_agent,
    _send_native_input_to_agent,
    _window_agent_type,
    _window_supports_native_input_routing,
    _route_can_use_native_input,
    _offer_input_route_choice,
    _agent_input_key,
    _should_offer_input_route_choice,
    _agent_input_lock,
    _reset_transient_agent_retry,
    _agent_input_queue_max_size,
    _agent_input_queue_max_wait_seconds,
    _agent_max_active_turns,
    _queue_agent_input,
    _delete_persisted_agent_inputs,
    _mark_persisted_agent_input_submitted,
    _confirm_persisted_agent_input,
    _clear_persisted_agent_input_target,
    _clear_persisted_agent_input_route,
    _same_agent_input_route,
    _is_current_bound_agent_input_target,
    _target_has_agent_input_confirmation,
    _route_has_agent_input_queue,
    _route_has_agent_input_drain,
    _target_has_persisted_agent_input_confirmation,
    _agent_input_confirmation_expired,
    _run_agent_input_confirmation,
    _ensure_agent_input_confirmation_task,
    _cancel_agent_input_confirmations_for_target,
    _restore_agent_input_queues,
    _drop_expired_agent_input,
    _ensure_agent_input_drain_task,
    _queue_agent_input_after_interrupt,
)
from telegram_agent_bot._bot_queue_drain import (  # noqa: F401
    _send_native_agent_input,
    _agent_cli_startup_failure,
    _send_or_queue_agent_input,
    _run_transient_agent_retry,
    _schedule_transient_agent_retry,
    _restore_transient_agent_retries,
    _cancel_transient_agent_retry_tasks,
    _notify_queued_input_failure,
    _notify_agent_input_confirmation_delayed,
    _notify_agent_input_confirmation_expired,
    _notify_rebound_agent_input_unconfirmed,
    _drain_agent_input_queue,
    _cancel_agent_input_drain_tasks,
    _cancel_agent_input_confirmation_tasks,
    _discard_queued_agent_input,
)
from telegram_agent_bot._bot_window_startup import (  # noqa: F401
    _cancel_bash_capture,
    _capture_bash_output,
    _rotate_thread_after_usage_limit,
    _maybe_confirm_startup_trust_prompt,
    _send_to_window_when_codex_ready,
    _send_or_queue_to_starting_window,
    _enable_fast_mode_if_requested,
    _refresh_session_map_after_first_prompt,
    _confirm_first_prompt_delivery,
)
from telegram_agent_bot._bot_commands import (  # noqa: F401
    _output_mode_keyboard,
    _output_mode_text,
    output_mode_command,
    start_command,
    history_command,
    screenshot_command,
    unbind_command,
    esc_command,
    interrupt_command,
    _native_input_command,
    steer_command,
    queue_command,
    health_command,
    usage_command,
)
from telegram_agent_bot._bot_media_topics import (  # noqa: F401
    _remote_target_for_thread,
    _format_remote_target,
    _create_agent_local_window,
    _create_agent_target,
    topic_closed_handler,
    topic_edited_handler,
    _send_local_forwarded_command,
    forward_command_handler,
    agent_command_mode,
    unsupported_content_handler,
    _safe_upload_filename,
    _download_telegram_media,
    process_pending_topic_deletions,
    photo_handler,
    document_handler,
    voice_handler,
)
from telegram_agent_bot._bot_text_recovery import (  # noqa: F401
    _recover_missing_bound_window,
    _handle_missing_bound_window_input,
    _recovered_agent_process_status,
    _wait_for_recovered_agent_process,
    _handle_non_codex_bound_window,
    _handle_auth_error_bound_window,
    text_handler,
    _create_and_bind_window,
)
from telegram_agent_bot._bot_callbacks import (  # noqa: F401
    _get_codex_update_apply_lock,
    _codex_update_prompt_key,
    _codex_update_prompt_state_file,
    _load_codex_update_prompted_versions,
    _save_codex_update_prompted_versions,
    _mark_codex_update_prompted,
    notify_codex_update_available,
    _apply_codex_update_from_callback,
    _safe_answer_callback,
    _handle_input_routing_callback,
    CallbackRegistry,
    build_default_callback_registry,
    callback_handler,
    _mark_transcript_message_delivered,
    _transcript_message_already_delivered,
    CallbackHandler,
    DEFAULT_CALLBACK_REGISTRY,
)
from telegram_agent_bot._bot_delivery import (  # noqa: F401
    handle_new_message,
)


async def post_init(application: Application) -> None:
    global agent_backend, session_monitor, _status_poll_task, _auto_update_task
    global _health_task
    global _runtime_stopped

    _runtime_stopped = False

    try:
        _runtime_store.initialize()
        session_manager.activate_durable_registry(_runtime_store)
        _runtime_store.delete_expired_input_choices(
            time.time() - _INPUT_ROUTE_CHOICE_MAX_AGE_SECONDS
        )
    except Exception:
        logger.exception(
            "Durable runtime store unavailable at startup; immediate agent input "
            "remains available but busy inputs cannot be queued safely"
        )

    runtime_logger.info(
        "Runtime safeguards: agent_input_queue_max_size=%d "
        "agent_input_queue_max_wait_seconds=%.1f agent_max_active_turns=%d "
        "health_alert_cooldown_seconds=%.1f "
        "health_queue_oldest_seconds=%.1f health_recovery_stable_seconds=%.1f",
        _agent_input_queue_max_size(),
        _agent_input_queue_max_wait_seconds(),
        _agent_max_active_turns(),
        float(config.health_alert_cooldown_seconds),
        float(config.health_queue_oldest_seconds),
        float(config.health_recovery_stable_seconds),
    )

    await refresh_model_catalog()

    await application.bot.delete_my_commands()

    bot_commands = [
        BotCommand("start", "Show welcome message"),
        BotCommand("history", "Message history for this topic"),
        BotCommand("mode", "Choose clean or trace output"),
        BotCommand("screenshot", "Terminal screenshot with control keys"),
        BotCommand("esc", ESC_COMMAND_DESCRIPTION),
        BotCommand("interrupt", INTERRUPT_COMMAND_DESCRIPTION),
        BotCommand("steer", STEER_COMMAND_DESCRIPTION),
        BotCommand("queue", QUEUE_COMMAND_DESCRIPTION),
        BotCommand("kill", "Kill session and delete topic"),
        BotCommand("unbind", "Unbind topic from session (keeps window running)"),
        BotCommand("usage", USAGE_COMMAND_DESCRIPTION),
        BotCommand("health", "Show host and AgentBot health"),
        BotCommand("agentlogin", AGENT_LOGIN_COMMAND_DESCRIPTION),
        BotCommand("agentaccount", ACCOUNT_COMMAND_DESCRIPTION),
        BotCommand("codexlogin", "Login to Codex"),
        BotCommand("codexaccount", "Manage Codex accounts"),
        BotCommand("claudelogin", "Login to Claude Code"),
        BotCommand("claudeaccount", "Manage Claude Code accounts"),
        BotCommand("cursorlogin", "Login to Cursor Agent"),
    ]
    # Add Codex slash commands
    for cmd_name, desc in CC_COMMANDS.items():
        bot_commands.append(BotCommand(cmd_name, desc))

    await application.bot.set_my_commands(bot_commands)

    # Re-resolve stale window IDs from persisted state against live tmux windows
    await session_manager.resolve_stale_ids()

    # One-shot queued cleanup of historical Telegram topics
    await process_pending_topic_deletions(application.bot)

    # Pre-fill global rate limiter bucket on restart.
    # AsyncLimiter starts at _level=0 (full burst capacity), but Telegram's
    # server-side counter persists across bot restarts.  Setting _level=max_rate
    # forces the bucket to start "full" so capacity drains in naturally (~1s).
    # AIORateLimiter has no per-private-chat limiter, so max_retries is the
    # primary protection (retry + pause all concurrent requests on 429).
    rate_limiter = application.bot.rate_limiter
    if rate_limiter and rate_limiter._base_limiter:
        rate_limiter._base_limiter._level = rate_limiter._base_limiter.max_rate
        logger.info("Pre-filled global rate limiter bucket")

    async def message_callback(msg: NewMessage) -> None:
        await handle_new_message(msg, application.bot)

    agent_backend = get_configured_backend()
    await agent_backend.start(message_callback)
    session_monitor = getattr(agent_backend, "session_monitor", None)
    backend_info = agent_backend.info()
    logger.info(
        "Agent backend started: %s (%s)",
        backend_info.backend_id,
        backend_info.display_name,
    )
    _restore_agent_input_queues(application.bot)
    _restore_transient_agent_retries(application.bot)

    # Start status polling task
    _status_poll_task = asyncio.create_task(status_poll_loop(application.bot))
    logger.info("Status polling task started")

    if config.health_alerts_enabled:
        _health_task = asyncio.create_task(
            _health_monitor.run(
                application.bot,
                config,
                lambda: session_monitor,
            )
        )
        logger.info("Host health monitor started")

    from .updater import auto_update_loop_with_notifier

    _auto_update_task = asyncio.create_task(
        auto_update_loop_with_notifier(
            codex_update_notifier=lambda result: notify_codex_update_available(
                application.bot,
                result,
            ),
        )
    )
    logger.info("Auto-update task initialized")


async def _stop_runtime(
    application: Application, *, drain_message_workers: bool
) -> None:
    global agent_backend, session_monitor, _status_poll_task, _auto_update_task
    global _health_task
    global _runtime_stopped

    if _runtime_stopped:
        return
    _runtime_stopped = True

    # Stop status polling
    if _status_poll_task:
        _status_poll_task.cancel()
        try:
            await _status_poll_task
        except asyncio.CancelledError:
            pass
        _status_poll_task = None
        logger.info("Status polling stopped")

    if _auto_update_task:
        _auto_update_task.cancel()
        try:
            await _auto_update_task
        except asyncio.CancelledError:
            pass
        _auto_update_task = None
        logger.info("Auto-update task stopped")

    if _health_task:
        _health_task.cancel()
        try:
            await _health_task
        except asyncio.CancelledError:
            pass
        _health_task = None
        logger.info("Host health monitor stopped")

    await _cancel_transient_agent_retry_tasks()
    await _cancel_agent_input_confirmation_tasks()
    await _cancel_agent_input_drain_tasks()

    if agent_backend:
        await agent_backend.stop()
        agent_backend = None
        session_monitor = None
        logger.info("Agent backend stopped")
    elif session_monitor:
        await session_monitor.stop()
        session_monitor = None
        logger.info("Session monitor stopped")

    # Stop all queue workers while the Telegram request client is still usable.
    await shutdown_workers(drain=drain_message_workers)

    await close_transcribe_client()


async def post_stop(application: Application) -> None:
    """Stop runtime producers and drain Telegram sends before request shutdown."""
    await _stop_runtime(application, drain_message_workers=True)


async def post_shutdown(application: Application) -> None:
    """Fallback cleanup after Telegram's request client has shut down."""
    await _stop_runtime(application, drain_message_workers=False)


def create_bot() -> Application:
    application = (
        Application.builder()
        .concurrent_updates(
            TopicUpdateProcessor(
                config.telegram_max_concurrent_updates,
                update_store=_runtime_store,
            )
        )
        .token(config.telegram_bot_token)
        .request(
            _build_request(
                connect_timeout=DEFAULT_REQUEST_CONNECT_TIMEOUT_SECONDS,
                read_timeout=DEFAULT_REQUEST_READ_TIMEOUT_SECONDS,
                write_timeout=DEFAULT_REQUEST_WRITE_TIMEOUT_SECONDS,
                pool_timeout=DEFAULT_REQUEST_POOL_TIMEOUT_SECONDS,
            )
        )
        .get_updates_request(
            _build_request(
                connect_timeout=GET_UPDATES_CONNECT_TIMEOUT_SECONDS,
                read_timeout=GET_UPDATES_READ_TIMEOUT_SECONDS,
                write_timeout=GET_UPDATES_WRITE_TIMEOUT_SECONDS,
                pool_timeout=GET_UPDATES_POOL_TIMEOUT_SECONDS,
            )
        )
        .rate_limiter(AIORateLimiter(max_retries=5))
        .post_init(post_init)
        .post_stop(post_stop)
        .post_shutdown(post_shutdown)
        .build()
    )

    application.add_error_handler(application_error_handler)

    # TypeHandler also covers callbacks; reject collisions before every handler.
    application.add_handler(TypeHandler(Update, inbound_group_route_guard), group=-2)

    # Group -1 observes every inbound message before command/content handlers.
    # A message arriving inside a forum topic proves that a previously
    # quarantined route has become usable again.
    application.add_handler(
        MessageHandler(filters.ALL, inbound_route_health_handler),
        group=-1,
    )

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("history", history_command))
    application.add_handler(CommandHandler("mode", output_mode_command))
    application.add_handler(CommandHandler("screenshot", screenshot_command))
    application.add_handler(CommandHandler("esc", esc_command))
    application.add_handler(CommandHandler("interrupt", interrupt_command))
    application.add_handler(CommandHandler("steer", steer_command))
    application.add_handler(CommandHandler("queue", queue_command))
    application.add_handler(CommandHandler("kill", topic_closed_handler))
    application.add_handler(CommandHandler("unbind", unbind_command))
    application.add_handler(CommandHandler("usage", usage_command))
    application.add_handler(CommandHandler("health", health_command))
    application.add_handler(CommandHandler("agentlogin", agent_login_command))
    application.add_handler(CommandHandler("agentaccount", agent_account_command))
    application.add_handler(CommandHandler("codexlogin", codex_login_command))
    application.add_handler(CommandHandler("codexaccount", codex_account_command))
    application.add_handler(CommandHandler("claudelogin", claude_login_command))
    application.add_handler(CommandHandler("claudeaccount", claude_account_command))
    application.add_handler(CommandHandler("cursorlogin", cursor_login_command))
    application.add_handler(CommandHandler(["agentcmd", "cmd"], agent_command_mode))
    application.add_handler(CallbackQueryHandler(callback_handler))
    # Topic closed event — auto-kill associated window
    application.add_handler(
        MessageHandler(
            filters.StatusUpdate.FORUM_TOPIC_CLOSED,
            topic_closed_handler,
        )
    )
    # Topic edited event — sync renamed topic to tmux window
    application.add_handler(
        MessageHandler(
            filters.StatusUpdate.FORUM_TOPIC_EDITED,
            topic_edited_handler,
        )
    )
    # Forward any other /command to Codex
    application.add_handler(MessageHandler(filters.COMMAND, forward_command_handler))
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler)
    )
    # Photos: download and forward file path to Codex
    application.add_handler(MessageHandler(filters.PHOTO, photo_handler))
    # Files: download and forward file path to Codex
    application.add_handler(MessageHandler(filters.Document.ALL, document_handler))
    # Voice: transcribe via OpenAI and forward text to Codex
    application.add_handler(MessageHandler(filters.VOICE, voice_handler))
    # Catch-all: non-text content (stickers, video, etc.)
    application.add_handler(
        MessageHandler(
            ~filters.COMMAND & ~filters.TEXT & ~filters.StatusUpdate.ALL,
            unsupported_content_handler,
        )
    )

    return application
