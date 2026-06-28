"""JSON protocol helpers for the socket backend plugin."""

from __future__ import annotations

import base64
from typing import Any

from telegram_agent_bot.backends.base import AgentTarget
from telegram_agent_bot.backends.browser import BrowserRoot, DirectoryListing
from telegram_agent_bot.session import CodexSession
from telegram_agent_bot.session_monitor import NewMessage


def target_session_id(node_id: str, window_id: str) -> str:
    """Return the center-visible session key for a node/window pair."""
    return f"{node_id}:{window_id}" if node_id and window_id else ""


def target_to_dict(target: AgentTarget) -> dict[str, str]:
    """Serialize an AgentTarget into JSON-safe values."""
    return {
        "backend_id": target.backend_id,
        "node_id": target.node_id,
        "session_id": target.session_id,
        "window_id": target.window_id,
    }


def target_from_dict(data: dict[str, Any]) -> AgentTarget:
    """Deserialize an AgentTarget from JSON values."""
    return AgentTarget(
        backend_id=str(data.get("backend_id", "")),
        node_id=str(data.get("node_id", "")),
        session_id=str(data.get("session_id", "")),
        window_id=str(data.get("window_id", "")),
    )


def root_to_dict(root: BrowserRoot) -> dict[str, str]:
    """Serialize a BrowserRoot."""
    return {
        "label": root.label,
        "path": root.path,
        "backend_id": root.backend_id,
        "node_id": root.node_id,
    }


def root_from_dict(data: dict[str, Any]) -> BrowserRoot:
    """Deserialize a BrowserRoot."""
    return BrowserRoot(
        label=str(data.get("label", "")),
        path=str(data.get("path", "")),
        backend_id=str(data.get("backend_id", "")),
        node_id=str(data.get("node_id", "")),
    )


def listing_to_dict(listing: DirectoryListing) -> dict[str, Any]:
    """Serialize a DirectoryListing."""
    return {
        "path": listing.path,
        "subdirs": listing.subdirs,
        "root_label": listing.root_label,
        "root_path": listing.root_path,
        "can_go_up": listing.can_go_up,
        "error": listing.error,
    }


def listing_from_dict(data: dict[str, Any]) -> DirectoryListing:
    """Deserialize a DirectoryListing."""
    raw_subdirs = data.get("subdirs", [])
    subdirs = (
        [str(item) for item in raw_subdirs] if isinstance(raw_subdirs, list) else []
    )
    return DirectoryListing(
        path=str(data.get("path", "")),
        subdirs=subdirs,
        root_label=str(data.get("root_label", "")),
        root_path=str(data.get("root_path", "")),
        can_go_up=bool(data.get("can_go_up", True)),
        error=str(data.get("error", "")),
    )


def session_to_dict(session: CodexSession) -> dict[str, Any]:
    """Serialize a CodexSession."""
    return {
        "session_id": session.session_id,
        "summary": session.summary,
        "message_count": session.message_count,
        "file_path": session.file_path,
    }


def session_from_dict(data: dict[str, Any]) -> CodexSession:
    """Deserialize a CodexSession."""
    return CodexSession(
        session_id=str(data.get("session_id", "")),
        summary=str(data.get("summary", "")),
        message_count=int(data.get("message_count", 0) or 0),
        file_path=str(data.get("file_path", "")),
    )


def message_to_dict(message: NewMessage) -> dict[str, Any]:
    """Serialize a NewMessage, including optional image bytes."""
    images = None
    if message.image_data:
        images = [
            (name, base64.b64encode(data).decode("ascii"))
            for name, data in message.image_data
        ]
    return {
        "session_id": message.session_id,
        "text": message.text,
        "is_complete": message.is_complete,
        "content_type": message.content_type,
        "tool_use_id": message.tool_use_id,
        "role": message.role,
        "tool_name": message.tool_name,
        "image_data": images,
    }


def message_from_dict(data: dict[str, Any]) -> NewMessage:
    """Deserialize a NewMessage."""
    image_data = None
    raw_images = data.get("image_data")
    if isinstance(raw_images, list):
        image_data = []
        for item in raw_images:
            if (
                isinstance(item, (list, tuple))
                and len(item) == 2
                and isinstance(item[0], str)
                and isinstance(item[1], str)
            ):
                image_data.append((item[0], base64.b64decode(item[1])))
    return NewMessage(
        session_id=str(data.get("session_id", "")),
        text=str(data.get("text", "")),
        is_complete=bool(data.get("is_complete", False)),
        content_type=str(data.get("content_type", "text")),
        tool_use_id=data.get("tool_use_id"),
        role=str(data.get("role", "assistant")),
        tool_name=data.get("tool_name"),
        image_data=image_data,
    )
