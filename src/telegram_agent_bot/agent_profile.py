"""Per-session agent launch profiles.

The bot keeps the legacy ``claude`` spelling internally while accepting
``claudecode`` from user-facing configuration and callbacks.
"""

from __future__ import annotations

from dataclasses import dataclass

AGENT_CODEX = "codex"
AGENT_CLAUDE = "claude"
SUPPORTED_AGENT_TYPES = (AGENT_CODEX, AGENT_CLAUDE)

EFFORT_FAST = "low"
EFFORT_STANDARD = "medium"
EFFORT_DEEP = "high"
EFFORT_MAX = "max"
SUPPORTED_EFFORTS = (EFFORT_FAST, EFFORT_STANDARD, EFFORT_DEEP, EFFORT_MAX)


def normalize_agent_type(value: str | None, default: str = AGENT_CODEX) -> str:
    """Normalize user/config input to a supported agent type."""
    normalized = (value or default).strip().lower().replace("-", "")
    if normalized == "claudecode":
        normalized = AGENT_CLAUDE
    return normalized if normalized in SUPPORTED_AGENT_TYPES else default


def normalize_effort(value: str | None, default: str = EFFORT_STANDARD) -> str:
    """Normalize an effort value, accepting ``fast`` as a friendly alias."""
    normalized = (value or default).strip().lower()
    if normalized == "fast":
        normalized = EFFORT_FAST
    return normalized if normalized in SUPPORTED_EFFORTS else default


def agent_display_name(agent_type: str) -> str:
    return (
        "Claude Code" if normalize_agent_type(agent_type) == AGENT_CLAUDE else "Codex"
    )


@dataclass(frozen=True)
class AgentProfile:
    """Launch-time settings for one Telegram topic."""

    agent_type: str = AGENT_CODEX
    model: str = ""
    reasoning_effort: str = EFFORT_STANDARD

    def __post_init__(self) -> None:
        object.__setattr__(self, "agent_type", normalize_agent_type(self.agent_type))
        if self.reasoning_effort:
            object.__setattr__(
                self,
                "reasoning_effort",
                normalize_effort(self.reasoning_effort),
            )

    @property
    def display_name(self) -> str:
        return agent_display_name(self.agent_type)

    @property
    def effort_label(self) -> str:
        return {
            EFFORT_FAST: "Fast",
            EFFORT_STANDARD: "Standard",
            EFFORT_DEEP: "Deep",
            EFFORT_MAX: "Max",
        }.get(self.reasoning_effort, "Default")
