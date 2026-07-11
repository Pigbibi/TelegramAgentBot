"""Application configuration — reads env vars and exposes a singleton.

Loads TELEGRAM_BOT_TOKEN, ALLOWED_USERS, tmux/Codex paths, and
monitoring intervals from environment variables (with .env support).
.env loading priority: local .env (cwd) > $TELEGRAM_AGENT_BOT_DIR/.env (default ~/.telegram-agent-bot).
The module-level `config` instance is imported by nearly every other module.

Key class: Config (singleton instantiated as `config`).
"""

import logging
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

from .utils import app_dir
from .agent_profile import (
    AGENT_CLAUDE,
    AGENT_CODEX,
    EFFORT_STANDARD,
    normalize_agent_type,
    normalize_effort,
)

logger = logging.getLogger(__name__)

# Env vars that must not leak to child processes (e.g. Codex/Claude via tmux)
SENSITIVE_ENV_VARS = {
    "TELEGRAM_BOT_TOKEN",
    "ALLOWED_USERS",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "DEEPSEEK_API_KEY",
    "AI_TRANSCRIPTION_OPENAI_API_KEY",
    "AI_TRANSCRIPTION_GOOGLE_API_KEY",
}


@dataclass(frozen=True)
class ProjectRoot:
    """Named project root shown before creating a new Codex session."""

    label: str
    path: Path


def _parse_project_roots(raw: str, fallback: Path) -> list[ProjectRoot]:
    """Parse TELEGRAM_AGENT_BOT_PROJECT_ROOTS as label=path pairs."""

    roots: list[ProjectRoot] = []
    seen_labels: set[str] = set()
    for item in raw.replace("\n", ",").split(","):
        entry = item.strip()
        if not entry:
            continue

        if "=" in entry:
            label, path_value = entry.split("=", 1)
        elif ":" in entry:
            label, path_value = entry.split(":", 1)
        else:
            path_value = entry
            label = Path(path_value).expanduser().name or path_value

        label = label.strip()
        path_value = path_value.strip()
        if not label or not path_value:
            continue

        base_label = label
        suffix = 2
        while label in seen_labels:
            label = f"{base_label}-{suffix}"
            suffix += 1
        seen_labels.add(label)
        roots.append(ProjectRoot(label=label, path=Path(path_value).expanduser()))

    return roots or [ProjectRoot(label="Default", path=fallback)]


class Config:
    """Application configuration loaded from environment variables."""

    def __init__(self) -> None:
        self.config_dir = app_dir()
        self.config_dir.mkdir(parents=True, exist_ok=True)

        # Load .env: local (cwd) takes priority over config_dir
        # load_dotenv default override=False means first-loaded wins
        local_env = Path(".env")
        global_env = self.config_dir / ".env"
        if local_env.is_file():
            load_dotenv(local_env)
            logger.debug("Loaded env from %s", local_env.resolve())
        if global_env.is_file():
            load_dotenv(global_env)
            logger.debug("Loaded env from %s", global_env)

        self.telegram_bot_token: str = os.getenv("TELEGRAM_BOT_TOKEN") or ""
        if not self.telegram_bot_token:
            raise ValueError("TELEGRAM_BOT_TOKEN environment variable is required")

        allowed_users_str = os.getenv("ALLOWED_USERS", "")
        if not allowed_users_str:
            raise ValueError("ALLOWED_USERS environment variable is required")
        try:
            self.allowed_users: set[int] = {
                int(uid.strip()) for uid in allowed_users_str.split(",") if uid.strip()
            }
        except ValueError as e:
            raise ValueError(
                f"ALLOWED_USERS contains non-numeric value: {e}. "
                "Expected comma-separated Telegram user IDs."
            ) from e

        # Agent type: "codex" (default) or "claude". Controls which CLI is launched,
        # where transcripts are stored, and which update mechanism is used.
        raw_agent_type = os.getenv("TELEGRAM_AGENT_BOT_AGENT_TYPE", AGENT_CODEX)
        normalized_raw_agent_type = raw_agent_type.strip().lower().replace("-", "")
        self.agent_type = normalize_agent_type(normalized_raw_agent_type)
        if (normalized_raw_agent_type or AGENT_CODEX) not in (
            AGENT_CODEX,
            AGENT_CLAUDE,
            "claudecode",
        ):
            logger.warning(
                "Unknown agent_type %r, falling back to 'codex'",
                raw_agent_type,
            )
            self.agent_type = AGENT_CODEX
        self.agent_type_display = (
            "Codex" if self.agent_type == "codex" else "Claude Code"
        )

        # Tmux session name/socket and window naming
        self.tmux_socket_name = os.getenv("TELEGRAM_AGENT_BOT_TMUX_SOCKET_NAME") or None
        self.tmux_session_name = os.getenv(
            "TELEGRAM_AGENT_BOT_TMUX_SESSION_NAME", "telegram-agent-bot"
        )
        self.tmux_main_window_name = "__main__"

        # Keep codex_command as the legacy global-mode value. The explicit
        # codex_cli_command lets mixed per-topic mode launch Codex even when
        # the global default is Claude Code.
        self.codex_cli_command = os.getenv("TELEGRAM_AGENT_BOT_CODEX_COMMAND", "codex")
        _default_command = "claude" if self.agent_type == AGENT_CLAUDE else "codex"
        self.codex_command = os.getenv(
            "TELEGRAM_AGENT_BOT_CODEX_COMMAND", _default_command
        )
        self.codex_model = os.getenv(
            "TELEGRAM_AGENT_BOT_CODEX_MODEL", "gpt-5.4-mini"
        ).strip()
        self.claude_command = os.getenv("TELEGRAM_AGENT_BOT_CLAUDE_COMMAND", "claude")
        self.claude_model = os.getenv(
            "TELEGRAM_AGENT_BOT_CLAUDE_MODEL", "deepseek-v4-flash"
        ).strip()
        claude_env_file = os.getenv("TELEGRAM_AGENT_BOT_CLAUDE_ENV_FILE", "")
        self.claude_env_file = (
            Path(claude_env_file).expanduser()
            if claude_env_file
            else self.config_dir / "claude.env"
        )
        self.codex_reasoning_effort = normalize_effort(
            os.getenv("TELEGRAM_AGENT_BOT_CODEX_REASONING_EFFORT", EFFORT_STANDARD)
        )
        self.claude_reasoning_effort = normalize_effort(
            os.getenv("TELEGRAM_AGENT_BOT_CLAUDE_REASONING_EFFORT", "high")
        )
        self.model_discovery_enabled = os.getenv(
            "TELEGRAM_AGENT_BOT_MODEL_DISCOVERY", "true"
        ).strip().lower() not in {"0", "false", "no", "off"}
        self.codex_models_raw = os.getenv("TELEGRAM_AGENT_BOT_CODEX_MODELS", "")
        self.claude_models_raw = os.getenv("TELEGRAM_AGENT_BOT_CLAUDE_MODELS", "")
        self.codex_models = self._parse_models(self.codex_models_raw, self.codex_model)
        self.claude_models = self._parse_models(
            self.claude_models_raw, self.claude_model
        )
        self.codex_bypass_hook_trust = (
            os.getenv("TELEGRAM_AGENT_BOT_CODEX_BYPASS_HOOK_TRUST", "").lower()
            == "true"
        )

        # Agent backend selection. The default backend keeps the current
        # single-machine tmux behavior. Optional plugins can register additional
        # backends, for example a center-bot plus remote agent-node backend.
        self.agent_backend = (
            os.getenv("TELEGRAM_AGENT_BOT_BACKEND", "local").strip().lower() or "local"
        )
        backend_plugins = os.getenv(
            "TELEGRAM_AGENT_BOT_BACKEND_PLUGINS",
            os.getenv("TELEGRAM_AGENT_BOT_BACKEND_PLUGIN", ""),
        )
        self.backend_plugins = tuple(
            item.strip()
            for item in backend_plugins.replace("\n", ",").split(",")
            if item.strip()
        )

        # All state files live under config_dir
        self.state_file = self.config_dir / "state.json"
        self.session_map_file = self.config_dir / "session_map.json"
        self.monitor_state_file = self.config_dir / "monitor_state.json"

        # Transcript/session monitoring configuration.
        # Priority: explicit TELEGRAM_CODEX_BOT path >
        #   CODEX_HOME (codex) > default per agent_type.
        #
        # Default: ~/.codex for codex, ~/.claude/projects for claude.
        custom_projects_path = os.getenv("TELEGRAM_AGENT_BOT_CODEX_PROJECTS_PATH")
        codex_home = os.getenv("CODEX_HOME")

        if custom_projects_path:
            self.codex_projects_path = Path(custom_projects_path)
        elif self.agent_type == "claude":
            self.codex_projects_path = Path.home() / ".claude" / "projects"
        elif codex_home:
            self.codex_projects_path = Path(codex_home)
        else:
            self.codex_projects_path = Path.home() / ".codex"

        default_projects_path = os.getenv("TELEGRAM_AGENT_BOT_DEFAULT_PROJECTS_PATH")
        self.default_projects_path = (
            Path(default_projects_path).expanduser()
            if default_projects_path
            else Path.home() / "Projects"
        )
        project_roots = os.getenv("TELEGRAM_AGENT_BOT_PROJECT_ROOTS", "")
        self.project_roots_configured = bool(project_roots.strip())
        self.project_roots = _parse_project_roots(
            project_roots,
            self.default_projects_path,
        )

        self.monitor_poll_interval = float(
            os.getenv("TELEGRAM_AGENT_BOT_MONITOR_POLL_INTERVAL", "2.0")
        )
        self.status_poll_interval = float(
            os.getenv("TELEGRAM_AGENT_BOT_STATUS_POLL_INTERVAL", "1.0")
        )
        self.status_repost_interval = max(
            0.0,
            float(os.getenv("TELEGRAM_AGENT_BOT_STATUS_REPOST_INTERVAL", "60.0")),
        )
        self.agent_input_queue_max_size = max(
            1,
            int(os.getenv("TELEGRAM_AGENT_BOT_AGENT_INPUT_QUEUE_MAX_SIZE", "20")),
        )
        self.agent_input_queue_max_wait_seconds = max(
            0.0,
            float(
                os.getenv(
                    "TELEGRAM_AGENT_BOT_AGENT_INPUT_QUEUE_MAX_WAIT_SECONDS",
                    "1800",
                )
            ),
        )
        self.agent_startup_timeout_seconds = max(
            30.0,
            float(
                os.getenv(
                    "TELEGRAM_AGENT_BOT_AGENT_STARTUP_TIMEOUT_SECONDS",
                    "180",
                )
            ),
        )

        # Display user messages in history and real-time notifications
        # When True, user messages are shown with a 👤 prefix
        self.show_user_messages = (
            os.getenv("TELEGRAM_AGENT_BOT_SHOW_USER_MESSAGES", "true").lower()
            != "false"
        )

        # Show Codex intermediary commentary updates in Telegram
        # When False, only final answers and non-commentary assistant output are sent
        self.show_commentary_messages = (
            os.getenv("TELEGRAM_AGENT_BOT_SHOW_COMMENTARY_MESSAGES", "false").lower()
            == "true"
        )

        # Show tool call notifications (tool_use/tool_result) in Telegram
        # When False, only text responses, thinking, and interactive prompts are sent
        self.show_tool_calls = (
            os.getenv("TELEGRAM_AGENT_BOT_SHOW_TOOL_CALLS", "true").lower() != "false"
        )

        # Show Bash command and output notifications while keeping other tools visible
        self.show_bash_tool_calls = (
            os.getenv("TELEGRAM_AGENT_BOT_SHOW_BASH_TOOL_CALLS", "true").lower()
            != "false"
        )

        # Show Codex sessions created outside telegram-agent-bot in the Telegram resume picker.
        # Keep this opt-in so local VSCode/CLI history does not clutter Telegram.
        self.show_external_resume_sessions = (
            os.getenv("TELEGRAM_AGENT_BOT_SHOW_EXTERNAL_RESUME_SESSIONS", "").lower()
            == "true"
        )

        # Automatic multi-account failover is opt-in. Manual account
        # switching remains available via Telegram commands. The default only
        # reports usage/auth errors instead of rotating accounts automatically.
        self.enable_account_rotation = (
            os.getenv("TELEGRAM_AGENT_BOT_ENABLE_ACCOUNT_ROTATION", "").lower()
            == "true"
        )

        # Show hidden (dot) directories in directory browser
        self.show_hidden_dirs = (
            os.getenv("TELEGRAM_AGENT_BOT_SHOW_HIDDEN_DIRS", "").lower() == "true"
        )

        # ── Voice transcription providers ──────────────────────────────────
        # Comma-separated provider IDs tried in order. First successful
        # transcription wins; on failure the next provider is attempted.
        # Supported providers: openai, google
        transcription_providers_raw = os.getenv("AI_TRANSCRIPTION_PROVIDERS", "openai")
        self.transcription_providers = tuple(
            p.strip().lower()
            for p in transcription_providers_raw.split(",")
            if p.strip()
        ) or ("openai",)

        # OpenAI-compatible provider (OpenAI, Groq, Azure, etc.)
        self.transcription_openai_api_key: str = os.getenv(
            "AI_TRANSCRIPTION_OPENAI_API_KEY", ""
        )
        self.transcription_openai_base_url: str = os.getenv(
            "AI_TRANSCRIPTION_OPENAI_BASE_URL", "https://api.openai.com/v1"
        )
        self.transcription_openai_model: str = os.getenv(
            "AI_TRANSCRIPTION_OPENAI_MODEL", "gpt-4o-transcribe"
        )

        # Google Gemini provider
        self.transcription_google_api_key: str = os.getenv(
            "AI_TRANSCRIPTION_GOOGLE_API_KEY", ""
        )
        self.transcription_google_model: str = os.getenv(
            "AI_TRANSCRIPTION_GOOGLE_MODEL", "gemini-2.0-flash-lite"
        )

        # Scrub sensitive vars from os.environ so child processes never inherit them.
        # Values are already captured in Config attributes above.
        for var in SENSITIVE_ENV_VARS:
            os.environ.pop(var, None)

        logger.debug(
            "Config initialized: dir=%s, token=%s..., allowed_users=%d, "
            "tmux_socket=%s, tmux_session=%s, agent_type=%s, "
            "codex_command=%s, codex_projects_path=%s, "
            "default_projects_path=%s, project_roots=%d, backend=%s",
            self.config_dir,
            self.telegram_bot_token[:8],
            len(self.allowed_users),
            self.tmux_socket_name,
            self.tmux_session_name,
            self.agent_type,
            self.codex_command,
            self.codex_projects_path,
            self.default_projects_path,
            len(self.project_roots),
            self.agent_backend,
        )

    def is_user_allowed(self, user_id: int) -> bool:
        """Check if a user is in the allowed list."""
        return user_id in self.allowed_users

    @staticmethod
    def _parse_models(raw: str, configured_model: str) -> tuple[str, ...]:
        if raw.strip().lower() == "auto":
            raw = ""
        models = [
            item.strip() for item in raw.replace("\n", ",").split(",") if item.strip()
        ]
        if configured_model and configured_model not in models:
            models.insert(0, configured_model)
        return tuple(dict.fromkeys(models))


config = Config()
