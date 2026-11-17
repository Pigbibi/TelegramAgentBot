"""Pure session-file and session-map persistence helpers.

Handles serialization, deserialization, and locked atomic updates for
``state.json`` and ``session_map.json`` without holding ``SessionManager``
runtime state.
"""

from __future__ import annotations

import fcntl
import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import aiofiles

from .agent_profile import normalize_agent_type
from .backends.base import AgentTarget
from .output_mode import normalize_output_mode
from .utils import atomic_write_json

logger = logging.getLogger(__name__)

_UUID_SUFFIX_RE = re.compile(
    r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$"
)


def is_window_id(key: str) -> bool:
    """Return True when key looks like a tmux window ID (e.g. '@0', '@12')."""
    return key.startswith("@") and len(key) > 1 and key[1:].isdigit()


def canonical_session_id(session_id: str) -> str:
    """Normalize bare UUID and rollout-prefixed ids to one comparable form."""
    if not session_id:
        return ""
    match = _UUID_SUFFIX_RE.search(session_id)
    if match:
        return match.group(1)
    return session_id


def session_ids_match(left: str, right: str) -> bool:
    """Return whether two session ids refer to the same Codex session."""
    return bool(
        left and right and canonical_session_id(left) == canonical_session_id(right)
    )


def target_to_dict(target: AgentTarget) -> dict[str, str]:
    """Serialize an AgentTarget into a JSON-ready dictionary."""
    data: dict[str, str] = {
        "backend_id": target.backend_id,
        "node_id": target.node_id,
    }
    if target.session_id:
        data["session_id"] = target.session_id
    if target.window_id:
        data["window_id"] = target.window_id
    return data


def target_from_dict(data: Any) -> AgentTarget | None:
    """Deserialize an AgentTarget from a JSON dictionary, or return None."""
    if not isinstance(data, dict):
        return None
    backend_id = str(data.get("backend_id") or "").strip()
    node_id = str(data.get("node_id") or "").strip()
    if not backend_id or not node_id:
        return None
    return AgentTarget(
        backend_id=backend_id,
        node_id=node_id,
        session_id=str(data.get("session_id") or ""),
        window_id=str(data.get("window_id") or ""),
    )


@dataclass
class WindowState:
    """Persistent state for a tmux window."""

    session_id: str = ""
    cwd: str = ""
    window_name: str = ""
    account_name: str = ""
    usage_limit_exceeded: bool = False
    launch_started_at: float = 0.0
    agent_type: str = ""
    model: str = ""
    reasoning_effort: str = ""
    fast_mode: bool = False
    hibernated_at: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "session_id": self.session_id,
            "cwd": self.cwd,
        }
        if self.window_name:
            d["window_name"] = self.window_name
        if self.account_name:
            d["account_name"] = self.account_name
        if self.usage_limit_exceeded:
            d["usage_limit_exceeded"] = True
        if self.launch_started_at:
            d["launch_started_at"] = self.launch_started_at
        if self.agent_type:
            d["agent_type"] = normalize_agent_type(self.agent_type)
        if self.model:
            d["model"] = self.model
        if self.reasoning_effort:
            d["reasoning_effort"] = self.reasoning_effort
        if self.fast_mode:
            d["fast_mode"] = True
        if self.hibernated_at:
            d["hibernated_at"] = self.hibernated_at
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "WindowState":
        return cls(
            session_id=data.get("session_id", ""),
            cwd=data.get("cwd", ""),
            window_name=data.get("window_name", ""),
            account_name=data.get("account_name", ""),
            usage_limit_exceeded=bool(data.get("usage_limit_exceeded", False)),
            launch_started_at=float(data.get("launch_started_at", 0.0) or 0.0),
            agent_type=normalize_agent_type(data.get("agent_type", ""), default="")
            if data.get("agent_type")
            else "",
            model=str(data.get("model") or ""),
            reasoning_effort=str(data.get("reasoning_effort") or ""),
            fast_mode=bool(data.get("fast_mode", False)),
            hibernated_at=float(data.get("hibernated_at", 0.0) or 0.0),
        )


@dataclass
class PersistedSessionState:
    """In-memory snapshot parsed from ``state.json``."""

    window_states: dict[str, WindowState] = field(default_factory=dict)
    user_window_offsets: dict[int, dict[str, int]] = field(default_factory=dict)
    user_window_offset_sessions: dict[int, dict[str, str]] = field(default_factory=dict)
    thread_bindings: dict[int, dict[int, str]] = field(default_factory=dict)
    thread_targets: dict[int, dict[int, AgentTarget]] = field(default_factory=dict)
    window_display_names: dict[str, str] = field(default_factory=dict)
    group_chat_ids: dict[str, int] = field(default_factory=dict)
    thread_output_modes: dict[str, str] = field(default_factory=dict)
    hidden_session_ids: set[str] = field(default_factory=set)
    topic_managed_session_ids: set[str] = field(default_factory=set)
    needs_migration: bool = False


def load_state_file(
    state_file: Path,
    *,
    default_output_mode: str = "clean",
) -> PersistedSessionState | None:
    """Load and normalize state.json; return None when state_file is absent."""
    if not state_file.exists():
        return None
    try:
        state = json.loads(state_file.read_text())
        window_states = {
            k: WindowState.from_dict(v)
            for k, v in state.get("window_states", {}).items()
        }
        user_window_offsets = {
            int(uid): offsets
            for uid, offsets in state.get("user_window_offsets", {}).items()
        }
        user_window_offset_sessions = {
            int(uid): {
                str(window_id): str(session_id)
                for window_id, session_id in sessions.items()
                if isinstance(session_id, str) and session_id
            }
            for uid, sessions in state.get("user_window_offset_sessions", {}).items()
            if isinstance(sessions, dict)
        }
        thread_bindings = {
            int(uid): {int(tid): wid for tid, wid in bindings.items()}
            for uid, bindings in state.get("thread_bindings", {}).items()
        }
        thread_targets: dict[int, dict[int, AgentTarget]] = {}
        for uid, targets in state.get("thread_targets", {}).items():
            parsed_targets: dict[int, AgentTarget] = {}
            for tid, target_data in targets.items():
                target = target_from_dict(target_data)
                if target is not None:
                    parsed_targets[int(tid)] = target
            if parsed_targets:
                thread_targets[int(uid)] = parsed_targets
        window_display_names = state.get("window_display_names", {})
        group_chat_ids = {k: int(v) for k, v in state.get("group_chat_ids", {}).items()}
        thread_output_modes = {
            str(key): normalize_output_mode(value, default_output_mode)
            for key, value in state.get("thread_output_modes", {}).items()
            if isinstance(key, str) and isinstance(value, str)
        }
        hidden_session_ids = {
            canonical_session_id(session_id)
            for session_id in state.get("hidden_session_ids", [])
            if isinstance(session_id, str) and session_id
        }
        topic_managed_session_ids = {
            canonical_session_id(session_id)
            for session_id in state.get("topic_managed_session_ids", [])
            if isinstance(session_id, str) and session_id
        }

        needs_migration = any(not is_window_id(k) for k in window_states)
        if not needs_migration:
            needs_migration = any(
                not is_window_id(wid)
                for bindings in thread_bindings.values()
                for wid in bindings.values()
            )

        return PersistedSessionState(
            window_states=window_states,
            user_window_offsets=user_window_offsets,
            user_window_offset_sessions=user_window_offset_sessions,
            thread_bindings=thread_bindings,
            thread_targets=thread_targets,
            window_display_names=window_display_names,
            group_chat_ids=group_chat_ids,
            thread_output_modes=thread_output_modes,
            hidden_session_ids=hidden_session_ids,
            topic_managed_session_ids=topic_managed_session_ids,
            needs_migration=needs_migration,
        )
    except (json.JSONDecodeError, ValueError) as exc:
        logger.warning("Failed to load state: %s", exc)
        return PersistedSessionState()


def serialize_state_payload(
    *,
    window_states: dict[str, WindowState],
    user_window_offsets: dict[int, dict[str, int]],
    user_window_offset_sessions: dict[int, dict[str, str]],
    thread_bindings: dict[int, dict[int, str]],
    thread_targets: dict[int, dict[int, AgentTarget]],
    window_display_names: dict[str, str],
    group_chat_ids: dict[str, int],
    thread_output_modes: dict[str, str],
    hidden_session_ids: set[str],
    topic_managed_session_ids: set[str],
) -> dict[str, Any]:
    """Build the JSON-serializable state dictionary."""
    return {
        "window_states": {k: v.to_dict() for k, v in window_states.items()},
        "user_window_offsets": {
            str(uid): offsets for uid, offsets in user_window_offsets.items()
        },
        "user_window_offset_sessions": {
            str(uid): sessions for uid, sessions in user_window_offset_sessions.items()
        },
        "thread_bindings": {
            str(uid): {str(tid): wid for tid, wid in bindings.items()}
            for uid, bindings in thread_bindings.items()
        },
        "thread_targets": {
            str(uid): {
                str(tid): target_to_dict(target) for tid, target in targets.items()
            }
            for uid, targets in thread_targets.items()
        },
        "window_display_names": window_display_names,
        "group_chat_ids": group_chat_ids,
        "thread_output_modes": thread_output_modes,
        "hidden_session_ids": sorted(hidden_session_ids),
        "topic_managed_session_ids": sorted(topic_managed_session_ids),
    }


def save_state_file(
    state_file: Path,
    state: dict[str, Any],
    *,
    writer: Callable[[Path, dict[str, Any]], None] = atomic_write_json,
) -> None:
    """Persist serialized state atomically to state_file."""
    writer(state_file, state)
    logger.debug("State saved to %s", state_file)


async def read_session_map_async(map_file: Path) -> dict[str, Any] | None:
    """Read and decode session_map.json asynchronously; return None on error/missing."""
    if not map_file.exists():
        return None
    try:
        async with aiofiles.open(map_file, "r") as f:
            content = await f.read()
        data = json.loads(content)
        return data if isinstance(data, dict) else None
    except (json.JSONDecodeError, OSError):
        return None


def mutate_session_map(
    map_file: Path,
    mutate: Callable[[dict[str, Any]], bool],
    *,
    create_if_missing: bool = False,
    writer: Callable[[Path, dict[str, Any]], None] = atomic_write_json,
) -> bool:
    """Update session_map.json under the exclusive hook file lock."""
    if not create_if_missing and not map_file.exists():
        return True
    try:
        map_file.parent.mkdir(parents=True, exist_ok=True)
        with map_file.with_suffix(".lock").open("w") as lock_file:
            fcntl.flock(lock_file, fcntl.LOCK_EX)
            session_map = json.loads(map_file.read_text()) if map_file.exists() else {}
            if not isinstance(session_map, dict):
                logger.warning("session_map must be a JSON object: %s", map_file)
                return False
            if mutate(session_map):
                writer(map_file, session_map)
        return True
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Failed to update session_map: %s", exc)
        return False
