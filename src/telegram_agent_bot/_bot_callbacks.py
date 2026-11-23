"""Extracted bot runtime helpers (ADR 0001 Phase D)."""

from __future__ import annotations

from telegram import CallbackQuery, User

from telegram_agent_bot._bot_shared import (
    AGENT_CODEX,
    AGENT_INPUT_MODE_QUEUE,
    AGENT_INPUT_MODE_STEER,
    ASK_REFRESH_ACTIONS,
    Any,
    Awaitable,
    BadRequest,
    Bot,
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
    Callable,
    CallbackContext,
    CodexUpdateResult,
    ContextTypes,
    INPUT_ROUTE_CANCEL,
    INPUT_ROUTE_INTERRUPT,
    INPUT_ROUTE_QUEUE,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    NewMessage,
    Path,
    Update,
    _CODEX_UPDATE_PROMPT_STATE_FILENAME,
    _INPUT_ROUTE_CHOICE_MAX_AGE_SECONDS,
    _b,
    _codex_update_apply_lock,
    _codex_update_prompted_versions,
    _runtime_store,
    _session_ids_match,
    agent_capabilities,
    agent_display_name,
    app_dir,
    asyncio,
    atomic_write_json,
    capture_agent_output,
    check_codex_update,
    config,
    extract_auth_error_message,
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
    is_codex_input_ready,
    is_interactive_ui,
    json,
    load_codex_update_settings,
    load_update_env,
    logger,
    mark_window_working,
    safe_edit,
    safe_send,
    session_manager,
    sys,
)
from telegram_agent_bot._bot_helpers import (
    _get_thread_id,
    is_user_allowed,
)
from telegram_agent_bot._bot_input_routing import (
    _send_control_to_agent,
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


def _get_codex_update_apply_lock() -> asyncio.Lock:
    """Return the process-local lock for manual Codex CLI updates."""
    global _codex_update_apply_lock
    if _codex_update_apply_lock is None:
        _codex_update_apply_lock = _b("asyncio", asyncio).Lock()
    return _codex_update_apply_lock


def _codex_update_prompt_key(result: CodexUpdateResult) -> str:
    """Return the dedupe key for a Codex update prompt."""
    return result.latest_version or result.message or "unknown"


def _codex_update_prompt_state_file() -> Path:
    """Return the state file tracking already prompted Codex CLI versions."""
    return _b("app_dir", app_dir)() / _b(
        "_CODEX_UPDATE_PROMPT_STATE_FILENAME", _CODEX_UPDATE_PROMPT_STATE_FILENAME
    )


def _load_codex_update_prompted_versions() -> set[str]:
    """Load Codex CLI versions that have already shown an update prompt."""
    path = _b("_codex_update_prompt_state_file", _codex_update_prompt_state_file)()
    try:
        payload = _b("json", json).loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return set()
    except (OSError, _b("json", json).JSONDecodeError) as exc:
        _b("logger", logger).warning(
            "Failed to read Codex update prompt state %s: %s", path, exc
        )
        return set()

    if not isinstance(payload, dict):
        return set()

    prompted_versions = payload.get("prompted_versions")
    if not isinstance(prompted_versions, list):
        return set()
    return {
        version for version in prompted_versions if isinstance(version, str) and version
    }


def _save_codex_update_prompted_versions(prompted_versions: set[str]) -> None:
    """Persist Codex CLI versions that have already shown an update prompt."""
    path = _b("_codex_update_prompt_state_file", _codex_update_prompt_state_file)()
    try:
        _b("atomic_write_json", atomic_write_json)(
            path,
            {"prompted_versions": sorted(prompted_versions)},
        )
    except OSError as exc:
        _b("logger", logger).warning(
            "Failed to write Codex update prompt state %s: %s", path, exc
        )


def _mark_codex_update_prompted(key: str) -> bool:
    """Return True when a Codex update prompt key is newly marked."""
    persisted_versions = _b(
        "_load_codex_update_prompted_versions", _load_codex_update_prompted_versions
    )()
    prompted_versions = persisted_versions | _b(
        "_codex_update_prompted_versions", _codex_update_prompted_versions
    )
    if key in prompted_versions:
        _b("_codex_update_prompted_versions", _codex_update_prompted_versions).update(
            prompted_versions
        )
        return False

    prompted_versions.add(key)
    _b("_codex_update_prompted_versions", _codex_update_prompted_versions).update(
        prompted_versions
    )
    _b("_save_codex_update_prompted_versions", _save_codex_update_prompted_versions)(
        prompted_versions
    )
    return True


async def notify_codex_update_available(
    bot: Bot,
    result: CodexUpdateResult,
) -> None:
    """Notify allowed Telegram users that a Codex CLI update needs approval."""
    key = _b("_codex_update_prompt_key", _codex_update_prompt_key)(result)
    if not _b("_mark_codex_update_prompted", _mark_codex_update_prompted)(key):
        return

    current = result.current_version or "unknown"
    latest = result.latest_version or "unknown"
    text = (
        "⬆️ Codex CLI update available\n\n"
        f"Current: `{current}`\n"
        f"Latest: `{latest}`\n\n"
        "Upgrade on this host now? Existing Codex sessions keep running; "
        "new sessions will use the updated CLI."
    )
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "Upgrade Codex",
                    callback_data=_b("CB_CODEX_UPDATE_APPLY", CB_CODEX_UPDATE_APPLY),
                ),
                InlineKeyboardButton(
                    "Not now",
                    callback_data=_b(
                        "CB_CODEX_UPDATE_DISMISS", CB_CODEX_UPDATE_DISMISS
                    ),
                ),
            ]
        ]
    )
    for user_id in sorted(_b("config", config).allowed_users):
        await _b("safe_send", safe_send)(bot, user_id, text, reply_markup=keyboard)


async def _apply_codex_update_from_callback(query: Any) -> None:
    """Apply a Codex CLI update after an authorized Telegram button click."""
    lock = _b("_get_codex_update_apply_lock", _get_codex_update_apply_lock)()
    if lock.locked():
        await query.answer("Codex update is already running", show_alert=True)
        return

    await query.answer("Updating Codex CLI...")
    async with lock:
        await _b("safe_edit", safe_edit)(query, "⏳ Updating Codex CLI…")
        _b("load_update_env", load_update_env)()
        settings = _b("load_codex_update_settings", load_codex_update_settings)()
        result = await _b("asyncio", asyncio).to_thread(
            _b("check_codex_update", check_codex_update),
            settings,
            apply_update=True,
        )

    if result.updated:
        await _b("safe_edit", safe_edit)(
            query,
            f"✅ {result.message}\n\nNew Codex sessions will use the updated CLI.",
        )
        return
    if result.checked and result.supported and not result.update_available:
        await _b("safe_edit", safe_edit)(query, f"✅ {result.message}")
        return
    await _b("safe_edit", safe_edit)(query, f"⚠️ {result.message}")


async def _safe_answer_callback(query: Any, *args: Any, **kwargs: Any) -> bool:
    """Answer a callback while treating an expired query as harmless."""
    try:
        await query.answer(*args, **kwargs)
        return True
    except BadRequest as exc:
        message = str(exc).lower()
        if "query is too old" in message or "query id is invalid" in message:
            _b("logger", logger).debug(
                "Callback query answer skipped because it expired"
            )
            return False
        raise


async def _load_input_routing_choice(
    query: CallbackQuery,
    user: User,
    thread_id: int,
    *,
    mode: str,
    record_id: int,
) -> Any | None:
    try:
        choice = _b("_runtime_store", _runtime_store).claim_pending_input_choice(
            record_id,
            user_id=user.id,
            thread_id=thread_id,
            max_age_seconds=_b(
                "_INPUT_ROUTE_CHOICE_MAX_AGE_SECONDS",
                _INPUT_ROUTE_CHOICE_MAX_AGE_SECONDS,
            ),
        )
    except Exception:
        _b("logger", logger).exception(
            "Failed to claim input routing choice (record=%d user=%d thread=%d)",
            record_id,
            user.id,
            thread_id,
        )
        await _b("_safe_answer_callback", _safe_answer_callback)(
            query, "Could not load this input", show_alert=True
        )
        return None
    if choice is None:
        await _b("_safe_answer_callback", _safe_answer_callback)(
            query,
            "This input was already handled or expired",
            show_alert=True,
        )
        return None
    if mode == _b("INPUT_ROUTE_CANCEL", INPUT_ROUTE_CANCEL):
        await _b("safe_edit", safe_edit)(query, "已取消，这条消息没有发送给 Agent。")
        await _b("_safe_answer_callback", _safe_answer_callback)(query, "Cancelled")
        return None
    return choice


async def _prepare_input_routing_pane(
    query: CallbackQuery,
    user: User,
    thread_id: int,
    choice: Any,
) -> tuple[str, str, str, str] | None:
    current_window_id = _b(
        "session_manager", session_manager
    ).resolve_window_for_thread(user.id, thread_id)
    if not current_window_id or current_window_id != choice.window_id:
        await _b("safe_edit", safe_edit)(
            query, "❌ 会话绑定已经变化，这条消息没有发送。"
        )
        await _b("_safe_answer_callback", _safe_answer_callback)(
            query, "Session changed", show_alert=True
        )
        return None
    if not _b(
        "_window_supports_native_input_routing", _window_supports_native_input_routing
    )(current_window_id):
        await _b("safe_edit", safe_edit)(query, "❌ 当前会话不支持原生引导/排队。")
        await _b("_safe_answer_callback", _safe_answer_callback)(
            query, "Unsupported session", show_alert=True
        )
        return None
    agent_type = _b("_window_agent_type", _window_agent_type)(current_window_id)
    display_name = _b("agent_display_name", agent_display_name)(agent_type)
    capture = await _b("capture_agent_output", capture_agent_output)(
        user.id, thread_id, current_window_id
    )
    if capture is None or capture.missing:
        await _b("safe_edit", safe_edit)(
            query,
            f"❌ {display_name} 窗口已不存在，这条消息没有发送。",
        )
        await _b("_safe_answer_callback", _safe_answer_callback)(
            query, "Window missing", show_alert=True
        )
        return None
    pane_text = capture.text or ""
    auth_error = _b("extract_auth_error_message", extract_auth_error_message)(pane_text)
    if auth_error:
        recovery_message = (
            _b("_codex_auth_recovery_message", _codex_auth_recovery_message)(auth_error)
            if agent_type == _b("AGENT_CODEX", AGENT_CODEX)
            else auth_error
        )
        await _b("safe_edit", safe_edit)(query, f"❌ {recovery_message}")
        await _b("_safe_answer_callback", _safe_answer_callback)(
            query, "Login required", show_alert=True
        )
        return None
    return current_window_id, agent_type, display_name, pane_text


async def _execute_input_route_interrupt(
    query: CallbackQuery,
    context: ContextTypes.DEFAULT_TYPE,
    user: User,
    thread_id: int,
    current_window_id: str,
    text: str,
) -> None:
    await _b("_discard_queued_agent_input", _discard_queued_agent_input)(
        user.id, thread_id, current_window_id
    )
    ok, control_message = await _b("_send_control_to_agent", _send_control_to_agent)(
        user.id, thread_id, current_window_id, "Escape"
    )
    if not ok:
        await _b("safe_edit", safe_edit)(query, f"❌ {control_message}")
        await _b("_safe_answer_callback", _safe_answer_callback)(
            query, "Interrupt failed", show_alert=True
        )
        return
    await _b("asyncio", asyncio).sleep(0.3)
    success, message, queued = await _b(
        "_send_or_queue_agent_input", _send_or_queue_agent_input
    )(context.bot, user.id, thread_id, current_window_id, text)
    if not success:
        await _b("safe_edit", safe_edit)(query, f"❌ {message}")
        await _b("_safe_answer_callback", _safe_answer_callback)(
            query, "Send failed", show_alert=True
        )
        return
    await _b("mark_window_working", mark_window_working)(
        context.bot, user.id, current_window_id, thread_id
    )
    label = f"⎋ 已中断；{message}" if queued else "⎋ 已中断并发送新任务。"
    await _b("safe_edit", safe_edit)(query, label)
    await _b("_safe_answer_callback", _safe_answer_callback)(query, "Sent")


async def _try_bot_queue_fallback(
    query: CallbackQuery,
    context: ContextTypes.DEFAULT_TYPE,
    user: User,
    thread_id: int,
    window_id: str,
    text: str,
) -> bool:
    success, message, _queued = await _b(
        "_send_or_queue_agent_input", _send_or_queue_agent_input
    )(context.bot, user.id, thread_id, window_id, text)
    if not success:
        return False
    await _b("safe_edit", safe_edit)(query, f"⏳ {message}")
    await _b("_safe_answer_callback", _safe_answer_callback)(
        query, "Queued by AgentBot"
    )
    return True


async def _dispatch_idle_or_native_callback_input(
    query: CallbackQuery,
    context: ContextTypes.DEFAULT_TYPE,
    user: User,
    thread_id: int,
    current_window_id: str,
    display_name: str,
    pane_text: str,
    text: str,
    mode: str,
) -> None:
    if _b("is_codex_input_ready", is_codex_input_ready)(pane_text):
        success, message, queued = await _b(
            "_send_or_queue_agent_input", _send_or_queue_agent_input
        )(context.bot, user.id, thread_id, current_window_id, text)
        if not success:
            await _b("safe_edit", safe_edit)(query, f"❌ {message}")
            await _b("_safe_answer_callback", _safe_answer_callback)(
                query, "Send failed", show_alert=True
            )
            return
        await _b("mark_window_working", mark_window_working)(
            context.bot, user.id, current_window_id, thread_id
        )
        label = (
            f"⏳ {message}"
            if queued
            else f"✅ {display_name} 已空闲，消息已作为新一轮发送。"
        )
        await _b("safe_edit", safe_edit)(query, label)
        await _b("_safe_answer_callback", _safe_answer_callback)(query, "Sent")
        return

    native_mode = (
        _b("AGENT_INPUT_MODE_QUEUE", AGENT_INPUT_MODE_QUEUE)
        if mode == _b("INPUT_ROUTE_QUEUE", INPUT_ROUTE_QUEUE)
        else _b("AGENT_INPUT_MODE_STEER", AGENT_INPUT_MODE_STEER)
    )
    success, message = await _b("_send_native_agent_input", _send_native_agent_input)(
        context.bot, user.id, thread_id, current_window_id, text, mode=native_mode
    )
    if not success:
        await _b("safe_edit", safe_edit)(query, f"❌ {message}")
        await _b("_safe_answer_callback", _safe_answer_callback)(
            query, "Send failed", show_alert=True
        )
        return
    await _b("mark_window_working", mark_window_working)(
        context.bot, user.id, current_window_id, thread_id
    )
    await _b("safe_edit", safe_edit)(query, f"✅ {message}")
    await _b("_safe_answer_callback", _safe_answer_callback)(query, "Sent")


async def _handle_input_routing_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    mode: str,
    record_id: int,
) -> None:
    """Consume one persisted mode choice and route it to the bound agent window."""
    query = update.callback_query
    user = update.effective_user
    thread_id = _b("_get_thread_id", _get_thread_id)(update)
    if not query or not user or thread_id is None:
        if query:
            await _b("_safe_answer_callback", _safe_answer_callback)(
                query, "Invalid topic", show_alert=True
            )
        return

    choice = await _load_input_routing_choice(
        query, user, thread_id, mode=mode, record_id=record_id
    )
    if choice is None:
        return
    prepared = await _prepare_input_routing_pane(query, user, thread_id, choice)
    if prepared is None:
        return
    current_window_id, agent_type, display_name, pane_text = prepared

    if (
        mode == _b("INPUT_ROUTE_QUEUE", INPUT_ROUTE_QUEUE)
        and not _b("agent_capabilities", agent_capabilities)(
            agent_type
        ).supports_native_queue
    ):
        success, message, queued = await _b(
            "_send_or_queue_agent_input", _send_or_queue_agent_input
        )(context.bot, user.id, thread_id, current_window_id, choice.text)
        if not success:
            await _b("safe_edit", safe_edit)(query, f"❌ {message}")
            await _b("_safe_answer_callback", _safe_answer_callback)(
                query, "Queue failed", show_alert=True
            )
            return
        if not queued:
            await _b("mark_window_working", mark_window_working)(
                context.bot, user.id, current_window_id, thread_id
            )
        label = f"⏳ {message}" if queued else "✅ Claude Code 已空闲，消息已发送。"
        await _b("safe_edit", safe_edit)(query, label)
        await _b("_safe_answer_callback", _safe_answer_callback)(
            query, "Queued by AgentBot" if queued else "Sent"
        )
        return

    if not pane_text.strip():
        if mode == _b(
            "INPUT_ROUTE_QUEUE", INPUT_ROUTE_QUEUE
        ) and await _try_bot_queue_fallback(
            query, context, user, thread_id, current_window_id, choice.text
        ):
            return
        await _b("safe_edit", safe_edit)(
            query,
            f"❌ 暂时无法确认 {display_name} 状态；请稍后重新发送这条消息。",
        )
        await _b("_safe_answer_callback", _safe_answer_callback)(
            query, "Agent state unavailable", show_alert=True
        )
        return

    if mode == _b("INPUT_ROUTE_INTERRUPT", INPUT_ROUTE_INTERRUPT):
        await _execute_input_route_interrupt(
            query, context, user, thread_id, current_window_id, choice.text
        )
        return

    if _b("is_interactive_ui", is_interactive_ui)(pane_text):
        if mode == _b(
            "INPUT_ROUTE_QUEUE", INPUT_ROUTE_QUEUE
        ) and await _try_bot_queue_fallback(
            query, context, user, thread_id, current_window_id, choice.text
        ):
            return
        await _b("safe_edit", safe_edit)(
            query,
            f"❌ {display_name} 当前显示交互确认界面；请先处理该界面，再重新发送这条消息。",
        )
        await _b("_safe_answer_callback", _safe_answer_callback)(
            query, "Interactive prompt active", show_alert=True
        )
        return

    await _dispatch_idle_or_native_callback_input(
        query,
        context,
        user,
        thread_id,
        current_window_id,
        display_name,
        pane_text,
        choice.text,
        mode,
    )


class CallbackRegistry:
    """Registry mapping exact callback keys and prefixes to async handlers."""

    def __init__(self) -> None:
        self._exact: dict[str, CallbackHandler] = {}
        self._prefixes: list[tuple[str, CallbackHandler]] = []

    def register_exact(self, key: str, handler: CallbackHandler) -> None:
        self._exact[key] = handler

    def register_prefix(self, prefix: str, handler: CallbackHandler) -> None:
        self._prefixes.append((prefix, handler))

    def resolve(self, data: str) -> CallbackHandler | None:
        exact = self._exact.get(data)
        if exact is not None:
            return exact
        for prefix, handler in self._prefixes:
            if data.startswith(prefix):
                return handler
        return None

    async def dispatch(self, ctx: CallbackContext) -> bool:
        handler = self.resolve(ctx.data)
        if handler is None:
            return False
        await handler(ctx)
        return True


CallbackHandler = Callable[[CallbackContext], Awaitable[None]]


def build_default_callback_registry() -> CallbackRegistry:
    """Construct and wire the default callback registry in bot.py."""
    registry = CallbackRegistry()

    registry.register_exact(
        _b("CB_CODEX_UPDATE_APPLY", CB_CODEX_UPDATE_APPLY),
        _b("handle_codex_update_apply", handle_codex_update_apply),
    )
    registry.register_exact(
        _b("CB_CODEX_UPDATE_DISMISS", CB_CODEX_UPDATE_DISMISS),
        _b("handle_codex_update_dismiss", handle_codex_update_dismiss),
    )
    registry.register_prefix(
        _b("CB_INPUT_ROUTE", CB_INPUT_ROUTE),
        _b("handle_input_route", handle_input_route),
    )

    registry.register_prefix(
        _b("CB_HISTORY_PREV", CB_HISTORY_PREV),
        _b("handle_history_page", handle_history_page),
    )
    registry.register_prefix(
        _b("CB_HISTORY_NEXT", CB_HISTORY_NEXT),
        _b("handle_history_page", handle_history_page),
    )
    registry.register_prefix(
        _b("CB_OUTPUT_MODE", CB_OUTPUT_MODE),
        _b("handle_output_mode", handle_output_mode),
    )

    registry.register_prefix(
        _b("CB_ROOT_SELECT", CB_ROOT_SELECT),
        _b("handle_root_select", handle_root_select),
    )
    registry.register_exact(
        _b("CB_ROOT_CANCEL", CB_ROOT_CANCEL),
        _b("handle_root_cancel", handle_root_cancel),
    )

    registry.register_prefix(
        _b("CB_PROFILE_AGENT", CB_PROFILE_AGENT),
        _b("handle_profile_agent", handle_profile_agent),
    )
    registry.register_prefix(
        _b("CB_PROFILE_MODEL", CB_PROFILE_MODEL),
        _b("handle_profile_model", handle_profile_model),
    )
    registry.register_prefix(
        _b("CB_PROFILE_EFFORT", CB_PROFILE_EFFORT),
        _b("handle_profile_effort", handle_profile_effort),
    )
    registry.register_prefix(
        _b("CB_PROFILE_FAST", CB_PROFILE_FAST),
        _b("handle_profile_fast", handle_profile_fast),
    )
    registry.register_prefix(
        _b("CB_PROFILE_PERMISSION", CB_PROFILE_PERMISSION),
        _b("handle_profile_permission", handle_profile_permission),
    )
    registry.register_exact(
        _b("CB_PROFILE_CONFIRM", CB_PROFILE_CONFIRM),
        _b("handle_profile_confirm", handle_profile_confirm),
    )
    registry.register_exact(
        _b("CB_PROFILE_CANCEL", CB_PROFILE_CANCEL),
        _b("handle_profile_cancel", handle_profile_cancel),
    )

    registry.register_prefix(
        _b("CB_DIR_SELECT", CB_DIR_SELECT), _b("handle_dir_select", handle_dir_select)
    )
    registry.register_exact(
        _b("CB_DIR_UP", CB_DIR_UP), _b("handle_dir_up", handle_dir_up)
    )
    registry.register_prefix(
        _b("CB_DIR_PAGE", CB_DIR_PAGE), _b("handle_dir_page", handle_dir_page)
    )
    registry.register_exact(
        _b("CB_DIR_CONFIRM", CB_DIR_CONFIRM),
        _b("handle_dir_confirm", handle_dir_confirm),
    )
    registry.register_exact(
        _b("CB_DIR_CANCEL", CB_DIR_CANCEL), _b("handle_dir_cancel", handle_dir_cancel)
    )

    registry.register_prefix(
        _b("CB_SESSION_SELECT", CB_SESSION_SELECT),
        _b("handle_session_select", handle_session_select),
    )
    registry.register_exact(
        _b("CB_SESSION_NEW", CB_SESSION_NEW),
        _b("handle_session_new", handle_session_new),
    )
    registry.register_exact(
        _b("CB_SESSION_CANCEL", CB_SESSION_CANCEL),
        _b("handle_session_cancel", handle_session_cancel),
    )

    registry.register_prefix(
        _b("CB_WIN_BIND", CB_WIN_BIND), _b("handle_window_bind", handle_window_bind)
    )
    registry.register_exact(
        _b("CB_WIN_NEW", CB_WIN_NEW), _b("handle_window_new", handle_window_new)
    )
    registry.register_exact(
        _b("CB_WIN_CANCEL", CB_WIN_CANCEL),
        _b("handle_window_cancel", handle_window_cancel),
    )

    registry.register_prefix(
        _b("CB_SCREENSHOT_REFRESH", CB_SCREENSHOT_REFRESH),
        _b("handle_screenshot_refresh", handle_screenshot_refresh),
    )
    registry.register_exact("noop", _b("handle_noop", handle_noop))
    for prefix in _b("ASK_REFRESH_ACTIONS", ASK_REFRESH_ACTIONS):
        registry.register_prefix(prefix, _b("handle_ask_control", handle_ask_control))
    registry.register_prefix(
        _b("CB_ASK_ESC", CB_ASK_ESC), _b("handle_ask_esc", handle_ask_esc)
    )
    registry.register_prefix(
        _b("CB_ASK_REFRESH", CB_ASK_REFRESH),
        _b("handle_ask_refresh", handle_ask_refresh),
    )
    registry.register_prefix(
        _b("CB_KEYS_PREFIX", CB_KEYS_PREFIX), _b("handle_quick_keys", handle_quick_keys)
    )

    return registry


DEFAULT_CALLBACK_REGISTRY = build_default_callback_registry()


async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.data:
        return

    user = update.effective_user
    if not user or not _b("is_user_allowed", is_user_allowed)(user.id):
        await query.answer("Not authorized")
        return

    data = query.data

    # Capture group chat_id for supergroup forum topic routing.
    # Required: Telegram Bot API needs group chat_id (not user_id) to send
    # messages with message_thread_id. Do NOT remove — see session.py docs.
    cb_thread_id = _b("_get_thread_id", _get_thread_id)(update)
    chat = update.effective_chat
    if chat and chat.type in ("group", "supergroup"):
        _b("session_manager", session_manager).set_group_chat_id(
            user.id, cb_thread_id, chat.id
        )

    ctx = CallbackContext(
        update=update,
        context=context,
        query=query,
        data=data,
        user=user,
        thread_id=cb_thread_id,
        deps=_b("sys", sys).modules.get(
            "telegram_agent_bot.bot", sys.modules[__name__]
        ),
    )
    await DEFAULT_CALLBACK_REGISTRY.dispatch(ctx)


async def _mark_transcript_message_delivered(
    user_id: int,
    window_id: str,
    msg: NewMessage,
) -> None:
    """Advance read offset only through the transcript bytes just delivered."""
    if not window_id:
        return

    if msg.source_offset > 0:
        _b("session_manager", session_manager).update_user_window_offset(
            user_id,
            window_id,
            msg.source_offset,
            msg.session_id,
        )
        return

    session = await _b("session_manager", session_manager).resolve_session_for_window(
        window_id
    )
    if session and session.file_path:
        try:
            file_size = Path(session.file_path).stat().st_size
            _b("session_manager", session_manager).update_user_window_offset(
                user_id,
                window_id,
                file_size,
                msg.session_id,
            )
        except OSError:
            pass


def _transcript_message_already_delivered(
    user_id: int,
    window_id: str,
    msg: NewMessage,
) -> bool:
    """Return True when this recipient has already received this JSONL line."""
    if not window_id or msg.source_offset <= 0:
        return False

    user_window_offsets = getattr(
        _b("session_manager", session_manager), "user_window_offsets", {}
    )
    if not isinstance(user_window_offsets, dict):
        return False
    user_window_offset_sessions = getattr(
        _b("session_manager", session_manager), "user_window_offset_sessions", {}
    )
    if not isinstance(user_window_offset_sessions, dict):
        return False
    user_offsets = user_window_offsets.get(user_id, {})
    if not isinstance(user_offsets, dict):
        return False
    user_sessions = user_window_offset_sessions.get(user_id, {})
    if not isinstance(user_sessions, dict):
        return False
    recorded_session_id = user_sessions.get(window_id)
    if not isinstance(recorded_session_id, str) or not _b(
        "_session_ids_match", _session_ids_match
    )(recorded_session_id, msg.session_id):
        return False
    delivered_offset = user_offsets.get(window_id)
    return isinstance(delivered_offset, int) and delivered_offset >= msg.source_offset
