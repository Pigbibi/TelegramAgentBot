"""Forward-command normalization and remote/local directory routing helpers."""

from __future__ import annotations

from collections.abc import Collection
import logging
from pathlib import Path
import posixpath
import re
from typing import Any, TypedDict, cast

from telegram import InlineKeyboardMarkup

from .agent_profile import agent_capabilities, normalize_agent_type
from .backends.base import AgentBackend
from .backends.browser import AgentBrowser, DirectoryListing
from .backends.registry import get_configured_backend
from .config import config as default_config
from .handlers.directory_browser import (
    BROWSE_BACKEND_ID_KEY,
    BROWSE_NODE_ID_KEY,
    BROWSE_ROOT_LABEL_KEY,
    BROWSE_ROOT_PATH_KEY,
    build_directory_browser_from_listing,
)

logger = logging.getLogger(__name__)

FORWARDED_COMMAND_RE = re.compile(
    r"^/(?P<name>[A-Za-z0-9_]+)(?:@[A-Za-z0-9_]+)?(?P<rest>.*)$"
)
COMMAND_ARG_SEPARATORS = ".。"


class DirectoryBrowserKwargs(TypedDict, total=False):
    root_label: str
    root_path: str


class RootSelection(TypedDict):
    label: str
    path: str
    backend_id: str
    node_id: str


def normalize_forward_command_text(
    cmd_text: str,
    *,
    known_commands: Collection[str] = (),
) -> str:
    """Normalize Telegram slash commands before forwarding them to an agent."""
    match = FORWARDED_COMMAND_RE.match(cmd_text)
    if not match:
        return cmd_text

    command_name = match.group("name")
    rest = match.group("rest") or ""
    normalized = f"/{command_name}"

    # Telegram users sometimes type "/goal.<arg>" from CJK IME input. Codex
    # expects a space between a known slash command and its argument; without it
    # the TUI can leave the text pending and the bot reports a low-level send
    # failure. Keep this limited to known commands so arbitrary forwarded
    # commands keep their original spelling.
    if (
        command_name.lower() in known_commands
        and rest
        and rest[0] in COMMAND_ARG_SEPARATORS
    ):
        rest = " " + rest[1:].lstrip()

    return normalized + rest


def adapt_forward_command_for_agent(
    cmd_text: str,
    agent_type: str,
    *,
    known_commands: Collection[str] = (),
) -> str:
    """Map one stable Telegram command to the active agent's native spelling."""
    normalized = normalize_forward_command_text(
        cmd_text,
        known_commands=known_commands,
    )
    match = FORWARDED_COMMAND_RE.match(normalized)
    if not match:
        return normalized

    command_name = match.group("name").lower()
    rest = match.group("rest") or ""
    normalized_agent_type = normalize_agent_type(agent_type)
    if command_name in {"plugin", "plugins"}:
        native_name = agent_capabilities(normalized_agent_type).plugin_command
        return f"/{native_name}{rest}"
    return normalized


def default_directory_browser_path(
    root_path: str | None = None,
    *,
    default_projects_path: Path | None = None,
) -> str:
    """Choose a generic starting directory for the project browser."""
    if root_path:
        root = Path(root_path).expanduser()
        if root.is_dir():
            return str(root)
    base_projects_path = (
        default_projects_path
        if default_projects_path is not None
        else default_config.default_projects_path
    )
    projects_dir = base_projects_path.expanduser()
    if projects_dir.is_dir():
        return str(projects_dir)
    return str(Path.home())


def browse_root_context(
    user_data: dict[Any, Any] | None,
) -> tuple[str | None, str | None]:
    """Return selected browser root label/path from user state."""
    if not user_data:
        return None, None
    label = user_data.get(BROWSE_ROOT_LABEL_KEY)
    path = user_data.get(BROWSE_ROOT_PATH_KEY)
    return (
        label if isinstance(label, str) and label else None,
        path if isinstance(path, str) and path else None,
    )


def directory_browser_kwargs(
    user_data: dict[Any, Any] | None,
) -> DirectoryBrowserKwargs:
    """Build root kwargs for the directory browser from user state."""
    label, path = browse_root_context(user_data)
    kwargs: DirectoryBrowserKwargs = {}
    if label:
        kwargs["root_label"] = label
    if path:
        kwargs["root_path"] = path
    return kwargs


def browse_backend_context(user_data: dict[Any, Any] | None) -> tuple[str, str]:
    """Return selected backend/node for a remote directory browser."""
    if not user_data:
        return "", ""
    backend_id = user_data.get(BROWSE_BACKEND_ID_KEY)
    node_id = user_data.get(BROWSE_NODE_ID_KEY)
    return (
        backend_id if isinstance(backend_id, str) else "",
        node_id if isinstance(node_id, str) else "",
    )


def as_browser(backend: AgentBackend | None) -> AgentBrowser | None:
    """Return backend as AgentBrowser when it exposes the optional methods."""
    if backend is None:
        return None
    required = ("list_roots", "list_directory", "list_sessions")
    if not all(callable(getattr(backend, attr, None)) for attr in required):
        return None
    return cast(AgentBrowser, backend)


def active_remote_root_browser(backend: AgentBackend | None) -> AgentBrowser | None:
    """Return the configured non-local backend browser for root selection."""
    if backend is None:
        return None
    try:
        info = backend.info()
    except Exception:
        logger.exception("Unable to read backend info for root browser")
        return None
    if backend.backend_id == "local" or info.mode == "local":
        return None
    return as_browser(backend)


def browser_for_backend_id(
    backend_id: str,
    backend: AgentBackend | None = None,
) -> AgentBrowser | None:
    """Return the active backend browser matching a cached root selection."""
    if not backend_id:
        return None
    active = backend if backend is not None else get_configured_backend()
    if active.backend_id != backend_id:
        return None
    return as_browser(active)


def parse_root_selection(entry: object) -> RootSelection | None:
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


def remote_child_path(current_path: str, subdir_name: str) -> str:
    """Join one path segment for a backend node using POSIX-like paths."""
    current = current_path.rstrip("/") or "/"
    return posixpath.normpath(posixpath.join(current, subdir_name))


def remote_parent_path(current_path: str, root_path: str | None) -> str:
    """Move one level up for a backend node while respecting selected root."""
    current = current_path.rstrip("/") or "/"
    root = root_path.rstrip("/") if root_path else ""
    if root and current == root:
        return root_path or root

    parent = posixpath.dirname(current) or "/"
    if root and not (parent == root or parent.startswith(root + "/")):
        return root_path or root
    return parent


def directory_listing_with_root(
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


async def build_backend_directory_browser(
    *,
    backend_id: str,
    node_id: str,
    path: str,
    page: int = 0,
    root_label: str | None = None,
    root_path: str | None = None,
    backend: AgentBackend | None = None,
    browser: AgentBrowser | None = None,
) -> tuple[str, InlineKeyboardMarkup, list[str], str] | None:
    """Build directory browser UI through an optional remote backend."""
    active_browser = (
        browser if browser is not None else browser_for_backend_id(backend_id, backend)
    )
    if active_browser is None:
        return None
    try:
        listing = await active_browser.list_directory(
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

    listing = directory_listing_with_root(
        listing,
        root_label=root_label,
        root_path=root_path,
    )
    text, keyboard, subdirs = build_directory_browser_from_listing(listing, page=page)
    return text, keyboard, subdirs, listing.path


def clamp_to_selected_root(path: str, user_data: dict[Any, Any] | None) -> str:
    """Keep selected paths inside the configured project root when present."""
    _label, root_path = browse_root_context(user_data)
    if not root_path:
        return path

    root = Path(root_path).expanduser().resolve()
    selected = Path(path).expanduser().resolve()
    if selected == root or root in selected.parents:
        return str(selected)
    return str(root)
