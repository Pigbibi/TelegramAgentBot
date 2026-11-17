"""Domain-organized callback query routing for Telegram inline keyboards.

Each domain handler receives a ``CallbackContext`` whose ``deps`` object is the
``telegram_agent_bot.bot`` module (or compatible mock namespace in tests).
Resolving runtime helpers and singletons via ``ctx.deps`` preserves existing
``monkeypatch.setattr`` / ``unittest.mock.patch`` hooks on ``bot.*``.
"""

from __future__ import annotations

import io
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from telegram import CallbackQuery, InputMediaDocument, Update, User
from telegram.ext import ContextTypes

from ..agent_profile import (
    AGENT_CLAUDE,
    AGENT_CLAUDE_OFFICIAL,
    AGENT_CODEX,
    AGENT_CURSOR,
    normalize_agent_type,
    normalize_effort,
)
from ..output_mode import normalize_output_mode, output_mode_label
from .callback_data import (
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
from .input_routing import parse_input_routing_callback
from .directory_browser import (
    BROWSE_BACKEND_ID_KEY,
    BROWSE_DIRS_KEY,
    BROWSE_NODE_ID_KEY,
    BROWSE_PAGE_KEY,
    BROWSE_PATH_KEY,
    BROWSE_ROOT_LABEL_KEY,
    BROWSE_ROOT_PATH_KEY,
    PROFILE_AGENT_KEY,
    PROFILE_EFFORT_KEY,
    PROFILE_FAST_MODE_KEY,
    PROFILE_MODEL_KEY,
    PROFILE_MODELS_KEY,
    PROFILE_PERMISSION_MODE_KEY,
    ROOTS_KEY,
    SESSIONS_KEY,
    STATE_BROWSING_DIRECTORY,
    STATE_KEY,
    UNBOUND_WINDOWS_KEY,
    clear_browse_state,
    clear_root_picker_state,
    clear_session_picker_state,
    clear_window_picker_state,
)

logger = logging.getLogger(__name__)


@dataclass
class CallbackContext:
    """Normalized callback invocation passed to domain dispatchers."""

    update: Update
    context: ContextTypes.DEFAULT_TYPE
    query: CallbackQuery
    data: str
    user: User
    thread_id: int | None
    deps: Any


CallbackHandler = Callable[[CallbackContext], Awaitable[None]]


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


def _pending_thread_id(ctx: CallbackContext) -> int | None:
    user_data = ctx.context.user_data
    return user_data.get("_pending_thread_id") if user_data else None


async def _ensure_matching_topic(
    ctx: CallbackContext,
    alert_text: str,
    *,
    fallback_to_current: bool = False,
) -> tuple[bool, int | None]:
    pending_tid = _pending_thread_id(ctx)
    current_tid = ctx.deps._get_thread_id(ctx.update)
    if pending_tid is None and fallback_to_current:
        pending_tid = current_tid
    if pending_tid is not None and current_tid != pending_tid:
        await ctx.query.answer(alert_text, show_alert=True)
        return False, pending_tid
    return True, pending_tid


# --- Update & Input Routing ---


async def _handle_codex_update_apply(ctx: CallbackContext) -> None:
    await ctx.deps._apply_codex_update_from_callback(ctx.query)


async def _handle_codex_update_dismiss(ctx: CallbackContext) -> None:
    await ctx.deps._safe_answer_callback(ctx.query, "Dismissed")
    await ctx.deps.safe_edit(ctx.query, "Codex CLI update dismissed.")


async def _handle_input_route(ctx: CallbackContext) -> None:
    parsed = parse_input_routing_callback(ctx.data)
    if parsed is None:
        await ctx.deps._safe_answer_callback(
            ctx.query, "Invalid input action", show_alert=True
        )
        return
    mode, record_id = parsed
    await ctx.deps._handle_input_routing_callback(
        ctx.update,
        ctx.context,
        mode=mode,
        record_id=record_id,
    )


# --- History & Output Mode ---


async def _handle_history_page(ctx: CallbackContext) -> None:
    prefix_len = len(
        CB_HISTORY_PREV
    )  # same length for CB_HISTORY_PREV and CB_HISTORY_NEXT
    rest = ctx.data[prefix_len:]
    try:
        parts = rest.split(":")
        if len(parts) < 4:
            offset_str, window_id = rest.split(":", 1)
            start_byte, end_byte = 0, 0
        else:
            offset_str = parts[0]
            start_byte = int(parts[-2])
            end_byte = int(parts[-1])
            window_id = ":".join(parts[1:-2])
        offset = int(offset_str)
    except (ValueError, IndexError):
        await ctx.query.answer("Invalid data")
        return

    w = await ctx.deps.tmux_manager.find_window_by_id(window_id)
    if w:
        await ctx.deps.send_history(
            ctx.query,
            window_id,
            offset=offset,
            edit=True,
            start_byte=start_byte,
            end_byte=end_byte,
            output_mode=ctx.deps.session_manager.get_output_mode(
                ctx.user.id, ctx.thread_id
            ),
        )
    else:
        await ctx.deps.safe_edit(ctx.query, "Window no longer exists.")
    await ctx.query.answer("Page updated")


async def _handle_output_mode(ctx: CallbackContext) -> None:
    mode = normalize_output_mode(ctx.data[len(CB_OUTPUT_MODE) :])
    thread_id = ctx.deps._get_thread_id(ctx.update)
    selected = ctx.deps.session_manager.set_output_mode(ctx.user.id, thread_id, mode)
    await ctx.deps.safe_edit(
        ctx.query,
        ctx.deps._output_mode_text(selected),
        reply_markup=ctx.deps._output_mode_keyboard(selected),
    )
    await ctx.query.answer(f"Output mode: {output_mode_label(selected)}")


# --- Project Root Picker ---


async def _handle_root_select(ctx: CallbackContext) -> None:
    ok, _ = await _ensure_matching_topic(ctx, "Stale root picker (topic mismatch)")
    if not ok:
        return
    try:
        idx = int(ctx.data[len(CB_ROOT_SELECT) :])
    except ValueError:
        await ctx.query.answer("Invalid data")
        return

    user_data = ctx.context.user_data
    cached_roots: list[object] = user_data.get(ROOTS_KEY, []) if user_data else []
    if idx < 0 or idx >= len(cached_roots):
        await ctx.query.answer("Root list changed, please retry", show_alert=True)
        return

    root = ctx.deps._parse_root_selection(cached_roots[idx])
    if root is None:
        await ctx.query.answer("Invalid root data", show_alert=True)
        return

    root_label = root["label"]
    root_path = root["path"]
    backend_id = root["backend_id"]
    node_id = root["node_id"]

    if backend_id:
        rendered = await ctx.deps._build_backend_directory_browser(
            backend_id=backend_id,
            node_id=node_id,
            path=root_path,
            root_label=root_label,
            root_path=root_path,
        )
        if rendered is None:
            await ctx.query.answer("Backend browser unavailable", show_alert=True)
            return
        msg_text, keyboard, subdirs, start_path = rendered
    else:
        start_path = ctx.deps._default_directory_browser_path(root_path)
        msg_text, keyboard, subdirs = ctx.deps.build_directory_browser(
            start_path,
            root_label=root_label,
            root_path=root_path,
        )
    if user_data is not None:
        user_data[STATE_KEY] = STATE_BROWSING_DIRECTORY
        user_data[BROWSE_PATH_KEY] = start_path
        user_data[BROWSE_ROOT_LABEL_KEY] = root_label
        user_data[BROWSE_ROOT_PATH_KEY] = root_path
        if backend_id:
            user_data[BROWSE_BACKEND_ID_KEY] = backend_id
            user_data[BROWSE_NODE_ID_KEY] = node_id
        user_data[BROWSE_PAGE_KEY] = 0
        user_data[BROWSE_DIRS_KEY] = subdirs
        user_data.pop(ROOTS_KEY, None)
    await ctx.deps.safe_edit(ctx.query, msg_text, reply_markup=keyboard)
    await ctx.query.answer()


async def _handle_root_cancel(ctx: CallbackContext) -> None:
    ok, _ = await _ensure_matching_topic(ctx, "Stale root picker (topic mismatch)")
    if not ok:
        return
    clear_root_picker_state(ctx.context.user_data)
    if ctx.context.user_data is not None:
        ctx.deps._clear_pending_thread_input(ctx.context.user_data)
    await ctx.deps.safe_edit(ctx.query, "Cancelled")
    await ctx.query.answer("Cancelled")


# --- Agent Profile Picker ---


def _configured_effort(deps: Any, agent_type: str) -> str:
    if agent_type in {AGENT_CLAUDE, AGENT_CLAUDE_OFFICIAL}:
        return deps.config.claude_reasoning_effort
    if agent_type == AGENT_CODEX:
        return deps.config.codex_reasoning_effort
    return ""


async def _handle_profile_agent(ctx: CallbackContext) -> None:
    ok, _ = await _ensure_matching_topic(ctx, "Stale picker (topic mismatch)")
    if not ok:
        return
    agent_type = normalize_agent_type(ctx.data[len(CB_PROFILE_AGENT) :])
    await ctx.deps.refresh_model_catalog(agent_type)
    models = ctx.deps._profile_models(agent_type)
    model = models[0] if models else ""
    configured_effort = _configured_effort(ctx.deps, agent_type)
    if ctx.context.user_data is not None:
        ctx.context.user_data[PROFILE_AGENT_KEY] = agent_type
        ctx.context.user_data[PROFILE_MODEL_KEY] = model
        ctx.context.user_data[PROFILE_FAST_MODE_KEY] = False
        ctx.context.user_data[PROFILE_PERMISSION_MODE_KEY] = (
            ctx.deps._default_profile_permission(agent_type)
        )
        ctx.context.user_data[PROFILE_EFFORT_KEY] = ctx.deps._resolve_profile_effort(
            agent_type,
            model,
            configured_effort,
        )
    await ctx.deps._show_agent_profile_settings(ctx.query, ctx.context)
    await ctx.query.answer()


async def _handle_profile_model(ctx: CallbackContext) -> None:
    ok, _ = await _ensure_matching_topic(ctx, "Stale picker (topic mismatch)")
    if not ok:
        return
    user_data = ctx.context.user_data
    models = user_data.get(PROFILE_MODELS_KEY, []) if user_data else []
    try:
        model_index = int(ctx.data[len(CB_PROFILE_MODEL) :])
    except ValueError:
        await ctx.query.answer("Invalid model", show_alert=True)
        return
    if model_index < 0 or model_index >= len(models):
        await ctx.query.answer("Model list changed", show_alert=True)
        return
    if user_data is not None:
        model = models[model_index]
        agent_type = normalize_agent_type(
            user_data.get(PROFILE_AGENT_KEY, ctx.deps.config.agent_type)
        )
        current_effort = user_data.get(
            PROFILE_EFFORT_KEY, _configured_effort(ctx.deps, agent_type)
        )
        user_data[PROFILE_MODEL_KEY] = model
        user_data[PROFILE_EFFORT_KEY] = ctx.deps._resolve_profile_effort(
            agent_type,
            model,
            current_effort if isinstance(current_effort, str) else "",
        )
    await ctx.deps._show_agent_profile_settings(ctx.query, ctx.context)
    await ctx.query.answer("Model selected")


async def _handle_profile_effort(ctx: CallbackContext) -> None:
    ok, _ = await _ensure_matching_topic(ctx, "Stale picker (topic mismatch)")
    if not ok:
        return
    user_data = ctx.context.user_data
    raw_effort = ctx.data[len(CB_PROFILE_EFFORT) :]
    effort = normalize_effort(raw_effort, "")
    agent_type = normalize_agent_type(
        user_data.get(PROFILE_AGENT_KEY, ctx.deps.config.agent_type)
        if user_data
        else ctx.deps.config.agent_type
    )
    model = user_data.get(PROFILE_MODEL_KEY, "") if user_data else ""
    if effort not in ctx.deps._profile_effort_values(
        agent_type, model if isinstance(model, str) else ""
    ):
        await ctx.query.answer("Reasoning level unavailable", show_alert=True)
        return
    if user_data is not None:
        user_data[PROFILE_EFFORT_KEY] = effort
    await ctx.deps._show_agent_profile_settings(ctx.query, ctx.context)
    await ctx.query.answer("Reasoning selected")


async def _handle_profile_fast(ctx: CallbackContext) -> None:
    ok, _ = await _ensure_matching_topic(ctx, "Stale picker (topic mismatch)")
    if not ok:
        return
    fast_mode = ctx.data[len(CB_PROFILE_FAST) :] == "on"
    if ctx.context.user_data is not None:
        ctx.context.user_data[PROFILE_FAST_MODE_KEY] = fast_mode
    await ctx.deps._show_agent_profile_settings(ctx.query, ctx.context)
    await ctx.query.answer("Fast mode updated")


async def _handle_profile_permission(ctx: CallbackContext) -> None:
    ok, _ = await _ensure_matching_topic(ctx, "Stale picker (topic mismatch)")
    if not ok:
        return
    permission_mode = ctx.data[len(CB_PROFILE_PERMISSION) :]
    if permission_mode not in {"ask", "full"}:
        await ctx.query.answer("Invalid permission mode", show_alert=True)
        return
    user_data = ctx.context.user_data
    if (
        user_data is not None
        and user_data.get(PROFILE_AGENT_KEY) == AGENT_CURSOR
        and ctx.deps.config.cursor_permission_mode == "full"
        and permission_mode == "ask"
    ):
        await ctx.query.answer("Cursor uses VPS-wide full access", show_alert=True)
        return
    if user_data is not None:
        user_data[PROFILE_PERMISSION_MODE_KEY] = permission_mode
    await ctx.deps._show_agent_profile_settings(ctx.query, ctx.context)
    await ctx.query.answer("Permission mode updated")


async def _handle_profile_confirm(ctx: CallbackContext) -> None:
    ok, _ = await _ensure_matching_topic(ctx, "Stale picker (topic mismatch)")
    if not ok:
        return
    await ctx.deps._continue_creation_with_profile(ctx.query, ctx.context, ctx.user)


async def _handle_profile_cancel(ctx: CallbackContext) -> None:
    ok, _ = await _ensure_matching_topic(ctx, "Stale picker (topic mismatch)")
    if not ok:
        return
    ctx.deps._clear_creation_state(ctx.context.user_data)
    await ctx.deps.safe_edit(ctx.query, "Cancelled")
    await ctx.query.answer("Cancelled")


# --- Directory Browser ---


async def _handle_dir_select(ctx: CallbackContext) -> None:
    ok, _ = await _ensure_matching_topic(ctx, "Stale browser (topic mismatch)")
    if not ok:
        return
    try:
        idx = int(ctx.data[len(CB_DIR_SELECT) :])
    except ValueError:
        await ctx.query.answer("Invalid data")
        return

    user_data = ctx.context.user_data
    cached_dirs: list[str] = user_data.get(BROWSE_DIRS_KEY, []) if user_data else []
    if idx < 0 or idx >= len(cached_dirs):
        await ctx.query.answer(
            "Directory list changed, please refresh", show_alert=True
        )
        return
    subdir_name = cached_dirs[idx]

    current_path = ctx.deps._creation_path(user_data, BROWSE_PATH_KEY)
    if current_path is None:
        await ctx.deps._reject_expired_project_selection(ctx.query, ctx.context)
        return
    backend_id, node_id = ctx.deps._browse_backend_context(user_data)
    if backend_id:
        root_label, root_path = ctx.deps._browse_root_context(user_data)
        new_path_str = ctx.deps._remote_child_path(current_path, subdir_name)
        rendered = await ctx.deps._build_backend_directory_browser(
            backend_id=backend_id,
            node_id=node_id,
            path=new_path_str,
            root_label=root_label,
            root_path=root_path,
        )
        if rendered is None:
            await ctx.query.answer("Directory not found", show_alert=True)
            return
        msg_text, keyboard, subdirs, resolved_path = rendered
        if user_data is not None:
            user_data[BROWSE_PATH_KEY] = resolved_path
            user_data[BROWSE_PAGE_KEY] = 0
            user_data[BROWSE_DIRS_KEY] = subdirs
        await ctx.deps.safe_edit(ctx.query, msg_text, reply_markup=keyboard)
        await ctx.query.answer()
        return

    current_path = ctx.deps._clamp_to_selected_root(current_path, user_data)
    new_path = (Path(current_path) / subdir_name).resolve()
    if not new_path.exists() or not new_path.is_dir():
        await ctx.query.answer("Directory not found", show_alert=True)
        return

    new_path_str = str(new_path)
    if user_data is not None:
        user_data[BROWSE_PATH_KEY] = new_path_str
        user_data[BROWSE_PAGE_KEY] = 0

    msg_text, keyboard, subdirs = ctx.deps.build_directory_browser(
        new_path_str,
        **ctx.deps._directory_browser_kwargs(user_data),
    )
    if user_data is not None:
        user_data[BROWSE_DIRS_KEY] = subdirs
    await ctx.deps.safe_edit(ctx.query, msg_text, reply_markup=keyboard)
    await ctx.query.answer()


async def _handle_dir_up(ctx: CallbackContext) -> None:
    ok, _ = await _ensure_matching_topic(ctx, "Stale browser (topic mismatch)")
    if not ok:
        return
    user_data = ctx.context.user_data
    current_path = ctx.deps._creation_path(user_data, BROWSE_PATH_KEY)
    if current_path is None:
        await ctx.deps._reject_expired_project_selection(ctx.query, ctx.context)
        return
    backend_id, node_id = ctx.deps._browse_backend_context(user_data)
    if backend_id:
        root_label, root_path = ctx.deps._browse_root_context(user_data)
        parent_path = ctx.deps._remote_parent_path(current_path, root_path)
        rendered = await ctx.deps._build_backend_directory_browser(
            backend_id=backend_id,
            node_id=node_id,
            path=parent_path,
            root_label=root_label,
            root_path=root_path,
        )
        if rendered is None:
            await ctx.query.answer("Directory not found", show_alert=True)
            return
        msg_text, keyboard, subdirs, resolved_path = rendered
        if user_data is not None:
            user_data[BROWSE_PATH_KEY] = resolved_path
            user_data[BROWSE_PAGE_KEY] = 0
            user_data[BROWSE_DIRS_KEY] = subdirs
        await ctx.deps.safe_edit(ctx.query, msg_text, reply_markup=keyboard)
        await ctx.query.answer()
        return

    parent = Path(current_path).resolve().parent
    parent_path = ctx.deps._clamp_to_selected_root(str(parent), user_data)
    if user_data is not None:
        user_data[BROWSE_PATH_KEY] = parent_path
        user_data[BROWSE_PAGE_KEY] = 0

    msg_text, keyboard, subdirs = ctx.deps.build_directory_browser(
        parent_path,
        **ctx.deps._directory_browser_kwargs(user_data),
    )
    if user_data is not None:
        user_data[BROWSE_DIRS_KEY] = subdirs
    await ctx.deps.safe_edit(ctx.query, msg_text, reply_markup=keyboard)
    await ctx.query.answer()


async def _handle_dir_page(ctx: CallbackContext) -> None:
    ok, _ = await _ensure_matching_topic(ctx, "Stale browser (topic mismatch)")
    if not ok:
        return
    try:
        pg = int(ctx.data[len(CB_DIR_PAGE) :])
    except ValueError:
        await ctx.query.answer("Invalid data")
        return
    user_data = ctx.context.user_data
    current_path = ctx.deps._creation_path(user_data, BROWSE_PATH_KEY)
    if current_path is None:
        await ctx.deps._reject_expired_project_selection(ctx.query, ctx.context)
        return
    backend_id, node_id = ctx.deps._browse_backend_context(user_data)
    if backend_id:
        root_label, root_path = ctx.deps._browse_root_context(user_data)
        rendered = await ctx.deps._build_backend_directory_browser(
            backend_id=backend_id,
            node_id=node_id,
            path=current_path,
            page=pg,
            root_label=root_label,
            root_path=root_path,
        )
        if rendered is None:
            await ctx.query.answer("Directory list changed", show_alert=True)
            return
        msg_text, keyboard, subdirs, resolved_path = rendered
        if user_data is not None:
            user_data[BROWSE_PATH_KEY] = resolved_path
            user_data[BROWSE_PAGE_KEY] = pg
            user_data[BROWSE_DIRS_KEY] = subdirs
        await ctx.deps.safe_edit(ctx.query, msg_text, reply_markup=keyboard)
        await ctx.query.answer()
        return

    if user_data is not None:
        user_data[BROWSE_PAGE_KEY] = pg

    msg_text, keyboard, subdirs = ctx.deps.build_directory_browser(
        current_path,
        pg,
        **ctx.deps._directory_browser_kwargs(user_data),
    )
    if user_data is not None:
        user_data[BROWSE_DIRS_KEY] = subdirs
    await ctx.deps.safe_edit(ctx.query, msg_text, reply_markup=keyboard)
    await ctx.query.answer()


async def _handle_dir_confirm(ctx: CallbackContext) -> None:
    user_data = ctx.context.user_data
    pending_thread_id = _pending_thread_id(ctx)
    if pending_thread_id is None:
        pending_thread_id = ctx.deps._get_thread_id(ctx.update)

    confirm_thread_id = ctx.deps._get_thread_id(ctx.update)
    if pending_thread_id is not None and confirm_thread_id != pending_thread_id:
        clear_browse_state(user_data)
        if user_data is not None:
            ctx.deps._clear_pending_thread_input(user_data)
        await ctx.query.answer("Stale browser (topic mismatch)", show_alert=True)
        return

    selected_path = ctx.deps._creation_path(user_data, BROWSE_PATH_KEY)
    if selected_path is None:
        await ctx.deps._reject_expired_project_selection(ctx.query, ctx.context)
        return
    backend_id, node_id = ctx.deps._browse_backend_context(user_data)
    if not backend_id:
        selected_path = ctx.deps._clamp_to_selected_root(selected_path, user_data)

    if user_data is not None:
        user_data["_pending_thread_id"] = pending_thread_id
        user_data["_selected_path"] = selected_path
        user_data["_selected_backend_id"] = backend_id
        user_data["_selected_node_id"] = node_id
    clear_browse_state(user_data)
    await ctx.query.answer("Choose agent")
    await ctx.deps._show_agent_profile_picker(ctx.query, ctx.context)


async def _handle_dir_cancel(ctx: CallbackContext) -> None:
    ok, _ = await _ensure_matching_topic(ctx, "Stale browser (topic mismatch)")
    if not ok:
        return
    clear_browse_state(ctx.context.user_data)
    if ctx.context.user_data is not None:
        ctx.deps._clear_pending_thread_input(ctx.context.user_data)
    await ctx.deps.safe_edit(ctx.query, "Cancelled")
    await ctx.query.answer("Cancelled")


# --- Session Picker ---


async def _handle_session_select(ctx: CallbackContext) -> None:
    ok, pending_tid = await _ensure_matching_topic(
        ctx, "Stale picker (topic mismatch)", fallback_to_current=True
    )
    if not ok:
        return
    try:
        idx = int(ctx.data[len(CB_SESSION_SELECT) :])
    except ValueError:
        await ctx.query.answer("Invalid data")
        return

    user_data = ctx.context.user_data
    cached_sessions = user_data.get(SESSIONS_KEY, []) if user_data else []
    if idx < 0 or idx >= len(cached_sessions):
        await ctx.query.answer("Session not found")
        return

    session = cached_sessions[idx]
    ctx.deps.session_manager.unhide_session(session.session_id)
    if ctx.deps.session_manager.has_bound_thread_for_session(session.session_id):
        resumable_sessions = ctx.deps._filter_resumable_sessions(cached_sessions)
        if user_data is not None:
            user_data[SESSIONS_KEY] = resumable_sessions

        if resumable_sessions:
            text, keyboard = ctx.deps.build_session_picker(resumable_sessions)
            await ctx.deps.safe_edit(
                ctx.query,
                "⚠️ This session is already active in another topic.\n"
                "To avoid cross-talk, pick a different session or start a new one.\n\n"
                f"{text}",
                reply_markup=keyboard,
            )
        else:
            await ctx.deps.safe_edit(
                ctx.query,
                "⚠️ This session is already active in another topic.\n"
                "To avoid cross-talk, start a new session here instead.",
                reply_markup=ctx.deps._build_resume_conflict_keyboard(),
            )
        await ctx.query.answer("Session already active", show_alert=True)
        return

    selected_path = ctx.deps._creation_path(user_data, "_selected_path")
    if selected_path is None:
        await ctx.deps._reject_expired_project_selection(ctx.query, ctx.context)
        return
    selected_node_id = user_data.get("_selected_node_id", "") if user_data else ""
    profile = ctx.deps._profile_from_context(user_data)
    clear_session_picker_state(user_data)
    if user_data is not None:
        user_data.pop("_selected_path", None)
        user_data.pop("_selected_backend_id", None)
        user_data.pop("_selected_node_id", None)

    create_kwargs: dict[str, Any] = {
        "resume_session_id": session.session_id,
        "agent_type": profile.agent_type,
        "model": profile.model,
        "reasoning_effort": profile.reasoning_effort,
        "fast_mode": profile.fast_mode,
        "permission_mode": profile.permission_mode,
    }
    if selected_node_id:
        create_kwargs["node_id"] = selected_node_id
    await ctx.deps._create_and_bind_window(
        ctx.query,
        ctx.context,
        ctx.user,
        selected_path,
        pending_tid,
        **create_kwargs,
    )


async def _handle_session_new(ctx: CallbackContext) -> None:
    ok, pending_tid = await _ensure_matching_topic(
        ctx, "Stale picker (topic mismatch)", fallback_to_current=True
    )
    if not ok:
        return
    user_data = ctx.context.user_data
    selected_path = ctx.deps._creation_path(user_data, "_selected_path")
    if selected_path is None:
        await ctx.deps._reject_expired_project_selection(ctx.query, ctx.context)
        return
    selected_node_id = user_data.get("_selected_node_id", "") if user_data else ""
    profile = ctx.deps._profile_from_context(user_data)
    clear_session_picker_state(user_data)
    if user_data is not None:
        user_data.pop("_selected_path", None)
        user_data.pop("_selected_backend_id", None)
        user_data.pop("_selected_node_id", None)

    create_kwargs: dict[str, Any] = {
        "agent_type": profile.agent_type,
        "model": profile.model,
        "reasoning_effort": profile.reasoning_effort,
        "fast_mode": profile.fast_mode,
    }
    if selected_node_id:
        create_kwargs["node_id"] = selected_node_id
    await ctx.deps._create_and_bind_window(
        ctx.query,
        ctx.context,
        ctx.user,
        selected_path,
        pending_tid,
        **create_kwargs,
    )


async def _handle_session_cancel(ctx: CallbackContext) -> None:
    ok, _ = await _ensure_matching_topic(ctx, "Stale picker (topic mismatch)")
    if not ok:
        return
    user_data = ctx.context.user_data
    clear_session_picker_state(user_data)
    if user_data is not None:
        ctx.deps._clear_pending_thread_input(user_data)
        user_data.pop("_selected_path", None)
        user_data.pop("_selected_backend_id", None)
        user_data.pop("_selected_node_id", None)
        user_data.pop(PROFILE_AGENT_KEY, None)
        user_data.pop(PROFILE_MODEL_KEY, None)
        user_data.pop(PROFILE_EFFORT_KEY, None)
        user_data.pop(PROFILE_FAST_MODE_KEY, None)
        user_data.pop(PROFILE_PERMISSION_MODE_KEY, None)
        user_data.pop(PROFILE_MODELS_KEY, None)
    await ctx.deps.safe_edit(ctx.query, "Cancelled")
    await ctx.query.answer("Cancelled")


# --- Window Picker ---


async def _handle_window_bind(ctx: CallbackContext) -> None:
    ok, _ = await _ensure_matching_topic(ctx, "Stale picker (topic mismatch)")
    if not ok:
        return
    try:
        idx = int(ctx.data[len(CB_WIN_BIND) :])
    except ValueError:
        await ctx.query.answer("Invalid data")
        return

    user_data = ctx.context.user_data
    cached_windows: list[str] = (
        user_data.get(UNBOUND_WINDOWS_KEY, []) if user_data else []
    )
    if idx < 0 or idx >= len(cached_windows):
        await ctx.query.answer("Window list changed, please retry", show_alert=True)
        return
    selected_wid = cached_windows[idx]

    w = await ctx.deps.tmux_manager.find_window_by_id(selected_wid)
    if not w:
        display = ctx.deps.session_manager.get_display_name(selected_wid)
        await ctx.query.answer(f"Window '{display}' no longer exists", show_alert=True)
        return
    if not ctx.deps._has_trackable_session_for_window(selected_wid):
        await ctx.query.answer(
            "This window has no tracked agent session yet. Please choose New Session instead.",
            show_alert=True,
        )
        return

    thread_id = ctx.deps._get_thread_id(ctx.update)
    if thread_id is None:
        await ctx.query.answer("Not in a topic", show_alert=True)
        return

    display = w.window_name
    clear_window_picker_state(user_data)
    ctx.deps.session_manager.bind_thread(
        ctx.user.id, thread_id, selected_wid, window_name=display
    )

    resolved_chat = ctx.deps.session_manager.resolve_chat_id(ctx.user.id, thread_id)
    await ctx.deps.safe_edit(ctx.query, f"✅ Bound to window `{display}`")

    pending_text, pending_error = await ctx.deps._take_pending_thread_message(
        ctx.context,
        user_id=ctx.user.id,
        thread_id=thread_id,
        remote=False,
    )
    if user_data is not None:
        user_data.pop("_pending_thread_id", None)
    if pending_error:
        await ctx.deps.safe_send(
            ctx.context.bot,
            resolved_chat,
            f"❌ Failed to send pending attachment: {pending_error}",
            message_thread_id=thread_id,
        )
    elif pending_text:
        send_ok, send_msg, queued = await ctx.deps._send_or_queue_agent_input(
            ctx.context.bot,
            ctx.user.id,
            thread_id,
            selected_wid,
            pending_text,
        )
        if send_ok and not queued:
            await ctx.deps.mark_window_working(
                ctx.context.bot,
                ctx.user.id,
                selected_wid,
                thread_id,
            )
        elif queued:
            await ctx.deps.safe_send(
                ctx.context.bot,
                resolved_chat,
                f"⏳ {send_msg}",
                message_thread_id=thread_id,
            )
        else:
            logger.warning("Failed to forward pending text: %s", send_msg)
            await ctx.deps.safe_send(
                ctx.context.bot,
                resolved_chat,
                f"❌ Failed to send pending message: {send_msg}",
                message_thread_id=thread_id,
            )
    await ctx.query.answer("Bound")


async def _handle_window_new(ctx: CallbackContext) -> None:
    ok, _ = await _ensure_matching_topic(ctx, "Stale picker (topic mismatch)")
    if not ok:
        return
    clear_window_picker_state(ctx.context.user_data)
    await ctx.deps._show_root_or_directory_picker(ctx.query, ctx.context, edit=True)
    await ctx.query.answer()


async def _handle_window_cancel(ctx: CallbackContext) -> None:
    ok, _ = await _ensure_matching_topic(ctx, "Stale picker (topic mismatch)")
    if not ok:
        return
    clear_window_picker_state(ctx.context.user_data)
    if ctx.context.user_data is not None:
        ctx.deps._clear_pending_thread_input(ctx.context.user_data)
    await ctx.deps.safe_edit(ctx.query, "Cancelled")
    await ctx.query.answer("Cancelled")


# --- Screenshot, Quick Keys & Interactive UI ---


async def _handle_screenshot_refresh(ctx: CallbackContext) -> None:
    window_id = ctx.data[len(CB_SCREENSHOT_REFRESH) :]
    capture = await ctx.deps.capture_agent_output(
        ctx.user.id,
        ctx.thread_id,
        window_id,
        with_ansi=True,
    )
    if capture is None or capture.missing:
        await ctx.query.answer("Window no longer exists", show_alert=True)
        return

    text = capture.text
    if not text:
        await ctx.query.answer("Failed to capture pane", show_alert=True)
        return

    png_bytes = await ctx.deps.text_to_image(text, with_ansi=True)
    keyboard = ctx.deps._build_screenshot_keyboard(window_id)
    try:
        await ctx.query.edit_message_media(
            media=InputMediaDocument(
                media=io.BytesIO(png_bytes), filename="screenshot.png"
            ),
            reply_markup=keyboard,
        )
        await ctx.query.answer("Refreshed")
    except Exception as e:
        logger.error("Failed to refresh screenshot: %s", e)
        await ctx.query.answer("Failed to refresh", show_alert=True)


async def _handle_noop(ctx: CallbackContext) -> None:
    await ctx.query.answer()


_ASK_REFRESH_ACTIONS: dict[str, tuple[str, str | None]] = {
    CB_ASK_UP: ("Up", None),
    CB_ASK_DOWN: ("Down", None),
    CB_ASK_LEFT: ("Left", None),
    CB_ASK_RIGHT: ("Right", None),
    CB_ASK_ENTER: ("Enter", "⏎ Enter"),
    CB_ASK_SPACE: ("Space", "␣ Space"),
    CB_ASK_TAB: ("Tab", "⇥ Tab"),
    CB_ASK_TRUST: ("t", "Trusted hooks"),
}


async def _handle_ask_control(ctx: CallbackContext) -> None:
    for prefix, (tmux_key, toast) in _ASK_REFRESH_ACTIONS.items():
        if not ctx.data.startswith(prefix):
            continue
        window_id = ctx.data[len(prefix) :]
        thread_id = ctx.deps._get_thread_id(ctx.update)
        ok, message = await ctx.deps._send_control_to_agent(
            ctx.user.id, thread_id, window_id, tmux_key
        )
        if ok:
            await ctx.deps.asyncio.sleep(0.5)
            await ctx.deps.handle_interactive_ui(
                ctx.context.bot, ctx.user.id, window_id, thread_id
            )
        elif message:
            await ctx.query.answer(message, show_alert=True)
            return
        await ctx.query.answer(toast) if toast is not None else await ctx.query.answer()
        return


async def _handle_ask_esc(ctx: CallbackContext) -> None:
    window_id = ctx.data[len(CB_ASK_ESC) :]
    thread_id = ctx.deps._get_thread_id(ctx.update)
    ok, message = await ctx.deps._send_control_to_agent(
        ctx.user.id, thread_id, window_id, "Escape"
    )
    if ok:
        await ctx.deps.clear_interactive_msg(ctx.user.id, ctx.context.bot, thread_id)
    elif message:
        await ctx.query.answer(message, show_alert=True)
        return
    await ctx.query.answer("⎋ Esc")


async def _handle_ask_refresh(ctx: CallbackContext) -> None:
    window_id = ctx.data[len(CB_ASK_REFRESH) :]
    thread_id = ctx.deps._get_thread_id(ctx.update)
    await ctx.deps.handle_interactive_ui(
        ctx.context.bot, ctx.user.id, window_id, thread_id
    )
    await ctx.query.answer("🔄")


async def _handle_quick_keys(ctx: CallbackContext) -> None:
    rest = ctx.data[len(CB_KEYS_PREFIX) :]
    colon_idx = rest.find(":")
    if colon_idx < 0:
        await ctx.query.answer("Invalid data")
        return
    key_id = rest[:colon_idx]
    window_id = rest[colon_idx + 1 :]

    key_info = ctx.deps._KEYS_SEND_MAP.get(key_id)
    if not key_info:
        await ctx.query.answer("Unknown key")
        return

    tmux_key, _enter, _literal = key_info
    ok, message = await ctx.deps._send_control_to_agent(
        ctx.user.id, ctx.thread_id, window_id, tmux_key
    )
    if not ok:
        await ctx.query.answer(message or "Failed to send key", show_alert=True)
        return

    await ctx.query.answer(ctx.deps._KEY_LABELS.get(key_id, key_id))

    await ctx.deps.asyncio.sleep(0.5)
    capture = await ctx.deps.capture_agent_output(
        ctx.user.id,
        ctx.thread_id,
        window_id,
        with_ansi=True,
    )
    text = capture.text if capture and not capture.missing else None
    if text:
        png_bytes = await ctx.deps.text_to_image(text, with_ansi=True)
        keyboard = ctx.deps._build_screenshot_keyboard(window_id)
        try:
            await ctx.query.edit_message_media(
                media=InputMediaDocument(
                    media=io.BytesIO(png_bytes),
                    filename="screenshot.png",
                ),
                reply_markup=keyboard,
            )
        except Exception:
            pass  # Screenshot unchanged or message too old


def build_default_callback_registry() -> CallbackRegistry:
    """Create the default callback registry across all bot interaction domains."""
    registry = CallbackRegistry()

    registry.register_exact(CB_CODEX_UPDATE_APPLY, _handle_codex_update_apply)
    registry.register_exact(CB_CODEX_UPDATE_DISMISS, _handle_codex_update_dismiss)
    registry.register_prefix(CB_INPUT_ROUTE, _handle_input_route)

    registry.register_prefix(CB_HISTORY_PREV, _handle_history_page)
    registry.register_prefix(CB_HISTORY_NEXT, _handle_history_page)
    registry.register_prefix(CB_OUTPUT_MODE, _handle_output_mode)

    registry.register_prefix(CB_ROOT_SELECT, _handle_root_select)
    registry.register_exact(CB_ROOT_CANCEL, _handle_root_cancel)

    registry.register_prefix(CB_PROFILE_AGENT, _handle_profile_agent)
    registry.register_prefix(CB_PROFILE_MODEL, _handle_profile_model)
    registry.register_prefix(CB_PROFILE_EFFORT, _handle_profile_effort)
    registry.register_prefix(CB_PROFILE_FAST, _handle_profile_fast)
    registry.register_prefix(CB_PROFILE_PERMISSION, _handle_profile_permission)
    registry.register_exact(CB_PROFILE_CONFIRM, _handle_profile_confirm)
    registry.register_exact(CB_PROFILE_CANCEL, _handle_profile_cancel)

    registry.register_prefix(CB_DIR_SELECT, _handle_dir_select)
    registry.register_exact(CB_DIR_UP, _handle_dir_up)
    registry.register_prefix(CB_DIR_PAGE, _handle_dir_page)
    registry.register_exact(CB_DIR_CONFIRM, _handle_dir_confirm)
    registry.register_exact(CB_DIR_CANCEL, _handle_dir_cancel)

    registry.register_prefix(CB_SESSION_SELECT, _handle_session_select)
    registry.register_exact(CB_SESSION_NEW, _handle_session_new)
    registry.register_exact(CB_SESSION_CANCEL, _handle_session_cancel)

    registry.register_prefix(CB_WIN_BIND, _handle_window_bind)
    registry.register_exact(CB_WIN_NEW, _handle_window_new)
    registry.register_exact(CB_WIN_CANCEL, _handle_window_cancel)

    registry.register_prefix(CB_SCREENSHOT_REFRESH, _handle_screenshot_refresh)
    registry.register_exact("noop", _handle_noop)
    for prefix in _ASK_REFRESH_ACTIONS:
        registry.register_prefix(prefix, _handle_ask_control)
    registry.register_prefix(CB_ASK_ESC, _handle_ask_esc)
    registry.register_prefix(CB_ASK_REFRESH, _handle_ask_refresh)
    registry.register_prefix(CB_KEYS_PREFIX, _handle_quick_keys)

    return registry


DEFAULT_CALLBACK_REGISTRY = build_default_callback_registry()
