"""Agent launch profiles shared by the Telegram UI and backends."""

from __future__ import annotations

from dataclasses import dataclass

AGENT_CODEX = "codex"
AGENT_CLAUDE = "claude"
AGENT_CLAUDE_OFFICIAL = "claudeofficial"
AGENT_CURSOR = "cursor"
SUPPORTED_AGENT_TYPES = (AGENT_CODEX, AGENT_CLAUDE, AGENT_CLAUDE_OFFICIAL, AGENT_CURSOR)

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
DEFAULT_CURSOR_EFFORTS: tuple[str, ...] = ()


@dataclass(frozen=True)
class AgentCapabilities:
    """Provider semantics consumed by the Telegram UI and local runtime."""

    supports_model_selection: bool
    supports_fast_mode: bool
    supports_native_steer: bool
    supports_native_queue: bool
    supports_account_snapshots: bool
    plugin_command: str
    uses_claude_home: bool = False
    loads_claude_env: bool = False


_AGENT_CAPABILITIES = {
    AGENT_CODEX: AgentCapabilities(
        supports_model_selection=True,
        supports_fast_mode=True,
        supports_native_steer=True,
        supports_native_queue=True,
        supports_account_snapshots=True,
        plugin_command="plugins",
    ),
    AGENT_CLAUDE: AgentCapabilities(
        supports_model_selection=True,
        supports_fast_mode=True,
        supports_native_steer=True,
        supports_native_queue=False,
        supports_account_snapshots=True,
        plugin_command="plugin",
        uses_claude_home=True,
        loads_claude_env=True,
    ),
    AGENT_CLAUDE_OFFICIAL: AgentCapabilities(
        supports_model_selection=False,
        supports_fast_mode=True,
        supports_native_steer=True,
        supports_native_queue=False,
        supports_account_snapshots=True,
        plugin_command="plugin",
        uses_claude_home=True,
    ),
    AGENT_CURSOR: AgentCapabilities(
        supports_model_selection=True,
        supports_fast_mode=False,
        supports_native_steer=True,
        supports_native_queue=True,
        supports_account_snapshots=False,
        plugin_command="plugins",
    ),
}


def normalize_agent_type(value: str | None, default: str = AGENT_CODEX) -> str:
    """Normalize user/config input to a supported agent type."""
    normalized = (value or default).strip().lower().replace("-", "")
    if normalized == "claudecode":
        normalized = AGENT_CLAUDE
    elif normalized in {"claudeofficial", "claudeapp", "claudesubscription"}:
        normalized = AGENT_CLAUDE_OFFICIAL
    elif normalized in {"cursoragent", "cursorcli"}:
        normalized = AGENT_CURSOR
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
    return {
        AGENT_CLAUDE: "Claude Code",
        AGENT_CLAUDE_OFFICIAL: "Claude Code (Official)",
        AGENT_CURSOR: "Cursor Agent",
    }.get(normalize_agent_type(agent_type), "Codex")


def is_claude_agent(agent_type: str) -> bool:
    return normalize_agent_type(agent_type) in {AGENT_CLAUDE, AGENT_CLAUDE_OFFICIAL}


def agent_capabilities(agent_type: str) -> AgentCapabilities:
    """Return normalized runtime semantics for one supported agent type."""
    return _AGENT_CAPABILITIES[normalize_agent_type(agent_type)]


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
