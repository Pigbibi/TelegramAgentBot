"""Telegram bot handlers for TelegramAgentBot.

Registers all command/callback/message handlers and manages the bot lifecycle.
Each Telegram topic maps 1:1 to a tmux window (Codex session).

Core responsibilities:
  - Command handlers: /start, /history, /screenshot, /esc, /interrupt,
    /kill, /unbind, plus forwarding unknown /commands to Codex via tmux.
  - Callback query handler: directory browser, history pagination,
    interactive UI navigation, screenshot refresh.
  - Topic-based routing: each named topic binds to one tmux window.
    Unbound topics trigger the directory browser to create a new session.
  - Photo/file handling: attachments sent by user are downloaded and forwarded
    to Codex as file paths (photo_handler/document_handler).
  - Voice handling: voice messages are transcribed (OpenAI / Google Gemini) and
    forwarded as text (voice_handler).
  - Automatic cleanup: closing a topic kills the associated window
    (topic_closed_handler). Unsupported content (stickers, etc.)
    is rejected with a warning (unsupported_content_handler).
  - Bot lifecycle management: post_init, post_shutdown, create_bot.

Handler modules (in handlers/):
  - callback_data: Callback data constants
  - message_queue: Per-user message queue management
  - message_sender: Safe message sending helpers
  - history: Message history pagination
  - directory_browser: Directory selection UI
  - interactive_ui: Interactive UI handling
  - status_polling: Terminal status polling
  - response_builder: Response message building
  - agent_login: Agent device login and account management
  - native_input: Native input routing, queues, and confirmations
  - window_lifecycle: Agent/window launch specs, creation, binding, and recovery

Key functions: create_bot(), handle_new_message().
"""

import asyncio
import contextlib
import io
import json
import logging
import re
import shutil as shutil
import sys
import time
from collections import deque
from pathlib import Path
from typing import Any, Callable, TypeVar

from telegram import (
    Bot,
    BotCommand,
    Chat,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaDocument,
    Update,
)
from telegram.constants import ChatAction
from telegram.error import BadRequest, NetworkError, TelegramError, TimedOut
from telegram.ext import (
    AIORateLimiter,
    Application,
    ApplicationHandlerStop,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    TypeHandler,
    filters,
)
from telegram.request import HTTPXRequest

from .account_manager import (
    clear_current_account as clear_current_account,
    get_current_account_name as get_current_account_name,
    get_default_account_name as get_default_account_name,
    get_next_account_name,
    is_valid_account_name as is_valid_account_name,
    list_account_names as list_account_names,
    prepare_account_home as prepare_account_home,
    remember_current_account as remember_current_account,
    save_account_snapshot as save_account_snapshot,
)
from .agent_io import (
    capture_agent_output,
    create_agent_session as create_agent_session,
    send_agent_control,
    send_agent_input as send_agent_input,
    send_agent_message,
    upload_agent_file,
)
from .agent_profile import (
    AGENT_CLAUDE,
    AGENT_CLAUDE_OFFICIAL,
    AGENT_CODEX,
    AGENT_CURSOR,
    AgentProfile,
    normalize_agent_type,
    normalize_effort,
)
from .backends.base import AgentBackend, AgentTarget
from .backends.browser import AgentBrowser
from .backends.registry import get_configured_backend
from .browser_routing import (
    COMMAND_ARG_SEPARATORS,
    FORWARDED_COMMAND_RE,
    DirectoryBrowserKwargs,
    RootSelection,
    active_remote_root_browser,
    adapt_forward_command_for_agent,
    as_browser,
    browse_backend_context,
    browse_root_context,
    browser_for_backend_id,
    build_backend_directory_browser,
    clamp_to_selected_root,
    default_directory_browser_path,
    directory_browser_kwargs,
    directory_listing_with_root,
    normalize_forward_command_text,
    parse_root_selection,
    remote_child_path,
    remote_parent_path,
)
from .config import config
from .durable_state import (
    AGENT_INPUT_MODE_QUEUE,
    AGENT_INPUT_MODE_STEER,
    AGENT_INPUT_MODE_TURN,
    AGENT_INPUT_SUBMITTED_UNCONFIRMED as AGENT_INPUT_SUBMITTED_UNCONFIRMED,
    DurableRuntimeStore,
    PendingAgentInput,
    TRANSIENT_RETRY_SCHEDULED as TRANSIENT_RETRY_SCHEDULED,
)
from .handlers.callback_data import (
    CB_ASK_DOWN,
    CB_ASK_ENTER,
    CB_ASK_ESC,
    CB_ASK_LEFT,
    CB_ASK_REFRESH,
    CB_ASK_RIGHT,
    CB_ASK_SPACE,
    CB_ASK_TAB,
    CB_ASK_TRUST,
    CB_ASK_UP,
    CB_CODEX_UPDATE_APPLY,
    CB_CODEX_UPDATE_DISMISS,
    CB_DIR_CANCEL,
    CB_DIR_CONFIRM,
    CB_DIR_PAGE,
    CB_DIR_SELECT,
    CB_DIR_UP,
    CB_PROFILE_AGENT,
    CB_PROFILE_CANCEL,
    CB_PROFILE_CONFIRM,
    CB_PROFILE_EFFORT,
    CB_PROFILE_FAST,
    CB_PROFILE_MODEL,
    CB_PROFILE_PERMISSION,
    CB_OUTPUT_MODE,
    CB_HISTORY_NEXT,
    CB_HISTORY_PREV,
    CB_INPUT_ROUTE,
    CB_ROOT_CANCEL,
    CB_ROOT_SELECT,
    CB_SESSION_CANCEL,
    CB_SESSION_NEW,
    CB_SESSION_SELECT,
    CB_KEYS_PREFIX,
    CB_SCREENSHOT_REFRESH,
    CB_WIN_BIND,
    CB_WIN_CANCEL,
    CB_WIN_NEW,
)
from .handlers.directory_browser import (
    BROWSE_BACKEND_ID_KEY,
    BROWSE_DIRS_KEY,
    BROWSE_NODE_ID_KEY,
    BROWSE_PAGE_KEY,
    BROWSE_PATH_KEY,
    BROWSE_ROOT_LABEL_KEY,
    BROWSE_ROOT_PATH_KEY,
    ROOTS_KEY,
    SESSIONS_KEY,
    STATE_BROWSING_DIRECTORY,
    STATE_KEY,
    STATE_SELECTING_ROOT,
    STATE_SELECTING_SESSION,
    STATE_SELECTING_WINDOW,
    UNBOUND_WINDOWS_KEY,
    PROFILE_AGENT_KEY,
    PROFILE_EFFORT_KEY,
    PROFILE_FAST_MODE_KEY,
    PROFILE_MODEL_KEY,
    PROFILE_MODELS_KEY,
    PROFILE_PERMISSION_MODE_KEY,
    build_directory_browser,
    build_session_picker,
    clear_browse_state,
    clear_root_picker_state,
    clear_session_picker_state,
    clear_window_picker_state,
)
from .handlers.cleanup import clear_topic_state
from .handlers.history import send_history
from .output_mode import (
    OUTPUT_MODE_CLEAN,
    OUTPUT_MODE_TRACE,
    normalize_output_mode,
    output_mode_label,
)
from .profile_helpers import (
    default_profile_permission,
    profile_effort_values,
    profile_from_context,
    profile_models,
    resolve_profile_effort,
)
from .handlers.interactive_ui import (
    INTERACTIVE_TOOL_NAMES,
    clear_interactive_mode,
    clear_interactive_msg,
    get_interactive_msg_id,
    get_interactive_window,
    handle_interactive_ui,
    set_interactive_mode,
)
from .handlers.message_queue import (
    clear_permanent_delivery_failure,
    clear_status_msg_info,
    enqueue_content_message,
    enqueue_status_update,
    get_message_queue,
    shutdown_workers,
)
from .handlers.message_sender import (
    NO_LINK_PREVIEW,
    safe_edit,
    safe_reply,
    safe_send,
    send_with_fallback,
)
from .handlers.input_routing import (
    parse_input_routing_callback,
)
from .handlers import agent_login as _agent_login
from .handlers import native_input as _native_input
from .handlers import window_lifecycle as _window_lifecycle
from .handlers.agent_login import (
    ACCOUNT_COMMAND_DESCRIPTION,
    AGENT_AUTH_RECOVERY_MESSAGE as AGENT_AUTH_RECOVERY_MESSAGE,
    AGENT_LOGIN_COMMAND_DESCRIPTION,
    AGENT_LOGIN_TIMEOUT_SECONDS as AGENT_LOGIN_TIMEOUT_SECONDS,
)
from .handlers.native_input import (
    AGENT_INPUT_CONFIRM_MAX_WAIT_SECONDS,
    AGENT_INPUT_CONFIRM_RETRY_SECONDS,
    AGENT_INPUT_PERSIST_RETRY_SECONDS,
    AGENT_INPUT_POLL_INTERVAL_SECONDS,
    BOT_WRAPPER_MARKER,
    INPUT_ROUTE_CHOICE_MAX_AGE_SECONDS,
    TRANSIENT_AGENT_ERROR_CODES,
    TRANSIENT_AGENT_RETRY_DELAYS_SECONDS,
    TRANSIENT_AGENT_RETRY_PROMPT,
    QueuedAgentInput,
)
from .handlers.window_lifecycle import (
    PENDING_THREAD_MEDIA_KEY as PENDING_THREAD_MEDIA_KEY,
    PRODUCT_NAME,
    AgentLaunchSpec,
    PendingThreadMedia,
    WindowLaunchSpec,
)
from .markdown_v2 import convert_markdown
from .model_catalog import refresh_model_catalog
from .handlers.response_builder import build_response_parts
from .handlers.status_polling import (
    clear_window_working,
    forget_missing_bound_window as forget_missing_bound_window,
    mark_window_working,
    status_poll_loop,
)
from .idle_sessions import idle_session_hibernator as idle_session_hibernator
from .screenshot import text_to_image
from .session import (
    CodexSession,
    GroupChatRouteConflict,
    _session_ids_match,
    session_manager,
)
from .session_monitor import NewMessage
from .terminal_parser import (
    extract_bash_output,
    is_codex_input_ready,
    is_interactive_ui,
)
from .tmux_manager import tmux_manager
from .transcript_parser import TranscriptParser
from .transcribe import TranscriptionError, close_client as close_transcribe_client
from .transcribe import transcribe_voice, validate_voice_duration
from .turn_admission import turn_admission
from .update_processor import TopicUpdateProcessor
from .updater import (
    CodexUpdateResult,
    check_codex_update,
    load_codex_update_settings,
    load_update_env,
)
from .utils import app_dir, sanitize_forward_text
from .utils import atomic_write_json
from .vps_health import VpsHealthMonitor, format_health_snapshot

logger = logging.getLogger(__name__)
runtime_logger = logging.getLogger("telegram_agent_bot.runtime")

POLL_TIMEOUT_SECONDS = 30
DEFAULT_REQUEST_CONNECT_TIMEOUT_SECONDS = 10.0
DEFAULT_REQUEST_READ_TIMEOUT_SECONDS = 20.0
DEFAULT_REQUEST_WRITE_TIMEOUT_SECONDS = 10.0
DEFAULT_REQUEST_POOL_TIMEOUT_SECONDS = 5.0
GET_UPDATES_CONNECT_TIMEOUT_SECONDS = 10.0
GET_UPDATES_READ_TIMEOUT_SECONDS = POLL_TIMEOUT_SECONDS + 5.0
GET_UPDATES_WRITE_TIMEOUT_SECONDS = 10.0
GET_UPDATES_POOL_TIMEOUT_SECONDS = 5.0
MEDIA_DOWNLOAD_ATTEMPTS = 3
MEDIA_DOWNLOAD_RETRY_DELAY_SECONDS = 1.0
MEDIA_DOWNLOAD_CONNECT_TIMEOUT_SECONDS = 15.0
MEDIA_DOWNLOAD_READ_TIMEOUT_SECONDS = 60.0
MEDIA_DOWNLOAD_WRITE_TIMEOUT_SECONDS = 30.0
MEDIA_DOWNLOAD_POOL_TIMEOUT_SECONDS = 10.0
BACKGROUND_WAIT_TOOL_STATUS_TEXT = "💭 Thinking…\n◦ Working in background terminal…"
_F = TypeVar("_F", bound=Callable[..., Any])


def _bot_runtime() -> Any:
    return sys.modules[__name__]


def _bot_wrapper(func: _F) -> _F:
    setattr(func, "_bot_wrapper_marker", BOT_WRAPPER_MARKER)
    return func


# Active backend and local session monitor reference.
agent_backend: AgentBackend | None = None
session_monitor: Any | None = None

# Status polling task
_status_poll_task: asyncio.Task | None = None
_auto_update_task: asyncio.Task | None = None
_health_task: asyncio.Task | None = None
_runtime_stopped = False
_codex_update_prompted_versions: set[str] = set()
_codex_update_apply_lock: asyncio.Lock | None = None
_CODEX_UPDATE_PROMPT_STATE_FILENAME = "codex_update_prompt_state.json"


_QueuedAgentInput = QueuedAgentInput

_AGENT_INPUT_POLL_INTERVAL_SECONDS = AGENT_INPUT_POLL_INTERVAL_SECONDS
_AGENT_INPUT_PERSIST_RETRY_SECONDS = AGENT_INPUT_PERSIST_RETRY_SECONDS
_AGENT_INPUT_CONFIRM_RETRY_SECONDS = AGENT_INPUT_CONFIRM_RETRY_SECONDS
_AGENT_INPUT_CONFIRM_MAX_WAIT_SECONDS = AGENT_INPUT_CONFIRM_MAX_WAIT_SECONDS
_INPUT_ROUTE_CHOICE_MAX_AGE_SECONDS = INPUT_ROUTE_CHOICE_MAX_AGE_SECONDS
_TRANSIENT_AGENT_RETRY_DELAYS_SECONDS = TRANSIENT_AGENT_RETRY_DELAYS_SECONDS
_TRANSIENT_AGENT_ERROR_CODES = TRANSIENT_AGENT_ERROR_CODES
_TRANSIENT_AGENT_RETRY_PROMPT = TRANSIENT_AGENT_RETRY_PROMPT

_agent_input_queues: dict[tuple[int, int, str], deque[_QueuedAgentInput]] = (
    _native_input._agent_input_queues
)
_agent_input_tasks: dict[tuple[int, int, str], asyncio.Task] = (
    _native_input._agent_input_tasks
)
_agent_input_locks: dict[tuple[int, int], asyncio.Lock] = (
    _native_input._agent_input_locks
)
_agent_input_confirmation_tasks: dict[int, asyncio.Task[None]] = (
    _native_input._agent_input_confirmation_tasks
)
_agent_input_confirmation_targets: dict[int, tuple[int, int, str]] = (
    _native_input._agent_input_confirmation_targets
)
_agent_input_confirmation_modes: dict[int, str] = (
    _native_input._agent_input_confirmation_modes
)
_transient_agent_retry_attempts: dict[str, int] = (
    _native_input._transient_agent_retry_attempts
)
_transient_agent_retry_tasks: dict[str, asyncio.Task[None]] = (
    _native_input._transient_agent_retry_tasks
)
_runtime_store = DurableRuntimeStore(config.config_dir / "runtime.sqlite3")
_health_monitor = VpsHealthMonitor(_runtime_store)

WELCOME_MESSAGE = (
    f"🤖 *{PRODUCT_NAME} Monitor*\n\n"
    "Each topic is a session. Create a new topic to start."
)
UNSUPPORTED_CONTENT_MESSAGE = (
    "⚠ Only text, photo, file, and voice messages are supported. Stickers, video, "
    f"and other media cannot be forwarded to {PRODUCT_NAME}."
)
PHOTO_CONFIRMATION_MESSAGE = f"📷 Image sent to {PRODUCT_NAME}."
FILE_CONFIRMATION_MESSAGE = f"📎 File sent to {PRODUCT_NAME}."
PHOTO_QUEUED_MESSAGE = (
    f"📷 Image queued for {PRODUCT_NAME}; it will send after the current response."
)
FILE_QUEUED_MESSAGE = (
    f"📎 File queued for {PRODUCT_NAME}; it will send after the current response."
)
SESSION_STILL_RUNNING_MESSAGE = f"The {PRODUCT_NAME} session is still running in tmux."
HELP_COMMAND_DESCRIPTION = f"↗ Show {PRODUCT_NAME} help"
ESC_COMMAND_DESCRIPTION = f"Interrupt current {PRODUCT_NAME} run"
INTERRUPT_COMMAND_DESCRIPTION = "Interrupt; optional text sends next"
STEER_COMMAND_DESCRIPTION = "Guide the current agent turn"
QUEUE_COMMAND_DESCRIPTION = "Queue input for the next agent turn"
USAGE_COMMAND_DESCRIPTION = f"Show {PRODUCT_NAME} usage remaining"
_AGENT_LOGIN_DEFAULT_KEY = _agent_login._AGENT_LOGIN_DEFAULT_KEY
_ANSI_ESCAPE_RE = _agent_login._ANSI_ESCAPE_RE
_DEVICE_LOGIN_URL_RE = _agent_login._DEVICE_LOGIN_URL_RE
_DEVICE_LOGIN_CODE_RE = _agent_login._DEVICE_LOGIN_CODE_RE
_agent_login_tasks: dict[str, asyncio.Task[None]] = _agent_login._agent_login_tasks


async def _safe_send_typing_action(chat: Chat, *, source: str) -> None:
    """Send typing action best-effort without aborting the handler."""
    try:
        await chat.send_action(ChatAction.TYPING)
    except TelegramError as exc:
        logger.debug("Failed to send typing action (%s): %s", source, exc)


# Agent commands shown in the global bot menu (forwarded via tmux). Keep this
# list to commands that are useful in both Codex and Claude Code; agent-specific
# or plugin skill commands remain available through /agentcmd.
CC_COMMANDS: dict[str, str] = {
    "agentcmd": "↗ Send a custom agent command",
    "clear": "↗ Clear conversation history",
    "compact": "↗ Compact conversation context",
    "cost": "↗ Show token/cost usage",
    "goal": "↗ Set or update session goal",
    "help": HELP_COMMAND_DESCRIPTION,
    "memory": "↗ Edit AGENTS.md",
    "model": "↗ Switch AI model",
    "fast": "↗ Toggle Fast mode",
    "skills": "↗ Browse agent skills",
    "plugins": "↗ Browse and manage plugins",
    "status": "↗ Show agent session status",
    "permissions": "↗ Manage agent permissions",
    "mcp": "↗ Manage MCP servers",
    "plan": "↗ Enter plan mode",
    "init": "↗ Initialize project instructions",
}


_FORWARDED_COMMAND_RE = FORWARDED_COMMAND_RE
_COMMAND_ARG_SEPARATORS = COMMAND_ARG_SEPARATORS
_DirectoryBrowserKwargs = DirectoryBrowserKwargs
_RootSelection = RootSelection
_browse_root_context = browse_root_context
_directory_browser_kwargs = directory_browser_kwargs
_browse_backend_context = browse_backend_context
_as_browser = as_browser
_parse_root_selection = parse_root_selection
_remote_child_path = remote_child_path
_remote_parent_path = remote_parent_path
_directory_listing_with_root = directory_listing_with_root
_clamp_to_selected_root = clamp_to_selected_root


def _normalize_forward_command_text(cmd_text: str) -> str:
    """Normalize Telegram slash commands before forwarding them to an agent."""
    return normalize_forward_command_text(cmd_text, known_commands=CC_COMMANDS)


def _adapt_forward_command_for_agent(cmd_text: str, agent_type: str) -> str:
    """Map one stable Telegram command to the active agent's native spelling."""
    return adapt_forward_command_for_agent(
        cmd_text,
        agent_type,
        known_commands=CC_COMMANDS,
    )


_PendingThreadMedia = PendingThreadMedia


def _default_directory_browser_path(root_path: str | None = None) -> str:
    """Choose a generic starting directory for the project browser."""
    return default_directory_browser_path(
        root_path,
        default_projects_path=config.default_projects_path,
    )


def _active_remote_root_browser() -> AgentBrowser | None:
    """Return the configured non-local backend browser for root selection."""
    return active_remote_root_browser(agent_backend)


def _browser_for_backend_id(backend_id: str) -> AgentBrowser | None:
    """Return the active backend browser matching a cached root selection."""
    return browser_for_backend_id(backend_id, agent_backend)


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
    browser = _browser_for_backend_id(backend_id)
    if browser is None:
        return None
    return await build_backend_directory_browser(
        backend_id=backend_id,
        node_id=node_id,
        path=path,
        page=page,
        root_label=root_label,
        root_path=root_path,
        browser=browser,
    )


@_bot_wrapper
async def _show_root_or_directory_picker(
    target: Any,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    edit: bool = False,
) -> None:
    await _window_lifecycle.show_root_or_directory_picker(
        target, context, edit=edit, runtime=_bot_runtime()
    )


@_bot_wrapper
def _clear_pending_thread_input(user_data: dict | None) -> None:
    _window_lifecycle.clear_pending_thread_input(user_data)


@_bot_wrapper
async def _start_unbound_topic_creation(
    message: Any,
    context: ContextTypes.DEFAULT_TYPE,
    user: Any,
    thread_id: int,
    *,
    text: str | None = None,
) -> None:
    await _window_lifecycle.start_unbound_topic_creation(
        message, context, user, thread_id, text=text, runtime=_bot_runtime()
    )


@_bot_wrapper
def _store_pending_thread_media(
    context: ContextTypes.DEFAULT_TYPE,
    *,
    kind: str,
    path: Path,
    filename: str,
    caption: str,
) -> None:
    _window_lifecycle.store_pending_thread_media(
        context, kind=kind, path=path, filename=filename, caption=caption
    )


@_bot_wrapper
async def _take_pending_thread_message(
    context: ContextTypes.DEFAULT_TYPE,
    *,
    user_id: int,
    thread_id: int,
    remote: bool,
) -> tuple[str | None, str | None]:
    return await _window_lifecycle.take_pending_thread_message(
        context,
        user_id=user_id,
        thread_id=thread_id,
        remote=remote,
        runtime=_bot_runtime(),
    )


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
        logger.warning(
            "Transient Telegram polling error; polling will retry automatically: %s",
            error,
        )
        return
    if isinstance(error, BadRequest):
        message = str(error).lower()
        if "query is too old" in message or "query id is invalid" in message:
            logger.info("Ignored expired Telegram callback query: %s", error)
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

    logger.exception(
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
    return user_id is not None and config.is_user_allowed(user_id)


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
        or not is_user_allowed(user.id)
        or chat is None
        or chat.type not in ("group", "supergroup")
    ):
        return
    thread_id = _get_thread_id(update)
    if thread_id is None:
        return
    try:
        # Synchronous claim: another chat cannot win between validation and use.
        session_manager.set_group_chat_id(user.id, thread_id, chat.id)
    except GroupChatRouteConflict:
        logger.warning("Rejected a topic route owned by another group")
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
        or not is_user_allowed(user.id)
        or message is None
        or chat is None
        or chat.type not in ("group", "supergroup")
        or getattr(message, "forum_topic_closed", None) is not None
    ):
        return
    thread_id = _get_thread_id(update)
    if thread_id is None:
        return

    session_manager.set_group_chat_id(user.id, thread_id, chat.id)
    if clear_permanent_delivery_failure(user.id, thread_id, chat_id=chat.id):
        monitor = session_monitor
        clear_block = getattr(monitor, "clear_permanent_delivery_block", None)
        if callable(clear_block):
            clear_block(user.id, thread_id)
        logger.info(
            "Cleared permanent Telegram delivery block after inbound route proof "
            "(user=%d thread=%d chat=%d)",
            user.id,
            thread_id,
            chat.id,
        )


@_bot_wrapper
def _filter_resumable_sessions(
    sessions: list["CodexSession"],
) -> list["CodexSession"]:
    return _window_lifecycle.filter_resumable_sessions(sessions, runtime=_bot_runtime())


@_bot_wrapper
def _has_trackable_session_for_window(window_id: str) -> bool:
    return _window_lifecycle.has_trackable_session_for_window(
        window_id, runtime=_bot_runtime()
    )


@_bot_wrapper
def _build_resume_conflict_keyboard() -> InlineKeyboardMarkup:
    return _window_lifecycle.build_resume_conflict_keyboard()


def _profile_models(agent_type: str) -> tuple[str, ...]:
    """Return configured model choices for one agent type."""
    return profile_models(agent_type, cfg=config)


def _profile_effort_values(agent_type: str, model: str) -> tuple[str, ...]:
    """Return reasoning efforts supported by the selected agent/model."""
    return profile_effort_values(agent_type, model, cfg=config)


def _resolve_profile_effort(agent_type: str, model: str, requested: str) -> str:
    """Keep a supported effort or fall back to the model/provider default."""
    return resolve_profile_effort(
        agent_type,
        model,
        requested,
        cfg=config,
        effort_values=_profile_effort_values,
    )


def _profile_from_context(user_data: dict | None) -> AgentProfile:
    """Build the pending topic profile, retaining legacy defaults."""
    return profile_from_context(
        user_data,
        cfg=config,
        resolve_effort=_resolve_profile_effort,
        default_permission_fn=_default_profile_permission,
    )


def _default_profile_permission(agent_type: str) -> str:
    """Return the configured default permission mode for one agent type."""
    return default_profile_permission(agent_type, cfg=config)


@_bot_wrapper
async def _show_agent_profile_picker(
    target: Any,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    edit: bool = True,
) -> None:
    await _window_lifecycle.show_agent_profile_picker(
        target, context, edit=edit, runtime=_bot_runtime()
    )


@_bot_wrapper
async def _show_agent_profile_settings(
    target: Any,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    edit: bool = True,
) -> None:
    await _window_lifecycle.show_agent_profile_settings(
        target, context, edit=edit, runtime=_bot_runtime()
    )


@_bot_wrapper
def _clear_creation_state(user_data: dict | None) -> None:
    _window_lifecycle.clear_creation_state(user_data)


@_bot_wrapper
def _creation_path(user_data: dict | None, key: str) -> str | None:
    return _window_lifecycle.creation_path(user_data, key)


@_bot_wrapper
async def _reject_expired_project_selection(
    query: Any, context: ContextTypes.DEFAULT_TYPE
) -> None:
    await _window_lifecycle.reject_expired_project_selection(
        query, context, runtime=_bot_runtime()
    )


@_bot_wrapper
async def _continue_creation_with_profile(
    query: Any,
    context: ContextTypes.DEFAULT_TYPE,
    user: Any,
) -> None:
    await _window_lifecycle.continue_creation_with_profile(
        query, context, user, runtime=_bot_runtime()
    )


# --- Command handlers ---


def _output_mode_keyboard(mode: str) -> InlineKeyboardMarkup:
    normalized = normalize_output_mode(mode)
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    f"{'✅ ' if normalized == OUTPUT_MODE_CLEAN else ''}Clean",
                    callback_data=f"{CB_OUTPUT_MODE}{OUTPUT_MODE_CLEAN}",
                ),
                InlineKeyboardButton(
                    f"{'✅ ' if normalized == OUTPUT_MODE_TRACE else ''}Trace",
                    callback_data=f"{CB_OUTPUT_MODE}{OUTPUT_MODE_TRACE}",
                ),
            ]
        ]
    )


def _output_mode_text(mode: str) -> str:
    normalized = normalize_output_mode(mode)
    if normalized == OUTPUT_MODE_CLEAN:
        detail = "只显示最终回答、错误和必要的交互确认。"
    else:
        detail = "额外显示公开工具摘要；模型 reasoning 始终隐藏。"
    return f"Output mode: *{output_mode_label(normalized)}*\n\n{detail}"


async def output_mode_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Show or update the per-topic Telegram output mode."""
    del context
    user = update.effective_user
    if not user or not is_user_allowed(user.id) or not update.message:
        return

    thread_id = _get_thread_id(update)
    parts = (update.message.text or "").split(maxsplit=1)
    if len(parts) > 1 and parts[1].strip().lower() in {"clean", "trace"}:
        mode = session_manager.set_output_mode(
            user.id, thread_id, parts[1].strip().lower()
        )
        await safe_reply(update.message, f"✅ {_output_mode_text(mode)}")
        return

    mode = session_manager.get_output_mode(user.id, thread_id)
    await safe_reply(
        update.message,
        _output_mode_text(mode),
        reply_markup=_output_mode_keyboard(mode),
    )


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        if update.message:
            await safe_reply(update.message, "You are not authorized to use this bot.")
        return

    clear_browse_state(context.user_data)

    if update.message:
        await safe_reply(
            update.message,
            WELCOME_MESSAGE,
        )


async def history_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show message history for the active session or bound thread."""
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        return
    if not update.message:
        return

    thread_id = _get_thread_id(update)
    wid = session_manager.resolve_window_for_thread(user.id, thread_id)
    if not wid:
        target = _remote_target_for_thread(user.id, thread_id)
        if target:
            await safe_reply(
                update.message,
                "❌ History is only available for local tmux sessions.",
            )
            return
        await safe_reply(update.message, "❌ No session bound to this topic.")
        return

    await send_history(
        update.message,
        wid,
        output_mode=session_manager.get_output_mode(user.id, thread_id),
    )


async def screenshot_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Capture the current tmux pane and send it as an image."""
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        return
    if not update.message:
        return

    thread_id = _get_thread_id(update)
    wid = session_manager.resolve_window_for_thread(user.id, thread_id)
    target = session_manager.resolve_target_for_thread(user.id, thread_id)
    if not wid and not target:
        await safe_reply(update.message, "❌ No session bound to this topic.")
        return

    capture = await capture_agent_output(
        user.id,
        thread_id,
        wid or "",
        with_ansi=True,
    )
    if capture is None:
        await safe_reply(update.message, "❌ No session bound to this topic.")
        return
    if capture.missing:
        display = session_manager.get_display_name(wid or capture.target.window_id)
        await safe_reply(update.message, f"❌ Window '{display}' no longer exists.")
        return

    text = capture.text
    if not text:
        await safe_reply(update.message, "❌ Failed to capture pane content.")
        return

    png_bytes = await text_to_image(text, with_ansi=True)
    keyboard = (
        _build_screenshot_keyboard(capture.target.window_id)
        if capture.target.window_id
        else None
    )
    await update.message.reply_document(
        document=io.BytesIO(png_bytes),
        filename="screenshot.png",
        reply_markup=keyboard,
    )


async def unbind_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Unbind this topic from its Codex session without killing the window."""
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        return
    if not update.message:
        return

    thread_id = _get_thread_id(update)
    if thread_id is None:
        await safe_reply(update.message, "❌ This command only works in a topic.")
        return

    ambiguous_wid = session_manager.get_ambiguous_window_for_thread(user.id, thread_id)
    if isinstance(ambiguous_wid, str) and ambiguous_wid:
        await _discard_queued_agent_input(user.id, thread_id, ambiguous_wid)
        session_manager.unbind_thread(user.id, thread_id)
        await clear_topic_state(user.id, thread_id, context.bot, context.user_data)
        await safe_reply(
            update.message,
            "✅ Removed the ambiguous topic binding. Send a message to create or "
            "resume a dedicated session.",
        )
        return

    wid = session_manager.get_window_for_thread(user.id, thread_id)
    if not wid:
        target = _remote_target_for_thread(user.id, thread_id)
        if target:
            session_manager.unbind_thread(user.id, thread_id)
            await clear_topic_state(user.id, thread_id, context.bot, context.user_data)
            await safe_reply(
                update.message,
                f"✅ Topic unbound from `{_format_remote_target(target)}`.\n"
                "Send a message to bind to a new session.",
            )
            return
        await safe_reply(update.message, "❌ No session bound to this topic.")
        return

    display = session_manager.get_display_name(wid)
    await _discard_queued_agent_input(user.id, thread_id, wid)
    session_manager.unbind_thread(user.id, thread_id)
    await clear_topic_state(user.id, thread_id, context.bot, context.user_data)

    await safe_reply(
        update.message,
        f"✅ Topic unbound from window '{display}'.\n"
        f"{SESSION_STILL_RUNNING_MESSAGE}\n"
        "Send a message to bind to a new session.",
    )


async def esc_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send Escape key to interrupt Codex."""
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        return
    if not update.message:
        return

    thread_id = _get_thread_id(update)
    ambiguous_wid = (
        session_manager.get_ambiguous_window_for_thread(user.id, thread_id)
        if thread_id is not None
        else None
    )
    if isinstance(ambiguous_wid, str) and ambiguous_wid:
        await safe_reply(
            update.message,
            "❌ This topic shares an agent window with another topic. Control is "
            "paused to prevent affecting the wrong session; use /unbind here first.",
        )
        return
    wid = session_manager.resolve_window_for_thread(user.id, thread_id)
    target = session_manager.resolve_target_for_thread(user.id, thread_id)
    if not wid and not target:
        await safe_reply(update.message, "❌ No session bound to this topic.")
        return

    dropped = await _discard_queued_agent_input(user.id, thread_id, wid or "")
    if dropped:
        logger.info(
            "Discarded %d queued input(s) before Escape (user=%d thread=%s window=%s)",
            dropped,
            user.id,
            thread_id,
            wid or "",
        )

    result = await send_agent_control(user.id, thread_id, wid or "", "Escape")
    if result is None:
        await safe_reply(update.message, "❌ No session bound to this topic.")
        return
    if result.missing:
        display = session_manager.get_display_name(wid or result.target.window_id)
        await safe_reply(update.message, f"❌ Window '{display}' no longer exists.")
        return

    if not result.ok:
        await safe_reply(update.message, "❌ Failed to send Escape.")
        return
    if wid:
        clear_window_working(user.id, wid, thread_id)
        await enqueue_status_update(
            context.bot, user.id, wid, None, thread_id=thread_id
        )
    else:
        clear_status_msg_info(user.id, thread_id)
    await safe_reply(update.message, "⎋ Sent Escape")


async def interrupt_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send Escape, then submit an optional replacement message."""
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        return
    if not update.message:
        return

    parts = (update.message.text or "").split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        await esc_command(update, context)
        return

    thread_id = _get_thread_id(update)
    if thread_id is None:
        await safe_reply(update.message, "❌ Please use a named topic.")
        return

    ambiguous_wid = session_manager.get_ambiguous_window_for_thread(user.id, thread_id)
    if isinstance(ambiguous_wid, str) and ambiguous_wid:
        await safe_reply(
            update.message,
            "❌ This topic shares an agent window with another topic. Interrupt is "
            "paused to prevent affecting the wrong session; use /unbind here first.",
        )
        return

    chat = update.effective_chat
    if chat and chat.type in ("group", "supergroup"):
        session_manager.set_group_chat_id(user.id, thread_id, chat.id)

    payload = sanitize_forward_text(parts[1])
    if not payload:
        await safe_reply(
            update.message,
            "❌ This message only contained wrapper metadata and no forwardable text.",
        )
        return

    wid = session_manager.resolve_window_for_thread(user.id, thread_id)
    target = session_manager.resolve_target_for_thread(user.id, thread_id)
    if not wid and not target:
        await safe_reply(update.message, "❌ No session bound to this topic.")
        return

    w = None
    if wid:
        w = await tmux_manager.find_window_by_id(wid)
        if not w:
            display = session_manager.get_display_name(wid)
            await safe_reply(update.message, f"❌ Window '{display}' no longer exists.")
            return

    await _safe_send_typing_action(update.message.chat, source="interrupt_command")
    if wid:
        await enqueue_status_update(
            context.bot, user.id, wid, None, thread_id=thread_id
        )
    else:
        clear_status_msg_info(user.id, thread_id)
    _cancel_bash_capture(user.id, thread_id)
    dropped = await _discard_queued_agent_input(user.id, thread_id, wid or "")
    if dropped:
        logger.info(
            "Discarded %d queued input(s) before interrupt replacement "
            "(user=%d thread=%s window=%s)",
            dropped,
            user.id,
            thread_id,
            wid or "",
        )

    pane_text = None
    if wid:
        capture = await capture_agent_output(user.id, thread_id, wid)
        pane_text = capture.text if capture and not capture.missing else None
        if pane_text and is_interactive_ui(pane_text):
            await handle_interactive_ui(context.bot, user.id, wid, thread_id)
            await asyncio.sleep(0.3)

        if w and await session_manager.window_has_usage_limit_exceeded(wid):
            handled = await _rotate_thread_after_usage_limit(
                context=context,
                user_id=user.id,
                thread_id=thread_id,
                current_window_id=wid,
                current_window_cwd=w.cwd,
                text=payload,
            )
            if handled:
                return

    queued_replacement = False
    if wid:
        if not is_codex_input_ready(pane_text or ""):
            ok, control_message = await _send_control_to_agent(
                user.id,
                thread_id,
                wid,
                "Escape",
            )
            if not ok:
                await safe_reply(update.message, f"❌ {control_message}")
                return
            await asyncio.sleep(0.3)
        success, message, queued_replacement = await _send_or_queue_agent_input(
            context.bot,
            user.id,
            thread_id,
            wid,
            payload,
        )
    else:
        ok, control_message = await _send_control_to_agent(
            user.id,
            thread_id,
            "",
            "Escape",
        )
        if not ok:
            await safe_reply(update.message, f"❌ {control_message}")
            return
        await asyncio.sleep(0.3)
        success, message = await _send_message_to_agent(
            user.id,
            thread_id,
            "",
            payload,
        )

    if not success:
        await safe_reply(update.message, f"❌ {message}")
        return
    if wid:
        if queued_replacement:
            await mark_window_working(context.bot, user.id, wid, thread_id)
            await safe_reply(update.message, f"⎋ {message}")
            return
        await mark_window_working(context.bot, user.id, wid, thread_id)


@_bot_wrapper
async def _native_input_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    mode: str,
) -> None:
    await _native_input.run_native_input_command(
        update, context, mode=mode, runtime=_bot_runtime()
    )


async def steer_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /steer: send native steer input to an active supported turn."""
    await _native_input_command(update, context, mode=AGENT_INPUT_MODE_STEER)


async def queue_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /queue: send native queue input for the current topic turn."""
    await _native_input_command(update, context, mode=AGENT_INPUT_MODE_QUEUE)


async def health_command(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show one bounded host/runtime health snapshot on demand."""
    user = update.effective_user
    if not user or not is_user_allowed(user.id) or not update.message:
        return
    try:
        snapshot = await _health_monitor.snapshot(session_monitor)
        issues = _health_monitor.issues(
            snapshot,
            config,
            active_keys=_health_monitor.active_issue_keys(),
        )
        await safe_reply(
            update.message,
            format_health_snapshot(
                snapshot,
                issues,
                language=config.health_notification_language,
            ),
        )
    except Exception:
        logger.exception("Failed to build on-demand host health snapshot")
        await safe_reply(update.message, "❌ Host health snapshot is unavailable.")


async def usage_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Fetch Codex usage stats from TUI and send to Telegram."""
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        return
    if not update.message:
        return

    thread_id = _get_thread_id(update)
    wid = session_manager.resolve_window_for_thread(user.id, thread_id)
    if not wid:
        await safe_reply(update.message, "No session bound to this topic.")
        return

    w = await tmux_manager.find_window_by_id(wid)
    if not w:
        await safe_reply(update.message, f"Window '{wid}' no longer exists.")
        return

    # Send /usage command to Codex TUI
    await tmux_manager.send_keys(w.window_id, "/usage")
    # Wait for the modal to render
    await asyncio.sleep(2.0)
    # Capture the pane content
    pane_text = await tmux_manager.capture_pane(w.window_id)
    # Dismiss the modal
    await tmux_manager.send_keys(w.window_id, "Escape", enter=False, literal=False)

    if not pane_text:
        await safe_reply(update.message, "Failed to capture usage info.")
        return

    # Try to parse structured usage info
    from .terminal_parser import parse_usage_output

    usage = parse_usage_output(pane_text)
    if usage and usage.parsed_lines:
        text = "\n".join(usage.parsed_lines)
        await safe_reply(update.message, f"```\n{text}\n```")
    else:
        # Fallback: send raw pane capture trimmed
        trimmed = pane_text.strip()
        if len(trimmed) > 3000:
            trimmed = trimmed[:3000] + "\n... (truncated)"
        await safe_reply(update.message, f"```\n{trimmed}\n```")


def _requested_agent_type(agent_type: str | None = None) -> str:
    return _agent_login.requested_agent_type(agent_type, runtime=_bot_runtime())


def _agent_command_name(agent_type: str, action: str) -> str:
    return _agent_login.agent_command_name(agent_type, action)


def _agent_login_executable(agent_type: str | None = None) -> str:
    return _agent_login.agent_login_executable(agent_type, runtime=_bot_runtime())


def _agent_login_args(agent_type: str | None = None) -> list[str]:
    return _agent_login.agent_login_args(agent_type, runtime=_bot_runtime())


def _extract_device_login_details(output: str) -> tuple[str | None, str | None]:
    return _agent_login.extract_device_login_details(output)


def _login_display_name(account_name: str | None, agent_type: str | None = None) -> str:
    return _agent_login.login_display_name(
        account_name, agent_type, runtime=_bot_runtime()
    )


def _codex_auth_recovery_message(_pane_error: str | None = None) -> str:
    return _agent_login.codex_auth_recovery_message(_pane_error)


def _account_command_usage(agent_type: str | None = None) -> str:
    return _agent_login.account_command_usage(agent_type, runtime=_bot_runtime())


def _format_account_status(agent_type: str | None = None) -> str:
    return _agent_login.format_account_status(agent_type, runtime=_bot_runtime())


async def _wait_for_agent_login_details(
    process: asyncio.subprocess.Process,
    *,
    require_code: bool = True,
) -> tuple[str | None, str | None]:
    return await _agent_login.wait_for_agent_login_details(
        process, require_code=require_code
    )


async def _agent_login_worker(
    *,
    bot: Bot,
    chat_id: int,
    thread_id: int | None,
    account_name: str | None,
    login_key: str,
    agent_type: str,
) -> None:
    await _agent_login.agent_login_worker(
        bot=bot,
        chat_id=chat_id,
        thread_id=thread_id,
        account_name=account_name,
        login_key=login_key,
        agent_type=agent_type,
        runtime=_bot_runtime(),
    )


async def _agent_login_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE, agent_type: str | None = None
) -> None:
    await _agent_login.run_agent_login_command(
        update, context, agent_type=agent_type, runtime=_bot_runtime()
    )


async def agent_login_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle /agentlogin [account]: start device login for the configured agent."""
    await _agent_login_command(update, context)


async def codex_login_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle /codexlogin [account]: start Codex device auth."""
    await _agent_login_command(update, context, AGENT_CODEX)


async def claude_login_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle /claudelogin [account]: start Claude login."""
    await _agent_login_command(update, context, AGENT_CLAUDE)


async def cursor_login_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle /cursorlogin [account]: start Cursor agent login."""
    await _agent_login_command(update, context, AGENT_CURSOR)


async def _agent_account_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE, agent_type: str | None = None
) -> None:
    await _agent_login.run_agent_account_command(
        update, context, agent_type=agent_type, runtime=_bot_runtime()
    )


async def agent_account_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle /agentaccount: inspect, switch, or add agent login accounts."""
    await _agent_account_command(update, context)


async def codex_account_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle /codexaccount: inspect, switch, or add Codex login accounts."""
    await _agent_account_command(update, context, AGENT_CODEX)


async def claude_account_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle /claudeaccount: inspect, switch, or add Claude login accounts."""
    await _agent_account_command(update, context, AGENT_CLAUDE)


# --- Screenshot keyboard with quick control keys ---

# key_id → (tmux_key, enter, literal)
_KEYS_SEND_MAP: dict[str, tuple[str, bool, bool]] = {
    "up": ("Up", False, False),
    "dn": ("Down", False, False),
    "lt": ("Left", False, False),
    "rt": ("Right", False, False),
    "esc": ("Escape", False, False),
    "ent": ("Enter", False, False),
    "spc": ("Space", False, False),
    "tab": ("Tab", False, False),
    "cc": ("C-c", False, False),
}

# key_id → display label (shown in callback answer toast)
_KEY_LABELS: dict[str, str] = {
    "up": "↑",
    "dn": "↓",
    "lt": "←",
    "rt": "→",
    "esc": "⎋ Esc",
    "ent": "⏎ Enter",
    "spc": "␣ Space",
    "tab": "⇥ Tab",
    "cc": "^C",
}


def _build_screenshot_keyboard(window_id: str) -> InlineKeyboardMarkup:
    """Build inline keyboard for screenshot: control keys + refresh."""

    def btn(label: str, key_id: str) -> InlineKeyboardButton:
        return InlineKeyboardButton(
            label,
            callback_data=f"{CB_KEYS_PREFIX}{key_id}:{window_id}"[:64],
        )

    return InlineKeyboardMarkup(
        [
            [btn("␣ Space", "spc"), btn("↑", "up"), btn("⇥ Tab", "tab")],
            [btn("←", "lt"), btn("↓", "dn"), btn("→", "rt")],
            [btn("⎋ Esc", "esc"), btn("^C", "cc"), btn("⏎ Enter", "ent")],
            [
                InlineKeyboardButton(
                    "🔄 Refresh",
                    callback_data=f"{CB_SCREENSHOT_REFRESH}{window_id}"[:64],
                )
            ],
        ]
    )


@_bot_wrapper
async def _send_control_to_agent(
    user_id: int,
    thread_id: int | None,
    window_id: str,
    key: str,
) -> tuple[bool, str]:
    return await _native_input.send_control_to_agent(
        user_id, thread_id, window_id, key, runtime=_bot_runtime()
    )


@_bot_wrapper
async def _send_message_to_agent(
    user_id: int,
    thread_id: int | None,
    window_id: str,
    text: str,
) -> tuple[bool, str]:
    return await _native_input.send_message_to_agent(
        user_id, thread_id, window_id, text, runtime=_bot_runtime()
    )


@_bot_wrapper
async def _send_native_input_to_agent(
    user_id: int,
    thread_id: int | None,
    window_id: str,
    text: str,
    *,
    mode: str,
) -> tuple[bool, str]:
    return await _native_input.send_native_input_to_agent(
        user_id, thread_id, window_id, text, mode=mode, runtime=_bot_runtime()
    )


@_bot_wrapper
def _window_agent_type(window_id: str) -> str:
    return _native_input.window_agent_type(window_id, runtime=_bot_runtime())


@_bot_wrapper
def _window_supports_native_input_routing(window_id: str) -> bool:
    return _native_input.window_supports_native_input_routing(
        window_id, runtime=_bot_runtime()
    )


@_bot_wrapper
def _route_can_use_native_input(key: tuple[int, int, str]) -> bool:
    return _native_input.route_can_use_native_input(key, runtime=_bot_runtime())


@_bot_wrapper
async def _offer_input_route_choice(
    message: Any,
    *,
    user_id: int,
    thread_id: int,
    window_id: str,
    text: str,
) -> bool:
    return await _native_input.offer_input_route_choice(
        message,
        user_id=user_id,
        thread_id=thread_id,
        window_id=window_id,
        text=text,
        runtime=_bot_runtime(),
    )


@_bot_wrapper
def _agent_input_key(
    user_id: int, thread_id: int | None, window_id: str
) -> tuple[int, int, str]:
    return _native_input.agent_input_key(user_id, thread_id, window_id)


@_bot_wrapper
def _should_offer_input_route_choice(
    *,
    user_id: int,
    thread_id: int,
    window_id: str,
    pane_text: str | None,
) -> bool:
    return _native_input.should_offer_input_route_choice(
        user_id=user_id,
        thread_id=thread_id,
        window_id=window_id,
        pane_text=pane_text,
        runtime=_bot_runtime(),
    )


@_bot_wrapper
def _agent_input_lock(key: tuple[int, int, str]) -> asyncio.Lock:
    return _native_input.agent_input_lock(key, runtime=_bot_runtime())


@_bot_wrapper
def _reset_transient_agent_retry(window_id: str) -> None:
    _native_input.reset_transient_agent_retry(window_id, runtime=_bot_runtime())


@_bot_wrapper
def _agent_input_queue_max_size() -> int:
    return _native_input.agent_input_queue_max_size(runtime=_bot_runtime())


@_bot_wrapper
def _agent_input_queue_max_wait_seconds() -> float:
    return _native_input.agent_input_queue_max_wait_seconds(runtime=_bot_runtime())


@_bot_wrapper
def _agent_max_active_turns() -> int:
    return _native_input.agent_max_active_turns(runtime=_bot_runtime())


@_bot_wrapper
def _queue_agent_input(
    key: tuple[int, int, str],
    text: str,
    *,
    submission_mode: str = AGENT_INPUT_MODE_TURN,
) -> tuple[bool, int, int]:
    return _native_input.queue_agent_input(
        key, text, submission_mode=submission_mode, runtime=_bot_runtime()
    )


@_bot_wrapper
def _delete_persisted_agent_inputs(items: list[_QueuedAgentInput]) -> None:
    _native_input.delete_persisted_agent_inputs(items, runtime=_bot_runtime())


@_bot_wrapper
async def _mark_persisted_agent_input_submitted(
    item: _QueuedAgentInput,
    window_id: str,
) -> bool:
    return await _native_input.mark_persisted_agent_input_submitted(
        item,
        window_id,
        runtime=_bot_runtime(),
    )


@_bot_wrapper
def _confirm_persisted_agent_input(item: _QueuedAgentInput) -> None:
    _native_input.confirm_persisted_agent_input(item, runtime=_bot_runtime())


@_bot_wrapper
def _clear_persisted_agent_input_target(key: tuple[int, int, str]) -> None:
    _native_input.clear_persisted_agent_input_target(key, runtime=_bot_runtime())


@_bot_wrapper
def _clear_persisted_agent_input_route(key: tuple[int, int, str]) -> None:
    _native_input.clear_persisted_agent_input_route(key, runtime=_bot_runtime())


@_bot_wrapper
def _same_agent_input_route(
    left: tuple[int, int, str],
    right: tuple[int, int, str],
) -> bool:
    return _native_input.same_agent_input_route(left, right)


@_bot_wrapper
def _is_current_bound_agent_input_target(key: tuple[int, int, str]) -> bool:
    return _native_input.is_current_bound_agent_input_target(
        key, runtime=_bot_runtime()
    )


@_bot_wrapper
def _target_has_agent_input_confirmation(key: tuple[int, int, str]) -> bool:
    return _native_input.target_has_agent_input_confirmation(
        key, runtime=_bot_runtime()
    )


@_bot_wrapper
def _route_has_agent_input_queue(key: tuple[int, int, str]) -> bool:
    return _native_input.route_has_agent_input_queue(key, runtime=_bot_runtime())


@_bot_wrapper
def _route_has_agent_input_drain(key: tuple[int, int, str]) -> bool:
    return _native_input.route_has_agent_input_drain(key, runtime=_bot_runtime())


@_bot_wrapper
def _target_has_persisted_agent_input_confirmation(
    key: tuple[int, int, str],
) -> bool:
    return _native_input.target_has_persisted_agent_input_confirmation(
        key, runtime=_bot_runtime()
    )


@_bot_wrapper
def _agent_input_confirmation_expired(record: PendingAgentInput) -> bool:
    return _native_input.agent_input_confirmation_expired(
        record, runtime=_bot_runtime()
    )


@_bot_wrapper
async def _run_agent_input_confirmation(
    bot: Bot,
    record_id: int,
    key: tuple[int, int, str],
    *,
    recover_submission: bool,
    initial_delay_seconds: float | None = None,
    notify_on_first_delay: bool = False,
) -> None:
    await _native_input.run_agent_input_confirmation(
        bot,
        record_id,
        key,
        recover_submission=recover_submission,
        initial_delay_seconds=initial_delay_seconds,
        notify_on_first_delay=notify_on_first_delay,
        runtime=_bot_runtime(),
    )


@_bot_wrapper
def _ensure_agent_input_confirmation_task(
    bot: Bot,
    record_id: int,
    key: tuple[int, int, str],
    *,
    recover_submission: bool,
    initial_delay_seconds: float | None = None,
    notify_on_first_delay: bool = False,
    submission_mode: str = AGENT_INPUT_MODE_TURN,
) -> None:
    _native_input.ensure_agent_input_confirmation_task(
        bot,
        record_id,
        key,
        recover_submission=recover_submission,
        initial_delay_seconds=initial_delay_seconds,
        notify_on_first_delay=notify_on_first_delay,
        submission_mode=submission_mode,
        runtime=_bot_runtime(),
    )


@_bot_wrapper
async def _cancel_agent_input_confirmations_for_target(
    key: tuple[int, int, str],
) -> None:
    await _native_input.cancel_agent_input_confirmations_for_target(
        key, runtime=_bot_runtime()
    )


@_bot_wrapper
def _restore_agent_input_queues(bot: Bot) -> int:
    return _native_input.restore_agent_input_queues(bot, runtime=_bot_runtime())


@_bot_wrapper
async def _drop_expired_agent_input(
    bot: Bot,
    key: tuple[int, int, str],
    queue: deque[_QueuedAgentInput],
) -> None:
    await _native_input.drop_expired_agent_input(
        bot, key, queue, runtime=_bot_runtime()
    )


@_bot_wrapper
def _ensure_agent_input_drain_task(
    bot: Bot,
    key: tuple[int, int, str],
) -> None:
    _native_input.ensure_agent_input_drain_task(bot, key, runtime=_bot_runtime())


@_bot_wrapper
async def _queue_agent_input_after_interrupt(
    bot: Bot,
    user_id: int,
    thread_id: int | None,
    window_id: str,
    text: str,
) -> tuple[bool, str]:
    return await _native_input.queue_agent_input_after_interrupt(
        bot, user_id, thread_id, window_id, text, runtime=_bot_runtime()
    )


@_bot_wrapper
async def _send_native_agent_input(
    bot: Bot,
    user_id: int,
    thread_id: int,
    window_id: str,
    text: str,
    *,
    mode: str,
) -> tuple[bool, str]:
    return await _native_input.send_native_agent_input(
        bot,
        user_id,
        thread_id,
        window_id,
        text,
        mode=mode,
        runtime=_bot_runtime(),
    )


@_bot_wrapper
def _agent_cli_startup_failure(capture: Any) -> str | None:
    return _native_input.agent_cli_startup_failure(capture)


@_bot_wrapper
async def _send_or_queue_agent_input(
    bot: Bot,
    user_id: int,
    thread_id: int | None,
    window_id: str,
    text: str,
    *,
    transient_retry: bool = False,
) -> tuple[bool, str, bool]:
    return await _native_input.send_or_queue_agent_input(
        bot,
        user_id,
        thread_id,
        window_id,
        text,
        transient_retry=transient_retry,
        runtime=_bot_runtime(),
    )


@_bot_wrapper
async def _run_transient_agent_retry(
    bot: Bot,
    key: tuple[int, int, str],
    *,
    attempt: int,
    delay_seconds: float,
) -> None:
    await _native_input.run_transient_agent_retry(
        bot,
        key,
        attempt=attempt,
        delay_seconds=delay_seconds,
        runtime=_bot_runtime(),
    )


@_bot_wrapper
def _schedule_transient_agent_retry(
    bot: Bot,
    user_id: int,
    thread_id: int | None,
    window_id: str,
) -> tuple[int, float] | None:
    return _native_input.schedule_transient_agent_retry(
        bot,
        user_id,
        thread_id,
        window_id,
        runtime=_bot_runtime(),
    )


@_bot_wrapper
def _restore_transient_agent_retries(bot: Bot) -> int:
    return _native_input.restore_transient_agent_retries(bot, runtime=_bot_runtime())


@_bot_wrapper
async def _cancel_transient_agent_retry_tasks() -> None:
    await _native_input.cancel_transient_agent_retry_tasks(runtime=_bot_runtime())


@_bot_wrapper
async def _notify_queued_input_failure(
    bot: Bot,
    user_id: int,
    thread_id: int | None,
    message: str,
) -> None:
    await _native_input.notify_queued_input_failure(
        bot, user_id, thread_id, message, runtime=_bot_runtime()
    )


@_bot_wrapper
async def _notify_agent_input_confirmation_delayed(
    bot: Bot,
    user_id: int,
    thread_id: int | None,
) -> None:
    await _native_input.notify_agent_input_confirmation_delayed(
        bot, user_id, thread_id, runtime=_bot_runtime()
    )


@_bot_wrapper
async def _notify_agent_input_confirmation_expired(
    bot: Bot,
    user_id: int,
    thread_id: int | None,
) -> None:
    await _native_input.notify_agent_input_confirmation_expired(
        bot, user_id, thread_id, runtime=_bot_runtime()
    )


@_bot_wrapper
async def _notify_rebound_agent_input_unconfirmed(
    bot: Bot,
    user_id: int,
    thread_id: int | None,
) -> None:
    await _native_input.notify_rebound_agent_input_unconfirmed(
        bot, user_id, thread_id, runtime=_bot_runtime()
    )


@_bot_wrapper
async def _drain_agent_input_queue(
    bot: Bot,
    key: tuple[int, int, str],
) -> None:
    await _native_input.drain_agent_input_queue(bot, key, runtime=_bot_runtime())


@_bot_wrapper
async def _cancel_agent_input_drain_tasks() -> None:
    await _native_input.cancel_agent_input_drain_tasks(runtime=_bot_runtime())


@_bot_wrapper
async def _cancel_agent_input_confirmation_tasks() -> None:
    await _native_input.cancel_agent_input_confirmation_tasks(runtime=_bot_runtime())


@_bot_wrapper
async def _discard_queued_agent_input(
    user_id: int,
    thread_id: int | None,
    window_id: str,
) -> int:
    return await _native_input.discard_queued_agent_input(
        user_id, thread_id, window_id, runtime=_bot_runtime()
    )


@_bot_wrapper
def _remote_target_for_thread(
    user_id: int,
    thread_id: int | None,
) -> AgentTarget | None:
    return _window_lifecycle.remote_target_for_thread(
        user_id, thread_id, runtime=_bot_runtime()
    )


@_bot_wrapper
def _format_remote_target(target: AgentTarget) -> str:
    return _window_lifecycle.format_remote_target(target)


@_bot_wrapper
async def _create_agent_local_window(
    spec: AgentLaunchSpec | None = None,
    *,
    cwd: str = "",
    window_name: str = "",
    resume_session_id: str = "",
    account_name: str = "",
    agent_type: str = "",
    model: str = "",
    reasoning_effort: str = "",
    fast_mode: bool = False,
    permission_mode: str = "ask",
) -> tuple[bool, str, str, str, AgentTarget | None]:
    resolved = spec or AgentLaunchSpec(
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
    return await _window_lifecycle.create_agent_local_window(
        resolved, runtime=_bot_runtime()
    )


@_bot_wrapper
async def _create_agent_target(
    spec: AgentLaunchSpec | None = None,
    *,
    cwd: str = "",
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
    resolved = spec or AgentLaunchSpec(
        cwd=cwd,
        node_id=node_id,
        window_name=window_name,
        resume_session_id=resume_session_id,
        account_name=account_name,
        agent_type=agent_type,
        model=model,
        reasoning_effort=reasoning_effort,
        fast_mode=fast_mode,
        permission_mode=permission_mode,
    )
    return await _window_lifecycle.create_agent_target(resolved, runtime=_bot_runtime())


async def topic_closed_handler(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle topic closure — kill the associated tmux window and clean up state."""
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        return

    thread_id = _get_thread_id(update)
    if thread_id is None:
        return

    chat = update.effective_chat
    chat_id = chat.id if chat else session_manager.resolve_chat_id(user.id, thread_id)
    wid = session_manager.get_window_for_thread(user.id, thread_id)
    if wid:
        display = session_manager.get_display_name(wid)
        state = session_manager.window_states.get(wid)
        session_id = state.session_id if state else ""
        w = await tmux_manager.find_window_by_id(wid)
        if w:
            await tmux_manager.kill_window(w.window_id)
            logger.info(
                "Topic closed: killed window %s (user=%d, thread=%d)",
                display,
                user.id,
                thread_id,
            )
        else:
            logger.info(
                "Topic closed: window %s already gone (user=%d, thread=%d)",
                display,
                user.id,
                thread_id,
            )
        if session_id:
            session_manager.hide_session(session_id)
        await _discard_queued_agent_input(user.id, thread_id, wid)
        session_manager.unbind_thread(user.id, thread_id)
        await session_manager.remove_session_map_entry(wid)
        session_manager.remove_window_state(wid)
        if session_id and session_monitor is not None:
            session_monitor.state.remove_session(session_id)
            session_monitor.state.save_if_dirty()
        # Clean up all memory state for this topic
        await clear_topic_state(user.id, thread_id, context.bot, context.user_data)
        try:
            await context.bot.delete_forum_topic(
                chat_id=chat_id,
                message_thread_id=thread_id,
            )
            logger.info(
                "Topic closed: deleted topic (chat_id=%s, thread=%d)",
                chat_id,
                thread_id,
            )
        except BadRequest as exc:
            message = str(exc)
            if "Topic_id_invalid" in message or "message thread not found" in message:
                logger.info(
                    "Topic closed: topic already deleted (chat_id=%s, thread=%d)",
                    chat_id,
                    thread_id,
                )
            else:
                logger.warning(
                    "Topic closed: failed to delete topic (chat_id=%s, thread=%d): %s",
                    chat_id,
                    thread_id,
                    exc,
                )
        except TelegramError as exc:
            logger.warning(
                "Topic closed: failed to delete topic (chat_id=%s, thread=%d): %s",
                chat_id,
                thread_id,
                exc,
            )
    else:
        await clear_topic_state(user.id, thread_id, context.bot, context.user_data)
        try:
            await context.bot.delete_forum_topic(
                chat_id=chat_id,
                message_thread_id=thread_id,
            )
            logger.info(
                "Topic closed: deleted unbound topic (chat_id=%s, thread=%d)",
                chat_id,
                thread_id,
            )
        except BadRequest as exc:
            message = str(exc)
            if "Topic_id_invalid" in message or "message thread not found" in message:
                logger.info(
                    "Topic closed: unbound topic already deleted (chat_id=%s, thread=%d)",
                    chat_id,
                    thread_id,
                )
            else:
                logger.debug(
                    "Topic closed: no binding and delete failed (user=%d, thread=%d): %s",
                    user.id,
                    thread_id,
                    exc,
                )
        except TelegramError as exc:
            logger.debug(
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
    if not user or not is_user_allowed(user.id):
        return

    msg = update.message
    if not msg or not msg.forum_topic_edited:
        return

    new_name = msg.forum_topic_edited.name
    if new_name is None:
        # Icon-only change, no rename needed
        return

    thread_id = _get_thread_id(update)
    if thread_id is None:
        return

    wid = session_manager.get_window_for_thread(user.id, thread_id)
    if not wid:
        logger.debug(
            "Topic edited: no binding (user=%d, thread=%d)", user.id, thread_id
        )
        return

    old_name = session_manager.get_display_name(wid)
    await tmux_manager.rename_window(wid, new_name)
    session_manager.update_display_name(wid, new_name)
    logger.info(
        "Topic renamed: '%s' -> '%s' (window=%s, user=%d, thread=%d)",
        old_name,
        new_name,
        wid,
        user.id,
        thread_id,
    )


@_bot_wrapper
async def _send_local_forwarded_command(
    bot: Bot,
    message: Any,
    *,
    user_id: int,
    thread_id: int | None,
    window_id: str,
    command_text: str,
) -> tuple[bool, str, str]:
    return await _native_input.send_local_forwarded_command(
        bot,
        message,
        user_id=user_id,
        thread_id=thread_id,
        window_id=window_id,
        command_text=command_text,
        runtime=_bot_runtime(),
    )


async def forward_command_handler(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Forward any non-bot command to the active Codex or Claude session."""
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        return
    if not update.message:
        return

    thread_id = _get_thread_id(update)

    # Capture group chat_id for supergroup forum topic routing.
    # Required: Telegram Bot API needs group chat_id (not user_id) to send
    # messages with message_thread_id. Do NOT remove — see session.py docs.
    chat = update.effective_chat
    if chat and chat.type in ("group", "supergroup"):
        session_manager.set_group_chat_id(user.id, thread_id, chat.id)

    cmd_text = update.message.text or ""
    # The full text is already a slash command like "/clear" or "/compact foo".
    # Strip only a Telegram bot mention in the command token; keep @mentions
    # in command arguments intact.
    normalized_slash = _normalize_forward_command_text(cmd_text)
    wid = session_manager.resolve_window_for_thread(user.id, thread_id)
    target = session_manager.resolve_target_for_thread(user.id, thread_id)
    if not wid and not target:
        await safe_reply(update.message, "❌ No session bound to this topic.")
        return

    agent_type = _window_agent_type(wid) if wid else config.agent_type
    cc_slash = _adapt_forward_command_for_agent(normalized_slash, agent_type)
    target_window_id = target.window_id if target else ""
    display = session_manager.get_display_name(wid or target_window_id)
    logger.info(
        "Forwarding command %s to window %s (user=%d)", cc_slash, display, user.id
    )
    await _safe_send_typing_action(update.message.chat, source="history_command")
    is_clear_command = cc_slash.strip().lower() == "/clear"
    if wid and not is_clear_command:
        success, message, delivery = await _send_local_forwarded_command(
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
            await safe_reply(update.message, f"❌ {message}")
            return
        if delivery == "native":
            await safe_reply(update.message, f"⚡ [{display}] {message}")
            return
        action = "Queued" if delivery == "queued" else "Sent"
        await safe_reply(update.message, f"⚡ [{display}] {action}: {cc_slash}")
        return

    result = await send_agent_message(user.id, thread_id, wid or "", cc_slash)
    if result is None:
        await safe_reply(update.message, "❌ No session bound to this topic.")
        return
    if result.missing:
        await safe_reply(update.message, f"❌ Window '{display}' no longer exists.")
        return

    if result.ok:
        await safe_reply(update.message, f"⚡ [{display}] Sent: {cc_slash}")
        # If /clear command was sent, clear the session association
        # so we can detect the new session after first message.  Keep /clear
        # out of the bot-side queue so this state update remains synchronous.
        if wid and is_clear_command:
            logger.info("Clearing session for window %s after /clear", display)
            session_manager.clear_window_session(wid)

        # Interactive commands (e.g. /model) render a terminal-based UI
        # with no JSONL tool_use entry.  The status poller already detects
        # interactive UIs every 1s (status_polling.py), so no
        # proactive detection needed here — the poller handles it.
    else:
        await safe_reply(update.message, f"❌ {result.message}")


async def agent_command_mode(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Forward an arbitrary agent slash command through one stable bot command.

    Examples: ``/agentcmd /review`` and ``/cmd compact``.
    """
    user = update.effective_user
    message = update.message
    if not user or not is_user_allowed(user.id) or not message:
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        await safe_reply(message, "Usage: /agentcmd /command [arguments]")
        return

    command_text = parts[1].strip()
    if not command_text.startswith("/"):
        command_text = "/" + command_text
    command_text = _normalize_forward_command_text(command_text)
    thread_id = _get_thread_id(update)
    wid = session_manager.resolve_window_for_thread(user.id, thread_id)
    target = session_manager.resolve_target_for_thread(user.id, thread_id)
    if not wid and not target:
        await safe_reply(message, "❌ No session bound to this topic.")
        return
    if wid:
        command_text = _adapt_forward_command_for_agent(
            command_text,
            _window_agent_type(wid),
        )
        success, result_message, delivery = await _send_local_forwarded_command(
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
            await safe_reply(message, f"❌ {result_message}")
            return
        if delivery == "native":
            await safe_reply(message, f"⚡ {result_message}")
            return
        action = "Queued" if delivery == "queued" else "Sent"
        await safe_reply(message, f"⚡ {action}: {command_text}")
        return

    command_text = _adapt_forward_command_for_agent(command_text, config.agent_type)
    result = await send_agent_message(user.id, thread_id, "", command_text)
    if result is None or not result.ok:
        await safe_reply(
            message, f"❌ {result.message if result else 'No session bound'}"
        )
        return
    await safe_reply(message, f"⚡ Sent: {command_text}")


async def unsupported_content_handler(
    update: Update,
    _context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Reply to non-text messages (stickers, video, etc.)."""
    if not update.message:
        return
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        return
    logger.debug("Unsupported content from user %d", user.id)
    await safe_reply(
        update.message,
        UNSUPPORTED_CONTENT_MESSAGE,
    )


# --- Image directory for incoming photos ---
_IMAGES_DIR = app_dir() / "images"
_IMAGES_DIR.mkdir(parents=True, exist_ok=True)
_FILES_DIR = app_dir() / "files"
_FILES_DIR.mkdir(parents=True, exist_ok=True)
_PENDING_TOPIC_DELETIONS_FILE = app_dir() / "pending_topic_deletions.json"
_SAFE_UPLOAD_FILENAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


def _safe_upload_filename(filename: str, *, fallback: str = "upload.bin") -> str:
    """Return a path-safe ASCII filename for local and remote uploads."""
    name = Path(filename).name or fallback
    safe = _SAFE_UPLOAD_FILENAME_RE.sub("_", name).strip("._")
    return safe or fallback


async def _download_telegram_media(media: Any, file_path: Path, *, label: str) -> None:
    """Download Telegram media with media-specific timeouts and transient retries."""
    timeout_kwargs = {
        "connect_timeout": MEDIA_DOWNLOAD_CONNECT_TIMEOUT_SECONDS,
        "read_timeout": MEDIA_DOWNLOAD_READ_TIMEOUT_SECONDS,
        "write_timeout": MEDIA_DOWNLOAD_WRITE_TIMEOUT_SECONDS,
        "pool_timeout": MEDIA_DOWNLOAD_POOL_TIMEOUT_SECONDS,
    }

    for attempt in range(1, MEDIA_DOWNLOAD_ATTEMPTS + 1):
        try:
            tg_file = await media.get_file(**timeout_kwargs)
            await tg_file.download_to_drive(file_path, **timeout_kwargs)
            return
        except (TimedOut, NetworkError) as exc:
            if attempt >= MEDIA_DOWNLOAD_ATTEMPTS:
                raise
            with contextlib.suppress(OSError):
                file_path.unlink()
            logger.warning(
                "Telegram %s download failed with transient network error; "
                "retrying: attempt=%d/%d path=%s error=%s",
                label,
                attempt,
                MEDIA_DOWNLOAD_ATTEMPTS,
                file_path,
                exc,
            )
            await asyncio.sleep(MEDIA_DOWNLOAD_RETRY_DELAY_SECONDS)


async def process_pending_topic_deletions(bot: Bot) -> None:
    """Delete queued forum topics from a previous local cleanup request."""
    if not _PENDING_TOPIC_DELETIONS_FILE.exists():
        return

    try:
        payload = json.loads(_PENDING_TOPIC_DELETIONS_FILE.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning(
            "Failed to load pending topic deletions file %s: %s",
            _PENDING_TOPIC_DELETIONS_FILE,
            exc,
        )
        return

    if not isinstance(payload, list):
        logger.warning(
            "Invalid pending topic deletions payload in %s",
            _PENDING_TOPIC_DELETIONS_FILE,
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
            logger.warning("Skipping invalid pending topic deletion entry: %s", entry)
            continue

        try:
            await bot.delete_forum_topic(
                chat_id=chat_id,
                message_thread_id=thread_id,
            )
            deleted_count += 1
            session_manager.clear_group_chat_id(user_id, thread_id)
            logger.info(
                "Deleted queued topic (chat_id=%s, thread_id=%d)",
                chat_id,
                thread_id,
            )
        except BadRequest as exc:
            message = str(exc)
            if "Topic_id_invalid" in message or "message thread not found" in message:
                deleted_count += 1
                session_manager.clear_group_chat_id(user_id, thread_id)
                logger.info(
                    "Queued topic already gone (chat_id=%s, thread_id=%d)",
                    chat_id,
                    thread_id,
                )
            else:
                failed_entries.append(entry)
                logger.warning(
                    "Failed to delete queued topic (chat_id=%s, thread_id=%d): %s",
                    chat_id,
                    thread_id,
                    exc,
                )
        except TelegramError as exc:
            failed_entries.append(entry)
            logger.warning(
                "Failed to delete queued topic (chat_id=%s, thread_id=%d): %s",
                chat_id,
                thread_id,
                exc,
            )

    if failed_entries:
        atomic_write_json(_PENDING_TOPIC_DELETIONS_FILE, failed_entries)
    else:
        _PENDING_TOPIC_DELETIONS_FILE.unlink(missing_ok=True)

    logger.info(
        "Processed pending topic deletions: total=%d deleted=%d failed=%d skipped=%d",
        len(payload),
        deleted_count,
        len(failed_entries),
        skipped_count,
    )


async def photo_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle photos sent by the user: download and forward path to Codex."""
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        if update.message:
            await safe_reply(update.message, "You are not authorized to use this bot.")
        return

    if not update.message or not update.message.photo:
        return

    chat = update.message.chat
    thread_id = _get_thread_id(update)
    if chat.type in ("group", "supergroup") and thread_id is not None:
        session_manager.set_group_chat_id(user.id, thread_id, chat.id)

    # Must be in a named topic
    if thread_id is None:
        await safe_reply(
            update.message,
            "❌ Please use a named topic. Create a new topic to start a session.",
        )
        return

    ambiguous_wid = session_manager.get_ambiguous_window_for_thread(user.id, thread_id)
    if isinstance(ambiguous_wid, str) and ambiguous_wid:
        await safe_reply(
            update.message,
            "❌ This topic shares an agent window with another topic. Messages are "
            "paused to prevent cross-topic delivery; use /unbind here first.",
        )
        return

    wid = session_manager.get_window_for_thread(user.id, thread_id)
    remote_target = None
    if wid is None:
        remote_target = _remote_target_for_thread(user.id, thread_id)

    w = None
    if wid is not None:
        w = await tmux_manager.find_window_by_id(wid)

    # Download the highest-resolution photo
    photo = update.message.photo[-1]

    # Save to ~/.telegram-agent-bot/images/<timestamp>_<file_unique_id>.jpg
    filename = f"{time.time_ns()}_{photo.file_unique_id}.jpg"
    file_path = _IMAGES_DIR / filename
    try:
        await _download_telegram_media(photo, file_path, label="photo")
    except (TelegramError, OSError) as exc:
        logger.exception("Failed to download Telegram photo")
        await safe_reply(update.message, f"❌ Failed to download image: {exc}")
        return

    if wid is None and remote_target is None:
        _store_pending_thread_media(
            context,
            kind="photo",
            path=file_path,
            filename=filename,
            caption=update.message.caption or "",
        )
        await safe_reply(
            update.message,
            "📎 Attachment received. Choose a project to start a session; it will be sent automatically.",
        )
        await _start_unbound_topic_creation(update.message, context, user, thread_id)
        return

    agent_file_path = str(file_path)
    if remote_target is not None:
        upload = await upload_agent_file(
            user.id,
            thread_id,
            "",
            str(file_path),
            filename=filename,
        )
        if upload is None:
            await safe_reply(update.message, "❌ No session bound")
            return
        if not upload.ok or not upload.path:
            await safe_reply(
                update.message,
                f"❌ File transfer failed: {upload.message or 'backend unavailable'}",
            )
            return
        agent_file_path = upload.path

    # Build the message to send to Codex
    caption = update.message.caption or ""
    if caption:
        text_to_send = f"{caption}\n\n(image attached: {agent_file_path})"
    else:
        text_to_send = f"(image attached: {agent_file_path})"

    if wid and not w:
        await _handle_missing_bound_window_input(
            update_message=update.message,
            user_id=user.id,
            thread_id=thread_id,
            window_id=wid,
            text=text_to_send,
            success_reply=PHOTO_CONFIRMATION_MESSAGE,
        )
        return

    pane_text: str | None = None
    if wid and w:
        pane_cmd = (getattr(w, "pane_current_command", "") or "").strip()
        handled = await _handle_non_codex_bound_window(
            update_message=update.message,
            user_id=user.id,
            thread_id=thread_id,
            window_id=wid,
            pane_command=pane_cmd,
            text=text_to_send,
            success_reply=PHOTO_CONFIRMATION_MESSAGE,
        )
        if handled:
            return
        capture = await capture_agent_output(user.id, thread_id, wid)
        pane_text = capture.text if capture and not capture.missing else None
        if pane_text:
            handled = await _handle_auth_error_bound_window(
                update_message=update.message,
                user_id=user.id,
                thread_id=thread_id,
                window_id=wid,
                pane_text=pane_text,
                text=text_to_send,
                success_reply=PHOTO_CONFIRMATION_MESSAGE,
            )
            if handled:
                return

    await _safe_send_typing_action(update.message.chat, source="photo_handler")
    if wid:
        await enqueue_status_update(
            context.bot, user.id, wid, None, thread_id=thread_id
        )
    else:
        clear_status_msg_info(user.id, thread_id)

    if wid and w and await session_manager.window_has_usage_limit_exceeded(wid):
        await _rotate_thread_after_usage_limit(
            context=context,
            user_id=user.id,
            thread_id=thread_id,
            current_window_id=wid,
            current_window_cwd=w.cwd,
            text=text_to_send,
        )
        return

    if wid and _should_offer_input_route_choice(
        user_id=user.id,
        thread_id=thread_id,
        window_id=wid,
        pane_text=pane_text,
    ):
        await _offer_input_route_choice(
            update.message,
            user_id=user.id,
            thread_id=thread_id,
            window_id=wid,
            text=text_to_send,
        )
        return

    if wid:
        success, message, queued = await _send_or_queue_agent_input(
            context.bot,
            user.id,
            thread_id,
            wid,
            text_to_send,
        )
    else:
        success, message = await _send_message_to_agent(
            user.id,
            thread_id,
            "",
            text_to_send,
        )
        queued = False
    if not success:
        await safe_reply(update.message, f"❌ {message}")
        return

    # Confirm to user
    await safe_reply(
        update.message,
        PHOTO_QUEUED_MESSAGE if queued else PHOTO_CONFIRMATION_MESSAGE,
    )
    if wid and not queued:
        await mark_window_working(context.bot, user.id, wid, thread_id)


async def document_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle files sent by the user: download and forward path to Codex."""
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        if update.message:
            await safe_reply(update.message, "You are not authorized to use this bot.")
        return

    if not update.message or not update.message.document:
        return

    chat = update.message.chat
    thread_id = _get_thread_id(update)
    if chat.type in ("group", "supergroup") and thread_id is not None:
        session_manager.set_group_chat_id(user.id, thread_id, chat.id)

    if thread_id is None:
        await safe_reply(
            update.message,
            "❌ Please use a named topic. Create a new topic to start a session.",
        )
        return

    ambiguous_wid = session_manager.get_ambiguous_window_for_thread(user.id, thread_id)
    if isinstance(ambiguous_wid, str) and ambiguous_wid:
        await safe_reply(
            update.message,
            "❌ This topic shares an agent window with another topic. Messages are "
            "paused to prevent cross-topic delivery; use /unbind here first.",
        )
        return

    wid = session_manager.get_window_for_thread(user.id, thread_id)
    remote_target = None
    if wid is None:
        remote_target = _remote_target_for_thread(user.id, thread_id)

    w = None
    if wid is not None:
        w = await tmux_manager.find_window_by_id(wid)

    document = update.message.document
    document_name = _safe_upload_filename(
        document.file_name or f"{document.file_unique_id}.bin"
    )
    unique_id = _safe_upload_filename(document.file_unique_id, fallback="file")
    local_filename = f"{time.time_ns()}_{unique_id}_{document_name}"
    file_path = _FILES_DIR / local_filename
    try:
        await _download_telegram_media(document, file_path, label="document")
    except (TelegramError, OSError) as exc:
        logger.exception("Failed to download Telegram document")
        await safe_reply(update.message, f"❌ Failed to download file: {exc}")
        return

    if wid is None and remote_target is None:
        _store_pending_thread_media(
            context,
            kind="document",
            path=file_path,
            filename=document_name,
            caption=update.message.caption or "",
        )
        await safe_reply(
            update.message,
            "📎 Attachment received. Choose a project to start a session; it will be sent automatically.",
        )
        await _start_unbound_topic_creation(update.message, context, user, thread_id)
        return

    agent_file_path = str(file_path)
    if remote_target is not None:
        upload = await upload_agent_file(
            user.id,
            thread_id,
            "",
            str(file_path),
            filename=document_name,
        )
        if upload is None:
            await safe_reply(update.message, "❌ No session bound")
            return
        if not upload.ok or not upload.path:
            await safe_reply(
                update.message,
                f"❌ File transfer failed: {upload.message or 'backend unavailable'}",
            )
            return
        agent_file_path = upload.path

    caption = update.message.caption or ""
    if caption:
        text_to_send = f"{caption}\n\n(file attached: {agent_file_path})"
    else:
        text_to_send = f"(file attached: {agent_file_path})"

    if wid and not w:
        await _handle_missing_bound_window_input(
            update_message=update.message,
            user_id=user.id,
            thread_id=thread_id,
            window_id=wid,
            text=text_to_send,
            success_reply=FILE_CONFIRMATION_MESSAGE,
        )
        return

    pane_text: str | None = None
    if wid and w:
        pane_cmd = (getattr(w, "pane_current_command", "") or "").strip()
        handled = await _handle_non_codex_bound_window(
            update_message=update.message,
            user_id=user.id,
            thread_id=thread_id,
            window_id=wid,
            pane_command=pane_cmd,
            text=text_to_send,
            success_reply=FILE_CONFIRMATION_MESSAGE,
        )
        if handled:
            return
        capture = await capture_agent_output(user.id, thread_id, wid)
        pane_text = capture.text if capture and not capture.missing else None
        if pane_text:
            handled = await _handle_auth_error_bound_window(
                update_message=update.message,
                user_id=user.id,
                thread_id=thread_id,
                window_id=wid,
                pane_text=pane_text,
                text=text_to_send,
                success_reply=FILE_CONFIRMATION_MESSAGE,
            )
            if handled:
                return

    await _safe_send_typing_action(update.message.chat, source="document_handler")
    if wid:
        await enqueue_status_update(
            context.bot, user.id, wid, None, thread_id=thread_id
        )
    else:
        clear_status_msg_info(user.id, thread_id)

    if wid and w and await session_manager.window_has_usage_limit_exceeded(wid):
        await _rotate_thread_after_usage_limit(
            context=context,
            user_id=user.id,
            thread_id=thread_id,
            current_window_id=wid,
            current_window_cwd=w.cwd,
            text=text_to_send,
        )
        return

    if wid and _should_offer_input_route_choice(
        user_id=user.id,
        thread_id=thread_id,
        window_id=wid,
        pane_text=pane_text,
    ):
        await _offer_input_route_choice(
            update.message,
            user_id=user.id,
            thread_id=thread_id,
            window_id=wid,
            text=text_to_send,
        )
        return

    if wid:
        success, message, queued = await _send_or_queue_agent_input(
            context.bot,
            user.id,
            thread_id,
            wid,
            text_to_send,
        )
    else:
        success, message = await _send_message_to_agent(
            user.id,
            thread_id,
            "",
            text_to_send,
        )
        queued = False
    if not success:
        await safe_reply(update.message, f"❌ {message}")
        return

    await safe_reply(
        update.message,
        FILE_QUEUED_MESSAGE if queued else FILE_CONFIRMATION_MESSAGE,
    )
    if wid and not queued:
        await mark_window_working(context.bot, user.id, wid, thread_id)


async def voice_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle voice messages: transcribe and forward text to Codex/Claude."""
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        if update.message:
            await safe_reply(update.message, "You are not authorized to use this bot.")
        return

    if not update.message or not update.message.voice:
        return

    duration_seconds = getattr(update.message.voice, "duration", None)
    try:
        validate_voice_duration(duration_seconds)
    except TranscriptionError as exc:
        await safe_reply(update.message, f"⚠ {exc}")
        return

    if (
        not config.transcription_openai_api_key
        and not config.transcription_google_api_key
    ):
        await safe_reply(
            update.message,
            "⚠ Voice transcription requires an API key.\n"
            "Set `AI_TRANSCRIPTION_OPENAI_API_KEY` or `AI_TRANSCRIPTION_GOOGLE_API_KEY` "
            "in your `.env` file and restart the bot.",
        )
        return

    chat = update.message.chat
    thread_id = _get_thread_id(update)
    if chat.type in ("group", "supergroup") and thread_id is not None:
        session_manager.set_group_chat_id(user.id, thread_id, chat.id)

    if thread_id is None:
        await safe_reply(
            update.message,
            "❌ Please use a named topic. Create a new topic to start a session.",
        )
        return

    wid = session_manager.get_window_for_thread(user.id, thread_id)
    remote_target = None
    if wid is None:
        remote_target = _remote_target_for_thread(user.id, thread_id)
    if wid is None and remote_target is None:
        await safe_reply(
            update.message,
            "❌ No session bound to this topic. Send a text message first to create one.",
        )
        return

    w = None
    if wid:
        w = await tmux_manager.find_window_by_id(wid)

    # Download voice as in-memory bytes
    voice_file = await update.message.voice.get_file()
    ogg_data = bytes(await voice_file.download_as_bytearray())

    # Transcribe
    try:
        text = await transcribe_voice(ogg_data, duration_seconds=duration_seconds)
    except TranscriptionError as e:
        await safe_reply(update.message, f"⚠ {e}")
        return
    except Exception as e:
        logger.error("Voice transcription failed: %s", e)
        await safe_reply(update.message, f"⚠ Transcription failed: {e}")
        return

    if wid and not w:
        await _handle_missing_bound_window_input(
            update_message=update.message,
            user_id=user.id,
            thread_id=thread_id,
            window_id=wid,
            text=text,
            success_reply=f'🎤 "{text}"',
        )
        return

    await _safe_send_typing_action(update.message.chat, source="voice_handler")
    if wid:
        await enqueue_status_update(
            context.bot, user.id, wid, None, thread_id=thread_id
        )
    else:
        clear_status_msg_info(user.id, thread_id)

    if wid and w and await session_manager.window_has_usage_limit_exceeded(wid):
        await _rotate_thread_after_usage_limit(
            context=context,
            user_id=user.id,
            thread_id=thread_id,
            current_window_id=wid,
            current_window_cwd=w.cwd,
            text=text,
        )
        return

    if wid:
        capture = await capture_agent_output(user.id, thread_id, wid)
        pane_text = capture.text if capture and not capture.missing else None
        if pane_text:
            handled = await _handle_auth_error_bound_window(
                update_message=update.message,
                user_id=user.id,
                thread_id=thread_id,
                window_id=wid,
                pane_text=pane_text,
                text=text,
                success_reply=f'🎤 "{text}"',
            )
            if handled:
                return

    if wid:
        success, message, queued = await _send_or_queue_agent_input(
            context.bot,
            user.id,
            thread_id,
            wid,
            text,
        )
    else:
        success, message = await _send_message_to_agent(
            user.id,
            thread_id,
            "",
            text,
        )
        queued = False
    if not success:
        await safe_reply(update.message, f"❌ {message}")
        return

    await safe_reply(
        update.message,
        f'🎤 Queued: "{text}"' if queued else f'🎤 "{text}"',
    )
    if wid and not queued:
        await mark_window_working(context.bot, user.id, wid, thread_id)


# Active bash capture tasks: (user_id, thread_id) → asyncio.Task
_bash_capture_tasks: dict[tuple[int, int], asyncio.Task[None]] = {}


def _cancel_bash_capture(user_id: int, thread_id: int) -> None:
    """Cancel any running bash capture for this topic."""
    key = (user_id, thread_id)
    task = _bash_capture_tasks.pop(key, None)
    if task and not task.done():
        task.cancel()


async def _capture_bash_output(
    bot: Bot,
    user_id: int,
    thread_id: int,
    window_id: str,
    command: str,
) -> None:
    """Background task: capture ``!`` bash command output from tmux pane.

    Sends the first captured output as a new message, then edits it
    in-place as more output appears.  Stops after 30 s or when cancelled
    (e.g. user sends a new message, which pushes content down).
    """
    try:
        # Wait for the command to start producing output
        await asyncio.sleep(2.0)

        chat_id = session_manager.resolve_chat_id(user_id, thread_id)
        msg_id: int | None = None
        last_output: str = ""

        for _ in range(30):
            capture = await capture_agent_output(user_id, thread_id, window_id)
            if capture is None or capture.missing:
                return
            raw = capture.text
            if raw is None:
                return

            output = extract_bash_output(raw, command)
            if not output:
                await asyncio.sleep(1.0)
                continue

            # Skip edit if nothing changed
            if output == last_output:
                await asyncio.sleep(1.0)
                continue

            last_output = output

            # Truncate to fit Telegram's 4096-char limit
            if len(output) > 3800:
                output = "… " + output[-3800:]

            if msg_id is None:
                # First capture — send a new message
                sent = await send_with_fallback(
                    bot,
                    chat_id,
                    output,
                    message_thread_id=thread_id,
                )
                if sent:
                    msg_id = sent.message_id
            else:
                # Subsequent captures — edit in place
                try:
                    await bot.edit_message_text(
                        chat_id=chat_id,
                        message_id=msg_id,
                        text=convert_markdown(output),
                        parse_mode="MarkdownV2",
                        link_preview_options=NO_LINK_PREVIEW,
                    )
                except Exception:
                    try:
                        await bot.edit_message_text(
                            chat_id=chat_id,
                            message_id=msg_id,
                            text=output,
                            link_preview_options=NO_LINK_PREVIEW,
                        )
                    except Exception:
                        pass

            await asyncio.sleep(1.0)
    except asyncio.CancelledError:
        return
    finally:
        _bash_capture_tasks.pop((user_id, thread_id), None)


@_bot_wrapper
async def _rotate_thread_after_usage_limit(
    *,
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
    thread_id: int,
    current_window_id: str,
    current_window_cwd: str,
    text: str,
) -> bool:
    return await _window_lifecycle.rotate_thread_after_usage_limit(
        context=context,
        user_id=user_id,
        thread_id=thread_id,
        current_window_id=current_window_id,
        current_window_cwd=current_window_cwd,
        text=text,
        runtime=_bot_runtime(),
    )


@_bot_wrapper
async def _maybe_confirm_startup_trust_prompt(
    window_id: str,
    pane_text: str,
) -> tuple[bool, str]:
    return await _native_input.maybe_confirm_startup_trust_prompt(
        window_id, pane_text, runtime=_bot_runtime()
    )


@_bot_wrapper
async def _send_to_window_when_codex_ready(
    user_id: int,
    thread_id: int | None,
    window_id: str,
    text: str,
    *,
    timeout: float | None = None,
    interval: float = 0.5,
    auto_confirm_startup_trust: bool = False,
    return_on_capacity_full: bool = False,
) -> tuple[bool, str]:
    return await _window_lifecycle.send_to_window_when_codex_ready(
        user_id,
        thread_id,
        window_id,
        text,
        timeout=timeout,
        interval=interval,
        auto_confirm_startup_trust=auto_confirm_startup_trust,
        return_on_capacity_full=return_on_capacity_full,
        runtime=_bot_runtime(),
    )


@_bot_wrapper
async def _send_or_queue_to_starting_window(
    bot: Bot,
    user_id: int,
    thread_id: int | None,
    window_id: str,
    text: str,
    *,
    auto_confirm_startup_trust: bool = False,
) -> tuple[bool, str, bool]:
    return await _window_lifecycle.send_or_queue_to_starting_window(
        bot,
        user_id,
        thread_id,
        window_id,
        text,
        auto_confirm_startup_trust=auto_confirm_startup_trust,
        runtime=_bot_runtime(),
    )


@_bot_wrapper
async def _enable_fast_mode_if_requested(
    bot: Bot,
    user_id: int,
    thread_id: int | None,
    window_id: str,
    profile: AgentProfile,
    chat_id: int,
) -> None:
    await _window_lifecycle.enable_fast_mode_if_requested(
        bot, user_id, thread_id, window_id, profile, chat_id, runtime=_bot_runtime()
    )


@_bot_wrapper
async def _refresh_session_map_after_first_prompt(
    window_id: str,
    *,
    text: str | None = None,
    confirm_existing_session: bool = False,
    timeout: float = 20.0,
    transcript_session_id: str | None = None,
    transcript_after_offset: int | None = None,
    submit_key: str = "Enter",
) -> bool:
    return await _native_input.refresh_session_map_after_first_prompt(
        window_id,
        text=text,
        confirm_existing_session=confirm_existing_session,
        timeout=timeout,
        transcript_session_id=transcript_session_id,
        transcript_after_offset=transcript_after_offset,
        submit_key=submit_key,
        runtime=_bot_runtime(),
    )


@_bot_wrapper
async def _confirm_first_prompt_delivery(
    window_id: str,
    text: str,
    *,
    transcript_timeout: float | None = None,
    transcript_session_id: str | None = None,
    transcript_after_offset: int | None = None,
    submit_key: str = "Enter",
) -> bool:
    return await _native_input.confirm_first_prompt_delivery(
        window_id,
        text,
        transcript_timeout=transcript_timeout,
        transcript_session_id=transcript_session_id,
        transcript_after_offset=transcript_after_offset,
        submit_key=submit_key,
        runtime=_bot_runtime(),
    )


@_bot_wrapper
async def _recover_missing_bound_window(
    *,
    bot: Bot,
    user_id: int,
    thread_id: int,
    old_window_id: str,
    text: str,
) -> tuple[bool, str]:
    return await _window_lifecycle.recover_missing_bound_window(
        bot=bot,
        user_id=user_id,
        thread_id=thread_id,
        old_window_id=old_window_id,
        text=text,
        runtime=_bot_runtime(),
    )


@_bot_wrapper
async def _handle_missing_bound_window_input(
    *,
    update_message: Any,
    user_id: int,
    thread_id: int,
    window_id: str,
    text: str,
    success_reply: str | None = None,
) -> None:
    await _window_lifecycle.handle_missing_bound_window_input(
        update_message=update_message,
        user_id=user_id,
        thread_id=thread_id,
        window_id=window_id,
        text=text,
        success_reply=success_reply,
        runtime=_bot_runtime(),
    )


@_bot_wrapper
async def _recovered_agent_process_status(window_id: str) -> tuple[bool, str]:
    return await _window_lifecycle.recovered_agent_process_status(
        window_id, runtime=_bot_runtime()
    )


@_bot_wrapper
async def _wait_for_recovered_agent_process(
    window_id: str,
    *,
    timeout: float = 10.0,
    interval: float = 0.5,
) -> tuple[bool, str]:
    return await _window_lifecycle.wait_for_recovered_agent_process(
        window_id, timeout=timeout, interval=interval, runtime=_bot_runtime()
    )


@_bot_wrapper
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
    return await _window_lifecycle.handle_non_codex_bound_window(
        update_message=update_message,
        user_id=user_id,
        thread_id=thread_id,
        window_id=window_id,
        pane_command=pane_command,
        text=text,
        success_reply=success_reply,
        runtime=_bot_runtime(),
    )


@_bot_wrapper
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
    return await _window_lifecycle.handle_auth_error_bound_window(
        update_message=update_message,
        user_id=user_id,
        thread_id=thread_id,
        window_id=window_id,
        pane_text=pane_text,
        text=text,
        success_reply=success_reply,
        runtime=_bot_runtime(),
    )


async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        if update.message:
            await safe_reply(update.message, "You are not authorized to use this bot.")
        return

    if not update.message or not update.message.text:
        return

    thread_id = _get_thread_id(update)

    # Capture group chat_id for supergroup forum topic routing.
    # Required: Telegram Bot API needs group chat_id (not user_id) to send
    # messages with message_thread_id. Do NOT remove — see session.py docs.
    chat = update.effective_chat
    if chat and chat.type in ("group", "supergroup"):
        session_manager.set_group_chat_id(user.id, thread_id, chat.id)

    text = sanitize_forward_text(update.message.text)
    if not text:
        await safe_reply(
            update.message,
            "❌ This message only contained wrapper metadata and no forwardable text.",
        )
        return

    # Ignore text in window picker mode (only for the same thread)
    if context.user_data and context.user_data.get(STATE_KEY) == STATE_SELECTING_WINDOW:
        pending_tid = context.user_data.get("_pending_thread_id")
        if pending_tid == thread_id:
            await safe_reply(
                update.message,
                "Please use the window picker above, or tap Cancel.",
            )
            return
        # Stale picker state from a different thread — clear it
        clear_window_picker_state(context.user_data)
        _clear_pending_thread_input(context.user_data)

    # Ignore text in project root picker mode (only for the same thread)
    if context.user_data and context.user_data.get(STATE_KEY) == STATE_SELECTING_ROOT:
        pending_tid = context.user_data.get("_pending_thread_id")
        if pending_tid == thread_id:
            await safe_reply(
                update.message,
                "Please choose a computer/VPS above, or tap Cancel.",
            )
            return
        # Stale root picker state from a different thread — clear it
        clear_root_picker_state(context.user_data)
        _clear_pending_thread_input(context.user_data)

    # Ignore text in directory browsing mode (only for the same thread)
    if (
        context.user_data
        and context.user_data.get(STATE_KEY) == STATE_BROWSING_DIRECTORY
    ):
        pending_tid = context.user_data.get("_pending_thread_id")
        if pending_tid == thread_id:
            await safe_reply(
                update.message,
                "Please use the directory browser above, or tap Cancel.",
            )
            return
        # Stale browsing state from a different thread — clear it
        clear_browse_state(context.user_data)
        _clear_pending_thread_input(context.user_data)

    # Ignore text in session picker mode (only for the same thread)
    if (
        context.user_data
        and context.user_data.get(STATE_KEY) == STATE_SELECTING_SESSION
    ):
        pending_tid = context.user_data.get("_pending_thread_id")
        if pending_tid == thread_id:
            await safe_reply(
                update.message,
                "Please use the session picker above, or tap Cancel.",
            )
            return
        # Stale picker state from a different thread — clear it
        clear_session_picker_state(context.user_data)
        _clear_pending_thread_input(context.user_data)
        context.user_data.pop("_selected_path", None)

    # Must be in a named topic
    if thread_id is None:
        await safe_reply(
            update.message,
            "❌ Please use a named topic. Create a new topic to start a session.",
        )
        return

    ambiguous_wid = session_manager.get_ambiguous_window_for_thread(user.id, thread_id)
    if isinstance(ambiguous_wid, str) and ambiguous_wid:
        await safe_reply(
            update.message,
            "❌ This topic shares an agent window with another topic. Messages are "
            "paused to prevent cross-topic delivery; use /unbind here first.",
        )
        return

    wid = session_manager.get_window_for_thread(user.id, thread_id)
    if wid is None:
        remote_target = _remote_target_for_thread(user.id, thread_id)
        if remote_target:
            await _safe_send_typing_action(update.message.chat, source="text_handler")
            clear_status_msg_info(user.id, thread_id)
            _cancel_bash_capture(user.id, thread_id)

            success, message = await _send_message_to_agent(
                user.id,
                thread_id,
                "",
                text,
            )
            if not success:
                await safe_reply(update.message, f"❌ {message}")
            return

        await _start_unbound_topic_creation(
            update.message,
            context,
            user,
            thread_id,
            text=text,
        )
        return

    # Bound topic — forward to bound window
    w = await tmux_manager.find_window_by_id(wid)
    if not w:
        await _handle_missing_bound_window_input(
            update_message=update.message,
            user_id=user.id,
            thread_id=thread_id,
            window_id=wid,
            text=text,
        )
        return

    pane_cmd = (getattr(w, "pane_current_command", "") or "").strip()
    handled = await _handle_non_codex_bound_window(
        update_message=update.message,
        user_id=user.id,
        thread_id=thread_id,
        window_id=wid,
        pane_command=pane_cmd,
        text=text,
    )
    if handled:
        return

    await _safe_send_typing_action(update.message.chat, source="text_handler")
    await enqueue_status_update(context.bot, user.id, wid, None, thread_id=thread_id)

    # Cancel any running bash capture — new message pushes pane content down
    _cancel_bash_capture(user.id, thread_id)

    # Check for pending interactive UI before sending text.
    # This catches UIs (permission prompts, etc.) that status polling might have missed.
    capture = await capture_agent_output(user.id, thread_id, wid)
    pane_text = capture.text if capture and not capture.missing else None
    if pane_text:
        handled = await _handle_auth_error_bound_window(
            update_message=update.message,
            user_id=user.id,
            thread_id=thread_id,
            window_id=wid,
            pane_text=pane_text,
            text=text,
        )
        if handled:
            return
    input_was_ready = is_codex_input_ready(pane_text or "")
    if pane_text and is_interactive_ui(pane_text):
        # UI detected: show it to the user before the text is bot-side queued.
        logger.info(
            "Detected pending interactive UI before sending text (user=%d, thread=%s)",
            user.id,
            thread_id,
        )
        await handle_interactive_ui(context.bot, user.id, wid, thread_id)
        # Small delay to let the Telegram controls render before queue feedback.
        await asyncio.sleep(0.3)

    if await session_manager.window_has_usage_limit_exceeded(wid):
        handled = await _rotate_thread_after_usage_limit(
            context=context,
            user_id=user.id,
            thread_id=thread_id,
            current_window_id=wid,
            current_window_cwd=w.cwd,
            text=text,
        )
        if handled:
            return

    if _should_offer_input_route_choice(
        user_id=user.id,
        thread_id=thread_id,
        window_id=wid,
        pane_text=pane_text,
    ):
        await _offer_input_route_choice(
            update.message,
            user_id=user.id,
            thread_id=thread_id,
            window_id=wid,
            text=text,
        )
        return

    success, message, queued = await _send_or_queue_agent_input(
        context.bot,
        user.id,
        thread_id,
        wid,
        text,
    )
    if not success:
        await safe_reply(update.message, f"❌ {message}")
        return
    if queued:
        await safe_reply(update.message, f"⏳ {message}")
    else:
        await mark_window_working(context.bot, user.id, wid, thread_id)

    # Start background capture for ! bash command output
    if not queued and input_was_ready and text.startswith("!") and len(text) > 1:
        bash_cmd = text[1:]  # strip leading "!"
        task = asyncio.create_task(
            _capture_bash_output(context.bot, user.id, thread_id, wid, bash_cmd)
        )
        _bash_capture_tasks[(user.id, thread_id)] = task

    # If in interactive mode, refresh the UI after sending text
    interactive_window = get_interactive_window(user.id, thread_id)
    if interactive_window and interactive_window == wid:
        await asyncio.sleep(0.2)
        await handle_interactive_ui(context.bot, user.id, wid, thread_id)


# --- Window creation helper ---


@_bot_wrapper
async def _create_and_bind_window(
    query: object,
    context: ContextTypes.DEFAULT_TYPE,
    user: object,
    selected_path: str | WindowLaunchSpec,
    pending_thread_id: int | None = None,
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
    spec = (
        selected_path
        if isinstance(selected_path, WindowLaunchSpec)
        else WindowLaunchSpec(
            selected_path=selected_path,
            pending_thread_id=pending_thread_id,
            resume_session_id=resume_session_id,
            account_name=account_name,
            agent_type=agent_type,
            model=model,
            reasoning_effort=reasoning_effort,
            fast_mode=fast_mode,
            permission_mode=permission_mode,
            node_id=node_id,
            answer_callback=answer_callback,
        )
    )
    await _window_lifecycle.create_and_bind_window(
        query, context, user, spec, runtime=_bot_runtime()
    )


# --- Callback query handler ---


def _get_codex_update_apply_lock() -> asyncio.Lock:
    """Return the process-local lock for manual Codex CLI updates."""
    global _codex_update_apply_lock
    if _codex_update_apply_lock is None:
        _codex_update_apply_lock = asyncio.Lock()
    return _codex_update_apply_lock


def _codex_update_prompt_key(result: CodexUpdateResult) -> str:
    """Return the dedupe key for a Codex update prompt."""
    return result.latest_version or result.message or "unknown"


def _codex_update_prompt_state_file() -> Path:
    """Return the state file tracking already prompted Codex CLI versions."""
    return app_dir() / _CODEX_UPDATE_PROMPT_STATE_FILENAME


def _load_codex_update_prompted_versions() -> set[str]:
    """Load Codex CLI versions that have already shown an update prompt."""
    path = _codex_update_prompt_state_file()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return set()
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Failed to read Codex update prompt state %s: %s", path, exc)
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
    path = _codex_update_prompt_state_file()
    try:
        atomic_write_json(
            path,
            {"prompted_versions": sorted(prompted_versions)},
        )
    except OSError as exc:
        logger.warning("Failed to write Codex update prompt state %s: %s", path, exc)


def _mark_codex_update_prompted(key: str) -> bool:
    """Return True when a Codex update prompt key is newly marked."""
    persisted_versions = _load_codex_update_prompted_versions()
    prompted_versions = persisted_versions | _codex_update_prompted_versions
    if key in prompted_versions:
        _codex_update_prompted_versions.update(prompted_versions)
        return False

    prompted_versions.add(key)
    _codex_update_prompted_versions.update(prompted_versions)
    _save_codex_update_prompted_versions(prompted_versions)
    return True


async def notify_codex_update_available(
    bot: Bot,
    result: CodexUpdateResult,
) -> None:
    """Notify allowed Telegram users that a Codex CLI update needs approval."""
    key = _codex_update_prompt_key(result)
    if not _mark_codex_update_prompted(key):
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
                    callback_data=CB_CODEX_UPDATE_APPLY,
                ),
                InlineKeyboardButton(
                    "Not now",
                    callback_data=CB_CODEX_UPDATE_DISMISS,
                ),
            ]
        ]
    )
    for user_id in sorted(config.allowed_users):
        await safe_send(bot, user_id, text, reply_markup=keyboard)


async def _apply_codex_update_from_callback(query: Any) -> None:
    """Apply a Codex CLI update after an authorized Telegram button click."""
    lock = _get_codex_update_apply_lock()
    if lock.locked():
        await query.answer("Codex update is already running", show_alert=True)
        return

    await query.answer("Updating Codex CLI...")
    async with lock:
        await safe_edit(query, "⏳ Updating Codex CLI…")
        load_update_env()
        settings = load_codex_update_settings()
        result = await asyncio.to_thread(
            check_codex_update,
            settings,
            apply_update=True,
        )

    if result.updated:
        await safe_edit(
            query,
            f"✅ {result.message}\n\nNew Codex sessions will use the updated CLI.",
        )
        return
    if result.checked and result.supported and not result.update_available:
        await safe_edit(query, f"✅ {result.message}")
        return
    await safe_edit(query, f"⚠️ {result.message}")


@_bot_wrapper
async def _safe_answer_callback(query: Any, *args: Any, **kwargs: Any) -> bool:
    return await _native_input.safe_answer_callback(query, *args, **kwargs)


@_bot_wrapper
async def _handle_input_routing_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    mode: str,
    record_id: int,
) -> None:
    await _native_input.handle_input_routing_callback(
        update, context, mode=mode, record_id=record_id, runtime=_bot_runtime()
    )


async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.data:
        return

    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        await query.answer("Not authorized")
        return

    data = query.data

    # Capture group chat_id for supergroup forum topic routing.
    # Required: Telegram Bot API needs group chat_id (not user_id) to send
    # messages with message_thread_id. Do NOT remove — see session.py docs.
    cb_thread_id = _get_thread_id(update)
    chat = update.effective_chat
    if chat and chat.type in ("group", "supergroup"):
        session_manager.set_group_chat_id(user.id, cb_thread_id, chat.id)

    if data == CB_CODEX_UPDATE_APPLY:
        await _apply_codex_update_from_callback(query)
        return

    if data == CB_CODEX_UPDATE_DISMISS:
        await _safe_answer_callback(query, "Dismissed")
        await safe_edit(query, "Codex CLI update dismissed.")
        return

    if data.startswith(CB_INPUT_ROUTE):
        parsed = parse_input_routing_callback(data)
        if parsed is None:
            await _safe_answer_callback(query, "Invalid input action", show_alert=True)
            return
        mode, record_id = parsed
        await _handle_input_routing_callback(
            update,
            context,
            mode=mode,
            record_id=record_id,
        )
        return

    # History: older/newer pagination
    # Format: hp:<page>:<window_id>:<start>:<end> or hn:<page>:<window_id>:<start>:<end>
    if data.startswith(CB_HISTORY_PREV) or data.startswith(CB_HISTORY_NEXT):
        prefix_len = len(CB_HISTORY_PREV)  # same length for both
        rest = data[prefix_len:]
        try:
            parts = rest.split(":")
            if len(parts) < 4:
                # Old format without byte range: page:window_id
                offset_str, window_id = rest.split(":", 1)
                start_byte, end_byte = 0, 0
            else:
                # New format: page:window_id:start:end (window_id may contain colons)
                offset_str = parts[0]
                start_byte = int(parts[-2])
                end_byte = int(parts[-1])
                window_id = ":".join(parts[1:-2])
            offset = int(offset_str)
        except (ValueError, IndexError):
            await query.answer("Invalid data")
            return

        w = await tmux_manager.find_window_by_id(window_id)
        if w:
            await send_history(
                query,
                window_id,
                offset=offset,
                edit=True,
                start_byte=start_byte,
                end_byte=end_byte,
                output_mode=session_manager.get_output_mode(user.id, cb_thread_id),
                # Don't pass user_id for pagination - offset update only on initial view
                # This prevents offset from going backwards if new messages arrive while paging
            )
        else:
            await safe_edit(query, "Window no longer exists.")
        await query.answer("Page updated")

    # Project root picker handlers
    elif data.startswith(CB_ROOT_SELECT):
        pending_tid = (
            context.user_data.get("_pending_thread_id") if context.user_data else None
        )
        if pending_tid is not None and _get_thread_id(update) != pending_tid:
            await query.answer("Stale root picker (topic mismatch)", show_alert=True)
            return
        try:
            idx = int(data[len(CB_ROOT_SELECT) :])
        except ValueError:
            await query.answer("Invalid data")
            return

        cached_roots: list[object] = (
            context.user_data.get(ROOTS_KEY, []) if context.user_data else []
        )
        if idx < 0 or idx >= len(cached_roots):
            await query.answer("Root list changed, please retry", show_alert=True)
            return

        root = _parse_root_selection(cached_roots[idx])
        if root is None:
            await query.answer("Invalid root data", show_alert=True)
            return

        root_label = root["label"]
        root_path = root["path"]
        backend_id = root["backend_id"]
        node_id = root["node_id"]

        if backend_id:
            rendered = await _build_backend_directory_browser(
                backend_id=backend_id,
                node_id=node_id,
                path=root_path,
                root_label=root_label,
                root_path=root_path,
            )
            if rendered is None:
                await query.answer("Backend browser unavailable", show_alert=True)
                return
            msg_text, keyboard, subdirs, start_path = rendered
        else:
            start_path = _default_directory_browser_path(root_path)
            msg_text, keyboard, subdirs = build_directory_browser(
                start_path,
                root_label=root_label,
                root_path=root_path,
            )
        if context.user_data is not None:
            context.user_data[STATE_KEY] = STATE_BROWSING_DIRECTORY
            context.user_data[BROWSE_PATH_KEY] = start_path
            context.user_data[BROWSE_ROOT_LABEL_KEY] = root_label
            context.user_data[BROWSE_ROOT_PATH_KEY] = root_path
            if backend_id:
                context.user_data[BROWSE_BACKEND_ID_KEY] = backend_id
                context.user_data[BROWSE_NODE_ID_KEY] = node_id
            context.user_data[BROWSE_PAGE_KEY] = 0
            context.user_data[BROWSE_DIRS_KEY] = subdirs
            context.user_data.pop(ROOTS_KEY, None)
        await safe_edit(query, msg_text, reply_markup=keyboard)
        await query.answer()

    elif data == CB_ROOT_CANCEL:
        pending_tid = (
            context.user_data.get("_pending_thread_id") if context.user_data else None
        )
        if pending_tid is not None and _get_thread_id(update) != pending_tid:
            await query.answer("Stale root picker (topic mismatch)", show_alert=True)
            return
        clear_root_picker_state(context.user_data)
        if context.user_data is not None:
            _clear_pending_thread_input(context.user_data)
        await safe_edit(query, "Cancelled")
        await query.answer("Cancelled")

    # Per-topic agent/profile picker
    elif data.startswith(CB_PROFILE_AGENT):
        pending_tid = (
            context.user_data.get("_pending_thread_id") if context.user_data else None
        )
        if pending_tid is not None and _get_thread_id(update) != pending_tid:
            await query.answer("Stale picker (topic mismatch)", show_alert=True)
            return
        agent_type = normalize_agent_type(data[len(CB_PROFILE_AGENT) :])
        await refresh_model_catalog(agent_type)
        models = _profile_models(agent_type)
        model = models[0] if models else ""
        configured_effort = (
            config.claude_reasoning_effort
            if agent_type in {AGENT_CLAUDE, AGENT_CLAUDE_OFFICIAL}
            else config.codex_reasoning_effort
            if agent_type == AGENT_CODEX
            else ""
        )
        if context.user_data is not None:
            context.user_data[PROFILE_AGENT_KEY] = agent_type
            context.user_data[PROFILE_MODEL_KEY] = model
            context.user_data[PROFILE_FAST_MODE_KEY] = False
            context.user_data[PROFILE_PERMISSION_MODE_KEY] = (
                _default_profile_permission(agent_type)
            )
            context.user_data[PROFILE_EFFORT_KEY] = _resolve_profile_effort(
                agent_type,
                model,
                configured_effort,
            )
        await _show_agent_profile_settings(query, context)
        await query.answer()

    elif data.startswith(CB_PROFILE_MODEL):
        pending_tid = (
            context.user_data.get("_pending_thread_id") if context.user_data else None
        )
        if pending_tid is not None and _get_thread_id(update) != pending_tid:
            await query.answer("Stale picker (topic mismatch)", show_alert=True)
            return
        models = (
            context.user_data.get(PROFILE_MODELS_KEY, []) if context.user_data else []
        )
        try:
            model_index = int(data[len(CB_PROFILE_MODEL) :])
        except ValueError:
            await query.answer("Invalid model", show_alert=True)
            return
        if model_index < 0 or model_index >= len(models):
            await query.answer("Model list changed", show_alert=True)
            return
        if context.user_data is not None:
            model = models[model_index]
            agent_type = normalize_agent_type(
                context.user_data.get(PROFILE_AGENT_KEY, config.agent_type)
            )
            current_effort = context.user_data.get(
                PROFILE_EFFORT_KEY,
                config.claude_reasoning_effort
                if agent_type in {AGENT_CLAUDE, AGENT_CLAUDE_OFFICIAL}
                else config.codex_reasoning_effort
                if agent_type == AGENT_CODEX
                else "",
            )
            context.user_data[PROFILE_MODEL_KEY] = model
            context.user_data[PROFILE_EFFORT_KEY] = _resolve_profile_effort(
                agent_type,
                model,
                current_effort if isinstance(current_effort, str) else "",
            )
        await _show_agent_profile_settings(query, context)
        await query.answer("Model selected")

    elif data.startswith(CB_PROFILE_EFFORT):
        pending_tid = (
            context.user_data.get("_pending_thread_id") if context.user_data else None
        )
        if pending_tid is not None and _get_thread_id(update) != pending_tid:
            await query.answer("Stale picker (topic mismatch)", show_alert=True)
            return
        raw_effort = data[len(CB_PROFILE_EFFORT) :]
        effort = normalize_effort(raw_effort, "")
        agent_type = normalize_agent_type(
            context.user_data.get(PROFILE_AGENT_KEY, config.agent_type)
            if context.user_data
            else config.agent_type
        )
        model = (
            context.user_data.get(PROFILE_MODEL_KEY, "") if context.user_data else ""
        )
        if effort not in _profile_effort_values(
            agent_type, model if isinstance(model, str) else ""
        ):
            await query.answer("Reasoning level unavailable", show_alert=True)
            return
        if context.user_data is not None:
            context.user_data[PROFILE_EFFORT_KEY] = effort
        await _show_agent_profile_settings(query, context)
        await query.answer("Reasoning selected")

    elif data.startswith(CB_PROFILE_FAST):
        pending_tid = (
            context.user_data.get("_pending_thread_id") if context.user_data else None
        )
        if pending_tid is not None and _get_thread_id(update) != pending_tid:
            await query.answer("Stale picker (topic mismatch)", show_alert=True)
            return
        fast_mode = data[len(CB_PROFILE_FAST) :] == "on"
        if context.user_data is not None:
            context.user_data[PROFILE_FAST_MODE_KEY] = fast_mode
        await _show_agent_profile_settings(query, context)
        await query.answer("Fast mode updated")

    elif data.startswith(CB_PROFILE_PERMISSION):
        pending_tid = (
            context.user_data.get("_pending_thread_id") if context.user_data else None
        )
        if pending_tid is not None and _get_thread_id(update) != pending_tid:
            await query.answer("Stale picker (topic mismatch)", show_alert=True)
            return
        permission_mode = data[len(CB_PROFILE_PERMISSION) :]
        if permission_mode not in {"ask", "full"}:
            await query.answer("Invalid permission mode", show_alert=True)
            return
        if (
            context.user_data is not None
            and context.user_data.get(PROFILE_AGENT_KEY) == AGENT_CURSOR
            and config.cursor_permission_mode == "full"
            and permission_mode == "ask"
        ):
            await query.answer("Cursor uses VPS-wide full access", show_alert=True)
            return
        if context.user_data is not None:
            context.user_data[PROFILE_PERMISSION_MODE_KEY] = permission_mode
        await _show_agent_profile_settings(query, context)
        await query.answer("Permission mode updated")

    elif data == CB_PROFILE_CONFIRM:
        pending_tid = (
            context.user_data.get("_pending_thread_id") if context.user_data else None
        )
        if pending_tid is not None and _get_thread_id(update) != pending_tid:
            await query.answer("Stale picker (topic mismatch)", show_alert=True)
            return
        await _continue_creation_with_profile(query, context, user)

    elif data == CB_PROFILE_CANCEL:
        pending_tid = (
            context.user_data.get("_pending_thread_id") if context.user_data else None
        )
        if pending_tid is not None and _get_thread_id(update) != pending_tid:
            await query.answer("Stale picker (topic mismatch)", show_alert=True)
            return
        _clear_creation_state(context.user_data)
        await safe_edit(query, "Cancelled")
        await query.answer("Cancelled")

    elif data.startswith(CB_OUTPUT_MODE):
        mode = normalize_output_mode(data[len(CB_OUTPUT_MODE) :])
        thread_id = _get_thread_id(update)
        selected = session_manager.set_output_mode(user.id, thread_id, mode)
        await safe_edit(
            query,
            _output_mode_text(selected),
            reply_markup=_output_mode_keyboard(selected),
        )
        await query.answer(f"Output mode: {output_mode_label(selected)}")

    # Directory browser handlers
    elif data.startswith(CB_DIR_SELECT):
        # Validate: callback must come from the same topic that started browsing
        pending_tid = (
            context.user_data.get("_pending_thread_id") if context.user_data else None
        )
        if pending_tid is not None and _get_thread_id(update) != pending_tid:
            await query.answer("Stale browser (topic mismatch)", show_alert=True)
            return
        # callback_data contains index, not dir name (to avoid 64-byte limit)
        try:
            idx = int(data[len(CB_DIR_SELECT) :])
        except ValueError:
            await query.answer("Invalid data")
            return

        # Look up dir name from cached subdirs
        cached_dirs: list[str] = (
            context.user_data.get(BROWSE_DIRS_KEY, []) if context.user_data else []
        )
        if idx < 0 or idx >= len(cached_dirs):
            await query.answer(
                "Directory list changed, please refresh", show_alert=True
            )
            return
        subdir_name = cached_dirs[idx]

        current_path = _creation_path(context.user_data, BROWSE_PATH_KEY)
        if current_path is None:
            await _reject_expired_project_selection(query, context)
            return
        backend_id, node_id = _browse_backend_context(context.user_data)
        if backend_id:
            root_label, root_path = _browse_root_context(context.user_data)
            new_path_str = _remote_child_path(current_path, subdir_name)
            rendered = await _build_backend_directory_browser(
                backend_id=backend_id,
                node_id=node_id,
                path=new_path_str,
                root_label=root_label,
                root_path=root_path,
            )
            if rendered is None:
                await query.answer("Directory not found", show_alert=True)
                return
            msg_text, keyboard, subdirs, resolved_path = rendered
            if context.user_data is not None:
                context.user_data[BROWSE_PATH_KEY] = resolved_path
                context.user_data[BROWSE_PAGE_KEY] = 0
                context.user_data[BROWSE_DIRS_KEY] = subdirs
            await safe_edit(query, msg_text, reply_markup=keyboard)
            await query.answer()
            return

        current_path = _clamp_to_selected_root(current_path, context.user_data)
        new_path = (Path(current_path) / subdir_name).resolve()

        if not new_path.exists() or not new_path.is_dir():
            await query.answer("Directory not found", show_alert=True)
            return

        new_path_str = str(new_path)
        if context.user_data is not None:
            context.user_data[BROWSE_PATH_KEY] = new_path_str
            context.user_data[BROWSE_PAGE_KEY] = 0

        msg_text, keyboard, subdirs = build_directory_browser(
            new_path_str,
            **_directory_browser_kwargs(context.user_data),
        )
        if context.user_data is not None:
            context.user_data[BROWSE_DIRS_KEY] = subdirs
        await safe_edit(query, msg_text, reply_markup=keyboard)
        await query.answer()

    elif data == CB_DIR_UP:
        pending_tid = (
            context.user_data.get("_pending_thread_id") if context.user_data else None
        )
        if pending_tid is not None and _get_thread_id(update) != pending_tid:
            await query.answer("Stale browser (topic mismatch)", show_alert=True)
            return
        current_path = _creation_path(context.user_data, BROWSE_PATH_KEY)
        if current_path is None:
            await _reject_expired_project_selection(query, context)
            return
        backend_id, node_id = _browse_backend_context(context.user_data)
        if backend_id:
            root_label, root_path = _browse_root_context(context.user_data)
            parent_path = _remote_parent_path(current_path, root_path)
            rendered = await _build_backend_directory_browser(
                backend_id=backend_id,
                node_id=node_id,
                path=parent_path,
                root_label=root_label,
                root_path=root_path,
            )
            if rendered is None:
                await query.answer("Directory not found", show_alert=True)
                return
            msg_text, keyboard, subdirs, resolved_path = rendered
            if context.user_data is not None:
                context.user_data[BROWSE_PATH_KEY] = resolved_path
                context.user_data[BROWSE_PAGE_KEY] = 0
                context.user_data[BROWSE_DIRS_KEY] = subdirs
            await safe_edit(query, msg_text, reply_markup=keyboard)
            await query.answer()
            return

        current = Path(current_path).resolve()
        parent = current.parent
        parent_path = _clamp_to_selected_root(str(parent), context.user_data)

        if context.user_data is not None:
            context.user_data[BROWSE_PATH_KEY] = parent_path
            context.user_data[BROWSE_PAGE_KEY] = 0

        msg_text, keyboard, subdirs = build_directory_browser(
            parent_path,
            **_directory_browser_kwargs(context.user_data),
        )
        if context.user_data is not None:
            context.user_data[BROWSE_DIRS_KEY] = subdirs
        await safe_edit(query, msg_text, reply_markup=keyboard)
        await query.answer()

    elif data.startswith(CB_DIR_PAGE):
        pending_tid = (
            context.user_data.get("_pending_thread_id") if context.user_data else None
        )
        if pending_tid is not None and _get_thread_id(update) != pending_tid:
            await query.answer("Stale browser (topic mismatch)", show_alert=True)
            return
        try:
            pg = int(data[len(CB_DIR_PAGE) :])
        except ValueError:
            await query.answer("Invalid data")
            return
        current_path = _creation_path(context.user_data, BROWSE_PATH_KEY)
        if current_path is None:
            await _reject_expired_project_selection(query, context)
            return
        backend_id, node_id = _browse_backend_context(context.user_data)
        if backend_id:
            root_label, root_path = _browse_root_context(context.user_data)
            rendered = await _build_backend_directory_browser(
                backend_id=backend_id,
                node_id=node_id,
                path=current_path,
                page=pg,
                root_label=root_label,
                root_path=root_path,
            )
            if rendered is None:
                await query.answer("Directory list changed", show_alert=True)
                return
            msg_text, keyboard, subdirs, resolved_path = rendered
            if context.user_data is not None:
                context.user_data[BROWSE_PATH_KEY] = resolved_path
                context.user_data[BROWSE_PAGE_KEY] = pg
                context.user_data[BROWSE_DIRS_KEY] = subdirs
            await safe_edit(query, msg_text, reply_markup=keyboard)
            await query.answer()
            return

        if context.user_data is not None:
            context.user_data[BROWSE_PAGE_KEY] = pg

        msg_text, keyboard, subdirs = build_directory_browser(
            current_path,
            pg,
            **_directory_browser_kwargs(context.user_data),
        )
        if context.user_data is not None:
            context.user_data[BROWSE_DIRS_KEY] = subdirs
        await safe_edit(query, msg_text, reply_markup=keyboard)
        await query.answer()

    elif data == CB_DIR_CONFIRM:
        # Check if this was initiated from a thread bind flow
        pending_thread_id: int | None = (
            context.user_data.get("_pending_thread_id") if context.user_data else None
        )
        if pending_thread_id is None:
            pending_thread_id = _get_thread_id(update)

        # Validate: confirm button must come from the same topic that started browsing
        confirm_thread_id = _get_thread_id(update)
        if pending_thread_id is not None and confirm_thread_id != pending_thread_id:
            clear_browse_state(context.user_data)
            if context.user_data is not None:
                _clear_pending_thread_input(context.user_data)
            await query.answer("Stale browser (topic mismatch)", show_alert=True)
            return

        selected_path = _creation_path(context.user_data, BROWSE_PATH_KEY)
        if selected_path is None:
            await _reject_expired_project_selection(query, context)
            return
        backend_id, node_id = _browse_backend_context(context.user_data)
        if not backend_id:
            selected_path = _clamp_to_selected_root(selected_path, context.user_data)

        if context.user_data is not None:
            context.user_data["_pending_thread_id"] = pending_thread_id
            context.user_data["_selected_path"] = selected_path
            context.user_data["_selected_backend_id"] = backend_id
            context.user_data["_selected_node_id"] = node_id
        clear_browse_state(context.user_data)
        await query.answer("Choose agent")
        await _show_agent_profile_picker(query, context)

    elif data == CB_DIR_CANCEL:
        pending_tid = (
            context.user_data.get("_pending_thread_id") if context.user_data else None
        )
        if pending_tid is not None and _get_thread_id(update) != pending_tid:
            await query.answer("Stale browser (topic mismatch)", show_alert=True)
            return
        clear_browse_state(context.user_data)
        if context.user_data is not None:
            _clear_pending_thread_input(context.user_data)
        await safe_edit(query, "Cancelled")
        await query.answer("Cancelled")

    # Session picker: resume existing session
    elif data.startswith(CB_SESSION_SELECT):
        pending_tid = (
            context.user_data.get("_pending_thread_id") if context.user_data else None
        )
        # Fallback: if _pending_thread_id was cleared (e.g. by a message in
        # another topic), recover it from the callback query's message context
        if pending_tid is None:
            pending_tid = _get_thread_id(update)
        if pending_tid is not None and _get_thread_id(update) != pending_tid:
            await query.answer("Stale picker (topic mismatch)", show_alert=True)
            return
        try:
            idx = int(data[len(CB_SESSION_SELECT) :])
        except ValueError:
            await query.answer("Invalid data")
            return

        cached_sessions = (
            context.user_data.get(SESSIONS_KEY, []) if context.user_data else []
        )
        if idx < 0 or idx >= len(cached_sessions):
            await query.answer("Session not found")
            return

        session = cached_sessions[idx]
        session_manager.unhide_session(session.session_id)
        if session_manager.has_bound_thread_for_session(session.session_id):
            resumable_sessions = _filter_resumable_sessions(cached_sessions)
            if context.user_data is not None:
                context.user_data[SESSIONS_KEY] = resumable_sessions

            if resumable_sessions:
                text, keyboard = build_session_picker(resumable_sessions)
                await safe_edit(
                    query,
                    "⚠️ This session is already active in another topic.\n"
                    "To avoid cross-talk, pick a different session or start a new one.\n\n"
                    f"{text}",
                    reply_markup=keyboard,
                )
            else:
                await safe_edit(
                    query,
                    "⚠️ This session is already active in another topic.\n"
                    "To avoid cross-talk, start a new session here instead.",
                    reply_markup=_build_resume_conflict_keyboard(),
                )
            await query.answer("Session already active", show_alert=True)
            return

        selected_path = _creation_path(context.user_data, "_selected_path")
        if selected_path is None:
            await _reject_expired_project_selection(query, context)
            return
        selected_node_id = (
            context.user_data.get("_selected_node_id", "") if context.user_data else ""
        )
        profile = _profile_from_context(context.user_data)
        clear_session_picker_state(context.user_data)
        if context.user_data is not None:
            context.user_data.pop("_selected_path", None)
            context.user_data.pop("_selected_backend_id", None)
            context.user_data.pop("_selected_node_id", None)

        await _create_and_bind_window(
            query,
            context,
            user,
            WindowLaunchSpec(
                selected_path=selected_path,
                pending_thread_id=pending_tid,
                resume_session_id=session.session_id,
                agent_type=profile.agent_type,
                model=profile.model,
                reasoning_effort=profile.reasoning_effort,
                fast_mode=profile.fast_mode,
                permission_mode=profile.permission_mode,
                node_id=selected_node_id,
            ),
        )

    elif data == CB_SESSION_NEW:
        pending_tid = (
            context.user_data.get("_pending_thread_id") if context.user_data else None
        )
        if pending_tid is None:
            pending_tid = _get_thread_id(update)
        if pending_tid is not None and _get_thread_id(update) != pending_tid:
            await query.answer("Stale picker (topic mismatch)", show_alert=True)
            return
        selected_path = _creation_path(context.user_data, "_selected_path")
        if selected_path is None:
            await _reject_expired_project_selection(query, context)
            return
        selected_node_id = (
            context.user_data.get("_selected_node_id", "") if context.user_data else ""
        )
        profile = _profile_from_context(context.user_data)
        clear_session_picker_state(context.user_data)
        if context.user_data is not None:
            context.user_data.pop("_selected_path", None)
            context.user_data.pop("_selected_backend_id", None)
            context.user_data.pop("_selected_node_id", None)

        await _create_and_bind_window(
            query,
            context,
            user,
            WindowLaunchSpec(
                selected_path=selected_path,
                pending_thread_id=pending_tid,
                agent_type=profile.agent_type,
                model=profile.model,
                reasoning_effort=profile.reasoning_effort,
                fast_mode=profile.fast_mode,
                node_id=selected_node_id,
            ),
        )

    elif data == CB_SESSION_CANCEL:
        pending_tid = (
            context.user_data.get("_pending_thread_id") if context.user_data else None
        )
        if pending_tid is not None and _get_thread_id(update) != pending_tid:
            await query.answer("Stale picker (topic mismatch)", show_alert=True)
            return
        clear_session_picker_state(context.user_data)
        if context.user_data is not None:
            _clear_pending_thread_input(context.user_data)
            context.user_data.pop("_selected_path", None)
            context.user_data.pop("_selected_backend_id", None)
            context.user_data.pop("_selected_node_id", None)
            context.user_data.pop(PROFILE_AGENT_KEY, None)
            context.user_data.pop(PROFILE_MODEL_KEY, None)
            context.user_data.pop(PROFILE_EFFORT_KEY, None)
            context.user_data.pop(PROFILE_FAST_MODE_KEY, None)
            context.user_data.pop(PROFILE_PERMISSION_MODE_KEY, None)
            context.user_data.pop(PROFILE_MODELS_KEY, None)
        await safe_edit(query, "Cancelled")
        await query.answer("Cancelled")

    # Window picker: bind existing window
    elif data.startswith(CB_WIN_BIND):
        pending_tid = (
            context.user_data.get("_pending_thread_id") if context.user_data else None
        )
        if pending_tid is not None and _get_thread_id(update) != pending_tid:
            await query.answer("Stale picker (topic mismatch)", show_alert=True)
            return
        try:
            idx = int(data[len(CB_WIN_BIND) :])
        except ValueError:
            await query.answer("Invalid data")
            return

        cached_windows: list[str] = (
            context.user_data.get(UNBOUND_WINDOWS_KEY, []) if context.user_data else []
        )
        if idx < 0 or idx >= len(cached_windows):
            await query.answer("Window list changed, please retry", show_alert=True)
            return
        selected_wid = cached_windows[idx]

        # Verify window still exists
        w = await tmux_manager.find_window_by_id(selected_wid)
        if not w:
            display = session_manager.get_display_name(selected_wid)
            await query.answer(f"Window '{display}' no longer exists", show_alert=True)
            return
        if not _has_trackable_session_for_window(selected_wid):
            await query.answer(
                "This window has no tracked agent session yet. Please choose New Session instead.",
                show_alert=True,
            )
            return

        thread_id = _get_thread_id(update)
        if thread_id is None:
            await query.answer("Not in a topic", show_alert=True)
            return

        display = w.window_name
        clear_window_picker_state(context.user_data)
        session_manager.bind_thread(
            user.id, thread_id, selected_wid, window_name=display
        )

        resolved_chat = session_manager.resolve_chat_id(user.id, thread_id)

        await safe_edit(
            query,
            f"✅ Bound to window `{display}`",
        )

        # Forward the first text or attachment retained while choosing a window.
        pending_text, pending_error = await _take_pending_thread_message(
            context,
            user_id=user.id,
            thread_id=thread_id,
            remote=False,
        )
        if context.user_data is not None:
            context.user_data.pop("_pending_thread_id", None)
        if pending_error:
            await safe_send(
                context.bot,
                resolved_chat,
                f"❌ Failed to send pending attachment: {pending_error}",
                message_thread_id=thread_id,
            )
        elif pending_text:
            send_ok, send_msg, queued = await _send_or_queue_agent_input(
                context.bot,
                user.id,
                thread_id,
                selected_wid,
                pending_text,
            )
            if send_ok and not queued:
                await mark_window_working(
                    context.bot,
                    user.id,
                    selected_wid,
                    thread_id,
                )
            elif queued:
                await safe_send(
                    context.bot,
                    resolved_chat,
                    f"⏳ {send_msg}",
                    message_thread_id=thread_id,
                )
            else:
                logger.warning("Failed to forward pending text: %s", send_msg)
                await safe_send(
                    context.bot,
                    resolved_chat,
                    f"❌ Failed to send pending message: {send_msg}",
                    message_thread_id=thread_id,
                )
        await query.answer("Bound")

    # Window picker: new session → transition to directory browser
    elif data == CB_WIN_NEW:
        pending_tid = (
            context.user_data.get("_pending_thread_id") if context.user_data else None
        )
        if pending_tid is not None and _get_thread_id(update) != pending_tid:
            await query.answer("Stale picker (topic mismatch)", show_alert=True)
            return
        # Preserve pending thread info, clear only picker state
        clear_window_picker_state(context.user_data)
        await _show_root_or_directory_picker(query, context, edit=True)
        await query.answer()

    # Window picker: cancel
    elif data == CB_WIN_CANCEL:
        pending_tid = (
            context.user_data.get("_pending_thread_id") if context.user_data else None
        )
        if pending_tid is not None and _get_thread_id(update) != pending_tid:
            await query.answer("Stale picker (topic mismatch)", show_alert=True)
            return
        clear_window_picker_state(context.user_data)
        if context.user_data is not None:
            _clear_pending_thread_input(context.user_data)
        await safe_edit(query, "Cancelled")
        await query.answer("Cancelled")

    # Screenshot: Refresh
    elif data.startswith(CB_SCREENSHOT_REFRESH):
        window_id = data[len(CB_SCREENSHOT_REFRESH) :]
        capture = await capture_agent_output(
            user.id,
            cb_thread_id,
            window_id,
            with_ansi=True,
        )
        if capture is None or capture.missing:
            await query.answer("Window no longer exists", show_alert=True)
            return

        text = capture.text
        if not text:
            await query.answer("Failed to capture pane", show_alert=True)
            return

        png_bytes = await text_to_image(text, with_ansi=True)
        keyboard = _build_screenshot_keyboard(window_id)
        try:
            await query.edit_message_media(
                media=InputMediaDocument(
                    media=io.BytesIO(png_bytes), filename="screenshot.png"
                ),
                reply_markup=keyboard,
            )
            await query.answer("Refreshed")
        except Exception as e:
            logger.error(f"Failed to refresh screenshot: {e}")
            await query.answer("Failed to refresh", show_alert=True)

    elif data == "noop":
        await query.answer()

    # Interactive UI: Up arrow
    elif data.startswith(CB_ASK_UP):
        window_id = data[len(CB_ASK_UP) :]
        thread_id = _get_thread_id(update)
        ok, message = await _send_control_to_agent(user.id, thread_id, window_id, "Up")
        if ok:
            await asyncio.sleep(0.5)
            await handle_interactive_ui(context.bot, user.id, window_id, thread_id)
        elif message:
            await query.answer(message, show_alert=True)
            return
        await query.answer()

    # Interactive UI: Down arrow
    elif data.startswith(CB_ASK_DOWN):
        window_id = data[len(CB_ASK_DOWN) :]
        thread_id = _get_thread_id(update)
        ok, message = await _send_control_to_agent(
            user.id, thread_id, window_id, "Down"
        )
        if ok:
            await asyncio.sleep(0.5)
            await handle_interactive_ui(context.bot, user.id, window_id, thread_id)
        elif message:
            await query.answer(message, show_alert=True)
            return
        await query.answer()

    # Interactive UI: Left arrow
    elif data.startswith(CB_ASK_LEFT):
        window_id = data[len(CB_ASK_LEFT) :]
        thread_id = _get_thread_id(update)
        ok, message = await _send_control_to_agent(
            user.id, thread_id, window_id, "Left"
        )
        if ok:
            await asyncio.sleep(0.5)
            await handle_interactive_ui(context.bot, user.id, window_id, thread_id)
        elif message:
            await query.answer(message, show_alert=True)
            return
        await query.answer()

    # Interactive UI: Right arrow
    elif data.startswith(CB_ASK_RIGHT):
        window_id = data[len(CB_ASK_RIGHT) :]
        thread_id = _get_thread_id(update)
        ok, message = await _send_control_to_agent(
            user.id, thread_id, window_id, "Right"
        )
        if ok:
            await asyncio.sleep(0.5)
            await handle_interactive_ui(context.bot, user.id, window_id, thread_id)
        elif message:
            await query.answer(message, show_alert=True)
            return
        await query.answer()

    # Interactive UI: Escape
    elif data.startswith(CB_ASK_ESC):
        window_id = data[len(CB_ASK_ESC) :]
        thread_id = _get_thread_id(update)
        ok, message = await _send_control_to_agent(
            user.id, thread_id, window_id, "Escape"
        )
        if ok:
            await clear_interactive_msg(user.id, context.bot, thread_id)
        elif message:
            await query.answer(message, show_alert=True)
            return
        await query.answer("⎋ Esc")

    # Interactive UI: Enter
    elif data.startswith(CB_ASK_ENTER):
        window_id = data[len(CB_ASK_ENTER) :]
        thread_id = _get_thread_id(update)
        ok, message = await _send_control_to_agent(
            user.id, thread_id, window_id, "Enter"
        )
        if ok:
            await asyncio.sleep(0.5)
            await handle_interactive_ui(context.bot, user.id, window_id, thread_id)
        elif message:
            await query.answer(message, show_alert=True)
            return
        await query.answer("⏎ Enter")

    # Interactive UI: Space
    elif data.startswith(CB_ASK_SPACE):
        window_id = data[len(CB_ASK_SPACE) :]
        thread_id = _get_thread_id(update)
        ok, message = await _send_control_to_agent(
            user.id, thread_id, window_id, "Space"
        )
        if ok:
            await asyncio.sleep(0.5)
            await handle_interactive_ui(context.bot, user.id, window_id, thread_id)
        elif message:
            await query.answer(message, show_alert=True)
            return
        await query.answer("␣ Space")

    # Interactive UI: Tab
    elif data.startswith(CB_ASK_TAB):
        window_id = data[len(CB_ASK_TAB) :]
        thread_id = _get_thread_id(update)
        ok, message = await _send_control_to_agent(user.id, thread_id, window_id, "Tab")
        if ok:
            await asyncio.sleep(0.5)
            await handle_interactive_ui(context.bot, user.id, window_id, thread_id)
        elif message:
            await query.answer(message, show_alert=True)
            return
        await query.answer("⇥ Tab")

    # Hook trust UI: Codex uses the literal "t" key to trust configured hooks.
    elif data.startswith(CB_ASK_TRUST):
        window_id = data[len(CB_ASK_TRUST) :]
        thread_id = _get_thread_id(update)
        ok, message = await _send_control_to_agent(user.id, thread_id, window_id, "t")
        if ok:
            await asyncio.sleep(0.5)
            await handle_interactive_ui(context.bot, user.id, window_id, thread_id)
        elif message:
            await query.answer(message, show_alert=True)
            return
        await query.answer("Trusted hooks")

    # Interactive UI: refresh display
    elif data.startswith(CB_ASK_REFRESH):
        window_id = data[len(CB_ASK_REFRESH) :]
        thread_id = _get_thread_id(update)
        await handle_interactive_ui(context.bot, user.id, window_id, thread_id)
        await query.answer("🔄")

    # Screenshot quick keys: send key to tmux window
    elif data.startswith(CB_KEYS_PREFIX):
        rest = data[len(CB_KEYS_PREFIX) :]
        colon_idx = rest.find(":")
        if colon_idx < 0:
            await query.answer("Invalid data")
            return
        key_id = rest[:colon_idx]
        window_id = rest[colon_idx + 1 :]

        key_info = _KEYS_SEND_MAP.get(key_id)
        if not key_info:
            await query.answer("Unknown key")
            return

        tmux_key, _enter, _literal = key_info
        ok, message = await _send_control_to_agent(
            user.id, cb_thread_id, window_id, tmux_key
        )
        if not ok:
            await query.answer(message or "Failed to send key", show_alert=True)
            return

        await query.answer(_KEY_LABELS.get(key_id, key_id))

        # Refresh screenshot after key press
        await asyncio.sleep(0.5)
        capture = await capture_agent_output(
            user.id,
            cb_thread_id,
            window_id,
            with_ansi=True,
        )
        text = capture.text if capture and not capture.missing else None
        if text:
            png_bytes = await text_to_image(text, with_ansi=True)
            keyboard = _build_screenshot_keyboard(window_id)
            try:
                await query.edit_message_media(
                    media=InputMediaDocument(
                        media=io.BytesIO(png_bytes),
                        filename="screenshot.png",
                    ),
                    reply_markup=keyboard,
                )
            except Exception:
                pass  # Screenshot unchanged or message too old


# --- Streaming response / notifications ---


async def _mark_transcript_message_delivered(
    user_id: int,
    window_id: str,
    msg: NewMessage,
) -> None:
    """Advance read offset only through the transcript bytes just delivered."""
    if not window_id:
        return

    if msg.source_offset > 0:
        session_manager.update_user_window_offset(
            user_id,
            window_id,
            msg.source_offset,
            msg.session_id,
        )
        return

    session = await session_manager.resolve_session_for_window(window_id)
    if session and session.file_path:
        try:
            file_size = Path(session.file_path).stat().st_size
            session_manager.update_user_window_offset(
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

    user_window_offsets = getattr(session_manager, "user_window_offsets", {})
    if not isinstance(user_window_offsets, dict):
        return False
    user_window_offset_sessions = getattr(
        session_manager, "user_window_offset_sessions", {}
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
    if not isinstance(recorded_session_id, str) or not _session_ids_match(
        recorded_session_id, msg.session_id
    ):
        return False
    delivered_offset = user_offsets.get(window_id)
    return isinstance(delivered_offset, int) and delivered_offset >= msg.source_offset


async def handle_new_message(msg: NewMessage, bot: Bot) -> None:
    """Handle a new assistant message — enqueue for sequential processing.

    Messages are queued per-user to ensure status messages always appear last.
    Routes via thread_bindings to deliver to the correct topic.
    """
    status = "complete" if msg.is_complete else "streaming"
    logger.info(
        f"handle_new_message [{status}]: session={msg.session_id}, "
        f"text_len={len(msg.text)}"
    )

    # Find users whose thread-bound window matches this session
    active_users = await session_manager.find_users_for_session(msg.session_id)
    remote_users = session_manager.find_users_for_target_session(msg.session_id)
    seen_targets = {(user_id, thread_id) for user_id, _wid, thread_id in active_users}
    active_users.extend(
        (user_id, wid, thread_id)
        for user_id, wid, thread_id in remote_users
        if (user_id, thread_id) not in seen_targets
    )

    if not active_users:
        logger.info(f"No active users for session {msg.session_id}")
        return

    delivery_failed = False
    for user_id, wid, thread_id in active_users:
        is_remote_target = not wid
        agent_error_notice: str | None = None
        if not is_remote_target and _transcript_message_already_delivered(
            user_id,
            wid,
            msg,
        ):
            logger.info(
                "Skipping already delivered transcript message: "
                "session=%s user=%d thread=%s window_id=%s source_offset=%d",
                msg.session_id,
                user_id,
                thread_id,
                wid,
                msg.source_offset,
            )
            continue

        if msg.content_type == "agent_error":
            if not is_remote_target:
                turn_admission.observe(wid, active=False)
            if not is_remote_target and msg.error_code in _TRANSIENT_AGENT_ERROR_CODES:
                scheduled = _schedule_transient_agent_retry(
                    bot,
                    user_id,
                    thread_id,
                    wid,
                )
                if scheduled is not None:
                    attempt, delay_seconds = scheduled
                    await enqueue_status_update(
                        bot,
                        user_id,
                        wid,
                        "⚠️ Agent stopped before finishing because the selected "
                        "model is temporarily at capacity.\n"
                        f"Automatic retry {attempt}/"
                        f"{len(_TRANSIENT_AGENT_RETRY_DELAYS_SECONDS)} in "
                        f"{int(delay_seconds)}s.",
                        thread_id=thread_id,
                    )
                    if msg.source_offset > 0:
                        await _mark_transcript_message_delivered(user_id, wid, msg)
                    continue
                agent_error_notice = (
                    f"{msg.text}\n\nAutomatic retries were exhausted "
                    f"({len(_TRANSIENT_AGENT_RETRY_DELAYS_SECONDS)} attempts). "
                    "Send the request again or switch models."
                )
        elif (
            not is_remote_target
            and msg.is_complete
            and msg.content_type == "text"
            and msg.role == "assistant"
        ):
            _reset_transient_agent_retry(wid)

        if msg.content_type == "usage_limit":
            if is_remote_target:
                await safe_send(
                    bot,
                    session_manager.resolve_chat_id(user_id, thread_id),
                    "⚠️ This remote session has hit its usage limit.",
                    message_thread_id=thread_id,
                )
                continue
            changed = session_manager.mark_window_usage_limit_exceeded(wid, True)
            current_state = session_manager.get_window_state(wid)
            next_account = get_next_account_name(current_state.account_name)
            note = "⚠️ This session has hit its usage limit."
            status_text = (
                "now marked as exhausted" if changed else "already marked as exhausted"
            )
            note += f"\nThe window is {status_text}."
            if config.enable_account_rotation and next_account:
                note += (
                    " On your next message, TelegramAgentBot will open a new "
                    f"`{next_account}` session automatically."
                )
            elif config.enable_account_rotation:
                note += (
                    "\nAutomatic rotation is enabled, but no backup account is "
                    "selected. Use /agentlogin <name> and /agentaccount use <name>."
                )
            else:
                note += (
                    "\nAutomatic account rotation is disabled. Use /agentlogin to "
                    "refresh the current login, or /agentaccount to choose a saved account."
                )
            await safe_send(
                bot,
                session_manager.resolve_chat_id(user_id, thread_id),
                note,
                message_thread_id=thread_id,
            )
            continue

        # Handle interactive tools specially - capture terminal and send UI
        if (
            not is_remote_target
            and msg.tool_name in INTERACTIVE_TOOL_NAMES
            and msg.content_type == "tool_use"
        ):
            # Mark interactive mode BEFORE sleeping so polling skips this window
            set_interactive_mode(user_id, wid, thread_id)
            # Flush pending messages (e.g. plan content) before sending interactive UI
            queue = get_message_queue(user_id, thread_id)
            if queue:
                await queue.join()
            # Wait briefly for Codex to render the question UI
            await asyncio.sleep(0.3)
            handled = await handle_interactive_ui(bot, user_id, wid, thread_id)
            if handled:
                await _mark_transcript_message_delivered(user_id, wid, msg)
                continue  # Don't send the normal tool_use message
            else:
                # UI not rendered — clear the early-set mode
                clear_interactive_mode(user_id, thread_id)

        # Any non-interactive message means the interaction is complete — delete the UI message
        if not is_remote_target and get_interactive_msg_id(user_id, thread_id):
            await clear_interactive_msg(user_id, bot, thread_id)

        # Keep reasoning out of persistent Telegram content. Encrypted-only
        # reasoning gets a generic status bubble; terminal polling provides
        # public progress separately.
        if msg.content_type == "thinking":
            # Never expose model reasoning text to Telegram. Keep only a
            # generic status for encrypted reasoning; terminal polling
            # provides live public progress updates separately.
            if TranscriptParser.is_encrypted_reasoning_placeholder(msg.text):
                await enqueue_status_update(
                    bot,
                    user_id,
                    wid,
                    "💭 Thinking…\n◦ Working on it…",
                    thread_id=thread_id,
                )
            if not is_remote_target and msg.source_offset > 0:
                await _mark_transcript_message_delivered(user_id, wid, msg)
            continue

        # Runtime write_stdin events are displayed as Wait(background terminal).
        # They are progress heartbeats rather than assistant output, so keep
        # them behind the single ephemeral Thinking status instead of sending
        # persistent Telegram bubbles that can outlive the run.
        if msg.tool_name == "Wait" and msg.content_type in ("tool_use", "tool_result"):
            if msg.content_type == "tool_use":
                await enqueue_status_update(
                    bot,
                    user_id,
                    wid,
                    BACKGROUND_WAIT_TOOL_STATUS_TEXT,
                    thread_id=thread_id,
                )
            if not is_remote_target:
                await _mark_transcript_message_delivered(user_id, wid, msg)
            continue

        # Clean mode keeps the topic focused on the final answer. Background
        # waits above remain visible as a generic status, while other tool and
        # local-command entries are omitted and still advance the read offset.
        if not session_manager.is_trace_mode(
            user_id, thread_id
        ) and msg.content_type in ("tool_use", "tool_result", "local_command"):
            if not is_remote_target and msg.source_offset > 0:
                await _mark_transcript_message_delivered(user_id, wid, msg)
            continue

        # Skip tool call notifications when TELEGRAM_AGENT_BOT_SHOW_TOOL_CALLS=false
        if not config.show_tool_calls and msg.content_type in (
            "tool_use",
            "tool_result",
        ):
            continue

        delivery_text = agent_error_notice or msg.text
        delivery_content_type = "text" if agent_error_notice else msg.content_type
        parts = build_response_parts(
            delivery_text,
            msg.is_complete,
            delivery_content_type,
            msg.role,
        )

        if msg.is_complete:
            # Enqueue content message task
            # Note: tool_result editing is handled inside _process_content_task
            # to ensure sequential processing with tool_use message sending
            delivered = await enqueue_content_message(
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

            # Mark only the delivered transcript bytes as read for this user.
            if not delivered:
                logger.warning(
                    "Transcript message was not delivered; leaving offset unchanged: "
                    "session=%s user=%d thread=%s window_id=%s content_type=%s",
                    msg.session_id,
                    user_id,
                    thread_id,
                    wid,
                    msg.content_type,
                )
                delivery_failed = True
                continue

            if not is_remote_target:
                await _mark_transcript_message_delivered(user_id, wid, msg)

    if delivery_failed:
        raise RuntimeError("One or more Telegram content deliveries failed")


# --- App lifecycle ---


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
