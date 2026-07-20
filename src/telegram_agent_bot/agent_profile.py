"""Agent launch profiles shared by the Telegram UI and backends."""

from __future__ import annotations

from dataclasses import dataclass

AGENT_CODEX = "codex"
AGENT_CLAUDE = "claude"
SUPPORTED_AGENT_TYPES = (AGENT_CODEX, AGENT_CLAUDE)

EFFORT_LOW = "low"
EFFORT_STANDARD = "medium"
EFFORT_DEEP = "high"
EFFORT_EXTRA_HIGH = "xhigh"
EFFORT_MAX = "max"
EFFORT_ULTRA = "ultra"
EFFORT_MINIMAL = "minimal"
EFFORT_NONE = "none"
SUPPORTED_EFFORTS = (
    EFFORT_NONE,
    EFFORT_MINIMAL,
    EFFORT_LOW,
    EFFORT_STANDARD,
    EFFORT_DEEP,
    EFFORT_EXTRA_HIGH,
    EFFORT_MAX,
    EFFORT_ULTRA,
)
DEFAULT_CODEX_EFFORTS = (
    EFFORT_LOW,
    EFFORT_STANDARD,
    EFFORT_DEEP,
    EFFORT_EXTRA_HIGH,
)
DEFAULT_CLAUDE_EFFORTS = (
    EFFORT_LOW,
    EFFORT_STANDARD,
    EFFORT_DEEP,
    EFFORT_MAX,
)


def normalize_agent_type(value: str | None, default: str = AGENT_CODEX) -> str:
    """Normalize user/config input to a supported agent type."""
    normalized = (value or default).strip().lower().replace("-", "")
    if normalized == "claudecode":
        normalized = AGENT_CLAUDE
    return normalized if normalized in SUPPORTED_AGENT_TYPES else default


def normalize_effort(value: str | None, default: str = EFFORT_STANDARD) -> str:
    """Normalize a reasoning effort value."""
    normalized = (value or default).strip().lower()
    return normalized if normalized in SUPPORTED_EFFORTS else default


def effort_display_label(effort: str) -> str:
    """Return the user-facing label for a reasoning effort value."""
    return {
        EFFORT_NONE: "None",
        EFFORT_MINIMAL: "Minimal",
        EFFORT_LOW: "Low",
        EFFORT_STANDARD: "Standard",
        EFFORT_DEEP: "Deep",
        EFFORT_EXTRA_HIGH: "Extra High",
        EFFORT_MAX: "Max",
        EFFORT_ULTRA: "Ultra",
    }.get(effort, effort)


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
    fast_mode: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "agent_type", normalize_agent_type(self.agent_type))
        if self.reasoning_effort:
            object.__setattr__(
                self,
                "reasoning_effort",
                normalize_effort(self.reasoning_effort),
            )
        object.__setattr__(self, "fast_mode", bool(self.fast_mode))

    @property
    def display_name(self) -> str:
        return agent_display_name(self.agent_type)

    @property
    def effort_label(self) -> str:
        return effort_display_label(self.reasoning_effort)

    @property
    def fast_label(self) -> str:
        return "On" if self.fast_mode else "Off"
