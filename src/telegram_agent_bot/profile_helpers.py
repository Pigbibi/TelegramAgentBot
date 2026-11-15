"""Pure agent-profile option and context resolution helpers."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .agent_profile import (
    AGENT_CLAUDE,
    AGENT_CLAUDE_OFFICIAL,
    AGENT_CODEX,
    AGENT_CURSOR,
    AgentProfile,
    DEFAULT_CLAUDE_EFFORTS,
    DEFAULT_CODEX_EFFORTS,
    DEFAULT_CURSOR_EFFORTS,
    is_claude_agent,
    normalize_agent_type,
    normalize_effort,
)
from .config import config as default_config
from .handlers.directory_browser import (
    PROFILE_AGENT_KEY,
    PROFILE_EFFORT_KEY,
    PROFILE_FAST_MODE_KEY,
    PROFILE_MODEL_KEY,
    PROFILE_PERMISSION_MODE_KEY,
)


def profile_models(agent_type: str, *, cfg: Any | None = None) -> tuple[str, ...]:
    """Return configured model choices for one agent type."""
    settings = cfg if cfg is not None else default_config
    normalized = normalize_agent_type(agent_type)
    if normalized == AGENT_CLAUDE_OFFICIAL:
        return ()
    if normalized == AGENT_CLAUDE:
        return settings.claude_models
    if normalized == AGENT_CURSOR:
        return settings.cursor_models
    return settings.codex_models


def profile_effort_values(
    agent_type: str,
    model: str,
    *,
    cfg: Any | None = None,
) -> tuple[str, ...]:
    """Return reasoning efforts supported by the selected agent/model."""
    settings = cfg if cfg is not None else default_config
    normalized = normalize_agent_type(agent_type)
    if normalized in {AGENT_CLAUDE, AGENT_CLAUDE_OFFICIAL}:
        return DEFAULT_CLAUDE_EFFORTS
    if normalized == AGENT_CURSOR:
        return DEFAULT_CURSOR_EFFORTS
    return settings.codex_model_efforts.get(model, DEFAULT_CODEX_EFFORTS)


def resolve_profile_effort(
    agent_type: str,
    model: str,
    requested: str,
    *,
    cfg: Any | None = None,
    effort_values: Callable[[str, str], tuple[str, ...]] | None = None,
) -> str:
    """Keep a supported effort or fall back to the model/provider default."""
    settings = cfg if cfg is not None else default_config
    normalized = normalize_agent_type(agent_type)
    supported = (
        effort_values(normalized, model)
        if effort_values is not None
        else profile_effort_values(normalized, model, cfg=settings)
    )
    if not supported:
        return ""
    selected = normalize_effort(requested, "")
    if selected in supported:
        return selected

    candidates: list[str] = []
    if normalized == AGENT_CODEX:
        candidates.append(settings.codex_model_default_efforts.get(model, ""))
        candidates.append(settings.codex_reasoning_effort)
    elif is_claude_agent(normalized):
        candidates.append(settings.claude_reasoning_effort)
    candidates.extend(("medium", supported[0] if supported else ""))
    return next((value for value in candidates if value in supported), "medium")


def default_profile_permission(
    agent_type: str,
    *,
    cfg: Any | None = None,
) -> str:
    """Return the configured default permission mode for one agent type."""
    settings = cfg if cfg is not None else default_config
    normalized = normalize_agent_type(agent_type)
    if normalized in {AGENT_CLAUDE, AGENT_CLAUDE_OFFICIAL}:
        return settings.claude_default_permission_mode
    if normalized == AGENT_CURSOR:
        return settings.cursor_permission_mode
    return "ask"


def profile_from_context(
    user_data: dict[Any, Any] | None,
    *,
    cfg: Any | None = None,
    resolve_effort: Callable[[str, str, str], str] | None = None,
    default_permission_fn: Callable[[str], str] | None = None,
) -> AgentProfile:
    """Build the pending topic profile, retaining legacy defaults."""
    settings = cfg if cfg is not None else default_config
    agent_type = (
        user_data.get(PROFILE_AGENT_KEY, settings.agent_type)
        if user_data
        else settings.agent_type
    )
    normalized = normalize_agent_type(agent_type, settings.agent_type)
    default_effort = (
        settings.claude_reasoning_effort
        if normalized in {AGENT_CLAUDE, AGENT_CLAUDE_OFFICIAL}
        else settings.codex_reasoning_effort
        if normalized == AGENT_CODEX
        else ""
    )
    requested_effort = (
        user_data.get(PROFILE_EFFORT_KEY, default_effort)
        if user_data
        else default_effort
    )
    model = user_data.get(PROFILE_MODEL_KEY, "") if user_data else ""
    fast_mode = user_data.get(PROFILE_FAST_MODE_KEY, False) if user_data else False
    default_permission = (
        default_permission_fn(normalized)
        if default_permission_fn is not None
        else default_profile_permission(normalized, cfg=settings)
    )
    permission_mode = (
        user_data.get(PROFILE_PERMISSION_MODE_KEY, default_permission)
        if user_data
        else default_permission
    )
    if normalized == AGENT_CURSOR and settings.cursor_permission_mode == "full":
        permission_mode = "full"
    model_str = model if isinstance(model, str) else ""
    requested_str = (
        requested_effort if isinstance(requested_effort, str) else default_effort
    )
    reasoning_effort = (
        resolve_effort(normalized, model_str, requested_str)
        if resolve_effort is not None
        else resolve_profile_effort(normalized, model_str, requested_str, cfg=settings)
    )
    return AgentProfile(
        agent_type=normalized,
        model=model_str,
        reasoning_effort=reasoning_effort,
        fast_mode=bool(fast_mode),
        permission_mode=(
            permission_mode if isinstance(permission_mode, str) else "ask"
        ),
    )
