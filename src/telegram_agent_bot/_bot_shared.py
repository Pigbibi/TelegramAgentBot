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
  - directory_browser: Directory browser UI
  - interactive_ui: Interactive UI handling
  - status_polling: Terminal status polling
  - response_builder: Response message building

Key functions: create_bot(), handle_new_message().
"""

import asyncio
import copy
import contextlib
import io
import json
import logging
import os
import re
import shlex
import shutil
import sys
import sys as _sys
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TypedDict, TypeVar as _TypeVar, cast, cast as _cast

from telegram import (
    Bot,
    BotCommand,
    Chat,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
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
    clear_current_account,
    get_current_account_name,
    get_default_account_name,
    get_next_account_name,
    is_valid_account_name,
    list_account_names,
    prepare_account_home,
    remember_current_account,
    save_account_snapshot,
)
from .agent_io import (
    capture_agent_output,
    create_agent_session,
    send_agent_control,
    send_agent_input,
    send_agent_message,
    upload_agent_file,
)
from .agent_profile import (
    AGENT_CLAUDE,
    AGENT_CLAUDE_OFFICIAL,
    AGENT_CODEX,
    AGENT_CURSOR,
    AgentProfile,
    agent_capabilities,
    agent_display_name,
    is_claude_agent,
    normalize_agent_type,
)
from .backends.base import AgentBackend, AgentTarget
from .backends.browser import AgentBrowser
from .backends.input_routing import AgentInputMode
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
    AGENT_INPUT_QUEUED,
    AGENT_INPUT_SUBMITTED_UNCONFIRMED,
    PendingAgentInput,
    TRANSIENT_RETRY_SCHEDULED,
    DurableRuntimeStore,
    TransientAgentRetry,
)
from .handlers.callback_data import (
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
)
from .handlers.directory_browser import (
    BROWSE_DIRS_KEY,
    BROWSE_PAGE_KEY,
    BROWSE_PATH_KEY,
    ROOTS_KEY,
    SESSIONS_KEY,
    STATE_BROWSING_DIRECTORY,
    STATE_KEY,
    STATE_SELECTING_ROOT,
    STATE_SELECTING_AGENT,
    STATE_SELECTING_PROFILE,
    STATE_SELECTING_SESSION,
    STATE_SELECTING_WINDOW,
    UNBOUND_WINDOWS_KEY,
    PROFILE_MODELS_KEY,
    build_backend_root_picker,
    build_directory_browser,
    build_project_root_picker,
    build_session_picker,
    build_window_picker,
    build_agent_picker,
    build_profile_picker,
    clear_browse_state,
    clear_root_picker_state,
    clear_session_picker_state,
    clear_window_picker_state,
    clear_profile_picker_state,
)
from .handlers.cleanup import clear_topic_state
from .handlers.callback_router import (
    ASK_REFRESH_ACTIONS,
    CallbackContext,
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
)
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
    INPUT_ROUTE_CANCEL,
    INPUT_ROUTE_INTERRUPT,
    INPUT_ROUTE_QUEUE,
    build_input_routing_keyboard,
)
from .markdown_v2 import convert_markdown
from .model_catalog import refresh_model_catalog
from .handlers.response_builder import build_response_parts
from .handlers.status_polling import (
    clear_window_working,
    forget_missing_bound_window,
    mark_window_working,
    status_poll_loop,
)
from .idle_sessions import idle_session_hibernator
from .screenshot import text_to_image
from .session import (
    CodexSession,
    GroupChatRouteConflict,
    _session_ids_match,
    is_shell_pane_command,
    session_manager,
)
from .session_monitor import NewMessage
from .terminal_parser import (
    extract_auth_error_message,
    extract_bash_output,
    extract_interactive_content,
    is_agent_cli_startup_error,
    is_codex_input_ready,
    is_interactive_ui,
    parse_status_update,
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

logger = logging.getLogger("telegram_agent_bot.bot")
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
AGENT_AUTH_RECOVERY_MESSAGE = (
    "Agent login expired or was revoked. Use /agentlogin to sign in again, "
    "then send your message again."
)

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


@dataclass
class _QueuedAgentInput:
    text: str
    created_at: float = field(default_factory=time.monotonic)
    created_at_epoch: float = field(default_factory=time.time)
    store_id: int | None = None
    state: str = AGENT_INPUT_QUEUED
    transcript_session_id: str | None = None
    transcript_offset: int | None = None
    submission_mode: str = AGENT_INPUT_MODE_TURN


_AGENT_INPUT_POLL_INTERVAL_SECONDS = 1.0
_AGENT_INPUT_PERSIST_RETRY_SECONDS = 5.0
_AGENT_INPUT_CONFIRM_RETRY_SECONDS = 60.0
_AGENT_INPUT_CONFIRM_MAX_WAIT_SECONDS = 5 * 60.0
_INPUT_ROUTE_CHOICE_MAX_AGE_SECONDS = 15 * 60.0
_TRANSIENT_AGENT_RETRY_DELAYS_SECONDS = (15.0, 45.0, 120.0)
_TRANSIENT_AGENT_ERROR_CODES = frozenset({"server_overloaded"})
_TRANSIENT_AGENT_RETRY_PROMPT = (
    "Continue the previous user request from where it stopped. The previous turn "
    "ended because the selected model was temporarily at capacity. Do not repeat "
    "completed work; finish the unfinished task and provide a final answer."
)
_agent_input_queues: dict[tuple[int, int, str], deque[_QueuedAgentInput]] = {}
_agent_input_tasks: dict[tuple[int, int, str], asyncio.Task] = {}
_agent_input_locks: dict[tuple[int, int], asyncio.Lock] = {}
_agent_input_confirmation_tasks: dict[int, asyncio.Task[None]] = {}
_agent_input_confirmation_targets: dict[int, tuple[int, int, str]] = {}
_agent_input_confirmation_modes: dict[int, str] = {}
_transient_agent_retry_attempts: dict[str, int] = {}
_transient_agent_retry_tasks: dict[str, asyncio.Task[None]] = {}
_runtime_store = DurableRuntimeStore(config.config_dir / "runtime.sqlite3")
_health_monitor = VpsHealthMonitor(_runtime_store)

PRODUCT_NAME = "Agent"
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
PENDING_THREAD_MEDIA_KEY = "_pending_thread_media"
SESSION_STILL_RUNNING_MESSAGE = f"The {PRODUCT_NAME} session is still running in tmux."
HELP_COMMAND_DESCRIPTION = f"↗ Show {PRODUCT_NAME} help"
ESC_COMMAND_DESCRIPTION = f"Interrupt current {PRODUCT_NAME} run"
INTERRUPT_COMMAND_DESCRIPTION = "Interrupt; optional text sends next"
STEER_COMMAND_DESCRIPTION = "Guide the current agent turn"
QUEUE_COMMAND_DESCRIPTION = "Queue input for the next agent turn"
USAGE_COMMAND_DESCRIPTION = f"Show {PRODUCT_NAME} usage remaining"
ACCOUNT_COMMAND_DESCRIPTION = "Manage agent login accounts"
AGENT_LOGIN_COMMAND_DESCRIPTION = "Start agent device login"
AGENT_LOGIN_TIMEOUT_SECONDS = 16 * 60
_AGENT_LOGIN_DEFAULT_KEY = "__default__"
_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_DEVICE_LOGIN_URL_RE = re.compile(r"https?://\S+")
_DEVICE_LOGIN_CODE_RE = re.compile(r"\b[A-Z0-9]{4}-[A-Z0-9]{4,}\b")
_agent_login_tasks: dict[str, asyncio.Task[None]] = {}


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

_IMAGES_DIR = app_dir() / "images"
_IMAGES_DIR.mkdir(parents=True, exist_ok=True)
_FILES_DIR = app_dir() / "files"
_FILES_DIR.mkdir(parents=True, exist_ok=True)
_PENDING_TOPIC_DELETIONS_FILE = app_dir() / "pending_topic_deletions.json"
_SAFE_UPLOAD_FILENAME_RE = re.compile(r"[^A-Za-z0-9._-]+")

_bash_capture_tasks: dict[tuple[int, int], asyncio.Task[None]] = {}


_T = _TypeVar("_T")


def _b(name: str, fallback: _T) -> _T:
    """Resolve attribute from telegram_agent_bot.bot facade so tests patching bot.* work."""
    mod = _sys.modules.get("telegram_agent_bot.bot")
    if mod is not None and hasattr(mod, name):
        return _cast(_T, getattr(mod, name))
    return fallback


__all__ = [
    "ACCOUNT_COMMAND_DESCRIPTION",
    "AGENT_AUTH_RECOVERY_MESSAGE",
    "AGENT_CLAUDE",
    "AGENT_CLAUDE_OFFICIAL",
    "AGENT_CODEX",
    "AGENT_CURSOR",
    "AGENT_INPUT_MODE_QUEUE",
    "AGENT_INPUT_MODE_STEER",
    "AGENT_INPUT_MODE_TURN",
    "AGENT_INPUT_QUEUED",
    "AGENT_INPUT_SUBMITTED_UNCONFIRMED",
    "AGENT_LOGIN_COMMAND_DESCRIPTION",
    "AGENT_LOGIN_TIMEOUT_SECONDS",
    "AIORateLimiter",
    "ASK_REFRESH_ACTIONS",
    "AgentBackend",
    "AgentBrowser",
    "AgentInputMode",
    "AgentProfile",
    "AgentTarget",
    "Any",
    "Application",
    "ApplicationHandlerStop",
    "Awaitable",
    "BACKGROUND_WAIT_TOOL_STATUS_TEXT",
    "BROWSE_DIRS_KEY",
    "BROWSE_PAGE_KEY",
    "BROWSE_PATH_KEY",
    "BadRequest",
    "Bot",
    "BotCommand",
    "CB_ASK_ESC",
    "CB_ASK_REFRESH",
    "CB_CODEX_UPDATE_APPLY",
    "CB_CODEX_UPDATE_DISMISS",
    "CB_DIR_CANCEL",
    "CB_DIR_CONFIRM",
    "CB_DIR_PAGE",
    "CB_DIR_SELECT",
    "CB_DIR_UP",
    "CB_HISTORY_NEXT",
    "CB_HISTORY_PREV",
    "CB_INPUT_ROUTE",
    "CB_KEYS_PREFIX",
    "CB_OUTPUT_MODE",
    "CB_PROFILE_AGENT",
    "CB_PROFILE_CANCEL",
    "CB_PROFILE_CONFIRM",
    "CB_PROFILE_EFFORT",
    "CB_PROFILE_FAST",
    "CB_PROFILE_MODEL",
    "CB_PROFILE_PERMISSION",
    "CB_ROOT_CANCEL",
    "CB_ROOT_SELECT",
    "CB_SCREENSHOT_REFRESH",
    "CB_SESSION_CANCEL",
    "CB_SESSION_NEW",
    "CB_SESSION_SELECT",
    "CB_WIN_BIND",
    "CB_WIN_CANCEL",
    "CB_WIN_NEW",
    "CC_COMMANDS",
    "COMMAND_ARG_SEPARATORS",
    "Callable",
    "CallbackContext",
    "CallbackQueryHandler",
    "Chat",
    "ChatAction",
    "CodexSession",
    "CodexUpdateResult",
    "CommandHandler",
    "ContextTypes",
    "DEFAULT_REQUEST_CONNECT_TIMEOUT_SECONDS",
    "DEFAULT_REQUEST_POOL_TIMEOUT_SECONDS",
    "DEFAULT_REQUEST_READ_TIMEOUT_SECONDS",
    "DEFAULT_REQUEST_WRITE_TIMEOUT_SECONDS",
    "DirectoryBrowserKwargs",
    "DurableRuntimeStore",
    "ESC_COMMAND_DESCRIPTION",
    "FILE_CONFIRMATION_MESSAGE",
    "FILE_QUEUED_MESSAGE",
    "FORWARDED_COMMAND_RE",
    "GET_UPDATES_CONNECT_TIMEOUT_SECONDS",
    "GET_UPDATES_POOL_TIMEOUT_SECONDS",
    "GET_UPDATES_READ_TIMEOUT_SECONDS",
    "GET_UPDATES_WRITE_TIMEOUT_SECONDS",
    "GroupChatRouteConflict",
    "HELP_COMMAND_DESCRIPTION",
    "HTTPXRequest",
    "INPUT_ROUTE_CANCEL",
    "INPUT_ROUTE_INTERRUPT",
    "INPUT_ROUTE_QUEUE",
    "INTERACTIVE_TOOL_NAMES",
    "INTERRUPT_COMMAND_DESCRIPTION",
    "InlineKeyboardButton",
    "InlineKeyboardMarkup",
    "MEDIA_DOWNLOAD_ATTEMPTS",
    "MEDIA_DOWNLOAD_CONNECT_TIMEOUT_SECONDS",
    "MEDIA_DOWNLOAD_POOL_TIMEOUT_SECONDS",
    "MEDIA_DOWNLOAD_READ_TIMEOUT_SECONDS",
    "MEDIA_DOWNLOAD_RETRY_DELAY_SECONDS",
    "MEDIA_DOWNLOAD_WRITE_TIMEOUT_SECONDS",
    "MessageHandler",
    "NO_LINK_PREVIEW",
    "NetworkError",
    "NewMessage",
    "OUTPUT_MODE_CLEAN",
    "OUTPUT_MODE_TRACE",
    "PENDING_THREAD_MEDIA_KEY",
    "PHOTO_CONFIRMATION_MESSAGE",
    "PHOTO_QUEUED_MESSAGE",
    "POLL_TIMEOUT_SECONDS",
    "PRODUCT_NAME",
    "PROFILE_MODELS_KEY",
    "Path",
    "PendingAgentInput",
    "QUEUE_COMMAND_DESCRIPTION",
    "ROOTS_KEY",
    "RootSelection",
    "SESSIONS_KEY",
    "SESSION_STILL_RUNNING_MESSAGE",
    "STATE_BROWSING_DIRECTORY",
    "STATE_KEY",
    "STATE_SELECTING_AGENT",
    "STATE_SELECTING_PROFILE",
    "STATE_SELECTING_ROOT",
    "STATE_SELECTING_SESSION",
    "STATE_SELECTING_WINDOW",
    "STEER_COMMAND_DESCRIPTION",
    "TRANSIENT_RETRY_SCHEDULED",
    "TelegramError",
    "TimedOut",
    "TopicUpdateProcessor",
    "TranscriptParser",
    "TranscriptionError",
    "TransientAgentRetry",
    "TypeHandler",
    "TypedDict",
    "UNBOUND_WINDOWS_KEY",
    "UNSUPPORTED_CONTENT_MESSAGE",
    "USAGE_COMMAND_DESCRIPTION",
    "Update",
    "VpsHealthMonitor",
    "WELCOME_MESSAGE",
    "_AGENT_INPUT_CONFIRM_MAX_WAIT_SECONDS",
    "_AGENT_INPUT_CONFIRM_RETRY_SECONDS",
    "_AGENT_INPUT_PERSIST_RETRY_SECONDS",
    "_AGENT_INPUT_POLL_INTERVAL_SECONDS",
    "_AGENT_LOGIN_DEFAULT_KEY",
    "_ANSI_ESCAPE_RE",
    "_CODEX_UPDATE_PROMPT_STATE_FILENAME",
    "_COMMAND_ARG_SEPARATORS",
    "_DEVICE_LOGIN_CODE_RE",
    "_DEVICE_LOGIN_URL_RE",
    "_DirectoryBrowserKwargs",
    "_FILES_DIR",
    "_FORWARDED_COMMAND_RE",
    "_IMAGES_DIR",
    "_INPUT_ROUTE_CHOICE_MAX_AGE_SECONDS",
    "_KEYS_SEND_MAP",
    "_KEY_LABELS",
    "_PENDING_TOPIC_DELETIONS_FILE",
    "_QueuedAgentInput",
    "_RootSelection",
    "_SAFE_UPLOAD_FILENAME_RE",
    "_TRANSIENT_AGENT_ERROR_CODES",
    "_TRANSIENT_AGENT_RETRY_DELAYS_SECONDS",
    "_TRANSIENT_AGENT_RETRY_PROMPT",
    "_agent_input_confirmation_modes",
    "_agent_input_confirmation_targets",
    "_agent_input_confirmation_tasks",
    "_agent_input_locks",
    "_agent_input_queues",
    "_agent_input_tasks",
    "_agent_login_tasks",
    "_as_browser",
    "_auto_update_task",
    "_b",
    "_bash_capture_tasks",
    "_browse_backend_context",
    "_browse_root_context",
    "_clamp_to_selected_root",
    "_codex_update_apply_lock",
    "_codex_update_prompted_versions",
    "_directory_browser_kwargs",
    "_directory_listing_with_root",
    "_health_monitor",
    "_health_task",
    "_parse_root_selection",
    "_remote_child_path",
    "_remote_parent_path",
    "_runtime_stopped",
    "_runtime_store",
    "_safe_send_typing_action",
    "_session_ids_match",
    "_status_poll_task",
    "_transient_agent_retry_attempts",
    "_transient_agent_retry_tasks",
    "active_remote_root_browser",
    "adapt_forward_command_for_agent",
    "agent_backend",
    "agent_capabilities",
    "agent_display_name",
    "app_dir",
    "as_browser",
    "asyncio",
    "atomic_write_json",
    "browse_backend_context",
    "browse_root_context",
    "browser_for_backend_id",
    "build_agent_picker",
    "build_backend_directory_browser",
    "build_backend_root_picker",
    "build_directory_browser",
    "build_input_routing_keyboard",
    "build_profile_picker",
    "build_project_root_picker",
    "build_response_parts",
    "build_session_picker",
    "build_window_picker",
    "capture_agent_output",
    "cast",
    "check_codex_update",
    "clamp_to_selected_root",
    "clear_browse_state",
    "clear_current_account",
    "clear_interactive_mode",
    "clear_interactive_msg",
    "clear_permanent_delivery_failure",
    "clear_profile_picker_state",
    "clear_root_picker_state",
    "clear_session_picker_state",
    "clear_status_msg_info",
    "clear_topic_state",
    "clear_window_picker_state",
    "clear_window_working",
    "close_transcribe_client",
    "config",
    "contextlib",
    "convert_markdown",
    "copy",
    "create_agent_session",
    "dataclass",
    "default_directory_browser_path",
    "default_profile_permission",
    "deque",
    "directory_browser_kwargs",
    "directory_listing_with_root",
    "enqueue_content_message",
    "enqueue_status_update",
    "extract_auth_error_message",
    "extract_bash_output",
    "extract_interactive_content",
    "field",
    "filters",
    "forget_missing_bound_window",
    "format_health_snapshot",
    "get_configured_backend",
    "get_current_account_name",
    "get_default_account_name",
    "get_interactive_msg_id",
    "get_interactive_window",
    "get_message_queue",
    "get_next_account_name",
    "handle_ask_control",
    "handle_ask_esc",
    "handle_ask_refresh",
    "handle_codex_update_apply",
    "handle_codex_update_dismiss",
    "handle_dir_cancel",
    "handle_dir_confirm",
    "handle_dir_page",
    "handle_dir_select",
    "handle_dir_up",
    "handle_history_page",
    "handle_input_route",
    "handle_interactive_ui",
    "handle_noop",
    "handle_output_mode",
    "handle_profile_agent",
    "handle_profile_cancel",
    "handle_profile_confirm",
    "handle_profile_effort",
    "handle_profile_fast",
    "handle_profile_model",
    "handle_profile_permission",
    "handle_quick_keys",
    "handle_root_cancel",
    "handle_root_select",
    "handle_screenshot_refresh",
    "handle_session_cancel",
    "handle_session_new",
    "handle_session_select",
    "handle_window_bind",
    "handle_window_cancel",
    "handle_window_new",
    "idle_session_hibernator",
    "io",
    "is_agent_cli_startup_error",
    "is_claude_agent",
    "is_codex_input_ready",
    "is_interactive_ui",
    "is_shell_pane_command",
    "is_valid_account_name",
    "json",
    "list_account_names",
    "load_codex_update_settings",
    "load_update_env",
    "logger",
    "logging",
    "mark_window_working",
    "normalize_agent_type",
    "normalize_forward_command_text",
    "normalize_output_mode",
    "os",
    "output_mode_label",
    "parse_root_selection",
    "parse_status_update",
    "prepare_account_home",
    "profile_effort_values",
    "profile_from_context",
    "profile_models",
    "re",
    "refresh_model_catalog",
    "remember_current_account",
    "remote_child_path",
    "remote_parent_path",
    "resolve_profile_effort",
    "runtime_logger",
    "safe_edit",
    "safe_reply",
    "safe_send",
    "sanitize_forward_text",
    "save_account_snapshot",
    "send_agent_control",
    "send_agent_input",
    "send_agent_message",
    "send_history",
    "send_with_fallback",
    "session_manager",
    "session_monitor",
    "set_interactive_mode",
    "shlex",
    "shutdown_workers",
    "shutil",
    "status_poll_loop",
    "sys",
    "text_to_image",
    "time",
    "tmux_manager",
    "transcribe_voice",
    "turn_admission",
    "upload_agent_file",
    "validate_voice_duration",
]
