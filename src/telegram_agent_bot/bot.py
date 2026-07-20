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
import contextlib
import io
import json
import logging
import os
import posixpath
import re
import shlex
import shutil
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TypedDict, cast

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
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
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
    send_agent_message,
    upload_agent_file,
)
from .agent_profile import (
    AGENT_CLAUDE,
    AGENT_CODEX,
    AgentProfile,
    DEFAULT_CLAUDE_EFFORTS,
    DEFAULT_CODEX_EFFORTS,
    agent_display_name,
    normalize_agent_type,
    normalize_effort,
)
from .backends.base import AgentBackend, AgentTarget
from .backends.browser import AgentBrowser, DirectoryListing
from .backends.registry import get_configured_backend
from .config import config
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
    CB_OUTPUT_MODE,
    CB_HISTORY_NEXT,
    CB_HISTORY_PREV,
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
    STATE_SELECTING_AGENT,
    STATE_SELECTING_PROFILE,
    STATE_SELECTING_SESSION,
    STATE_SELECTING_WINDOW,
    UNBOUND_WINDOWS_KEY,
    PROFILE_AGENT_KEY,
    PROFILE_EFFORT_KEY,
    PROFILE_FAST_MODE_KEY,
    PROFILE_MODEL_KEY,
    PROFILE_MODELS_KEY,
    build_backend_root_picker,
    build_directory_browser,
    build_directory_browser_from_listing,
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
from .handlers.history import send_history
from .output_mode import (
    OUTPUT_MODE_CLEAN,
    OUTPUT_MODE_TRACE,
    normalize_output_mode,
    output_mode_label,
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
from .markdown_v2 import convert_markdown
from .model_catalog import refresh_model_catalog
from .handlers.response_builder import build_response_parts
from .handlers.status_polling import (
    clear_window_working,
    forget_missing_bound_window,
    mark_window_working,
    status_poll_loop,
)
from .screenshot import text_to_image
from .session import CodexSession, is_shell_pane_command, session_manager
from .session_monitor import NewMessage
from .terminal_parser import (
    extract_auth_error_message,
    extract_bash_output,
    extract_interactive_content,
    is_codex_input_ready,
    is_interactive_ui,
    parse_status_update,
)
from .tmux_manager import tmux_manager
from .transcript_parser import TranscriptParser
from .transcribe import TranscriptionError, close_client as close_transcribe_client
from .transcribe import transcribe_voice
from .updater import (
    CodexUpdateResult,
    check_codex_update,
    load_codex_update_settings,
    load_update_env,
)
from .utils import app_dir, sanitize_forward_text
from .utils import atomic_write_json

logger = logging.getLogger(__name__)

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
_runtime_stopped = False
_codex_update_prompted_versions: set[str] = set()
_codex_update_apply_lock: asyncio.Lock | None = None
_CODEX_UPDATE_PROMPT_STATE_FILENAME = "codex_update_prompt_state.json"


@dataclass
class _QueuedAgentInput:
    text: str
    created_at: float = field(default_factory=time.monotonic)


_AGENT_INPUT_POLL_INTERVAL_SECONDS = 1.0
_agent_input_queues: dict[tuple[int, int, str], deque[_QueuedAgentInput]] = {}
_agent_input_tasks: dict[tuple[int, int, str], asyncio.Task] = {}
_agent_input_locks: dict[tuple[int, int, str], asyncio.Lock] = {}

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
SESSION_STILL_RUNNING_MESSAGE = f"The {PRODUCT_NAME} session is still running in tmux."
HELP_COMMAND_DESCRIPTION = f"↗ Show {PRODUCT_NAME} help"
ESC_COMMAND_DESCRIPTION = f"Interrupt current {PRODUCT_NAME} run"
INTERRUPT_COMMAND_DESCRIPTION = "Interrupt; optional text sends next"
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


# Agent commands shown in bot menu (forwarded via tmux)
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
}


_FORWARDED_COMMAND_RE = re.compile(
    r"^/(?P<name>[A-Za-z0-9_]+)(?:@[A-Za-z0-9_]+)?(?P<rest>.*)$"
)
_COMMAND_ARG_SEPARATORS = ".。"


def _normalize_forward_command_text(cmd_text: str) -> str:
    """Normalize Telegram slash commands before forwarding them to Codex."""
    match = _FORWARDED_COMMAND_RE.match(cmd_text)
    if not match:
        return cmd_text

    command_name = match.group("name")
    rest = match.group("rest") or ""
    normalized = f"/{command_name}"

    # Telegram users sometimes type "/goal.继续..." from Chinese input. Codex
    # expects a space between a known slash command and its argument; without it
    # the TUI can leave the text pending and the bot reports a low-level send
    # failure. Keep this limited to known Codex commands so arbitrary forwarded
    # commands keep their original spelling.
    if (
        command_name.lower() in CC_COMMANDS
        and rest
        and rest[0] in _COMMAND_ARG_SEPARATORS
    ):
        rest = " " + rest[1:].lstrip()

    return normalized + rest


class _DirectoryBrowserKwargs(TypedDict, total=False):
    root_label: str
    root_path: str


class _RootSelection(TypedDict):
    label: str
    path: str
    backend_id: str
    node_id: str


def _default_directory_browser_path(root_path: str | None = None) -> str:
    """Choose a generic starting directory for the project browser."""
    if root_path:
        root = Path(root_path).expanduser()
        if root.is_dir():
            return str(root)
    projects_dir = config.default_projects_path.expanduser()
    if projects_dir.is_dir():
        return str(projects_dir)
    return str(Path.home())


def _browse_root_context(user_data: dict | None) -> tuple[str | None, str | None]:
    """Return selected browser root label/path from user state."""
    if not user_data:
        return None, None
    label = user_data.get(BROWSE_ROOT_LABEL_KEY)
    path = user_data.get(BROWSE_ROOT_PATH_KEY)
    return (
        label if isinstance(label, str) and label else None,
        path if isinstance(path, str) and path else None,
    )


def _directory_browser_kwargs(user_data: dict | None) -> _DirectoryBrowserKwargs:
    """Build root kwargs for the directory browser from user state."""
    label, path = _browse_root_context(user_data)
    kwargs: _DirectoryBrowserKwargs = {}
    if label:
        kwargs["root_label"] = label
    if path:
        kwargs["root_path"] = path
    return kwargs


def _browse_backend_context(user_data: dict | None) -> tuple[str, str]:
    """Return selected backend/node for a remote directory browser."""
    if not user_data:
        return "", ""
    backend_id = user_data.get(BROWSE_BACKEND_ID_KEY)
    node_id = user_data.get(BROWSE_NODE_ID_KEY)
    return (
        backend_id if isinstance(backend_id, str) else "",
        node_id if isinstance(node_id, str) else "",
    )


def _as_browser(backend: AgentBackend | None) -> AgentBrowser | None:
    """Return backend as AgentBrowser when it exposes the optional methods."""
    if backend is None:
        return None
    required = ("list_roots", "list_directory", "list_sessions")
    if not all(callable(getattr(backend, attr, None)) for attr in required):
        return None
    return cast(AgentBrowser, backend)


def _active_remote_root_browser() -> AgentBrowser | None:
    """Return the configured non-local backend browser for root selection."""
    backend = agent_backend
    if backend is None:
        return None
    try:
        info = backend.info()
    except Exception:
        logger.exception("Unable to read backend info for root browser")
        return None
    if backend.backend_id == "local" or info.mode == "local":
        return None
    return _as_browser(backend)


def _browser_for_backend_id(backend_id: str) -> AgentBrowser | None:
    """Return the active backend browser matching a cached root selection."""
    if not backend_id:
        return None
    backend = agent_backend or get_configured_backend()
    if backend.backend_id != backend_id:
        return None
    return _as_browser(backend)


def _parse_root_selection(entry: object) -> _RootSelection | None:
    """Parse cached root tuples from old local or new backend root pickers."""
    if not isinstance(entry, (list, tuple)) or len(entry) < 2:
        return None
    label, path = entry[0], entry[1]
    if not isinstance(label, str) or not isinstance(path, str):
        return None
    backend_id = entry[2] if len(entry) > 2 and isinstance(entry[2], str) else ""
    node_id = entry[3] if len(entry) > 3 and isinstance(entry[3], str) else ""
    return {
        "label": label,
        "path": path,
        "backend_id": backend_id,
        "node_id": node_id,
    }


def _remote_child_path(current_path: str, subdir_name: str) -> str:
    """Join one path segment for a backend node using POSIX-like paths."""
    current = current_path.rstrip("/") or "/"
    return posixpath.normpath(posixpath.join(current, subdir_name))


def _remote_parent_path(current_path: str, root_path: str | None) -> str:
    """Move one level up for a backend node while respecting selected root."""
    current = current_path.rstrip("/") or "/"
    root = root_path.rstrip("/") if root_path else ""
    if root and current == root:
        return root_path or root

    parent = posixpath.dirname(current) or "/"
    if root and not (parent == root or parent.startswith(root + "/")):
        return root_path or root
    return parent


def _directory_listing_with_root(
    listing: DirectoryListing,
    *,
    root_label: str | None,
    root_path: str | None,
) -> DirectoryListing:
    """Fill root metadata when a backend leaves it blank."""
    if listing.root_label or not root_label:
        return listing
    return DirectoryListing(
        path=listing.path,
        subdirs=listing.subdirs,
        root_label=root_label,
        root_path=listing.root_path or root_path or "",
        can_go_up=listing.can_go_up,
        error=listing.error,
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
    browser = _browser_for_backend_id(backend_id)
    if browser is None:
        return None
    try:
        listing = await browser.list_directory(
            node_id,
            path,
            root_path=root_path or "",
        )
    except Exception:
        logger.exception(
            "Backend directory listing failed: backend=%s node=%s path=%s",
            backend_id,
            node_id,
            path,
        )
        return None

    listing = _directory_listing_with_root(
        listing,
        root_label=root_label,
        root_path=root_path,
    )
    text, keyboard, subdirs = build_directory_browser_from_listing(listing, page=page)
    return text, keyboard, subdirs, listing.path


def _clamp_to_selected_root(path: str, user_data: dict | None) -> str:
    """Keep selected paths inside the configured project root when present."""
    _label, root_path = _browse_root_context(user_data)
    if not root_path:
        return path

    root = Path(root_path).expanduser().resolve()
    selected = Path(path).expanduser().resolve()
    if selected == root or root in selected.parents:
        return str(selected)
    return str(root)


async def _show_root_or_directory_picker(
    target: Any,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    edit: bool = False,
) -> None:
    """Show the configured root picker or fall through to directory browsing."""

    browser = _active_remote_root_browser()
    if browser is not None:
        try:
            backend_roots = await browser.list_roots()
        except Exception:
            logger.exception("Unable to list backend roots")
            backend_roots = []
        if backend_roots:
            msg_text, keyboard, roots = build_backend_root_picker(backend_roots)
            if context.user_data is not None:
                context.user_data[STATE_KEY] = STATE_SELECTING_ROOT
                context.user_data[ROOTS_KEY] = roots
            if edit:
                await safe_edit(target, msg_text, reply_markup=keyboard)
            else:
                await safe_reply(target, msg_text, reply_markup=keyboard)
            return

    if getattr(config, "project_roots_configured", False):
        msg_text, keyboard, roots = build_project_root_picker(config.project_roots)
        if context.user_data is not None:
            context.user_data[STATE_KEY] = STATE_SELECTING_ROOT
            context.user_data[ROOTS_KEY] = roots
        if edit:
            await safe_edit(target, msg_text, reply_markup=keyboard)
        else:
            await safe_reply(target, msg_text, reply_markup=keyboard)
        return

    start_path = _default_directory_browser_path()
    msg_text, keyboard, subdirs = build_directory_browser(start_path)
    if context.user_data is not None:
        context.user_data[STATE_KEY] = STATE_BROWSING_DIRECTORY
        context.user_data[BROWSE_PATH_KEY] = start_path
        context.user_data[BROWSE_PAGE_KEY] = 0
        context.user_data[BROWSE_DIRS_KEY] = subdirs
    if edit:
        await safe_edit(target, msg_text, reply_markup=keyboard)
    else:
        await safe_reply(target, msg_text, reply_markup=keyboard)


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
            type(context.error),
            context.error,
            context.error.__traceback__,
        )
        if context.error
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


def _filter_resumable_sessions(
    sessions: list["CodexSession"],
) -> list["CodexSession"]:
    """Hide sessions that are already active in another Telegram topic."""
    return [
        session
        for session in sessions
        if not session_manager.has_bound_thread_for_session(session.session_id)
    ]


def _has_trackable_session_for_window(window_id: str) -> bool:
    """Return whether an existing tmux window already has a known Codex session."""
    state = session_manager.window_states.get(window_id)
    return bool(state and state.session_id)


def _build_resume_conflict_keyboard() -> InlineKeyboardMarkup:
    """Offer safe next steps after blocking a duplicate resume."""
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("➕ New Session", callback_data=CB_SESSION_NEW),
                InlineKeyboardButton("Cancel", callback_data=CB_SESSION_CANCEL),
            ]
        ]
    )


def _profile_models(agent_type: str) -> tuple[str, ...]:
    """Return configured model choices for one agent type."""
    normalized = normalize_agent_type(agent_type)
    return config.claude_models if normalized == AGENT_CLAUDE else config.codex_models


def _profile_effort_values(agent_type: str, model: str) -> tuple[str, ...]:
    """Return reasoning efforts supported by the selected agent/model."""
    normalized = normalize_agent_type(agent_type)
    if normalized == AGENT_CLAUDE:
        return DEFAULT_CLAUDE_EFFORTS
    return config.codex_model_efforts.get(model, DEFAULT_CODEX_EFFORTS)


def _resolve_profile_effort(agent_type: str, model: str, requested: str) -> str:
    """Keep a supported effort or fall back to the model/provider default."""
    normalized = normalize_agent_type(agent_type)
    supported = _profile_effort_values(normalized, model)
    if not supported:
        return ""
    selected = normalize_effort(requested, "")
    if selected in supported:
        return selected

    candidates = []
    if normalized == AGENT_CODEX:
        candidates.append(config.codex_model_default_efforts.get(model, ""))
        candidates.append(config.codex_reasoning_effort)
    else:
        candidates.append(config.claude_reasoning_effort)
    candidates.extend(("medium", supported[0] if supported else ""))
    return next((value for value in candidates if value in supported), "medium")


def _profile_from_context(user_data: dict | None) -> AgentProfile:
    """Build the pending topic profile, retaining legacy defaults."""
    agent_type = (
        user_data.get(PROFILE_AGENT_KEY, config.agent_type)
        if user_data
        else config.agent_type
    )
    normalized = normalize_agent_type(agent_type, config.agent_type)
    default_effort = (
        config.claude_reasoning_effort
        if normalized == AGENT_CLAUDE
        else config.codex_reasoning_effort
    )
    requested_effort = (
        user_data.get(PROFILE_EFFORT_KEY, default_effort)
        if user_data
        else default_effort
    )
    model = user_data.get(PROFILE_MODEL_KEY, "") if user_data else ""
    fast_mode = user_data.get(PROFILE_FAST_MODE_KEY, False) if user_data else False
    return AgentProfile(
        agent_type=normalized,
        model=model if isinstance(model, str) else "",
        reasoning_effort=_resolve_profile_effort(
            normalized,
            model if isinstance(model, str) else "",
            requested_effort if isinstance(requested_effort, str) else default_effort,
        ),
        fast_mode=bool(fast_mode),
    )


async def _show_agent_profile_picker(
    target: Any,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    edit: bool = True,
) -> None:
    """Show the agent selector before a new topic creates a session."""
    text, keyboard = build_agent_picker()
    if context.user_data is not None:
        context.user_data[STATE_KEY] = STATE_SELECTING_AGENT
    if edit:
        await safe_edit(target, text, reply_markup=keyboard)
    else:
        await safe_reply(target, text, reply_markup=keyboard)


async def _show_agent_profile_settings(
    target: Any,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    edit: bool = True,
) -> None:
    """Show model and reasoning controls for the selected agent."""
    profile = _profile_from_context(context.user_data)
    models = _profile_models(profile.agent_type)
    if context.user_data is not None:
        context.user_data[STATE_KEY] = STATE_SELECTING_PROFILE
        context.user_data[PROFILE_MODELS_KEY] = list(models)
    text, keyboard = build_profile_picker(
        profile,
        models,
        effort_values=_profile_effort_values(profile.agent_type, profile.model),
    )
    if edit:
        await safe_edit(target, text, reply_markup=keyboard)
    else:
        await safe_reply(target, text, reply_markup=keyboard)


def _clear_creation_state(user_data: dict | None) -> None:
    """Clear all transient topic-creation state."""
    clear_browse_state(user_data)
    clear_session_picker_state(user_data)
    clear_profile_picker_state(user_data)
    if user_data is not None:
        for key in (
            "_pending_thread_id",
            "_pending_thread_text",
            "_selected_path",
            "_selected_backend_id",
            "_selected_node_id",
        ):
            user_data.pop(key, None)


async def _continue_creation_with_profile(
    query: Any,
    context: ContextTypes.DEFAULT_TYPE,
    user: Any,
) -> None:
    """Find a resumable session or create a new window for the chosen profile."""
    user_data = context.user_data
    selected_path = (
        user_data.get("_selected_path", str(Path.cwd()))
        if user_data
        else str(Path.cwd())
    )
    backend_id = user_data.get("_selected_backend_id", "") if user_data else ""
    node_id = user_data.get("_selected_node_id", "") if user_data else ""
    pending_thread_id = user_data.get("_pending_thread_id") if user_data else None

    profile = _profile_from_context(user_data)
    await query.answer("Looking for sessions...")
    await safe_edit(query, "⏳ Looking for existing sessions in this directory...")

    if backend_id:
        browser = _browser_for_backend_id(backend_id)
        if browser is None:
            await safe_edit(query, "Backend browser unavailable. Please retry.")
            return
        try:
            sessions = _filter_resumable_sessions(
                await browser.list_sessions(node_id, selected_path)
            )
        except Exception:
            logger.exception(
                "Backend session lookup failed: backend=%s node=%s path=%s",
                backend_id,
                node_id,
                selected_path,
            )
            await safe_edit(query, "Unable to read sessions from backend.")
            return
    else:
        sessions = _filter_resumable_sessions(
            await session_manager.list_sessions_for_directory(selected_path)
        )

    if sessions:
        if user_data is not None:
            user_data[STATE_KEY] = STATE_SELECTING_SESSION
            user_data[SESSIONS_KEY] = sessions
        text, keyboard = build_session_picker(sessions)
        await safe_edit(query, text, reply_markup=keyboard)
        return

    create_kwargs: dict[str, Any] = {
        "answer_callback": False,
        "agent_type": profile.agent_type,
        "model": profile.model,
        "reasoning_effort": profile.reasoning_effort,
        "fast_mode": profile.fast_mode,
    }
    if node_id:
        create_kwargs["node_id"] = node_id
    await _create_and_bind_window(
        query,
        context,
        user,
        selected_path,
        pending_thread_id,
        **create_kwargs,
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
    input_was_ready = False
    if wid:
        capture = await capture_agent_output(user.id, thread_id, wid)
        pane_text = capture.text if capture and not capture.missing else None
        input_was_ready = is_codex_input_ready(pane_text or "")
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
    if input_was_ready:
        success, message = await _send_message_to_agent(
            user.id,
            thread_id,
            wid or "",
            payload,
        )
    else:
        ok, control_message = await _send_control_to_agent(
            user.id,
            thread_id,
            wid or "",
            "Escape",
        )
        if not ok:
            await safe_reply(update.message, f"❌ {control_message}")
            return

        await asyncio.sleep(0.3)
        if wid:
            success, message = await _send_to_window_when_codex_ready(
                user.id,
                thread_id,
                wid,
                payload,
                timeout=15.0,
                interval=0.25,
            )
            if not success:
                logger.warning(
                    "Agent did not become ready after interrupt in window %s: %s; "
                    "queuing replacement until ready",
                    wid,
                    message,
                )
                success, message = await _queue_agent_input_after_interrupt(
                    context.bot,
                    user.id,
                    thread_id,
                    wid,
                    payload,
                )
                queued_replacement = success
        else:
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
        await _refresh_session_map_after_first_prompt(wid)


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
    return normalize_agent_type(agent_type, config.agent_type)


def _agent_command_name(agent_type: str, action: str) -> str:
    prefix = "claude" if agent_type == AGENT_CLAUDE else "codex"
    return f"/{prefix}{action}"


def _agent_login_executable(agent_type: str | None = None) -> str:
    """Return the agent executable to use for device login.

    Supports both Codex (``codex login``) and Claude Code
    (``claude auth login``).
    """
    selected_agent = _requested_agent_type(agent_type)
    if agent_type is None:
        command = config.codex_command
    elif selected_agent == AGENT_CLAUDE:
        command = config.claude_command
    else:
        command = getattr(config, "codex_cli_command", config.codex_command)
    try:
        command_parts = shlex.split(command)
    except ValueError:
        command_parts = []

    for part in command_parts:
        name, sep, _value = part.partition("=")
        if sep and name.isidentifier():
            continue
        return shutil.which(part) or part
    default = "claude" if selected_agent == AGENT_CLAUDE else "codex"
    return shutil.which(default) or default


def _agent_login_args(agent_type: str | None = None) -> list[str]:
    """Return the argv used to start an interactive agent login."""
    selected_agent = _requested_agent_type(agent_type)
    executable = _agent_login_executable(agent_type)
    if selected_agent == AGENT_CLAUDE:
        return [executable, "auth", "login"]
    return [executable, "login", "--device-auth"]


def _extract_device_login_details(output: str) -> tuple[str | None, str | None]:
    """Extract the browser URL and one-time device code from Codex login output."""
    clean = _ANSI_ESCAPE_RE.sub("", output)
    url_match = _DEVICE_LOGIN_URL_RE.search(clean)
    code_match = _DEVICE_LOGIN_CODE_RE.search(clean)
    return (
        url_match.group(0).rstrip(".,)") if url_match else None,
        code_match.group(0) if code_match else None,
    )


def _login_display_name(account_name: str | None, agent_type: str | None = None) -> str:
    agent_label = agent_display_name(_requested_agent_type(agent_type))
    if account_name:
        return f"account `{account_name}`"
    return f"the service user's default {agent_label} account"


def _codex_auth_recovery_message(_pane_error: str | None = None) -> str:
    """Return the Telegram-facing recovery instruction for Codex auth failures."""
    return AGENT_AUTH_RECOVERY_MESSAGE


def _account_command_usage(agent_type: str | None = None) -> str:
    selected_agent = _requested_agent_type(agent_type)
    account_command = _agent_command_name(selected_agent, "account")
    login_command = _agent_command_name(selected_agent, "login")
    return (
        "Usage:\n"
        f"{account_command} list\n"
        f"{account_command} use <name>\n"
        f"{account_command} clear\n"
        f"{account_command} save <name>\n"
        f"{login_command} [name] — login to {agent_display_name(selected_agent)}"
    )


def _format_account_status(agent_type: str | None = None) -> str:
    names = list_account_names(agent_type)
    current = get_current_account_name(agent_type)
    rotation = "enabled" if config.enable_account_rotation else "disabled"
    selected_agent = _requested_agent_type(agent_type)
    agent_label = agent_display_name(selected_agent)
    env_var = "HOME" if selected_agent == AGENT_CLAUDE else "CODEX_HOME"
    login_command = _agent_command_name(selected_agent, "login")
    lines = [
        f"🔐 {agent_label} account status",
        f"Automatic quota rotation: {rotation}",
        (
            f"New sessions: saved account `{current}`"
            if current
            else f"New sessions: service user's default {env_var}"
        ),
    ]
    if names:
        lines.append("Saved accounts:")
        for name in names:
            suffix = " (selected)" if name == current else ""
            lines.append(f"- `{name}`{suffix}")
    else:
        lines.append("Saved accounts: none")
    lines.append(f"Use {login_command} [name] to refresh login from Telegram.")
    return "\n".join(lines)


async def _wait_for_agent_login_details(
    process: asyncio.subprocess.Process,
) -> tuple[str | None, str | None]:
    """Read Codex login output until the URL/code pair appears."""
    if process.stdout is None:
        return None, None

    output = ""
    deadline = asyncio.get_running_loop().time() + 30
    while asyncio.get_running_loop().time() < deadline:
        timeout = max(0.1, deadline - asyncio.get_running_loop().time())
        try:
            line = await asyncio.wait_for(process.stdout.readline(), timeout=timeout)
        except TimeoutError:
            break
        if not line:
            break
        output += line.decode("utf-8", errors="replace")
        login_url, login_code = _extract_device_login_details(output)
        if login_url and login_code:
            return login_url, login_code
    return _extract_device_login_details(output)


async def _agent_login_worker(
    *,
    bot: Bot,
    chat_id: int,
    thread_id: int | None,
    account_name: str | None,
    login_key: str,
    agent_type: str,
) -> None:
    """Run agent login and report status back to Telegram.

    For Codex: runs ``codex login --device-auth``.
    For Claude Code: runs ``claude auth login``.
    """
    account_home = None
    process: asyncio.subprocess.Process | None = None
    try:
        env = os.environ.copy()
        if account_name:
            account_home = prepare_account_home(account_name, agent_type)
            if agent_type == AGENT_CLAUDE:
                env["HOME"] = str(account_home)
            else:
                env["CODEX_HOME"] = str(account_home)

        login_args = _agent_login_args(agent_type)

        process = await asyncio.create_subprocess_exec(
            *login_args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=env,
        )

        login_url, login_code = await _wait_for_agent_login_details(process)
        if not login_url or not login_code:
            if process.returncode is None:
                process.terminate()
            await safe_send(
                bot,
                chat_id,
                "❌ Agent login did not print a device URL/code. "
                "Please check the service logs or try again.",
                message_thread_id=thread_id,
            )
            return

        await safe_send(
            bot,
            chat_id,
            "🔐 Agent login started for "
            f"{_login_display_name(account_name, agent_type)}.\n"
            f"Open: {login_url}\n"
            f"Code: `{login_code}`\n"
            "Expires in about 15 minutes. Only complete this login if you requested it.",
            message_thread_id=thread_id,
        )

        try:
            return_code = await asyncio.wait_for(
                process.wait(), timeout=AGENT_LOGIN_TIMEOUT_SECONDS
            )
        except TimeoutError:
            process.terminate()
            await safe_send(
                bot,
                chat_id,
                f"❌ Agent login timed out. Run {_agent_command_name(agent_type, 'login')} again when ready.",
                message_thread_id=thread_id,
            )
            return

        if return_code != 0:
            await safe_send(
                bot,
                chat_id,
                f"❌ Agent login failed or was cancelled. Run {_agent_command_name(agent_type, 'login')} again if needed.",
                message_thread_id=thread_id,
            )
            return

        if account_name and account_home is not None:
            save_account_snapshot(account_name, account_home, agent_type)
            remember_current_account(account_name, agent_type)
            await safe_send(
                bot,
                chat_id,
                f"✅ Agent login completed for account `{account_name}` and saved.\n"
                "New topics will use this account. Existing topics will be "
                "recreated automatically if their current pane is still blocked "
                "by the old login.",
                message_thread_id=thread_id,
            )
        else:
            await safe_send(
                bot,
                chat_id,
                "✅ Agent login completed. Send your message again. Existing topics "
                "will be recreated automatically if their current pane is still "
                "blocked by the old login.",
                message_thread_id=thread_id,
            )
    except Exception as exc:
        logger.exception("Agent login failed")
        await safe_send(
            bot,
            chat_id,
            f"❌ Agent login failed: {exc}",
            message_thread_id=thread_id,
        )
    finally:
        _agent_login_tasks.pop(login_key, None)


async def _agent_login_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE, agent_type: str | None = None
) -> None:
    """Start login for the configured or explicitly selected agent."""
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        return
    if not update.message or not update.effective_chat:
        return

    selected_agent = _requested_agent_type(agent_type)
    args = context.args or []
    if len(args) > 1:
        await safe_reply(
            update.message,
            f"Usage: {_agent_command_name(selected_agent, 'login')} [account-name]",
        )
        return

    account_name = args[0].strip() if args else None
    if account_name and not is_valid_account_name(account_name):
        await safe_reply(
            update.message,
            "❌ Invalid account name. Use letters, numbers, dot, underscore, or dash.",
        )
        return

    login_key = f"{selected_agent}:{account_name or _AGENT_LOGIN_DEFAULT_KEY}"
    existing = _agent_login_tasks.get(login_key)
    if existing and not existing.done():
        await safe_reply(
            update.message,
            f"⏳ {agent_display_name(selected_agent)} login is already running for "
            f"{_login_display_name(account_name, selected_agent)}.",
        )
        return

    task = asyncio.create_task(
        _agent_login_worker(
            bot=context.bot,
            chat_id=update.effective_chat.id,
            thread_id=_get_thread_id(update),
            account_name=account_name,
            login_key=login_key,
            agent_type=selected_agent,
        )
    )
    _agent_login_tasks[login_key] = task
    await safe_reply(
        update.message,
        f"⏳ Starting {agent_display_name(selected_agent)} login for "
        f"{_login_display_name(account_name, selected_agent)}...",
    )


async def agent_login_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    await _agent_login_command(update, context)


async def codex_login_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    await _agent_login_command(update, context, AGENT_CODEX)


async def claude_login_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    await _agent_login_command(update, context, AGENT_CLAUDE)


async def _agent_account_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE, agent_type: str | None = None
) -> None:
    """Manage saved account snapshots for one agent."""
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        return
    if not update.message:
        return

    selected_agent = _requested_agent_type(agent_type)
    args = context.args or []
    if not args or args[0].lower() == "list":
        await safe_reply(update.message, _format_account_status(selected_agent))
        return

    action = args[0].lower()
    if action == "clear":
        clear_current_account(selected_agent)
        await safe_reply(
            update.message,
            f"✅ New {agent_display_name(selected_agent)} sessions will use the "
            f"service user's default {'HOME' if selected_agent == AGENT_CLAUDE else 'CODEX_HOME'}. "
            "Existing topics keep their current window; use /unbind to start fresh.",
        )
        return

    if action not in {"save", "use", "switch"} or len(args) != 2:
        await safe_reply(update.message, _account_command_usage(selected_agent))
        return

    account_name = args[1].strip()
    if not is_valid_account_name(account_name):
        await safe_reply(
            update.message,
            "❌ Invalid account name. Use letters, numbers, dot, underscore, or dash.",
        )
        return

    if action == "save":
        try:
            save_account_snapshot(account_name, agent_type=selected_agent)
        except FileNotFoundError:
            await safe_reply(
                update.message,
                f"❌ {agent_display_name(selected_agent)} auth data was not found. "
                f"Use {_agent_command_name(selected_agent, 'login')} first.",
            )
            return
        await safe_reply(
            update.message,
            f"✅ Saved current {agent_display_name(selected_agent)} login as account "
            f"`{account_name}`. Use {_agent_command_name(selected_agent, 'account')} "
            f"use {account_name} to select it for new sessions.",
        )
        return

    names = list_account_names(selected_agent)
    if account_name not in names:
        await safe_reply(
            update.message,
            f"❌ Account `{account_name}` is not saved yet. Use "
            f"{_agent_command_name(selected_agent, 'login')} {account_name} first.",
        )
        return

    remember_current_account(account_name, selected_agent)
    await safe_reply(
        update.message,
        f"✅ New sessions will use saved account `{account_name}`. Existing topics "
        "keep their current window; use /unbind if you want this topic to start fresh.",
    )


async def agent_account_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    await _agent_account_command(update, context)


async def codex_account_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    await _agent_account_command(update, context, AGENT_CODEX)


async def claude_account_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
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


async def _send_control_to_agent(
    user_id: int,
    thread_id: int | None,
    window_id: str,
    key: str,
) -> tuple[bool, str]:
    result = await send_agent_control(user_id, thread_id, window_id, key)
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
    result = await send_agent_message(user_id, thread_id, window_id, text)
    if result is None:
        return False, "No session bound"
    if result.missing:
        return False, "Window not found (may have been closed)"
    return result.ok, result.message


def _agent_input_key(
    user_id: int,
    thread_id: int | None,
    window_id: str,
) -> tuple[int, int, str]:
    return (user_id, thread_id or 0, window_id)


def _agent_input_lock(key: tuple[int, int, str]) -> asyncio.Lock:
    lock = _agent_input_locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _agent_input_locks[key] = lock
    return lock


def _agent_input_queue_max_size() -> int:
    return max(1, int(getattr(config, "agent_input_queue_max_size", 20)))


def _agent_input_queue_max_wait_seconds() -> float:
    return max(
        0.0,
        float(getattr(config, "agent_input_queue_max_wait_seconds", 1800.0)),
    )


def _queue_agent_input(
    key: tuple[int, int, str],
    text: str,
) -> tuple[bool, int, int]:
    queue = _agent_input_queues.setdefault(key, deque())
    limit = _agent_input_queue_max_size()
    if len(queue) >= limit:
        return False, len(queue), limit
    queue.append(_QueuedAgentInput(text=text))
    return True, len(queue), limit


async def _drop_expired_agent_input(
    bot: Bot,
    key: tuple[int, int, str],
    queue: deque[_QueuedAgentInput],
) -> None:
    max_wait = _agent_input_queue_max_wait_seconds()
    if max_wait <= 0:
        return

    now = time.monotonic()
    expired = 0
    while queue and now - queue[0].created_at >= max_wait:
        queue.popleft()
        expired += 1

    if expired:
        user_id, thread_key, _window_id = key
        thread_id = thread_key or None
        wait_display = int(max_wait)
        await _notify_queued_input_failure(
            bot,
            user_id,
            thread_id,
            f"{expired} queued input(s) expired after waiting {wait_display}s "
            "for the agent to become ready",
        )


def _ensure_agent_input_drain_task(
    bot: Bot,
    key: tuple[int, int, str],
) -> None:
    task = _agent_input_tasks.get(key)
    if task and not task.done():
        return
    _agent_input_tasks[key] = asyncio.create_task(_drain_agent_input_queue(bot, key))


async def _queue_agent_input_after_interrupt(
    bot: Bot,
    user_id: int,
    thread_id: int | None,
    window_id: str,
    text: str,
) -> tuple[bool, str]:
    """Queue replacement text after an explicit interrupt request."""
    key = _agent_input_key(user_id, thread_id, window_id)
    async with _agent_input_lock(key):
        queued, depth, limit = _queue_agent_input(key, text)
        if not queued:
            _ensure_agent_input_drain_task(bot, key)
            return (
                False,
                "Agent is still handling the interrupt and the input queue is full "
                f"({limit} pending). Wait for it to finish or use /esc first.",
            )
        _ensure_agent_input_drain_task(bot, key)
        return (
            True,
            f"Interrupt requested; queued message until the agent is ready ({depth}/{limit})",
        )


async def _send_or_queue_agent_input(
    bot: Bot,
    user_id: int,
    thread_id: int | None,
    window_id: str,
    text: str,
) -> tuple[bool, str, bool]:
    """Send directly unless a pending interactive UI needs bot-side ordering."""
    key = _agent_input_key(user_id, thread_id, window_id)
    result: tuple[bool, str, bool]
    async with _agent_input_lock(key):
        queue = _agent_input_queues.get(key)
        if queue is not None:
            queued, depth, limit = _queue_agent_input(key, text)
            if not queued:
                _ensure_agent_input_drain_task(bot, key)
                result = (
                    False,
                    "Agent is still busy and the input queue is full "
                    f"({limit} pending). Wait for it to finish or use /interrupt.",
                    False,
                )
            else:
                _ensure_agent_input_drain_task(bot, key)
                result = (
                    True,
                    f"Queued until the agent is ready ({depth}/{limit})",
                    True,
                )
            return result

        capture = await capture_agent_output(user_id, thread_id, window_id)
        if capture is None:
            result = False, "No session bound", False
        elif capture.missing:
            result = False, "Window not found (may have been closed)", False
        else:
            pane_text = capture.text or ""
            auth_error = extract_auth_error_message(pane_text)
            if auth_error:
                result = False, _codex_auth_recovery_message(auth_error), False
            elif is_interactive_ui(pane_text):
                ok, control_message = await _send_control_to_agent(
                    user_id,
                    thread_id,
                    window_id,
                    "Escape",
                )
                if not ok:
                    logger.warning(
                        "Failed to interrupt Codex interactive prompt before "
                        "queuing input (user=%d thread=%s window=%s): %s",
                        user_id,
                        thread_id,
                        window_id,
                        control_message,
                    )
                queued, depth, limit = _queue_agent_input(key, text)
                if not queued:
                    result = (
                        False,
                        "The agent is waiting for an interactive choice and the input "
                        "queue is full "
                        f"({limit} pending). Wait for it to finish or use /interrupt.",
                        False,
                    )
                else:
                    _ensure_agent_input_drain_task(bot, key)
                    result = (
                        True,
                        "Interrupted agent prompt and queued until the agent is ready "
                        f"({depth}/{limit})",
                        True,
                    )
            else:
                success, message = await _send_message_to_agent(
                    user_id,
                    thread_id,
                    window_id,
                    text,
                )
                result = success, message, False

    if key not in _agent_input_queues and key not in _agent_input_tasks:
        _agent_input_locks.pop(key, None)
    return result


async def _notify_queued_input_failure(
    bot: Bot,
    user_id: int,
    thread_id: int | None,
    message: str,
) -> None:
    try:
        await safe_send(
            bot,
            session_manager.resolve_chat_id(user_id, thread_id),
            f"❌ Queued message failed: {message}",
            message_thread_id=thread_id,
        )
    except Exception:
        logger.exception(
            "Failed to notify queued input failure (user=%d thread=%s)",
            user_id,
            thread_id,
        )


async def _drain_agent_input_queue(
    bot: Bot,
    key: tuple[int, int, str],
) -> None:
    """Forward queued user prompts one at a time when Codex returns to input."""
    user_id, thread_key, window_id = key
    thread_id = thread_key or None
    try:
        while True:
            queue = _agent_input_queues.get(key)
            if not queue:
                return
            await _drop_expired_agent_input(bot, key, queue)
            if not queue:
                return

            capture = await capture_agent_output(user_id, thread_id, window_id)
            if capture is None:
                await _notify_queued_input_failure(
                    bot, user_id, thread_id, "No session bound"
                )
                queue.clear()
                return
            if capture.missing:
                await _notify_queued_input_failure(
                    bot,
                    user_id,
                    thread_id,
                    "Window not found (may have been closed)",
                )
                queue.clear()
                return

            pane_text = capture.text or ""
            auth_error = extract_auth_error_message(pane_text)
            if auth_error:
                await _notify_queued_input_failure(
                    bot,
                    user_id,
                    thread_id,
                    _codex_auth_recovery_message(auth_error),
                )
                queue.clear()
                return
            if not is_codex_input_ready(pane_text) or is_interactive_ui(pane_text):
                await asyncio.sleep(_AGENT_INPUT_POLL_INTERVAL_SECONDS)
                continue
            item = queue[0]
            success, message = await _send_message_to_agent(
                user_id,
                thread_id,
                window_id,
                item.text,
            )
            if not success:
                queue.popleft()
                await _notify_queued_input_failure(bot, user_id, thread_id, message)
                continue

            queue.popleft()
            await mark_window_working(bot, user_id, window_id, thread_id)
            confirmed = await _refresh_session_map_after_first_prompt(
                window_id,
                text=item.text,
                confirm_existing_session=True,
            )
            if not confirmed:
                await _notify_queued_input_failure(
                    bot,
                    user_id,
                    thread_id,
                    "Agent did not confirm that the queued message reached the "
                    "transcript after submit retry",
                )
            await asyncio.sleep(_AGENT_INPUT_POLL_INTERVAL_SECONDS)
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception(
            "Agent input queue failed (user=%d thread=%s window=%s)",
            user_id,
            thread_id,
            window_id,
        )
    finally:
        queue = _agent_input_queues.get(key)
        if not queue:
            _agent_input_queues.pop(key, None)
        if _agent_input_tasks.get(key) is asyncio.current_task():
            _agent_input_tasks.pop(key, None)
        if key not in _agent_input_queues and key not in _agent_input_tasks:
            _agent_input_locks.pop(key, None)


async def _cancel_agent_input_drain_tasks() -> None:
    tasks = [task for task in _agent_input_tasks.values() if not task.done()]
    for task in tasks:
        task.cancel()
    for task in tasks:
        with contextlib.suppress(asyncio.CancelledError):
            await task
    _agent_input_tasks.clear()
    _agent_input_queues.clear()
    _agent_input_locks.clear()


async def _discard_queued_agent_input(
    user_id: int,
    thread_id: int | None,
    window_id: str,
) -> int:
    """Drop pending user prompts for a target before an explicit interrupt."""
    key = _agent_input_key(user_id, thread_id, window_id)
    async with _agent_input_lock(key):
        queue = _agent_input_queues.pop(key, None)
        dropped = len(queue) if queue else 0

        task = _agent_input_tasks.pop(key, None)
        if task and not task.done() and task is not asyncio.current_task():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    if key not in _agent_input_queues and key not in _agent_input_tasks:
        _agent_input_locks.pop(key, None)

    return dropped


def _remote_target_for_thread(
    user_id: int,
    thread_id: int | None,
) -> AgentTarget | None:
    """Return a non-local target bound to this thread, if any."""
    target = session_manager.resolve_target_for_thread(user_id, thread_id)
    if isinstance(target, AgentTarget) and target.backend_id != "local":
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
) -> tuple[bool, str, str, str, AgentTarget | None]:
    success, message, display_name, window_id, target = await _create_agent_target(
        cwd=cwd,
        window_name=window_name,
        resume_session_id=resume_session_id,
        account_name=account_name,
        agent_type=agent_type,
        model=model,
        reasoning_effort=reasoning_effort,
        fast_mode=fast_mode,
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
    if node_id:
        create_kwargs["node_id"] = node_id
    result = await create_agent_session(**create_kwargs)
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


async def forward_command_handler(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Forward any non-bot command as a slash command to the active Codex session."""
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
    cc_slash = _normalize_forward_command_text(cmd_text)
    wid = session_manager.resolve_window_for_thread(user.id, thread_id)
    target = session_manager.resolve_target_for_thread(user.id, thread_id)
    if not wid and not target:
        await safe_reply(update.message, "❌ No session bound to this topic.")
        return

    target_window_id = target.window_id if target else ""
    display = session_manager.get_display_name(wid or target_window_id)
    logger.info(
        "Forwarding command %s to window %s (user=%d)", cc_slash, display, user.id
    )
    await _safe_send_typing_action(update.message.chat, source="history_command")
    is_clear_command = cc_slash.strip().lower() == "/clear"
    if wid and not is_clear_command:
        success, message, queued = await _send_or_queue_agent_input(
            context.bot,
            user.id,
            thread_id,
            wid,
            cc_slash,
        )
        if not success:
            await safe_reply(update.message, f"❌ {message}")
            return
        action = "Queued" if queued else "Sent"
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
        success, result_message, queued = await _send_or_queue_agent_input(
            context.bot, user.id, thread_id, wid, command_text
        )
        if not success:
            await safe_reply(message, f"❌ {result_message}")
            return
        action = "Queued" if queued else "Sent"
        await safe_reply(message, f"⚡ {action}: {command_text}")
        return

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

    wid = session_manager.get_window_for_thread(user.id, thread_id)
    remote_target = None
    if wid is None:
        remote_target = _remote_target_for_thread(user.id, thread_id)
        if remote_target is None:
            await safe_reply(
                update.message,
                "❌ No session bound to this topic. Send a text message first to create one.",
            )
            return

    w = None
    if wid is not None:
        w = await tmux_manager.find_window_by_id(wid)
        if not w:
            display = session_manager.get_display_name(wid)
            session_manager.unbind_thread(user.id, thread_id)
            await safe_reply(
                update.message,
                f"❌ Window '{display}' no longer exists. Binding removed.\n"
                "Send a message to start a new session.",
            )
            return

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

    wid = session_manager.get_window_for_thread(user.id, thread_id)
    remote_target = None
    if wid is None:
        remote_target = _remote_target_for_thread(user.id, thread_id)
        if remote_target is None:
            await safe_reply(
                update.message,
                "❌ No session bound to this topic. Send a text message first to create one.",
            )
            return

    w = None
    if wid is not None:
        w = await tmux_manager.find_window_by_id(wid)
        if not w:
            display = session_manager.get_display_name(wid)
            session_manager.unbind_thread(user.id, thread_id)
            await safe_reply(
                update.message,
                f"❌ Window '{display}' no longer exists. Binding removed.\n"
                "Send a message to start a new session.",
            )
            return

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
        if not w:
            display = session_manager.get_display_name(wid)
            session_manager.unbind_thread(user.id, thread_id)
            await safe_reply(
                update.message,
                f"❌ Window '{display}' no longer exists. Binding removed.\n"
                "Send a message to start a new session.",
            )
            return

    # Download voice as in-memory bytes
    voice_file = await update.message.voice.get_file()
    ogg_data = bytes(await voice_file.download_as_bytearray())

    # Transcribe
    try:
        text = await transcribe_voice(ogg_data)
    except TranscriptionError as e:
        await safe_reply(update.message, f"⚠ {e}")
        return
    except Exception as e:
        logger.error("Voice transcription failed: %s", e)
        await safe_reply(update.message, f"⚠ Transcription failed: {e}")
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


async def _rotate_thread_after_usage_limit(
    *,
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
    thread_id: int,
    current_window_id: str,
    current_window_cwd: str,
    text: str,
) -> bool:
    """Rotate the topic onto a fresh account-backed window after quota exhaustion."""
    if not config.enable_account_rotation:
        await safe_send(
            context.bot,
            session_manager.resolve_chat_id(user_id, thread_id),
            "⚠️ This session has hit its usage limit. Automatic account rotation "
            "is disabled. Use /agentlogin to refresh the current agent login, "
            "or /agentaccount to choose a saved account. Then /unbind if you "
            "want this topic to start a fresh session.",
            message_thread_id=thread_id,
        )
        return True

    current_state = session_manager.get_window_state(current_window_id)
    next_account = get_next_account_name(current_state.account_name)
    if not next_account:
        await safe_send(
            context.bot,
            session_manager.resolve_chat_id(user_id, thread_id),
            "⚠️ This session has hit its usage limit, but no backup account is "
            "selected for rotation.\n"
            "Use /agentlogin <name> to add a saved account, then "
            "/agentaccount use <name> to select it.",
            message_thread_id=thread_id,
        )
        return True

    selected_path = current_window_cwd or current_state.cwd
    if not selected_path:
        await safe_send(
            context.bot,
            session_manager.resolve_chat_id(user_id, thread_id),
            "⚠️ This session has hit its usage limit, but the window working "
            "directory is unavailable, so I cannot open a replacement "
            "session automatically. Please `/unbind` and choose New Session.",
            message_thread_id=thread_id,
        )
        return True

    (
        success,
        message,
        created_wname,
        created_wid,
        created_target,
    ) = await _create_agent_local_window(
        cwd=selected_path,
        account_name=next_account,
    )
    if not success:
        await safe_send(
            context.bot,
            session_manager.resolve_chat_id(user_id, thread_id),
            f"⚠️ This session hit its usage limit, and automatic rotation to "
            f"`{next_account}` failed: {message}",
            message_thread_id=thread_id,
        )
        return True

    remember_current_account(next_account)
    session_manager.prepare_window_launch(
        created_wid,
        cwd=str(selected_path),
        window_name=created_wname,
        account_name=next_account,
    )
    if created_target:
        session_manager.bind_thread_target(
            user_id,
            thread_id,
            created_target,
            window_name=created_wname,
        )
    else:
        session_manager.bind_thread(
            user_id,
            thread_id,
            created_wid,
            window_name=created_wname,
        )

    resolved_chat = session_manager.resolve_chat_id(user_id, thread_id)

    send_ok, send_msg = await _send_to_window_when_codex_ready(
        user_id,
        thread_id,
        created_wid,
        text,
        auto_confirm_startup_trust=True,
    )
    if send_ok:
        await mark_window_working(context.bot, user_id, created_wid, thread_id)
        await _refresh_session_map_after_first_prompt(
            created_wid,
            text=text,
            confirm_existing_session=True,
        )
    if send_ok:
        await safe_send(
            context.bot,
            resolved_chat,
            f"♻️ This session hit its usage limit, so I switched to a new "
            f"`{next_account}` session and forwarded your message there.",
            message_thread_id=thread_id,
        )
    else:
        await safe_send(
            context.bot,
            resolved_chat,
            "♻️ This session hit its usage limit, and I switched to a new "
            f"session automatically, but forwarding failed: {send_msg}\n"
            "Please send the message again.",
            message_thread_id=thread_id,
        )
    return True


async def _maybe_confirm_startup_trust_prompt(
    window_id: str,
    pane_text: str,
) -> tuple[bool, str]:
    """Confirm safe startup-only trust prompts before the first forwarded text."""
    content = extract_interactive_content(pane_text)
    if content is None:
        return False, ""
    if content.name != "DirectoryTrust":
        return False, f"Agent is waiting for interactive input: {content.name}"
    logger.info("Auto-confirming Codex directory trust prompt in window %s", window_id)
    if await tmux_manager.send_control_key(window_id, "Enter"):
        return True, "Confirmed Codex directory trust prompt"
    return False, "Failed to confirm Codex directory trust prompt"


async def _send_to_window_when_codex_ready(
    user_id: int,
    thread_id: int | None,
    window_id: str,
    text: str,
    *,
    timeout: float | None = None,
    interval: float = 0.5,
    auto_confirm_startup_trust: bool = False,
) -> tuple[bool, str]:
    """Send text once the new Codex TUI is ready to accept input."""
    startup_timeout = (
        timeout
        if timeout is not None
        else getattr(config, "agent_startup_timeout_seconds", 180.0)
    )
    deadline = asyncio.get_event_loop().time() + startup_timeout
    last_message = ""
    while asyncio.get_event_loop().time() < deadline:
        capture = await capture_agent_output(user_id, thread_id, window_id)
        if capture is None:
            return False, "No session bound"
        if capture.missing:
            return False, "Window not found (may have been closed)"
        pane_text = capture.text
        auth_error = extract_auth_error_message(pane_text or "")
        if auth_error:
            return False, _codex_auth_recovery_message(auth_error)
        interactive = extract_interactive_content(pane_text or "")
        if interactive is not None:
            if auto_confirm_startup_trust:
                confirmed, trust_message = await _maybe_confirm_startup_trust_prompt(
                    window_id,
                    pane_text or "",
                )
                last_message = trust_message
                if confirmed:
                    await asyncio.sleep(interval)
                    continue
                if trust_message.startswith("Failed to confirm"):
                    return False, trust_message
            last_message = f"Agent is waiting for interactive input: {interactive.name}"
            await asyncio.sleep(interval)
            continue
        if not is_codex_input_ready(pane_text or ""):
            status = parse_status_update(pane_text or "")
            if status:
                last_message = f"Agent is still busy: {status}"
            else:
                last_message = "Agent UI is not ready for input"
            await asyncio.sleep(interval)
            continue
        send_ok, send_msg = await _send_message_to_agent(
            user_id,
            thread_id,
            window_id,
            text,
        )
        if send_ok:
            return True, send_msg
        last_message = send_msg
        if (
            "Window is not running an agent" not in send_msg
            and "Window is not running Codex" not in send_msg
        ):
            return False, send_msg
        await asyncio.sleep(interval)
    return False, last_message or "Agent did not become ready"


async def _enable_fast_mode_if_requested(
    bot: Bot,
    user_id: int,
    thread_id: int | None,
    window_id: str,
    profile: AgentProfile,
    chat_id: int,
) -> None:
    """Enable Fast mode after the selected agent session is ready."""
    if not profile.fast_mode:
        return

    success, message = await _send_to_window_when_codex_ready(
        user_id,
        thread_id,
        window_id,
        "/fast",
        auto_confirm_startup_trust=True,
    )
    if success:
        return
    logger.warning(
        "Failed to enable %s Fast mode for window %s: %s",
        profile.display_name,
        window_id,
        message,
    )
    await safe_send(
        bot,
        chat_id,
        "⚠️ Fast mode could not be enabled for this model. "
        "The session will continue with the selected reasoning level.",
        message_thread_id=thread_id,
    )


async def _refresh_session_map_after_first_prompt(
    window_id: str,
    *,
    text: str | None = None,
    confirm_existing_session: bool = False,
    timeout: float = 20.0,
) -> bool:
    """Load the session_map entry that Codex writes after the first prompt starts."""
    if session_manager.get_window_state(window_id).session_id:
        if text and confirm_existing_session:
            return await _confirm_first_prompt_delivery(window_id, text)
        return True
    hook_ok = await session_manager.wait_for_session_map_entry(
        window_id, timeout=timeout
    )
    if hook_ok:
        if text:
            return await _confirm_first_prompt_delivery(window_id, text)
        return True

    if text:
        if await tmux_manager.prompt_still_pending(window_id, text):
            logger.warning(
                "Codex window %s still has the first prompt in the input row; "
                "retrying Enter before waiting for session_map again",
                window_id,
            )
            if await tmux_manager.send_control_key(window_id, "Enter"):
                hook_ok = await session_manager.wait_for_session_map_entry(
                    window_id,
                    timeout=min(timeout, 10.0),
                )
                if hook_ok:
                    return await _confirm_first_prompt_delivery(window_id, text)
        logger.warning(
            "Codex window %s accepted input but did not register session_map",
            window_id,
        )
    else:
        logger.warning(
            "Codex window %s did not register session_map",
            window_id,
        )
    return hook_ok


async def _confirm_first_prompt_delivery(
    window_id: str,
    text: str,
    *,
    transcript_timeout: float = 5.0,
) -> bool:
    """Confirm that the first forwarded prompt reached the Codex transcript."""
    transcript_ok = await session_manager.wait_for_transcript_user_message(
        window_id,
        text,
        timeout=transcript_timeout,
    )
    if transcript_ok:
        return True

    if await tmux_manager.prompt_still_pending(window_id, text):
        logger.warning(
            "Codex window %s still has the first prompt in the input row after session "
            "registration; retrying Enter",
            window_id,
        )
        if not await tmux_manager.send_control_key(window_id, "Enter"):
            return False

        transcript_ok = await session_manager.wait_for_transcript_user_message(
            window_id,
            text,
            timeout=transcript_timeout,
        )
        if transcript_ok:
            return True

        still_pending = await tmux_manager.prompt_still_pending(window_id, text)
        if still_pending:
            logger.warning(
                "Codex window %s still has the first prompt pending after retry",
                window_id,
            )
            return False

        logger.info(
            "Codex window %s accepted the first prompt after retry but transcript "
            "confirmation is still pending",
            window_id,
        )
        return True

    if transcript_ok:
        return True

    if not await tmux_manager.prompt_still_pending(window_id, text):
        logger.info(
            "Codex window %s has a session but the first prompt was not observed "
            "in the transcript yet; input row is clear, so continuing",
            window_id,
        )
        return True

    logger.warning(
        "Codex window %s still has the first prompt in the input row after "
        "transcript confirmation timed out; retrying Enter",
        window_id,
    )
    if not await tmux_manager.send_control_key(window_id, "Enter"):
        return False

    transcript_ok = await session_manager.wait_for_transcript_user_message(
        window_id,
        text,
        timeout=transcript_timeout,
    )
    if transcript_ok:
        return True

    if await tmux_manager.prompt_still_pending(window_id, text):
        logger.warning(
            "Codex window %s still has the first prompt pending after retry",
            window_id,
        )
        return False

    logger.info(
        "Codex window %s accepted the first prompt after retry but transcript "
        "confirmation is still pending",
        window_id,
    )
    return True


async def _recover_missing_bound_window(
    *,
    user_id: int,
    thread_id: int,
    old_window_id: str,
    text: str,
) -> tuple[bool, str]:
    """Recreate a missing bound tmux window and forward the pending text."""
    state = session_manager.window_states.get(old_window_id)
    if not state or not state.session_id or not state.cwd:
        return False, "missing window has no resumable session state"

    resume_session_id = state.session_id
    selected_path = state.cwd
    requested_window_name = state.window_name or session_manager.get_display_name(
        old_window_id
    )
    account_name = state.account_name or None
    old_offsets = {
        uid: offsets[old_window_id]
        for uid, offsets in session_manager.user_window_offsets.items()
        if old_window_id in offsets
    }

    (
        success,
        message,
        created_wname,
        created_wid,
        created_target,
    ) = await _create_agent_local_window(
        cwd=selected_path,
        window_name=requested_window_name,
        resume_session_id=resume_session_id,
        account_name=account_name or "",
    )
    if not success:
        return False, message

    session_manager.prepare_window_launch(
        created_wid,
        cwd=str(selected_path),
        window_name=created_wname,
        account_name=account_name or "",
    )
    session_manager.register_session_to_window(
        created_wid,
        resume_session_id,
        str(selected_path),
        window_name=created_wname,
        persist_session_map=True,
    )

    hook_ok = await session_manager.wait_for_session_map_entry(
        created_wid, timeout=15.0
    )
    ws = session_manager.get_window_state(created_wid)
    if not hook_ok or ws.session_id != resume_session_id:
        logger.info(
            "Recovered missing window %s as %s; tracking resumed session_id=%s",
            old_window_id,
            created_wid,
            resume_session_id,
        )
        ws.session_id = resume_session_id
        ws.cwd = str(selected_path)
        ws.window_name = created_wname
        ws.account_name = account_name or ""
        session_manager._save_state()

    session_manager.unhide_session(resume_session_id)
    if created_target:
        session_manager.bind_thread_target(
            user_id,
            thread_id,
            created_target,
            window_name=created_wname,
        )
    else:
        session_manager.bind_thread(
            user_id,
            thread_id,
            created_wid,
            window_name=created_wname,
        )

    for offset_user_id, offset in old_offsets.items():
        offsets = session_manager.user_window_offsets.setdefault(offset_user_id, {})
        offsets[created_wid] = offset
        offsets.pop(old_window_id, None)
    if old_offsets:
        session_manager._save_state()

    await session_manager.remove_session_map_entry(old_window_id)
    session_manager.remove_window_state(old_window_id)
    forget_missing_bound_window(user_id, thread_id, old_window_id)

    send_ok, send_msg = await _send_to_window_when_codex_ready(
        user_id,
        thread_id,
        created_wid,
        text,
        auto_confirm_startup_trust=True,
    )
    if send_ok:
        await _refresh_session_map_after_first_prompt(
            created_wid,
            text=text,
            confirm_existing_session=True,
        )
        return True, f"Recovered window `{created_wname}` and forwarded your message."
    return (
        False,
        "recovered the tmux window, but forwarding failed: "
        f"{send_msg}. Please send the message again.",
    )


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
    if not is_shell_pane_command(pane_command):
        return False

    display = session_manager.get_display_name(window_id)
    state = session_manager.window_states.get(window_id)
    if state and state.session_id and state.cwd:
        logger.info(
            "Recovering non-Codex bound window %s (command=%s, user=%d, thread=%d)",
            display,
            pane_command,
            user_id,
            thread_id,
        )
        await safe_reply(
            update_message,
            f"♻️ Window `{display}` is no longer running {PRODUCT_NAME} "
            f"(current command: `{pane_command}`). Recreating it and resuming "
            "the previous session...",
        )
        await tmux_manager.kill_window(window_id)
        recovered, recovery_message = await _recover_missing_bound_window(
            user_id=user_id,
            thread_id=thread_id,
            old_window_id=window_id,
            text=text,
        )
        if not recovered:
            await safe_reply(update_message, f"❌ {recovery_message}")
        elif success_reply:
            await safe_reply(update_message, success_reply)
        return True

    logger.info(
        "Stale non-Codex binding: window %s command=%s, unbinding (user=%d, thread=%d)",
        display,
        pane_command,
        user_id,
        thread_id,
    )
    session_manager.unbind_thread(user_id, thread_id)
    await safe_reply(
        update_message,
        f"❌ Window '{display}' is not running {PRODUCT_NAME} "
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
    auth_error = extract_auth_error_message(pane_text)
    if not auth_error:
        return False

    display = session_manager.get_display_name(window_id)
    state = session_manager.window_states.get(window_id)
    if state and state.session_id and state.cwd:
        logger.info(
            "Recovering auth-blocked Codex window %s (user=%d, thread=%d)",
            display,
            user_id,
            thread_id,
        )
        await safe_reply(
            update_message,
            f"♻️ Window `{display}` is blocked by an expired Codex login. "
            "Recreating it with the current login and resuming the previous session...",
        )
        await tmux_manager.kill_window(window_id)
        recovered, recovery_message = await _recover_missing_bound_window(
            user_id=user_id,
            thread_id=thread_id,
            old_window_id=window_id,
            text=text,
        )
        if not recovered:
            await safe_reply(update_message, f"❌ {recovery_message}")
        elif success_reply:
            await safe_reply(update_message, success_reply)
        return True

    await safe_reply(
        update_message,
        f"⚠️ {_codex_auth_recovery_message(auth_error)}",
    )
    return True


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
        context.user_data.pop("_pending_thread_id", None)
        context.user_data.pop("_pending_thread_text", None)

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
        context.user_data.pop("_pending_thread_id", None)
        context.user_data.pop("_pending_thread_text", None)

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
        context.user_data.pop("_pending_thread_id", None)
        context.user_data.pop("_pending_thread_text", None)

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
        context.user_data.pop("_pending_thread_id", None)
        context.user_data.pop("_pending_thread_text", None)
        context.user_data.pop("_selected_path", None)

    # Must be in a named topic
    if thread_id is None:
        await safe_reply(
            update.message,
            "❌ Please use a named topic. Create a new topic to start a session.",
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

        if getattr(config, "project_roots_configured", False) or (
            _active_remote_root_browser() is not None
        ):
            logger.info(
                "Unbound topic: showing configured project root picker (user=%d, thread=%d)",
                user.id,
                thread_id,
            )
            if context.user_data is not None:
                context.user_data["_pending_thread_id"] = thread_id
                context.user_data["_pending_thread_text"] = text
            await _show_root_or_directory_picker(update.message, context)
            return

        # Unbound topic — check for unbound windows first
        all_windows = await tmux_manager.list_windows()
        bound_ids = {wid for _, _, wid in session_manager.iter_thread_bindings()}
        bindable_unbound = [
            (w.window_id, w.window_name, w.cwd)
            for w in all_windows
            if w.window_id not in bound_ids
            and _has_trackable_session_for_window(w.window_id)
        ]
        logger.debug(
            "Window picker check: all=%s, bound=%s, bindable_unbound=%s",
            [w.window_name for w in all_windows],
            bound_ids,
            [name for _, name, _ in bindable_unbound],
        )

        if bindable_unbound:
            # Show window picker
            logger.info(
                "Unbound topic: showing window picker (%d bindable windows, user=%d, thread=%d)",
                len(bindable_unbound),
                user.id,
                thread_id,
            )
            msg_text, keyboard, win_ids = build_window_picker(bindable_unbound)
            if context.user_data is not None:
                context.user_data[STATE_KEY] = STATE_SELECTING_WINDOW
                context.user_data[UNBOUND_WINDOWS_KEY] = win_ids
                context.user_data["_pending_thread_id"] = thread_id
                context.user_data["_pending_thread_text"] = text
            await safe_reply(update.message, msg_text, reply_markup=keyboard)
            return

        # No unbound windows — show root/directory picker to create a new session
        logger.info(
            "Unbound topic: showing project root or directory picker (user=%d, thread=%d)",
            user.id,
            thread_id,
        )
        if context.user_data is not None:
            context.user_data["_pending_thread_id"] = thread_id
            context.user_data["_pending_thread_text"] = text
        await _show_root_or_directory_picker(update.message, context)
        return

    # Bound topic — forward to bound window
    w = await tmux_manager.find_window_by_id(wid)
    if not w:
        display = session_manager.get_display_name(wid)
        state = session_manager.window_states.get(wid)
        if state and state.session_id and state.cwd:
            logger.info(
                "Recovering missing bound window %s (user=%d, thread=%d)",
                display,
                user.id,
                thread_id,
            )
            await safe_reply(
                update.message,
                f"♻️ Window `{display}` disappeared. Recreating it and resuming "
                "the previous session...",
            )
            recovered, recovery_message = await _recover_missing_bound_window(
                user_id=user.id,
                thread_id=thread_id,
                old_window_id=wid,
                text=text,
            )
            if not recovered:
                await safe_reply(update.message, f"❌ {recovery_message}")
            return

        logger.info(
            "Stale binding: window %s gone, unbinding (user=%d, thread=%d)",
            display,
            user.id,
            thread_id,
        )
        session_manager.unbind_thread(user.id, thread_id)
        await safe_reply(
            update.message,
            f"❌ Window '{display}' no longer exists. Binding removed.\n"
            "Send a message to start a new session.",
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
    if not queued:
        await mark_window_working(context.bot, user.id, wid, thread_id)
        confirmed = await _refresh_session_map_after_first_prompt(
            wid,
            text=text,
            confirm_existing_session=True,
        )
        if not confirmed:
            await safe_reply(
                update.message,
                "⚠️ I sent the message, but the agent did not confirm it reached "
                "the transcript after a submit retry. If the topic stays idle, "
                "send it again or use /interrupt.",
            )

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
    node_id: str = "",
    answer_callback: bool = True,
) -> None:
    """Create a tmux window, bind it to a topic, and forward pending text.

    Shared by CB_DIR_CONFIRM (no sessions), CB_SESSION_NEW, and CB_SESSION_SELECT.
    """
    from telegram import CallbackQuery, User

    assert isinstance(query, CallbackQuery)
    assert isinstance(user, User)

    normalized_agent = normalize_agent_type(agent_type or config.agent_type)
    profile = AgentProfile(
        agent_type=normalized_agent,
        model=model,
        reasoning_effort=reasoning_effort
        or (
            config.claude_reasoning_effort
            if normalized_agent == AGENT_CLAUDE
            else config.codex_reasoning_effort
        ),
        fast_mode=fast_mode,
    )
    # Account snapshots are agent-specific. Do not apply a Codex snapshot to a
    # Claude topic (or vice versa) when both agents are enabled per topic.
    launch_account = account_name or get_default_account_name(profile.agent_type)
    (
        success,
        message,
        created_wname,
        created_wid,
        created_target,
    ) = await _create_agent_target(
        cwd=selected_path,
        node_id=node_id,
        resume_session_id=resume_session_id or "",
        account_name=launch_account or "",
        agent_type=profile.agent_type,
        model=profile.model,
        reasoning_effort=profile.reasoning_effort,
        fast_mode=profile.fast_mode,
    )
    if success:
        if launch_account:
            remember_current_account(launch_account, profile.agent_type)
        if created_target and created_target.backend_id != "local":
            created_wname = created_wname or _format_remote_target(created_target)
            logger.info(
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
                session_manager.bind_thread_target(
                    user.id,
                    pending_thread_id,
                    created_target,
                    window_name=created_wname,
                )
                resolved_chat = session_manager.resolve_chat_id(
                    user.id,
                    pending_thread_id,
                )
                status = "Resumed" if resume_session_id else "Created"
                await safe_edit(
                    query,
                    f"✅ {message}\n\n{status}. Send messages here.",
                )

                await _enable_fast_mode_if_requested(
                    context.bot,
                    query.from_user.id,
                    pending_thread_id,
                    "",
                    profile,
                    resolved_chat,
                )

                pending_text = (
                    context.user_data.get("_pending_thread_text")
                    if context.user_data
                    else None
                )
                if pending_text:
                    pending_text = sanitize_forward_text(pending_text)
                if context.user_data is not None:
                    context.user_data.pop("_pending_thread_text", None)
                    context.user_data.pop("_pending_thread_id", None)
                if pending_text:
                    send_ok, send_msg = await _send_message_to_agent(
                        query.from_user.id,
                        pending_thread_id,
                        "",
                        pending_text,
                    )
                    if not send_ok:
                        logger.warning(
                            "Failed to forward pending text to remote target: %s",
                            send_msg,
                        )
                        await safe_send(
                            context.bot,
                            resolved_chat,
                            f"❌ Failed to send pending message: {send_msg}",
                            message_thread_id=pending_thread_id,
                        )
            else:
                await safe_edit(query, f"✅ {message}")
            if answer_callback:
                try:
                    await query.answer("Created")
                except Exception:
                    logger.debug("Callback query answer skipped: query expired")
            return

        if not created_wid:
            await safe_edit(query, "❌ Agent backend did not return a local window id")
            if pending_thread_id is not None and context.user_data is not None:
                context.user_data.pop("_pending_thread_id", None)
                context.user_data.pop("_pending_thread_text", None)
            if answer_callback:
                try:
                    await query.answer("Failed")
                except Exception:
                    logger.debug("Callback query answer skipped: query expired")
            return

        session_manager.prepare_window_launch(
            created_wid,
            cwd=str(selected_path),
            window_name=created_wname,
            account_name=launch_account or "",
            agent_type=profile.agent_type,
            model=profile.model,
            reasoning_effort=profile.reasoning_effort,
            fast_mode=profile.fast_mode,
        )
        if resume_session_id:
            # A resumed Codex window continues writing to the original JSONL.
            # Persist that expected session immediately so the transcript
            # monitor cannot auto-bind an older same-cwd transcript while the
            # TUI is still restoring.
            session_manager.register_session_to_window(
                created_wid,
                resume_session_id,
                str(selected_path),
                window_name=created_wname,
                persist_session_map=True,
            )
        logger.info(
            "Window created: %s (id=%s) at %s (user=%d, thread=%s, resume=%s, account=%s)",
            created_wname,
            created_wid,
            selected_path,
            user.id,
            pending_thread_id,
            resume_session_id,
            launch_account,
        )
        # Current Codex CLIs run SessionStart hooks when the first turn starts,
        # not when an empty TUI first appears.  Fresh sessions therefore cannot
        # wait for session_map before forwarding the initial prompt.
        hook_ok = False
        if resume_session_id:
            hook_ok = await session_manager.wait_for_session_map_entry(
                created_wid, timeout=15.0
            )

        # --resume creates a new session_id in the hook, but messages continue
        # writing to the resumed session's JSONL file. Override window_state to
        # track the original session_id so the monitor can route messages back.
        if resume_session_id:
            ws = session_manager.get_window_state(created_wid)
            if not hook_ok:
                # Hook timed out — manually populate window_state so the
                # monitor can still route messages back to this topic.
                logger.warning(
                    "Hook timed out for resume window %s, "
                    "manually setting session_id=%s cwd=%s",
                    created_wid,
                    resume_session_id,
                    selected_path,
                )
                ws.session_id = resume_session_id
                ws.cwd = str(selected_path)
                ws.window_name = created_wname
                session_manager._save_state()
            elif ws.session_id != resume_session_id:
                logger.info(
                    "Resume override: window %s session_id %s -> %s",
                    created_wid,
                    ws.session_id,
                    resume_session_id,
                )
                ws.session_id = resume_session_id
                session_manager._save_state()

        if pending_thread_id is not None:
            # Thread bind flow: bind thread to newly created window
            if created_target:
                session_manager.bind_thread_target(
                    user.id,
                    pending_thread_id,
                    created_target,
                    window_name=created_wname,
                )
            else:
                session_manager.bind_thread(
                    user.id,
                    pending_thread_id,
                    created_wid,
                    window_name=created_wname,
                )

            resolved_chat = session_manager.resolve_chat_id(user.id, pending_thread_id)

            status = "Resumed" if resume_session_id else "Created"
            await safe_edit(
                query,
                f"✅ {message}\n\n{status}. Send messages here.",
            )

            await _enable_fast_mode_if_requested(
                context.bot,
                query.from_user.id,
                pending_thread_id,
                created_wid,
                profile,
                resolved_chat,
            )

            # Send pending text if any
            pending_text = (
                context.user_data.get("_pending_thread_text")
                if context.user_data
                else None
            )
            if pending_text:
                pending_text = sanitize_forward_text(pending_text)
            if pending_text:
                logger.debug(
                    "Forwarding pending text to window %s (len=%d)",
                    created_wname,
                    len(pending_text),
                )
                if context.user_data is not None:
                    context.user_data.pop("_pending_thread_text", None)
                    context.user_data.pop("_pending_thread_id", None)
                send_ok, send_msg = await _send_to_window_when_codex_ready(
                    query.from_user.id,
                    pending_thread_id,
                    created_wid,
                    pending_text,
                    auto_confirm_startup_trust=True,
                )
                if send_ok:
                    await mark_window_working(
                        context.bot,
                        query.from_user.id,
                        created_wid,
                        pending_thread_id,
                    )
                    confirmed = await _refresh_session_map_after_first_prompt(
                        created_wid,
                        text=pending_text,
                        confirm_existing_session=True,
                    )
                    if not confirmed:
                        await safe_send(
                            context.bot,
                            resolved_chat,
                            "⚠️ I sent the first message, but the agent did not "
                            "confirm it reached the transcript after a submit "
                            "retry. I will show any pending agent prompt below; "
                            "if the topic stays idle, send the message again.",
                            message_thread_id=pending_thread_id,
                        )
                        await handle_interactive_ui(
                            context.bot,
                            query.from_user.id,
                            created_wid,
                            pending_thread_id,
                        )
                if not send_ok:
                    logger.warning("Failed to forward pending text: %s", send_msg)
                    await safe_send(
                        context.bot,
                        resolved_chat,
                        f"❌ Failed to send pending message: {send_msg}",
                        message_thread_id=pending_thread_id,
                    )
            elif context.user_data is not None:
                context.user_data.pop("_pending_thread_id", None)
        else:
            # Should not happen in topic-only mode, but handle gracefully
            await safe_edit(query, f"✅ {message}")
    else:
        await safe_edit(query, f"❌ {message}")
        if pending_thread_id is not None and context.user_data is not None:
            context.user_data.pop("_pending_thread_id", None)
            context.user_data.pop("_pending_thread_text", None)
    if answer_callback:
        try:
            await query.answer("Created" if success else "Failed")
        except Exception:
            logger.debug("Callback query answer skipped: query expired")


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
        await query.answer("Dismissed")
        await safe_edit(query, "Codex CLI update dismissed.")
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
            context.user_data.pop("_pending_thread_id", None)
            context.user_data.pop("_pending_thread_text", None)
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
            if agent_type == AGENT_CLAUDE
            else config.codex_reasoning_effort
        )
        if context.user_data is not None:
            context.user_data[PROFILE_AGENT_KEY] = agent_type
            context.user_data[PROFILE_MODEL_KEY] = model
            context.user_data[PROFILE_FAST_MODE_KEY] = False
            context.user_data[PROFILE_EFFORT_KEY] = _resolve_profile_effort(
                agent_type,
                model,
                configured_effort,
            )
        await _show_agent_profile_settings(query, context)
        await query.answer()

    elif data.startswith(CB_PROFILE_MODEL):
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
                if agent_type == AGENT_CLAUDE
                else config.codex_reasoning_effort,
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

        default_path = str(Path.cwd())
        current_path = (
            context.user_data.get(BROWSE_PATH_KEY, default_path)
            if context.user_data
            else default_path
        )
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
        default_path = str(Path.cwd())
        current_path = (
            context.user_data.get(BROWSE_PATH_KEY, default_path)
            if context.user_data
            else default_path
        )
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
        default_path = str(Path.cwd())
        current_path = (
            context.user_data.get(BROWSE_PATH_KEY, default_path)
            if context.user_data
            else default_path
        )
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
        default_path = str(Path.cwd())
        selected_path = (
            context.user_data.get(BROWSE_PATH_KEY, default_path)
            if context.user_data
            else default_path
        )
        backend_id, node_id = _browse_backend_context(context.user_data)
        if not backend_id:
            selected_path = _clamp_to_selected_root(selected_path, context.user_data)
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
                context.user_data.pop("_pending_thread_id", None)
                context.user_data.pop("_pending_thread_text", None)
            await query.answer("Stale browser (topic mismatch)", show_alert=True)
            return

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
            context.user_data.pop("_pending_thread_id", None)
            context.user_data.pop("_pending_thread_text", None)
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

        selected_path = (
            context.user_data.get("_selected_path", str(Path.cwd()))
            if context.user_data
            else str(Path.cwd())
        )
        selected_node_id = (
            context.user_data.get("_selected_node_id", "") if context.user_data else ""
        )
        profile = _profile_from_context(context.user_data)
        clear_session_picker_state(context.user_data)
        if context.user_data is not None:
            context.user_data.pop("_selected_path", None)
            context.user_data.pop("_selected_backend_id", None)
            context.user_data.pop("_selected_node_id", None)

        create_kwargs: dict[str, Any] = {
            "resume_session_id": session.session_id,
            "agent_type": profile.agent_type,
            "model": profile.model,
            "reasoning_effort": profile.reasoning_effort,
            "fast_mode": profile.fast_mode,
        }
        if selected_node_id:
            create_kwargs["node_id"] = selected_node_id
        await _create_and_bind_window(
            query,
            context,
            user,
            selected_path,
            pending_tid,
            **create_kwargs,
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
        selected_path = (
            context.user_data.get("_selected_path", str(Path.cwd()))
            if context.user_data
            else str(Path.cwd())
        )
        selected_node_id = (
            context.user_data.get("_selected_node_id", "") if context.user_data else ""
        )
        profile = _profile_from_context(context.user_data)
        clear_session_picker_state(context.user_data)
        if context.user_data is not None:
            context.user_data.pop("_selected_path", None)
            context.user_data.pop("_selected_backend_id", None)
            context.user_data.pop("_selected_node_id", None)

        create_kwargs: dict[str, Any] = {
            "agent_type": profile.agent_type,
            "model": profile.model,
            "reasoning_effort": profile.reasoning_effort,
            "fast_mode": profile.fast_mode,
        }
        if selected_node_id:
            create_kwargs["node_id"] = selected_node_id
        await _create_and_bind_window(
            query,
            context,
            user,
            selected_path,
            pending_tid,
            **create_kwargs,
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
            context.user_data.pop("_pending_thread_id", None)
            context.user_data.pop("_pending_thread_text", None)
            context.user_data.pop("_selected_path", None)
            context.user_data.pop("_selected_backend_id", None)
            context.user_data.pop("_selected_node_id", None)
            context.user_data.pop(PROFILE_AGENT_KEY, None)
            context.user_data.pop(PROFILE_MODEL_KEY, None)
            context.user_data.pop(PROFILE_EFFORT_KEY, None)
            context.user_data.pop(PROFILE_FAST_MODE_KEY, None)
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

        # Forward pending text if any
        pending_text = (
            context.user_data.get("_pending_thread_text") if context.user_data else None
        )
        if pending_text:
            pending_text = sanitize_forward_text(pending_text)
        if context.user_data is not None:
            context.user_data.pop("_pending_thread_text", None)
            context.user_data.pop("_pending_thread_id", None)
        if pending_text:
            send_ok, send_msg = await _send_message_to_agent(
                user.id,
                thread_id,
                selected_wid,
                pending_text,
            )
            if send_ok:
                await mark_window_working(
                    context.bot,
                    user.id,
                    selected_wid,
                    thread_id,
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
            context.user_data.pop("_pending_thread_id", None)
            context.user_data.pop("_pending_thread_text", None)
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
        session_manager.update_user_window_offset(user_id, window_id, msg.source_offset)
        return

    session = await session_manager.resolve_session_for_window(window_id)
    if session and session.file_path:
        try:
            file_size = Path(session.file_path).stat().st_size
            session_manager.update_user_window_offset(user_id, window_id, file_size)
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
    user_offsets = user_window_offsets.get(user_id, {})
    if not isinstance(user_offsets, dict):
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

        parts = build_response_parts(
            msg.text,
            msg.is_complete,
            msg.content_type,
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
                content_type=msg.content_type,
                role=msg.role,
                text=msg.text,
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
    global _runtime_stopped

    _runtime_stopped = False

    await refresh_model_catalog()

    await application.bot.delete_my_commands()

    bot_commands = [
        BotCommand("start", "Show welcome message"),
        BotCommand("history", "Message history for this topic"),
        BotCommand("mode", "Choose clean or trace output"),
        BotCommand("screenshot", "Terminal screenshot with control keys"),
        BotCommand("esc", ESC_COMMAND_DESCRIPTION),
        BotCommand("interrupt", INTERRUPT_COMMAND_DESCRIPTION),
        BotCommand("kill", "Kill session and delete topic"),
        BotCommand("unbind", "Unbind topic from session (keeps window running)"),
        BotCommand("usage", USAGE_COMMAND_DESCRIPTION),
        BotCommand("agentlogin", AGENT_LOGIN_COMMAND_DESCRIPTION),
        BotCommand("agentaccount", ACCOUNT_COMMAND_DESCRIPTION),
        BotCommand("codexlogin", "Login to Codex"),
        BotCommand("codexaccount", "Manage Codex accounts"),
        BotCommand("claudelogin", "Login to Claude Code"),
        BotCommand("claudeaccount", "Manage Claude Code accounts"),
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

    # Start status polling task
    _status_poll_task = asyncio.create_task(status_poll_loop(application.bot))
    logger.info("Status polling task started")

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

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("history", history_command))
    application.add_handler(CommandHandler("mode", output_mode_command))
    application.add_handler(CommandHandler("screenshot", screenshot_command))
    application.add_handler(CommandHandler("esc", esc_command))
    application.add_handler(CommandHandler("interrupt", interrupt_command))
    application.add_handler(CommandHandler("kill", topic_closed_handler))
    application.add_handler(CommandHandler("unbind", unbind_command))
    application.add_handler(CommandHandler("usage", usage_command))
    application.add_handler(CommandHandler("agentlogin", agent_login_command))
    application.add_handler(CommandHandler("agentaccount", agent_account_command))
    application.add_handler(CommandHandler("codexlogin", codex_login_command))
    application.add_handler(CommandHandler("codexaccount", codex_account_command))
    application.add_handler(CommandHandler("claudelogin", claude_login_command))
    application.add_handler(CommandHandler("claudeaccount", claude_account_command))
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
