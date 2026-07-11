"""Directory browser and window picker UI for session creation.

Provides UIs in Telegram for:
  - Window picker: list unbound tmux windows for quick binding
  - Project root picker: choose a named local/mounted computer or VPS root
  - Directory browser: navigate directory hierarchies to create new sessions

Key components:
  - DIRS_PER_PAGE: Number of directories shown per page
  - User state keys for tracking browse/picker session
  - build_window_picker: Build unbound window picker UI
  - build_directory_browser: Build directory browser UI
  - clear_window_picker_state: Clear picker state from user_data
  - clear_browse_state: Clear browsing state from user_data
"""

import os
import time
from collections.abc import Sequence
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from ..backends.browser import BrowserRoot, DirectoryListing
from ..agent_profile import (
    AGENT_CLAUDE,
    AGENT_CODEX,
    AgentProfile,
    EFFORT_DEEP,
    EFFORT_LOW,
    EFFORT_MAX,
    EFFORT_STANDARD,
    agent_display_name,
)
from ..session import CodexSession

from ..config import ProjectRoot, config
from .callback_data import (
    CB_DIR_CANCEL,
    CB_DIR_CONFIRM,
    CB_DIR_PAGE,
    CB_DIR_SELECT,
    CB_DIR_UP,
    CB_ROOT_CANCEL,
    CB_ROOT_SELECT,
    CB_SESSION_CANCEL,
    CB_SESSION_NEW,
    CB_SESSION_SELECT,
    CB_PROFILE_AGENT,
    CB_PROFILE_CANCEL,
    CB_PROFILE_CONFIRM,
    CB_PROFILE_EFFORT,
    CB_PROFILE_FAST,
    CB_PROFILE_MODEL,
    CB_WIN_BIND,
    CB_WIN_CANCEL,
    CB_WIN_NEW,
)

# Directories per page in directory browser
DIRS_PER_PAGE = 6

# User state keys
STATE_KEY = "state"
STATE_SELECTING_ROOT = "selecting_root"
STATE_BROWSING_DIRECTORY = "browsing_directory"
STATE_SELECTING_WINDOW = "selecting_window"
ROOTS_KEY = "project_roots"  # Cache of root tuples
BROWSE_PATH_KEY = "browse_path"
BROWSE_ROOT_LABEL_KEY = "browse_root_label"
BROWSE_ROOT_PATH_KEY = "browse_root_path"
BROWSE_BACKEND_ID_KEY = "browse_backend_id"
BROWSE_NODE_ID_KEY = "browse_node_id"
BROWSE_PAGE_KEY = "browse_page"
BROWSE_DIRS_KEY = "browse_dirs"  # Cache of subdirs for current path
UNBOUND_WINDOWS_KEY = "unbound_windows"  # Cache of (name, cwd) tuples
STATE_SELECTING_SESSION = "selecting_session"
STATE_SELECTING_AGENT = "selecting_agent"
STATE_SELECTING_PROFILE = "selecting_profile"
SESSIONS_KEY = "cached_sessions"  # Cache of CodexSession list
PROFILE_AGENT_KEY = "profile_agent"
PROFILE_MODEL_KEY = "profile_model"
PROFILE_EFFORT_KEY = "profile_effort"
PROFILE_FAST_MODE_KEY = "profile_fast_mode"
PROFILE_MODELS_KEY = "profile_models"


def clear_browse_state(user_data: dict | None) -> None:
    """Clear directory browsing state keys from user_data."""
    if user_data is not None:
        user_data.pop(STATE_KEY, None)
        user_data.pop(ROOTS_KEY, None)
        user_data.pop(BROWSE_PATH_KEY, None)
        user_data.pop(BROWSE_ROOT_LABEL_KEY, None)
        user_data.pop(BROWSE_ROOT_PATH_KEY, None)
        user_data.pop(BROWSE_BACKEND_ID_KEY, None)
        user_data.pop(BROWSE_NODE_ID_KEY, None)
        user_data.pop(BROWSE_PAGE_KEY, None)
        user_data.pop(BROWSE_DIRS_KEY, None)


def clear_root_picker_state(user_data: dict | None) -> None:
    """Clear project root picker state keys from user_data."""
    if user_data is not None:
        user_data.pop(STATE_KEY, None)
        user_data.pop(ROOTS_KEY, None)


def clear_window_picker_state(user_data: dict | None) -> None:
    """Clear window picker state keys from user_data."""
    if user_data is not None:
        user_data.pop(STATE_KEY, None)
        user_data.pop(UNBOUND_WINDOWS_KEY, None)


def clear_session_picker_state(user_data: dict | None) -> None:
    """Clear session picker state keys from user_data."""
    if user_data is not None:
        user_data.pop(STATE_KEY, None)
        user_data.pop(SESSIONS_KEY, None)


def clear_profile_picker_state(user_data: dict | None) -> None:
    """Clear per-topic agent profile selection state."""
    if user_data is not None:
        user_data.pop(STATE_KEY, None)
        for key in (
            PROFILE_AGENT_KEY,
            PROFILE_MODEL_KEY,
            PROFILE_EFFORT_KEY,
            PROFILE_FAST_MODE_KEY,
            PROFILE_MODELS_KEY,
        ):
            user_data.pop(key, None)


def build_agent_picker() -> tuple[str, InlineKeyboardMarkup]:
    """Build the first step of the per-topic agent picker."""
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "🟢 Codex", callback_data=f"{CB_PROFILE_AGENT}{AGENT_CODEX}"
                ),
                InlineKeyboardButton(
                    "🟣 Claude Code",
                    callback_data=f"{CB_PROFILE_AGENT}{AGENT_CLAUDE}",
                ),
            ],
            [InlineKeyboardButton("Cancel", callback_data=CB_PROFILE_CANCEL)],
        ]
    )
    return "*Choose Agent*\n\nSelect the AI runtime for this Telegram topic.", keyboard


def build_profile_picker(
    profile: AgentProfile,
    models: Sequence[str],
) -> tuple[str, InlineKeyboardMarkup]:
    """Build model and reasoning-effort controls for a selected agent."""
    model_label = profile.model or "CLI default"
    lines = [
        f"*{agent_display_name(profile.agent_type)} settings*",
        f"Model: `{model_label}`",
        f"Reasoning: `{profile.effort_label}`",
        (
            f"Fast mode: `{profile.fast_label}`"
            if profile.agent_type == AGENT_CLAUDE
            else "Fast mode: `Not available for Codex CLI`"
        ),
        "\nChoose model and reasoning, then create:",
    ]
    buttons: list[list[InlineKeyboardButton]] = []
    for index, model in enumerate(models):
        label = model[:22] + "…" if len(model) > 23 else model
        prefix = "✅ " if model == profile.model else ""
        buttons.append(
            [
                InlineKeyboardButton(
                    f"{prefix}{label}", callback_data=f"{CB_PROFILE_MODEL}{index}"
                )
            ]
        )

    effort_options = (
        (EFFORT_LOW, "Low"),
        (EFFORT_STANDARD, "Standard"),
        (EFFORT_DEEP, "Deep"),
        (EFFORT_MAX, "Max"),
    )
    for index in range(0, len(effort_options), 2):
        buttons.append(
            [
                InlineKeyboardButton(
                    f"{'✅ ' if profile.reasoning_effort == value else ''}{label}",
                    callback_data=f"{CB_PROFILE_EFFORT}{value}",
                )
                for value, label in effort_options[index : index + 2]
            ]
        )
    if profile.agent_type == AGENT_CLAUDE:
        buttons.append(
            [
                InlineKeyboardButton(
                    f"⚡ Fast: {profile.fast_label}",
                    callback_data=f"{CB_PROFILE_FAST}{'off' if profile.fast_mode else 'on'}",
                )
            ]
        )
    buttons.append(
        [InlineKeyboardButton("✅ Create session", callback_data=CB_PROFILE_CONFIRM)]
    )
    buttons.append([InlineKeyboardButton("Cancel", callback_data=CB_PROFILE_CANCEL)])
    return "\n".join(lines), InlineKeyboardMarkup(buttons)


def build_project_root_picker(
    roots: list[ProjectRoot],
) -> tuple[str, InlineKeyboardMarkup, list[tuple[str, str]]]:
    """Build project root picker UI.

    Args:
        roots: Named root directories configured for the bot.

    Returns: (text, keyboard, cached_roots).
    """
    cached_roots = [(root.label, str(root.path)) for root in roots]
    text, keyboard = _build_root_picker_text_and_keyboard(roots)
    return text, keyboard, cached_roots


def build_backend_root_picker(
    roots: list[BrowserRoot],
) -> tuple[str, InlineKeyboardMarkup, list[tuple[str, str, str, str]]]:
    """Build root picker UI for backend-provided roots."""
    cached_roots = [
        (root.label, root.path, root.backend_id, root.node_id) for root in roots
    ]
    text, keyboard = _build_root_picker_text_and_keyboard(roots)
    return text, keyboard, cached_roots


def _build_root_picker_text_and_keyboard(
    roots: Sequence[ProjectRoot | BrowserRoot],
) -> tuple[str, InlineKeyboardMarkup]:
    """Build common root picker text and buttons."""
    lines = [
        "*Select Computer / VPS*\n",
        "Pick where this new Agent session should start.\n",
    ]
    buttons: list[list[InlineKeyboardButton]] = []
    for i, root in enumerate(roots):
        display_path = str(root.path).replace(str(Path.home()), "~")
        lines.append(f"• `{root.label}` — {display_path}")
        label = root.label[:18] + "…" if len(root.label) > 19 else root.label
        buttons.append(
            [
                InlineKeyboardButton(
                    f"🖥 {label}",
                    callback_data=f"{CB_ROOT_SELECT}{i}",
                )
            ]
        )

    buttons.append([InlineKeyboardButton("Cancel", callback_data=CB_ROOT_CANCEL)])
    return "\n".join(lines), InlineKeyboardMarkup(buttons)


def build_window_picker(
    windows: list[tuple[str, str, str]],
) -> tuple[str, InlineKeyboardMarkup, list[str]]:
    """Build window picker UI for unbound tmux windows.

    Args:
        windows: List of (window_id, window_name, cwd) tuples.

    Returns: (text, keyboard, window_ids) where window_ids is the ordered list for caching.
    """
    window_ids = [wid for wid, _, _ in windows]

    lines = [
        "*Bind to Existing Window*\n",
        "These windows are running but not bound to any topic.",
        "Pick one to attach it here, or start a new session.\n",
    ]
    for _wid, name, cwd in windows:
        display_cwd = cwd.replace(str(Path.home()), "~")
        lines.append(f"• `{name}` — {display_cwd}")

    buttons: list[list[InlineKeyboardButton]] = []
    for i in range(0, len(windows), 2):
        row = []
        for j in range(min(2, len(windows) - i)):
            name = windows[i + j][1]
            display = name[:12] + "…" if len(name) > 13 else name
            row.append(
                InlineKeyboardButton(
                    f"🖥 {display}", callback_data=f"{CB_WIN_BIND}{i + j}"
                )
            )
        buttons.append(row)

    buttons.append(
        [
            InlineKeyboardButton("➕ New Session", callback_data=CB_WIN_NEW),
            InlineKeyboardButton("Cancel", callback_data=CB_WIN_CANCEL),
        ]
    )

    text = "\n".join(lines)
    return text, InlineKeyboardMarkup(buttons), window_ids


def _resolve_browser_paths(
    current_path: str,
    root_path: str | None,
) -> tuple[Path, Path | None]:
    """Resolve and clamp the browser path to an optional configured root."""

    root = Path(root_path).expanduser().resolve() if root_path else None
    path = Path(current_path).expanduser().resolve()

    if root is not None:
        if not root.exists() or not root.is_dir():
            root = None
        elif not (path == root or root in path.parents):
            path = root

    if not path.exists() or not path.is_dir():
        if root is not None:
            path = root
        else:
            path = Path.cwd()

    return path, root


def build_directory_browser(
    current_path: str,
    page: int = 0,
    *,
    root_label: str | None = None,
    root_path: str | None = None,
) -> tuple[str, InlineKeyboardMarkup, list[str]]:
    """Build directory browser UI.

    Returns: (text, keyboard, subdirs) where subdirs is the full list for caching.
    """
    path, root = _resolve_browser_paths(current_path, root_path)

    try:
        subdirs = sorted(
            [
                d.name
                for d in path.iterdir()
                if d.is_dir()
                and (config.show_hidden_dirs or not d.name.startswith("."))
            ]
        )
    except (PermissionError, OSError):
        subdirs = []

    listing = DirectoryListing(
        path=str(path),
        subdirs=subdirs,
        root_label=root_label or "",
        root_path=str(root) if root else root_path or "",
        can_go_up=path != path.parent and (root is None or path != root),
    )
    return build_directory_browser_from_listing(listing, page=page)


def build_directory_browser_from_listing(
    listing: DirectoryListing,
    page: int = 0,
) -> tuple[str, InlineKeyboardMarkup, list[str]]:
    """Build directory browser UI from a backend-provided listing."""

    subdirs = listing.subdirs
    total_pages = max(1, (len(subdirs) + DIRS_PER_PAGE - 1) // DIRS_PER_PAGE)
    page = max(0, min(page, total_pages - 1))
    start = page * DIRS_PER_PAGE
    page_dirs = subdirs[start : start + DIRS_PER_PAGE]

    buttons: list[list[InlineKeyboardButton]] = []
    for i in range(0, len(page_dirs), 2):
        row = []
        for j, name in enumerate(page_dirs[i : i + 2]):
            display = name[:12] + "…" if len(name) > 13 else name
            # Use global index (start + i + j) to avoid long dir names in callback_data
            idx = start + i + j
            row.append(
                InlineKeyboardButton(
                    f"📁 {display}", callback_data=f"{CB_DIR_SELECT}{idx}"
                )
            )
        buttons.append(row)

    if total_pages > 1:
        nav: list[InlineKeyboardButton] = []
        if page > 0:
            nav.append(
                InlineKeyboardButton("◀", callback_data=f"{CB_DIR_PAGE}{page - 1}")
            )
        nav.append(
            InlineKeyboardButton(f"{page + 1}/{total_pages}", callback_data="noop")
        )
        if page < total_pages - 1:
            nav.append(
                InlineKeyboardButton("▶", callback_data=f"{CB_DIR_PAGE}{page + 1}")
            )
        buttons.append(nav)

    action_row: list[InlineKeyboardButton] = []
    if listing.can_go_up:
        action_row.append(InlineKeyboardButton("..", callback_data=CB_DIR_UP))
    action_row.append(InlineKeyboardButton("Select", callback_data=CB_DIR_CONFIRM))
    action_row.append(InlineKeyboardButton("Cancel", callback_data=CB_DIR_CANCEL))
    buttons.append(action_row)

    display_path = listing.path.replace(str(Path.home()), "~")
    root_line = ""
    if listing.root_label:
        display_root = listing.root_path.replace(str(Path.home()), "~")
        root_line = f"Root: `{listing.root_label}`"
        if display_root:
            root_line += f" — {display_root}"
        root_line += "\n"
    if not subdirs:
        text = (
            f"*Select Working Directory*\n\n"
            f"{root_line}"
            f"Current: `{display_path}`\n\n_(No subdirectories)_"
        )
    else:
        text = (
            f"*Select Working Directory*\n\n"
            f"{root_line}"
            f"Current: `{display_path}`\n\n"
            "Tap a folder to enter, or select current directory"
        )

    return text, InlineKeyboardMarkup(buttons), subdirs


def _relative_time(file_path: str) -> str:
    """Format file mtime as a human-readable relative time string."""
    try:
        mtime = os.path.getmtime(file_path)
    except OSError:
        return ""
    delta = int(time.time() - mtime)
    if delta < 60:
        return "just now"
    if delta < 3600:
        m = delta // 60
        return f"{m}m ago"
    if delta < 86400:
        h = delta // 3600
        return f"{h}h ago"
    d = delta // 86400
    return f"{d}d ago"


def build_session_picker(
    sessions: list[CodexSession],
) -> tuple[str, InlineKeyboardMarkup]:
    """Build session picker UI for resuming an existing Codex session.

    Args:
        sessions: List of CodexSession objects (sorted by recency).

    Returns: (text, keyboard).
    """
    lines = [
        "*Resume Session?*\n",
        "Existing sessions found in this directory.\n",
    ]
    for i, s in enumerate(sessions):
        summary = s.summary[:40] + "…" if len(s.summary) > 40 else s.summary
        rel = _relative_time(s.file_path)
        time_str = f" ({rel})" if rel else ""
        if s.message_count > 0:
            lines.append(f"{i + 1}. {summary} — {s.message_count} msgs{time_str}")
        else:
            lines.append(f"{i + 1}. {summary}{time_str}")

    buttons: list[list[InlineKeyboardButton]] = []
    for i in range(0, len(sessions), 2):
        row = []
        for j in range(min(2, len(sessions) - i)):
            s = sessions[i + j]
            label = s.summary[:14] + "…" if len(s.summary) > 14 else s.summary
            row.append(
                InlineKeyboardButton(
                    f"▶ {label}", callback_data=f"{CB_SESSION_SELECT}{i + j}"
                )
            )
        buttons.append(row)

    buttons.append(
        [
            InlineKeyboardButton("➕ New Session", callback_data=CB_SESSION_NEW),
            InlineKeyboardButton("Cancel", callback_data=CB_SESSION_CANCEL),
        ]
    )

    text = "\n".join(lines)
    return text, InlineKeyboardMarkup(buttons)
