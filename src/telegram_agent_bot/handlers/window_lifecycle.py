"""Topic window creation, binding, recovery, and launch specification helpers."""

from __future__ import annotations

import asyncio
import copy
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypedDict, cast

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from ..account_manager import (
    get_default_account_name,
    get_next_account_name,
    remember_current_account,
)
from ..agent_io import capture_agent_output, create_agent_session, upload_agent_file
from ..agent_profile import (
    AGENT_CLAUDE,
    AGENT_CLAUDE_OFFICIAL,
    AGENT_CODEX,
    AGENT_CURSOR,
    AgentProfile,
    normalize_agent_type,
)
from ..backends.base import AgentTarget
from ..browser_routing import (
    active_remote_root_browser,
    browser_for_backend_id,
    default_directory_browser_path,
)
from ..config import config
from ..idle_sessions import idle_session_hibernator
from ..profile_helpers import (
    default_profile_permission,
    profile_effort_values,
    profile_from_context,
    profile_models,
    resolve_profile_effort,
)
from ..session import CodexSession, is_shell_pane_command, session_manager
from ..terminal_parser import (
    extract_auth_error_message,
    extract_interactive_content,
    is_codex_input_ready,
    parse_status_update,
)
from ..tmux_manager import tmux_manager
from ..turn_admission import turn_admission
from ..utils import sanitize_forward_text
from .agent_login import codex_auth_recovery_message
from .callback_data import CB_SESSION_CANCEL, CB_SESSION_NEW
from .directory_browser import (
    BROWSE_DIRS_KEY,
    BROWSE_PAGE_KEY,
    BROWSE_PATH_KEY,
    ROOTS_KEY,
    SESSIONS_KEY,
    STATE_BROWSING_DIRECTORY,
    STATE_KEY,
    STATE_SELECTING_AGENT,
    STATE_SELECTING_PROFILE,
    STATE_SELECTING_ROOT,
    STATE_SELECTING_SESSION,
    STATE_SELECTING_WINDOW,
    UNBOUND_WINDOWS_KEY,
    PROFILE_MODELS_KEY,
    build_agent_picker,
    build_backend_root_picker,
    build_directory_browser,
    build_profile_picker,
    build_project_root_picker,
    build_session_picker,
    build_window_picker,
    clear_browse_state,
    clear_profile_picker_state,
    clear_session_picker_state,
)
from .message_sender import safe_edit, safe_reply, safe_send
from .native_input import (
    BOT_WRAPPER_MARKER,
    agent_max_active_turns,
    discard_queued_agent_input,
    maybe_confirm_startup_trust_prompt,
    reset_transient_agent_retry,
    send_message_to_agent,
    send_or_queue_agent_input,
)
from .status_polling import forget_missing_bound_window, mark_window_working

logger = logging.getLogger(__name__)

PENDING_THREAD_MEDIA_KEY = "_pending_thread_media"
PRODUCT_NAME = "Agent"


class PendingThreadMedia(TypedDict):
    """Attachment retained while a new topic is choosing its first session."""

    kind: str
    path: str
    filename: str
    caption: str


@dataclass(frozen=True)
class AgentLaunchSpec:
    """Immutable specification for launching an agent session or local window."""

    cwd: str
    node_id: str = ""
    window_name: str = ""
    resume_session_id: str = ""
    account_name: str = ""
    agent_type: str = ""
    model: str = ""
    reasoning_effort: str = ""
    fast_mode: bool = False
    permission_mode: str = "ask"

    def to_create_kwargs(self) -> dict[str, Any]:
        """Convert launch fields to create_agent_session keyword arguments."""
        create_kwargs: dict[str, Any] = {
            "cwd": self.cwd,
            "window_name": self.window_name,
            "resume_session_id": self.resume_session_id,
            "account_name": self.account_name,
        }
        if self.agent_type:
            create_kwargs["agent_type"] = self.agent_type
        if self.model:
            create_kwargs["model"] = self.model
        if self.reasoning_effort:
            create_kwargs["reasoning_effort"] = self.reasoning_effort
        if self.fast_mode:
            create_kwargs["fast_mode"] = True
        create_kwargs["permission_mode"] = self.permission_mode
        if self.node_id:
            create_kwargs["node_id"] = self.node_id
        return create_kwargs


@dataclass(frozen=True)
class WindowLaunchSpec:
    """Immutable specification for creating and binding a topic session."""

    selected_path: str
    pending_thread_id: int | None
    resume_session_id: str | None = None
    account_name: str | None = None
    agent_type: str = ""
    model: str = ""
    reasoning_effort: str = ""
    fast_mode: bool = False
    permission_mode: str = "ask"
    node_id: str = ""
    answer_callback: bool = True

    def resolve_profile(self, *, cfg: Any) -> AgentProfile:
        """Normalize agent type and apply configured default reasoning effort."""
        normalized_agent = normalize_agent_type(self.agent_type or cfg.agent_type)
        return AgentProfile(
            agent_type=normalized_agent,
            model=self.model,
            reasoning_effort=self.reasoning_effort
            or (
                cfg.claude_reasoning_effort
                if normalized_agent in {AGENT_CLAUDE, AGENT_CLAUDE_OFFICIAL}
                else cfg.codex_reasoning_effort
                if normalized_agent == AGENT_CODEX
                else ""
            ),
            fast_mode=self.fast_mode,
            permission_mode=self.permission_mode,
        )

    def to_agent_launch_spec(
        self,
        profile: AgentProfile,
        launch_account: str | None,
    ) -> AgentLaunchSpec:
        """Build the lower-level AgentLaunchSpec for target creation."""
        return AgentLaunchSpec(
            cwd=self.selected_path,
            node_id=self.node_id,
            resume_session_id=self.resume_session_id or "",
            account_name=launch_account or "",
            agent_type=profile.agent_type,
            model=profile.model,
            reasoning_effort=profile.reasoning_effort,
            fast_mode=profile.fast_mode,
            permission_mode=profile.permission_mode,
        )


def _dep(runtime: Any, name: str, default: Any) -> Any:
    return getattr(runtime, name, default) if runtime is not None else default


def _rt_fn(
    runtime: Any,
    attr_name: str,
    default_fn: Any,
) -> tuple[Any, bool]:
    """Return an overridden runtime callable when patched, or the default."""
    if runtime is not None:
        candidate = getattr(runtime, attr_name, None)
        if (
            candidate is not None
            and getattr(candidate, "_bot_wrapper_marker", None)
            is not BOT_WRAPPER_MARKER
        ):
            return candidate, True
    return default_fn, False


def _build_local_directory_browser(
    path: str,
    page: int = 0,
    *,
    runtime: Any = None,
    **kwargs: Any,
) -> tuple[str, InlineKeyboardMarkup, list[str]]:
    builder = _dep(runtime, "build_directory_browser", build_directory_browser)
    return builder(path, page, **kwargs)


async def show_root_or_directory_picker(
    target: Any,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    edit: bool = False,
    runtime: Any = None,
) -> None:
    """Show the configured root picker or fall through to directory browsing."""
    cfg = _dep(runtime, "config", config)
    backend = _dep(runtime, "agent_backend", None)
    reply_fn = _dep(runtime, "safe_reply", safe_reply)
    edit_fn = _dep(runtime, "safe_edit", safe_edit)
    remote_root_fn, _ = _rt_fn(
        runtime,
        "_active_remote_root_browser",
        lambda: active_remote_root_browser(backend),
    )
    default_dir_fn, _ = _rt_fn(
        runtime,
        "_default_directory_browser_path",
        lambda root_path=None: default_directory_browser_path(
            root_path, default_projects_path=cfg.default_projects_path
        ),
    )

    browser = remote_root_fn()
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
                await edit_fn(target, msg_text, reply_markup=keyboard)
            else:
                await reply_fn(target, msg_text, reply_markup=keyboard)
            return

    if getattr(cfg, "project_roots_configured", False):
        msg_text, keyboard, roots = build_project_root_picker(cfg.project_roots)
        if context.user_data is not None:
            context.user_data[STATE_KEY] = STATE_SELECTING_ROOT
            context.user_data[ROOTS_KEY] = roots
        if edit:
            await edit_fn(target, msg_text, reply_markup=keyboard)
        else:
            await reply_fn(target, msg_text, reply_markup=keyboard)
        return

    start_path = default_dir_fn()
    msg_text, keyboard, subdirs = _build_local_directory_browser(
        start_path, runtime=runtime
    )
    if context.user_data is not None:
        context.user_data[STATE_KEY] = STATE_BROWSING_DIRECTORY
        context.user_data[BROWSE_PATH_KEY] = start_path
        context.user_data[BROWSE_PAGE_KEY] = 0
        context.user_data[BROWSE_DIRS_KEY] = subdirs
    if edit:
        await edit_fn(target, msg_text, reply_markup=keyboard)
    else:
        await reply_fn(target, msg_text, reply_markup=keyboard)


def clear_pending_thread_input(user_data: dict | None) -> None:
    """Forget the first message retained while a topic is being configured."""
    if user_data is not None:
        user_data.pop("_pending_thread_id", None)
        user_data.pop("_pending_thread_text", None)
        user_data.pop(PENDING_THREAD_MEDIA_KEY, None)


async def start_unbound_topic_creation(
    message: Any,
    context: ContextTypes.DEFAULT_TYPE,
    user: Any,
    thread_id: int,
    *,
    text: str | None = None,
    runtime: Any = None,
) -> None:
    """Open the normal session picker and retain the topic's first input."""
    cfg = _dep(runtime, "config", config)
    backend = _dep(runtime, "agent_backend", None)
    sm = _dep(runtime, "session_manager", session_manager)
    tmux = _dep(runtime, "tmux_manager", tmux_manager)
    reply_fn = _dep(runtime, "safe_reply", safe_reply)

    remote_root_fn, _ = _rt_fn(
        runtime,
        "_active_remote_root_browser",
        lambda: active_remote_root_browser(backend),
    )
    picker_fn, picker_patched = _rt_fn(
        runtime, "_show_root_or_directory_picker", show_root_or_directory_picker
    )

    if context.user_data is not None:
        context.user_data["_pending_thread_id"] = thread_id
        if text is not None:
            context.user_data["_pending_thread_text"] = text
            context.user_data.pop(PENDING_THREAD_MEDIA_KEY, None)

    if getattr(cfg, "project_roots_configured", False) or (
        remote_root_fn() is not None
    ):
        logger.info(
            "Unbound topic: showing configured project root picker (user=%d, thread=%d)",
            user.id,
            thread_id,
        )
        if picker_patched:
            await picker_fn(message, context)
        else:
            await picker_fn(message, context, runtime=runtime)
        return

    all_windows = await tmux.list_windows()
    bound_ids = {wid for _, _, wid in sm.iter_thread_bindings()}
    bindable_unbound = [
        (w.window_id, w.window_name, w.cwd)
        for w in all_windows
        if w.window_id not in bound_ids
        and has_trackable_session_for_window(w.window_id, runtime=runtime)
    ]
    logger.debug(
        "Window picker check: all=%s, bound=%s, bindable_unbound=%s",
        [w.window_name for w in all_windows],
        bound_ids,
        [name for _, name, _ in bindable_unbound],
    )

    if bindable_unbound:
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
        await reply_fn(message, msg_text, reply_markup=keyboard)
        return

    logger.info(
        "Unbound topic: showing project root or directory picker (user=%d, thread=%d)",
        user.id,
        thread_id,
    )
    if picker_patched:
        await picker_fn(message, context)
    else:
        await picker_fn(message, context, runtime=runtime)


def store_pending_thread_media(
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
    context.user_data[PENDING_THREAD_MEDIA_KEY] = {
        "kind": kind,
        "path": str(path),
        "filename": filename,
        "caption": caption,
    }


async def take_pending_thread_message(
    context: ContextTypes.DEFAULT_TYPE,
    *,
    user_id: int,
    thread_id: int,
    remote: bool,
    runtime: Any = None,
) -> tuple[str | None, str | None]:
    """Return the retained first input, uploading an attachment when needed."""
    if context.user_data is None:
        return None, None

    pending_text = context.user_data.pop("_pending_thread_text", None)
    pending_media = context.user_data.pop(PENDING_THREAD_MEDIA_KEY, None)
    if pending_text:
        return sanitize_forward_text(pending_text), None
    if not isinstance(pending_media, dict):
        return None, None

    pending_media = cast(PendingThreadMedia, pending_media)
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
        upload_fn = _dep(runtime, "upload_agent_file", upload_agent_file)
        upload = await upload_fn(
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


def filter_resumable_sessions(
    sessions: list[CodexSession], *, runtime: Any = None
) -> list[CodexSession]:
    """Hide sessions that are already active in another Telegram topic."""
    sm = _dep(runtime, "session_manager", session_manager)
    return [
        session
        for session in sessions
        if not sm.has_bound_thread_for_session(session.session_id)
    ]


def has_trackable_session_for_window(window_id: str, *, runtime: Any = None) -> bool:
    """Return whether an existing tmux window already has a known Codex session."""
    sm = _dep(runtime, "session_manager", session_manager)
    state = sm.window_states.get(window_id)
    return bool(state and state.session_id)


def build_resume_conflict_keyboard() -> InlineKeyboardMarkup:
    """Offer safe next steps after blocking a duplicate resume."""
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("➕ New Session", callback_data=CB_SESSION_NEW),
                InlineKeyboardButton("Cancel", callback_data=CB_SESSION_CANCEL),
            ]
        ]
    )


async def show_agent_profile_picker(
    target: Any,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    edit: bool = True,
    runtime: Any = None,
) -> None:
    """Show the agent selector before a new topic creates a session."""
    edit_fn = _dep(runtime, "safe_edit", safe_edit)
    reply_fn = _dep(runtime, "safe_reply", safe_reply)
    text, keyboard = build_agent_picker()
    if context.user_data is not None:
        context.user_data[STATE_KEY] = STATE_SELECTING_AGENT
    if edit:
        await edit_fn(target, text, reply_markup=keyboard)
    else:
        await reply_fn(target, text, reply_markup=keyboard)


async def show_agent_profile_settings(
    target: Any,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    edit: bool = True,
    runtime: Any = None,
) -> None:
    """Show model and reasoning controls for the selected agent."""
    cfg = _dep(runtime, "config", config)
    edit_fn = _dep(runtime, "safe_edit", safe_edit)
    reply_fn = _dep(runtime, "safe_reply", safe_reply)

    models_fn = _dep(
        runtime,
        "_profile_models",
        lambda agent_type: profile_models(agent_type, cfg=cfg),
    )
    efforts_fn = _dep(
        runtime,
        "_profile_effort_values",
        lambda agent_type, model: profile_effort_values(agent_type, model, cfg=cfg),
    )
    resolve_effort_fn = _dep(
        runtime,
        "_resolve_profile_effort",
        lambda agent_type, model, requested: resolve_profile_effort(
            agent_type, model, requested, cfg=cfg, effort_values=efforts_fn
        ),
    )
    default_perm_fn = _dep(
        runtime,
        "_default_profile_permission",
        lambda agent_type: default_profile_permission(agent_type, cfg=cfg),
    )
    from_ctx_fn = _dep(
        runtime,
        "_profile_from_context",
        lambda user_data: profile_from_context(
            user_data,
            cfg=cfg,
            resolve_effort=resolve_effort_fn,
            default_permission_fn=default_perm_fn,
        ),
    )

    profile = from_ctx_fn(context.user_data)
    models = models_fn(profile.agent_type)
    if context.user_data is not None:
        context.user_data[STATE_KEY] = STATE_SELECTING_PROFILE
        context.user_data[PROFILE_MODELS_KEY] = list(models)
    text, keyboard = build_profile_picker(
        profile,
        models,
        effort_values=efforts_fn(profile.agent_type, profile.model),
    )
    if edit:
        await edit_fn(target, text, reply_markup=keyboard)
    else:
        await reply_fn(target, text, reply_markup=keyboard)


def clear_creation_state(user_data: dict | None) -> None:
    """Clear all transient topic-creation state."""
    clear_browse_state(user_data)
    clear_session_picker_state(user_data)
    clear_profile_picker_state(user_data)
    if user_data is not None:
        clear_pending_thread_input(user_data)
        for key in (
            "_selected_path",
            "_selected_backend_id",
            "_selected_node_id",
        ):
            user_data.pop(key, None)


def creation_path(user_data: dict | None, key: str) -> str | None:
    """Return an explicit project selection, never the service's cwd."""
    path = user_data.get(key) if user_data else None
    return path if isinstance(path, str) and path.strip() else None


async def reject_expired_project_selection(
    query: Any, context: ContextTypes.DEFAULT_TYPE, *, runtime: Any = None
) -> None:
    edit_fn = _dep(runtime, "safe_edit", safe_edit)
    clear_creation_state(context.user_data)
    await query.answer("Project selection expired", show_alert=True)
    await edit_fn(
        query,
        "⚠️ Project selection expired. Send your message again to choose a project.",
    )


async def continue_creation_with_profile(
    query: Any,
    context: ContextTypes.DEFAULT_TYPE,
    user: Any,
    *,
    runtime: Any = None,
) -> None:
    """Find a resumable session or create a new window for the chosen profile."""
    cfg = _dep(runtime, "config", config)
    backend = _dep(runtime, "agent_backend", None)
    sm = _dep(runtime, "session_manager", session_manager)
    edit_fn = _dep(runtime, "safe_edit", safe_edit)
    browser_fn = _dep(
        runtime,
        "_browser_for_backend_id",
        lambda backend_id: browser_for_backend_id(backend_id, backend),
    )

    efforts_fn = _dep(
        runtime,
        "_profile_effort_values",
        lambda agent_type, model: profile_effort_values(agent_type, model, cfg=cfg),
    )
    resolve_effort_fn = _dep(
        runtime,
        "_resolve_profile_effort",
        lambda agent_type, model, requested: resolve_profile_effort(
            agent_type, model, requested, cfg=cfg, effort_values=efforts_fn
        ),
    )
    default_perm_fn = _dep(
        runtime,
        "_default_profile_permission",
        lambda agent_type: default_profile_permission(agent_type, cfg=cfg),
    )
    from_ctx_fn = _dep(
        runtime,
        "_profile_from_context",
        lambda user_data: profile_from_context(
            user_data,
            cfg=cfg,
            resolve_effort=resolve_effort_fn,
            default_permission_fn=default_perm_fn,
        ),
    )

    user_data = context.user_data
    selected_path = creation_path(user_data, "_selected_path")
    if selected_path is None:
        await reject_expired_project_selection(query, context, runtime=runtime)
        return
    backend_id = user_data.get("_selected_backend_id", "") if user_data else ""
    node_id = user_data.get("_selected_node_id", "") if user_data else ""
    pending_thread_id = user_data.get("_pending_thread_id") if user_data else None

    profile = from_ctx_fn(user_data)
    await query.answer("Looking for sessions...")
    await edit_fn(query, "⏳ Looking for existing sessions in this directory...")

    if backend_id:
        browser = browser_fn(backend_id)
        if browser is None:
            await edit_fn(query, "Backend browser unavailable. Please retry.")
            return
        try:
            sessions = filter_resumable_sessions(
                await browser.list_sessions(node_id, selected_path),
                runtime=runtime,
            )
        except Exception:
            logger.exception(
                "Backend session lookup failed: backend=%s node=%s path=%s",
                backend_id,
                node_id,
                selected_path,
            )
            await edit_fn(query, "Unable to read sessions from backend.")
            return
    else:
        sessions = filter_resumable_sessions(
            await sm.list_sessions_for_directory(selected_path),
            runtime=runtime,
        )

    if sessions:
        if user_data is not None:
            user_data[STATE_KEY] = STATE_SELECTING_SESSION
            user_data[SESSIONS_KEY] = sessions
        text, keyboard = build_session_picker(sessions)
        await edit_fn(query, text, reply_markup=keyboard)
        return

    create_fn, create_patched = _rt_fn(
        runtime, "_create_and_bind_window", create_and_bind_window
    )
    if create_patched:
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
        await create_fn(
            query,
            context,
            user,
            selected_path,
            pending_thread_id,
            **create_kwargs,
        )
        return

    spec = WindowLaunchSpec(
        selected_path=selected_path,
        pending_thread_id=pending_thread_id,
        agent_type=profile.agent_type,
        model=profile.model,
        reasoning_effort=profile.reasoning_effort,
        fast_mode=profile.fast_mode,
        permission_mode=profile.permission_mode,
        node_id=node_id,
        answer_callback=False,
    )
    await create_and_bind_window(query, context, user, spec, runtime=runtime)


def remote_target_for_thread(
    user_id: int,
    thread_id: int | None,
    *,
    runtime: Any = None,
) -> AgentTarget | None:
    """Return a non-local target bound to this thread, if any."""
    sm = _dep(runtime, "session_manager", session_manager)
    target = sm.resolve_target_for_thread(user_id, thread_id)
    if isinstance(target, AgentTarget) and target.backend_id != "local":
        return target
    return None


def format_remote_target(target: AgentTarget) -> str:
    """Build a compact user-facing label for a remote target."""
    label = f"{target.backend_id}/{target.node_id}"
    if target.session_id:
        label = f"{label}:{target.session_id}"
    return label


async def create_agent_target(
    spec: AgentLaunchSpec,
    *,
    runtime: Any = None,
) -> tuple[bool, str, str, str, AgentTarget | None]:
    """Create a local or remote agent session from an AgentLaunchSpec."""
    create_fn = _dep(runtime, "create_agent_session", create_agent_session)
    result = await create_fn(**spec.to_create_kwargs())
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


async def create_agent_local_window(
    spec: AgentLaunchSpec,
    *,
    runtime: Any = None,
) -> tuple[bool, str, str, str, AgentTarget | None]:
    """Create a local tmux agent window from an AgentLaunchSpec."""
    target_fn, target_patched = _rt_fn(
        runtime, "_create_agent_target", create_agent_target
    )
    if target_patched:
        success, message, display_name, window_id, target = await target_fn(
            cwd=spec.cwd,
            window_name=spec.window_name,
            resume_session_id=spec.resume_session_id,
            account_name=spec.account_name,
            agent_type=spec.agent_type,
            model=spec.model,
            reasoning_effort=spec.reasoning_effort,
            fast_mode=spec.fast_mode,
            permission_mode=spec.permission_mode,
        )
    else:
        success, message, display_name, window_id, target = await target_fn(
            spec, runtime=runtime
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


async def _launch_local_window(
    spec: AgentLaunchSpec,
    *,
    runtime: Any = None,
    include_permission_mode: bool = True,
) -> tuple[bool, str, str, str, AgentTarget | None]:
    local_fn, local_patched = _rt_fn(
        runtime, "_create_agent_local_window", create_agent_local_window
    )
    if local_patched:
        kwargs: dict[str, Any] = {
            "cwd": spec.cwd,
            "window_name": spec.window_name,
            "resume_session_id": spec.resume_session_id,
            "account_name": spec.account_name,
            "agent_type": spec.agent_type,
            "model": spec.model,
            "reasoning_effort": spec.reasoning_effort,
            "fast_mode": spec.fast_mode,
        }
        if include_permission_mode:
            kwargs["permission_mode"] = spec.permission_mode
        return await local_fn(**kwargs)
    return await local_fn(spec, runtime=runtime)


async def rotate_thread_after_usage_limit(
    *,
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
    thread_id: int,
    current_window_id: str,
    current_window_cwd: str,
    text: str,
    runtime: Any = None,
) -> bool:
    """Rotate the topic onto a fresh account-backed window after quota exhaustion."""
    cfg = _dep(runtime, "config", config)
    sm = _dep(runtime, "session_manager", session_manager)
    send_fn = _dep(runtime, "safe_send", safe_send)
    next_account_fn = _dep(runtime, "get_next_account_name", get_next_account_name)
    remember_fn = _dep(runtime, "remember_current_account", remember_current_account)
    mark_working_fn = _dep(runtime, "mark_window_working", mark_window_working)

    if not cfg.enable_account_rotation:
        await send_fn(
            context.bot,
            sm.resolve_chat_id(user_id, thread_id),
            "⚠️ This session has hit its usage limit. Automatic account rotation "
            "is disabled. Use /agentlogin to refresh the current agent login, "
            "or /agentaccount to choose a saved account. Then /unbind if you "
            "want this topic to start a fresh session.",
            message_thread_id=thread_id,
        )
        return True

    current_state = sm.get_window_state(current_window_id)
    next_account = next_account_fn(current_state.account_name)
    if not next_account:
        await send_fn(
            context.bot,
            sm.resolve_chat_id(user_id, thread_id),
            "⚠️ This session has hit its usage limit, but no backup account is "
            "selected for rotation.\n"
            "Use /agentlogin <name> to add a saved account, then "
            "/agentaccount use <name> to select it.",
            message_thread_id=thread_id,
        )
        return True

    selected_path = current_window_cwd or current_state.cwd
    if not selected_path:
        await send_fn(
            context.bot,
            sm.resolve_chat_id(user_id, thread_id),
            "⚠️ This session has hit its usage limit, but the window working "
            "directory is unavailable, so I cannot open a replacement "
            "session automatically. Please `/unbind` and choose New Session.",
            message_thread_id=thread_id,
        )
        return True

    local_fn, local_patched = _rt_fn(
        runtime, "_create_agent_local_window", create_agent_local_window
    )
    if local_patched:
        (
            success,
            message,
            created_wname,
            created_wid,
            created_target,
        ) = await local_fn(cwd=selected_path, account_name=next_account)
    else:
        (
            success,
            message,
            created_wname,
            created_wid,
            created_target,
        ) = await local_fn(
            AgentLaunchSpec(cwd=selected_path, account_name=next_account),
            runtime=runtime,
        )
    if not success:
        await send_fn(
            context.bot,
            sm.resolve_chat_id(user_id, thread_id),
            f"⚠️ This session hit its usage limit, and automatic rotation to "
            f"`{next_account}` failed: {message}",
            message_thread_id=thread_id,
        )
        return True

    remember_fn(next_account)
    sm.prepare_window_launch(
        created_wid,
        cwd=str(selected_path),
        window_name=created_wname,
        account_name=next_account,
    )
    if created_target:
        sm.bind_thread_target(
            user_id,
            thread_id,
            created_target,
            window_name=created_wname,
        )
    else:
        sm.bind_thread(
            user_id,
            thread_id,
            created_wid,
            window_name=created_wname,
        )

    resolved_chat = sm.resolve_chat_id(user_id, thread_id)
    starting_fn, starting_patched = _rt_fn(
        runtime, "_send_or_queue_to_starting_window", send_or_queue_to_starting_window
    )
    if starting_patched:
        send_ok, send_msg, queued = await starting_fn(
            context.bot,
            user_id,
            thread_id,
            created_wid,
            text,
            auto_confirm_startup_trust=True,
        )
    else:
        send_ok, send_msg, queued = await starting_fn(
            context.bot,
            user_id,
            thread_id,
            created_wid,
            text,
            auto_confirm_startup_trust=True,
            runtime=runtime,
        )
    if send_ok and not queued:
        await mark_working_fn(context.bot, user_id, created_wid, thread_id)
    if send_ok:
        delivery = (
            "queued your message there" if queued else "forwarded your message there"
        )
        await send_fn(
            context.bot,
            resolved_chat,
            f"♻️ This session hit its usage limit, so I switched to a new "
            f"`{next_account}` session and {delivery}.",
            message_thread_id=thread_id,
        )
    else:
        await send_fn(
            context.bot,
            resolved_chat,
            "♻️ This session hit its usage limit, and I switched to a new "
            f"session automatically, but forwarding failed: {send_msg}\n"
            "Please send the message again.",
            message_thread_id=thread_id,
        )
    return True


async def send_to_window_when_codex_ready(
    user_id: int,
    thread_id: int | None,
    window_id: str,
    text: str,
    *,
    timeout: float | None = None,
    interval: float = 0.5,
    auto_confirm_startup_trust: bool = False,
    return_on_capacity_full: bool = False,
    runtime: Any = None,
) -> tuple[bool, str]:
    """Send text once the new Codex TUI is ready to accept input."""
    cfg = _dep(runtime, "config", config)
    asyncio_mod = _dep(runtime, "asyncio", asyncio)
    capture_fn = _dep(runtime, "capture_agent_output", capture_agent_output)
    admission = _dep(runtime, "turn_admission", turn_admission)
    hibernator = _dep(runtime, "idle_session_hibernator", idle_session_hibernator)
    send_msg_fn, send_msg_patched = _rt_fn(
        runtime, "_send_message_to_agent", send_message_to_agent
    )

    startup_timeout = (
        timeout
        if timeout is not None
        else getattr(cfg, "agent_startup_timeout_seconds", 180.0)
    )
    deadline = asyncio_mod.get_event_loop().time() + startup_timeout
    last_message = ""
    while asyncio_mod.get_event_loop().time() < deadline:
        capture = await capture_fn(user_id, thread_id, window_id)
        if capture is None:
            return False, "No session bound"
        if capture.missing:
            return False, "Window not found (may have been closed)"
        pane_text = capture.text
        auth_error = extract_auth_error_message(pane_text or "")
        if auth_error:
            return False, codex_auth_recovery_message(auth_error)
        interactive = extract_interactive_content(pane_text or "")
        if interactive is not None:
            if auto_confirm_startup_trust:
                confirmed, trust_message = await maybe_confirm_startup_trust_prompt(
                    window_id,
                    pane_text or "",
                    runtime=runtime,
                )
                last_message = trust_message
                if confirmed:
                    await asyncio_mod.sleep(interval)
                    continue
                if trust_message.startswith("Failed to confirm"):
                    return False, trust_message
            last_message = f"Agent is waiting for interactive input: {interactive.name}"
            await asyncio_mod.sleep(interval)
            continue
        if not is_codex_input_ready(pane_text or ""):
            status = parse_status_update(pane_text or "")
            if status:
                last_message = f"Agent is still busy: {status}"
            else:
                last_message = "Agent UI is not ready for input"
            await asyncio_mod.sleep(interval)
            continue
        admission.observe(window_id, active=False)
        admitted = await admission.try_acquire(
            window_id,
            limit=agent_max_active_turns(runtime=runtime),
        )
        if not admitted:
            last_message = (
                "Server is at its active-task limit; waiting for an available slot"
            )
            if return_on_capacity_full:
                return False, last_message
            await asyncio_mod.sleep(interval)
            continue
        try:
            if send_msg_patched:
                send_ok, send_msg = await send_msg_fn(
                    user_id, thread_id, window_id, text
                )
            else:
                send_ok, send_msg = await send_msg_fn(
                    user_id, thread_id, window_id, text, runtime=runtime
                )
        except BaseException:
            admission.release(window_id)
            raise
        if send_ok:
            admission.mark_submitted(window_id)
            hibernator.forget(window_id)
            return True, send_msg
        admission.release(window_id)
        last_message = send_msg
        if (
            "Window is not running an agent" not in send_msg
            and "Window is not running Codex" not in send_msg
        ):
            return False, send_msg
        await asyncio_mod.sleep(interval)
    return False, last_message or "Agent did not become ready"


async def send_or_queue_to_starting_window(
    bot: Bot,
    user_id: int,
    thread_id: int | None,
    window_id: str,
    text: str,
    *,
    auto_confirm_startup_trust: bool = False,
    runtime: Any = None,
) -> tuple[bool, str, bool]:
    """Persist a first prompt and use the same lifecycle as bound-window input."""
    del auto_confirm_startup_trust
    soq_fn, soq_patched = _rt_fn(
        runtime, "_send_or_queue_agent_input", send_or_queue_agent_input
    )
    if soq_patched:
        return await soq_fn(bot, user_id, thread_id, window_id, text)
    return await soq_fn(bot, user_id, thread_id, window_id, text, runtime=runtime)


async def enable_fast_mode_if_requested(
    bot: Bot,
    user_id: int,
    thread_id: int | None,
    window_id: str,
    profile: AgentProfile,
    chat_id: int,
    *,
    runtime: Any = None,
) -> None:
    """Enable Fast mode after the selected agent session is ready."""
    if not profile.fast_mode:
        return

    send_fn = _dep(runtime, "safe_send", safe_send)
    ready_fn, ready_patched = _rt_fn(
        runtime, "_send_to_window_when_codex_ready", send_to_window_when_codex_ready
    )
    if ready_patched:
        success, message = await ready_fn(
            user_id,
            thread_id,
            window_id,
            "/fast",
            auto_confirm_startup_trust=True,
        )
    else:
        success, message = await ready_fn(
            user_id,
            thread_id,
            window_id,
            "/fast",
            auto_confirm_startup_trust=True,
            runtime=runtime,
        )
    if success:
        return
    logger.warning(
        "Failed to enable %s Fast mode for window %s: %s",
        profile.display_name,
        window_id,
        message,
    )
    await send_fn(
        bot,
        chat_id,
        "⚠️ Fast mode could not be enabled for this model. "
        "The session will continue with the selected reasoning level.",
        message_thread_id=thread_id,
    )


async def recovered_agent_process_status(
    window_id: str, *, runtime: Any = None
) -> tuple[bool, str]:
    """Return whether a recovery window still owns a live agent process."""
    tmux = _dep(runtime, "tmux_manager", tmux_manager)
    window = await tmux.find_window_by_id(window_id)
    if not window:
        return False, "the recovery window exited before it became ready"

    pane_command = (window.pane_current_command or "").strip()
    if not is_shell_pane_command(pane_command):
        return True, ""

    pane_text = await tmux.capture_pane(window_id) or ""
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


async def wait_for_recovered_agent_process(
    window_id: str,
    *,
    timeout: float = 10.0,
    interval: float = 0.5,
    runtime: Any = None,
) -> tuple[bool, str]:
    """Wait for a recovery command to leave the shell, detecting fast failures."""
    asyncio_mod = _dep(runtime, "asyncio", asyncio)
    status_fn, status_patched = _rt_fn(
        runtime, "_recovered_agent_process_status", recovered_agent_process_status
    )
    deadline = asyncio.get_running_loop().time() + timeout
    last_message = "Codex did not start before the recovery timeout"
    while asyncio.get_running_loop().time() < deadline:
        if status_patched:
            healthy, message = await status_fn(window_id)
        else:
            healthy, message = await status_fn(window_id, runtime=runtime)
        if healthy:
            return True, ""
        last_message = message
        if "active in another Codex process" in message:
            return False, message
        await asyncio_mod.sleep(interval)
    return False, last_message


async def recover_missing_bound_window(
    *,
    bot: Bot,
    user_id: int,
    thread_id: int,
    old_window_id: str,
    text: str,
    runtime: Any = None,
) -> tuple[bool, str]:
    """Transactionally recreate a missing window and forward pending text."""
    cfg = _dep(runtime, "config", config)
    sm = _dep(runtime, "session_manager", session_manager)
    tmux = _dep(runtime, "tmux_manager", tmux_manager)
    mark_working_fn = _dep(runtime, "mark_window_working", mark_window_working)
    forget_fn = _dep(
        runtime, "forget_missing_bound_window", forget_missing_bound_window
    )
    wait_proc_fn, wait_proc_patched = _rt_fn(
        runtime, "_wait_for_recovered_agent_process", wait_for_recovered_agent_process
    )
    proc_status_fn, proc_status_patched = _rt_fn(
        runtime, "_recovered_agent_process_status", recovered_agent_process_status
    )
    soq_fn, soq_patched = _rt_fn(
        runtime, "_send_or_queue_agent_input", send_or_queue_agent_input
    )

    state = sm.window_states.get(old_window_id)
    if not state or not state.session_id or not state.cwd:
        return False, "missing window has no resumable session state"

    resume_session_id = state.session_id
    saved_state = copy.copy(state)
    selected_path = state.cwd
    agent_type = normalize_agent_type(getattr(state, "agent_type", ""), cfg.agent_type)
    resume_offset: int | None = None
    if agent_type == AGENT_CODEX:
        (
            _session_id,
            resume_offset,
        ) = await sm.transcript_confirmation_baseline(old_window_id)
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

    max_create_attempts = min(len(persisted_window_ids) + 1, 16)
    success = False
    message = ""
    created_wname = requested_window_name
    created_wid = ""
    created_target: AgentTarget | None = None
    launch_spec = AgentLaunchSpec(
        cwd=selected_path,
        window_name=requested_window_name,
        resume_session_id=resume_session_id,
        account_name=account_name or "",
        agent_type=agent_type,
        model=getattr(state, "model", ""),
        reasoning_effort=getattr(state, "reasoning_effort", ""),
        fast_mode=getattr(state, "fast_mode", False),
    )

    for create_attempt in range(1, max_create_attempts + 1):
        (
            success,
            message,
            created_wname,
            created_wid,
            created_target,
        ) = await _launch_local_window(
            launch_spec, runtime=runtime, include_permission_mode=False
        )
        if not success:
            return False, message

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
            break

        await tmux.kill_window(created_wid)
        logger.warning(
            "Discarded recovered window id collision: old=%s created=%s "
            "topics=%s attempt=%d/%d",
            old_window_id,
            created_wid,
            conflicting_topics,
            create_attempt,
            max_create_attempts,
        )
    else:
        return (
            False,
            "tmux could not allocate a window id that is not reserved by another "
            "saved topic. Temporary windows were stopped without changing any "
            "bindings; restart the bot and retry.",
        )

    reused_original_id = created_wid == old_window_id

    async def discard_failed_window() -> None:
        await tmux.kill_window(created_wid)
        if reused_original_id:
            await sm.remove_session_map_entry(old_window_id)
            sm.window_states[old_window_id] = saved_state
            sm._set_local_target_session(old_window_id, resume_session_id)
            sm._save_state()
            return
        await sm.remove_session_map_entry(created_wid)
        sm.remove_window_state(created_wid)

    if wait_proc_patched:
        started, startup_message = await wait_proc_fn(created_wid)
    else:
        started, startup_message = await wait_proc_fn(created_wid, runtime=runtime)
    if not started:
        await discard_failed_window()
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
    hook_ok = await sm.wait_for_session_map_entry(
        created_wid,
        timeout=15.0,
    )
    if proc_status_patched:
        healthy, health_message = await proc_status_fn(created_wid)
    else:
        healthy, health_message = await proc_status_fn(created_wid, runtime=runtime)
    if not healthy:
        await discard_failed_window()
        return False, health_message

    if not hook_ok:
        if agent_type == AGENT_CODEX and not await sm.wait_for_transcript_resume_ready(
            resume_session_id,
            str(selected_path),
            account_name=account_name or "",
            after_offset=resume_offset,
        ):
            await discard_failed_window()
            return (
                False,
                "the resumed Codex session did not finish restoring. Your message "
                "was not sent; try again or create a new session.",
            )
        logger.info(
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
            user_id,
            thread_id,
            created_target,
            window_name=created_wname,
        )
    else:
        sm.bind_thread(
            user_id,
            thread_id,
            created_wid,
            window_name=created_wname,
        )

    if not reused_original_id:
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
    forget_fn(user_id, thread_id, old_window_id)

    if soq_patched:
        send_ok, send_msg, queued = await soq_fn(
            bot, user_id, thread_id, created_wid, text
        )
    else:
        send_ok, send_msg, queued = await soq_fn(
            bot, user_id, thread_id, created_wid, text, runtime=runtime
        )
    if send_ok:
        if not queued:
            await mark_working_fn(bot, user_id, created_wid, thread_id)
        delivery = "queued" if queued else "forwarded"
        return True, f"Recovered window `{created_wname}` and {delivery} your message."
    return (
        False,
        "recovered the tmux window, but forwarding failed: "
        f"{send_msg}. Please send the message again.",
    )


async def handle_missing_bound_window_input(
    *,
    update_message: Any,
    user_id: int,
    thread_id: int,
    window_id: str,
    text: str,
    success_reply: str | None = None,
    runtime: Any = None,
) -> None:
    """Recover a missing bound window and forward the triggering input."""
    sm = _dep(runtime, "session_manager", session_manager)
    reply_fn = _dep(runtime, "safe_reply", safe_reply)
    reset_fn, reset_patched = _rt_fn(
        runtime, "_reset_transient_agent_retry", reset_transient_agent_retry
    )
    recover_fn, recover_patched = _rt_fn(
        runtime, "_recover_missing_bound_window", recover_missing_bound_window
    )
    discard_fn, discard_patched = _rt_fn(
        runtime, "_discard_queued_agent_input", discard_queued_agent_input
    )

    if reset_patched:
        reset_fn(window_id)
    else:
        reset_fn(window_id, runtime=runtime)
    display = sm.get_display_name(window_id)
    state = sm.window_states.get(window_id)
    if state and state.session_id and state.cwd:
        logger.info(
            "Recovering missing bound window %s (user=%d, thread=%d)",
            display,
            user_id,
            thread_id,
        )
        await reply_fn(
            update_message,
            (
                f"💤 Session `{display}` was paused while idle. Waking it and "
                "resuming your previous context..."
                if state.hibernated_at
                else f"♻️ Window `{display}` disappeared. Recreating it and "
                "resuming the previous session..."
            ),
        )
        recover_kwargs: dict[str, Any] = {
            "bot": update_message.get_bot(),
            "user_id": user_id,
            "thread_id": thread_id,
            "old_window_id": window_id,
            "text": text,
        }
        if not recover_patched:
            recover_kwargs["runtime"] = runtime
        recovered, recovery_message = await recover_fn(**recover_kwargs)
        if not recovered:
            await reply_fn(update_message, f"❌ {recovery_message}")
        elif success_reply:
            await reply_fn(update_message, success_reply)
        return

    logger.info(
        "Stale binding: window %s gone, unbinding (user=%d, thread=%d)",
        display,
        user_id,
        thread_id,
    )
    if discard_patched:
        await discard_fn(user_id, thread_id, window_id)
    else:
        await discard_fn(user_id, thread_id, window_id, runtime=runtime)
    sm.unbind_thread(user_id, thread_id)
    await reply_fn(
        update_message,
        f"❌ Window '{display}' no longer exists. Binding removed.\n"
        "Send a message to start a new session.",
    )


async def handle_non_codex_bound_window(
    *,
    update_message: Any,
    user_id: int,
    thread_id: int,
    window_id: str,
    pane_command: str,
    text: str,
    success_reply: str | None = None,
    runtime: Any = None,
) -> bool:
    """Recover or unbind a topic whose tmux window has fallen back to a shell."""
    if not is_shell_pane_command(pane_command):
        return False

    sm = _dep(runtime, "session_manager", session_manager)
    tmux = _dep(runtime, "tmux_manager", tmux_manager)
    reply_fn = _dep(runtime, "safe_reply", safe_reply)
    reset_fn, reset_patched = _rt_fn(
        runtime, "_reset_transient_agent_retry", reset_transient_agent_retry
    )
    recover_fn, recover_patched = _rt_fn(
        runtime, "_recover_missing_bound_window", recover_missing_bound_window
    )
    discard_fn, discard_patched = _rt_fn(
        runtime, "_discard_queued_agent_input", discard_queued_agent_input
    )

    if reset_patched:
        reset_fn(window_id)
    else:
        reset_fn(window_id, runtime=runtime)
    display = sm.get_display_name(window_id)
    state = sm.window_states.get(window_id)
    if state and state.session_id and state.cwd:
        logger.info(
            "Recovering non-Codex bound window %s (command=%s, user=%d, thread=%d)",
            display,
            pane_command,
            user_id,
            thread_id,
        )
        await reply_fn(
            update_message,
            f"♻️ Window `{display}` is no longer running {PRODUCT_NAME} "
            f"(current command: `{pane_command}`). Recreating it and resuming "
            "the previous session...",
        )
        await tmux.kill_window(window_id)
        recover_kwargs: dict[str, Any] = {
            "bot": update_message.get_bot(),
            "user_id": user_id,
            "thread_id": thread_id,
            "old_window_id": window_id,
            "text": text,
        }
        if not recover_patched:
            recover_kwargs["runtime"] = runtime
        recovered, recovery_message = await recover_fn(**recover_kwargs)
        if not recovered:
            await reply_fn(update_message, f"❌ {recovery_message}")
        elif success_reply:
            await reply_fn(update_message, success_reply)
        return True

    logger.info(
        "Stale non-Codex binding: window %s command=%s, unbinding (user=%d, thread=%d)",
        display,
        pane_command,
        user_id,
        thread_id,
    )
    if discard_patched:
        await discard_fn(user_id, thread_id, window_id)
    else:
        await discard_fn(user_id, thread_id, window_id, runtime=runtime)
    sm.unbind_thread(user_id, thread_id)
    await reply_fn(
        update_message,
        f"❌ Window '{display}' is not running {PRODUCT_NAME} "
        f"(current command: {pane_command}). Binding removed.\n"
        "Send a message to start a new session.",
    )
    return True


async def handle_auth_error_bound_window(
    *,
    update_message: Any,
    user_id: int,
    thread_id: int,
    window_id: str,
    pane_text: str,
    text: str,
    success_reply: str | None = None,
    runtime: Any = None,
) -> bool:
    """Recover a bound window whose Codex TUI is blocked by expired auth."""
    auth_error = extract_auth_error_message(pane_text)
    if not auth_error:
        return False

    sm = _dep(runtime, "session_manager", session_manager)
    tmux = _dep(runtime, "tmux_manager", tmux_manager)
    reply_fn = _dep(runtime, "safe_reply", safe_reply)
    recover_fn, recover_patched = _rt_fn(
        runtime, "_recover_missing_bound_window", recover_missing_bound_window
    )

    display = sm.get_display_name(window_id)
    state = sm.window_states.get(window_id)
    if state and state.session_id and state.cwd:
        logger.info(
            "Recovering auth-blocked Codex window %s (user=%d, thread=%d)",
            display,
            user_id,
            thread_id,
        )
        await reply_fn(
            update_message,
            f"♻️ Window `{display}` is blocked by an expired Codex login. "
            "Recreating it with the current login and resuming the previous session...",
        )
        await tmux.kill_window(window_id)
        recover_kwargs: dict[str, Any] = {
            "bot": update_message.get_bot(),
            "user_id": user_id,
            "thread_id": thread_id,
            "old_window_id": window_id,
            "text": text,
        }
        if not recover_patched:
            recover_kwargs["runtime"] = runtime
        recovered, recovery_message = await recover_fn(**recover_kwargs)
        if not recovered:
            await reply_fn(update_message, f"❌ {recovery_message}")
        elif success_reply:
            await reply_fn(update_message, success_reply)
        return True

    await reply_fn(
        update_message,
        f"⚠️ {codex_auth_recovery_message(auth_error)}",
    )
    return True


async def create_and_bind_window(
    query: object,
    context: ContextTypes.DEFAULT_TYPE,
    user: object,
    spec: WindowLaunchSpec,
    *,
    runtime: Any = None,
) -> None:
    """Create a tmux window, bind it to a topic, and forward pending text.

    Shared by CB_DIR_CONFIRM (no sessions), CB_SESSION_NEW, and CB_SESSION_SELECT.
    """
    from telegram import CallbackQuery, User

    assert isinstance(query, CallbackQuery)
    assert isinstance(user, User)

    cfg = _dep(runtime, "config", config)
    sm = _dep(runtime, "session_manager", session_manager)
    edit_fn = _dep(runtime, "safe_edit", safe_edit)
    send_fn = _dep(runtime, "safe_send", safe_send)
    default_acct_fn = _dep(
        runtime, "get_default_account_name", get_default_account_name
    )
    remember_fn = _dep(runtime, "remember_current_account", remember_current_account)
    mark_working_fn = _dep(runtime, "mark_window_working", mark_window_working)

    target_fn, target_patched = _rt_fn(
        runtime, "_create_agent_target", create_agent_target
    )
    fast_fn, fast_patched = _rt_fn(
        runtime, "_enable_fast_mode_if_requested", enable_fast_mode_if_requested
    )
    take_fn, take_patched = _rt_fn(
        runtime, "_take_pending_thread_message", take_pending_thread_message
    )
    send_msg_fn, send_msg_patched = _rt_fn(
        runtime, "_send_message_to_agent", send_message_to_agent
    )
    starting_fn, starting_patched = _rt_fn(
        runtime, "_send_or_queue_to_starting_window", send_or_queue_to_starting_window
    )
    clear_pending_fn, _ = _rt_fn(
        runtime, "_clear_pending_thread_input", clear_pending_thread_input
    )

    profile = spec.resolve_profile(cfg=cfg)
    # Account snapshots are agent-specific. Do not apply a Codex snapshot to a
    # Claude topic (or vice versa) when both agents are enabled per topic.
    launch_account = (
        None
        if profile.agent_type == AGENT_CURSOR
        else spec.account_name or default_acct_fn(profile.agent_type)
    )
    agent_spec = spec.to_agent_launch_spec(profile, launch_account)

    if target_patched:
        (
            success,
            message,
            created_wname,
            created_wid,
            created_target,
        ) = await target_fn(
            cwd=agent_spec.cwd,
            node_id=agent_spec.node_id,
            resume_session_id=agent_spec.resume_session_id,
            account_name=agent_spec.account_name,
            agent_type=agent_spec.agent_type,
            model=agent_spec.model,
            reasoning_effort=agent_spec.reasoning_effort,
            fast_mode=agent_spec.fast_mode,
            permission_mode=agent_spec.permission_mode,
        )
    else:
        (
            success,
            message,
            created_wname,
            created_wid,
            created_target,
        ) = await target_fn(agent_spec, runtime=runtime)

    async def _run_fast_mode(wid: str, chat_id: int) -> None:
        if fast_patched:
            await fast_fn(
                context.bot,
                query.from_user.id,
                spec.pending_thread_id,
                wid,
                profile,
                chat_id,
            )
        else:
            await fast_fn(
                context.bot,
                query.from_user.id,
                spec.pending_thread_id,
                wid,
                profile,
                chat_id,
                runtime=runtime,
            )

    async def _take_pending(*, remote: bool) -> tuple[str | None, str | None]:
        take_kwargs: dict[str, Any] = {
            "user_id": query.from_user.id,
            "thread_id": spec.pending_thread_id,
            "remote": remote,
        }
        if not take_patched:
            take_kwargs["runtime"] = runtime
        return await take_fn(context, **take_kwargs)

    if success:
        if launch_account:
            remember_fn(launch_account, profile.agent_type)
        if created_target and created_target.backend_id != "local":
            created_wname = created_wname or format_remote_target(created_target)
            logger.info(
                "Remote agent target created: %s/%s session=%s at %s "
                "(user=%d, thread=%s, resume=%s)",
                created_target.backend_id,
                created_target.node_id,
                created_target.session_id,
                spec.selected_path,
                user.id,
                spec.pending_thread_id,
                spec.resume_session_id,
            )

            if spec.pending_thread_id is not None:
                sm.bind_thread_target(
                    user.id,
                    spec.pending_thread_id,
                    created_target,
                    window_name=created_wname,
                )
                resolved_chat = sm.resolve_chat_id(user.id, spec.pending_thread_id)
                status = "Resumed" if spec.resume_session_id else "Created"
                await edit_fn(
                    query,
                    f"✅ {message}\n\n{status}. Send messages here.",
                )

                await _run_fast_mode("", resolved_chat)

                pending_text, pending_error = await _take_pending(remote=True)
                if context.user_data is not None:
                    context.user_data.pop("_pending_thread_id", None)
                if pending_error:
                    await send_fn(
                        context.bot,
                        resolved_chat,
                        f"❌ Failed to send pending attachment: {pending_error}",
                        message_thread_id=spec.pending_thread_id,
                    )
                elif pending_text:
                    if send_msg_patched:
                        send_ok, send_msg = await send_msg_fn(
                            query.from_user.id,
                            spec.pending_thread_id,
                            "",
                            pending_text,
                        )
                    else:
                        send_ok, send_msg = await send_msg_fn(
                            query.from_user.id,
                            spec.pending_thread_id,
                            "",
                            pending_text,
                            runtime=runtime,
                        )
                    if not send_ok:
                        logger.warning(
                            "Failed to forward pending text to remote target: %s",
                            send_msg,
                        )
                        await send_fn(
                            context.bot,
                            resolved_chat,
                            f"❌ Failed to send pending message: {send_msg}",
                            message_thread_id=spec.pending_thread_id,
                        )
            else:
                await edit_fn(query, f"✅ {message}")
            if spec.answer_callback:
                try:
                    await query.answer("Created")
                except Exception:
                    logger.debug("Callback query answer skipped: query expired")
            return

        if not created_wid:
            await edit_fn(query, "❌ Agent backend did not return a local window id")
            if spec.pending_thread_id is not None and context.user_data is not None:
                clear_pending_fn(context.user_data)
            if spec.answer_callback:
                try:
                    await query.answer("Failed")
                except Exception:
                    logger.debug("Callback query answer skipped: query expired")
            return

        sm.prepare_window_launch(
            created_wid,
            cwd=str(spec.selected_path),
            window_name=created_wname,
            account_name=launch_account or "",
            agent_type=profile.agent_type,
            model=profile.model,
            reasoning_effort=profile.reasoning_effort,
            fast_mode=profile.fast_mode,
        )
        logger.info(
            "Window created: %s (id=%s) at %s (user=%d, thread=%s, resume=%s, account=%s)",
            created_wname,
            created_wid,
            spec.selected_path,
            user.id,
            spec.pending_thread_id,
            spec.resume_session_id,
            launch_account,
        )
        hook_ok = False
        if spec.resume_session_id:
            hook_ok = await sm.wait_for_session_map_entry(created_wid, timeout=15.0)

        if spec.resume_session_id and not hook_ok:
            logger.warning(
                "Hook timed out for resume window %s, "
                "falling back to session_id=%s cwd=%s",
                created_wid,
                spec.resume_session_id,
                spec.selected_path,
            )
            sm.register_session_to_window(
                created_wid,
                spec.resume_session_id,
                str(spec.selected_path),
                window_name=created_wname,
                persist_session_map=True,
            )

        if spec.pending_thread_id is not None:
            if created_target:
                sm.bind_thread_target(
                    user.id,
                    spec.pending_thread_id,
                    created_target,
                    window_name=created_wname,
                )
            else:
                sm.bind_thread(
                    user.id,
                    spec.pending_thread_id,
                    created_wid,
                    window_name=created_wname,
                )

            resolved_chat = sm.resolve_chat_id(user.id, spec.pending_thread_id)
            status = "Resumed" if spec.resume_session_id else "Created"
            await edit_fn(
                query,
                f"✅ {message}\n\n{status}. Send messages here.",
            )

            await _run_fast_mode(created_wid, resolved_chat)

            pending_text, pending_error = await _take_pending(remote=False)
            if context.user_data is not None:
                context.user_data.pop("_pending_thread_id", None)
            if pending_error:
                await send_fn(
                    context.bot,
                    resolved_chat,
                    f"❌ Failed to send pending attachment: {pending_error}",
                    message_thread_id=spec.pending_thread_id,
                )
            elif pending_text:
                logger.debug(
                    "Forwarding pending topic input to window %s (len=%d)",
                    created_wname,
                    len(pending_text),
                )
                if starting_patched:
                    send_ok, send_msg, queued = await starting_fn(
                        context.bot,
                        query.from_user.id,
                        spec.pending_thread_id,
                        created_wid,
                        pending_text,
                        auto_confirm_startup_trust=True,
                    )
                else:
                    send_ok, send_msg, queued = await starting_fn(
                        context.bot,
                        query.from_user.id,
                        spec.pending_thread_id,
                        created_wid,
                        pending_text,
                        auto_confirm_startup_trust=True,
                        runtime=runtime,
                    )
                if send_ok and not queued:
                    await mark_working_fn(
                        context.bot,
                        query.from_user.id,
                        created_wid,
                        spec.pending_thread_id,
                    )
                elif queued:
                    await send_fn(
                        context.bot,
                        resolved_chat,
                        f"⏳ {send_msg}",
                        message_thread_id=spec.pending_thread_id,
                    )
                if not send_ok:
                    logger.warning("Failed to forward pending text: %s", send_msg)
                    await send_fn(
                        context.bot,
                        resolved_chat,
                        f"❌ Failed to send pending message: {send_msg}",
                        message_thread_id=spec.pending_thread_id,
                    )
        else:
            await edit_fn(query, f"✅ {message}")
    else:
        await edit_fn(query, f"❌ {message}")
        if spec.pending_thread_id is not None and context.user_data is not None:
            clear_pending_fn(context.user_data)
    if spec.answer_callback:
        try:
            await query.answer("Created" if success else "Failed")
        except Exception:
            logger.debug("Callback query answer skipped: query expired")
