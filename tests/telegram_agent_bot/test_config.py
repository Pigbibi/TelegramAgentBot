"""Unit tests for Config — env var loading, validation, and user access."""

from pathlib import Path

import pytest

from telegram_agent_bot.config import Config


@pytest.fixture
def _base_env(monkeypatch, tmp_path):
    # chdir to tmp_path so load_dotenv won't find the real .env in repo root
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("TELEGRAM_AGENT_BOT_CODEX_PROJECTS_PATH", raising=False)
    monkeypatch.delenv("TELEGRAM_AGENT_BOT_DEFAULT_PROJECTS_PATH", raising=False)
    monkeypatch.delenv("TELEGRAM_AGENT_BOT_PROJECT_ROOTS", raising=False)
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.delenv("TELEGRAM_AGENT_BOT_AGENT_TYPE", raising=False)
    monkeypatch.delenv("TELEGRAM_AGENT_BOT_CODEX_COMMAND", raising=False)
    monkeypatch.delenv("TELEGRAM_AGENT_BOT_CODEX_MODEL", raising=False)
    monkeypatch.delenv("TELEGRAM_AGENT_BOT_CLAUDE_MODEL", raising=False)
    monkeypatch.delenv("TELEGRAM_AGENT_BOT_CODEX_MODELS", raising=False)
    monkeypatch.delenv("TELEGRAM_AGENT_BOT_CLAUDE_MODELS", raising=False)
    monkeypatch.delenv("TELEGRAM_AGENT_BOT_CODEX_BYPASS_HOOK_TRUST", raising=False)
    monkeypatch.delenv("TELEGRAM_AGENT_BOT_ENABLE_ACCOUNT_ROTATION", raising=False)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test:token")
    monkeypatch.setenv("ALLOWED_USERS", "12345")
    monkeypatch.setenv("TELEGRAM_AGENT_BOT_DIR", str(tmp_path))


@pytest.mark.usefixtures("_base_env")
class TestConfigValid:
    def test_valid_config(self):
        cfg = Config()
        assert cfg.telegram_bot_token == "test:token"
        assert cfg.allowed_users == {12345}

    def test_default_command_is_codex(self, monkeypatch):
        monkeypatch.delenv("TELEGRAM_AGENT_BOT_CODEX_COMMAND", raising=False)
        cfg = Config()
        assert cfg.codex_command == "codex"
        assert cfg.codex_bypass_hook_trust is False

    def test_default_models_are_agent_specific(self):
        cfg = Config()
        assert cfg.codex_model == "gpt-5.4-mini"
        assert cfg.codex_models == ("gpt-5.4-mini",)
        assert cfg.claude_model == "deepseek-v4-flash"
        assert cfg.claude_models == ("deepseek-v4-flash",)

    def test_claude_agent_defaults(self, monkeypatch):
        monkeypatch.setenv("TELEGRAM_AGENT_BOT_AGENT_TYPE", "claude")
        cfg = Config()
        assert cfg.agent_type == "claude"
        assert cfg.agent_type_display == "Claude Code"
        assert cfg.codex_command == "claude"

    def test_codex_hook_trust_bypass_can_be_enabled(self, monkeypatch):
        monkeypatch.setenv("TELEGRAM_AGENT_BOT_CODEX_BYPASS_HOOK_TRUST", "true")
        cfg = Config()
        assert cfg.codex_bypass_hook_trust is True

    def test_custom_tmux_session_name(self, monkeypatch):
        monkeypatch.setenv("TELEGRAM_AGENT_BOT_TMUX_SESSION_NAME", "mysession")
        cfg = Config()
        assert cfg.tmux_session_name == "mysession"

    def test_default_agent_backend_is_local(self):
        cfg = Config()
        assert cfg.agent_backend == "local"
        assert cfg.backend_plugins == ()

    def test_custom_agent_backend_and_plugins(self, monkeypatch):
        monkeypatch.setenv("TELEGRAM_AGENT_BOT_BACKEND", "cluster")
        monkeypatch.setenv(
            "TELEGRAM_AGENT_BOT_BACKEND_PLUGINS",
            "plugin_one, plugin_two\nplugin_three",
        )
        cfg = Config()
        assert cfg.agent_backend == "cluster"
        assert cfg.backend_plugins == (
            "plugin_one",
            "plugin_two",
            "plugin_three",
        )

    def test_custom_monitor_poll_interval(self, monkeypatch):
        monkeypatch.setenv("TELEGRAM_AGENT_BOT_MONITOR_POLL_INTERVAL", "5.0")
        cfg = Config()
        assert cfg.monitor_poll_interval == 5.0

    def test_custom_status_poll_interval(self, monkeypatch):
        monkeypatch.setenv("TELEGRAM_AGENT_BOT_STATUS_POLL_INTERVAL", "0.5")
        cfg = Config()
        assert cfg.status_poll_interval == 0.5

    def test_status_repost_interval_defaults_to_one_minute(self):
        cfg = Config()
        assert cfg.status_repost_interval == 60.0

    def test_custom_status_repost_interval(self, monkeypatch):
        monkeypatch.setenv("TELEGRAM_AGENT_BOT_STATUS_REPOST_INTERVAL", "120")
        cfg = Config()
        assert cfg.status_repost_interval == 120.0

    def test_status_repost_interval_can_be_disabled(self, monkeypatch):
        monkeypatch.setenv("TELEGRAM_AGENT_BOT_STATUS_REPOST_INTERVAL", "0")
        cfg = Config()
        assert cfg.status_repost_interval == 0.0

    def test_agent_input_queue_defaults(self):
        cfg = Config()
        assert cfg.agent_input_queue_max_size == 20
        assert cfg.agent_input_queue_max_wait_seconds == 1800

    def test_custom_agent_input_queue_limits(self, monkeypatch):
        monkeypatch.setenv("TELEGRAM_AGENT_BOT_AGENT_INPUT_QUEUE_MAX_SIZE", "3")
        monkeypatch.setenv(
            "TELEGRAM_AGENT_BOT_AGENT_INPUT_QUEUE_MAX_WAIT_SECONDS",
            "15.5",
        )
        cfg = Config()
        assert cfg.agent_input_queue_max_size == 3
        assert cfg.agent_input_queue_max_wait_seconds == 15.5

    def test_agent_input_queue_expiry_can_be_disabled(self, monkeypatch):
        monkeypatch.setenv("TELEGRAM_AGENT_BOT_AGENT_INPUT_QUEUE_MAX_WAIT_SECONDS", "0")
        cfg = Config()
        assert cfg.agent_input_queue_max_wait_seconds == 0

    def test_account_rotation_defaults_disabled(self):
        cfg = Config()
        assert cfg.enable_account_rotation is False

    def test_account_rotation_can_be_enabled(self, monkeypatch):
        monkeypatch.setenv("TELEGRAM_AGENT_BOT_ENABLE_ACCOUNT_ROTATION", "true")
        cfg = Config()
        assert cfg.enable_account_rotation is True

    def test_is_user_allowed_true(self):
        cfg = Config()
        assert cfg.is_user_allowed(12345) is True

    def test_is_user_allowed_false(self):
        cfg = Config()
        assert cfg.is_user_allowed(99999) is False


@pytest.mark.usefixtures("_base_env")
class TestConfigMissingEnv:
    def test_missing_telegram_bot_token(self, monkeypatch):
        monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
        with pytest.raises(ValueError, match="TELEGRAM_BOT_TOKEN"):
            Config()

    def test_missing_allowed_users(self, monkeypatch):
        monkeypatch.delenv("ALLOWED_USERS", raising=False)
        with pytest.raises(ValueError, match="ALLOWED_USERS"):
            Config()

    def test_non_numeric_allowed_users(self, monkeypatch):
        monkeypatch.setenv("ALLOWED_USERS", "abc")
        with pytest.raises(ValueError, match="non-numeric"):
            Config()


@pytest.mark.usefixtures("_base_env")
class TestConfigCodexProjectsPath:
    def test_default_codex_projects_path(self, monkeypatch):
        """Default path is ~/.codex when no env vars are set."""
        # Ensure no custom path env vars are set
        monkeypatch.delenv("TELEGRAM_AGENT_BOT_CODEX_PROJECTS_PATH", raising=False)
        monkeypatch.delenv("CODEX_HOME", raising=False)
        cfg = Config()
        assert cfg.codex_projects_path == Path.home() / ".codex"

    def test_custom_codex_projects_path(self, monkeypatch):
        """TELEGRAM_AGENT_BOT_CODEX_PROJECTS_PATH overrides the default path."""
        custom_path = "/custom/projects/path"
        monkeypatch.setenv("TELEGRAM_AGENT_BOT_CODEX_PROJECTS_PATH", custom_path)
        cfg = Config()
        assert cfg.codex_projects_path == Path(custom_path)

    def test_codex_home_sets_projects_path(self, monkeypatch):
        """CODEX_HOME becomes the transcript root when no explicit override is set."""
        custom_codex_home = "/custom/codex/home"
        monkeypatch.setenv("CODEX_HOME", custom_codex_home)
        cfg = Config()
        assert cfg.codex_projects_path == Path(custom_codex_home)

    def test_claude_agent_uses_home_claude_projects_path(self, monkeypatch):
        """~/.claude/projects is the transcript root in Claude mode."""
        monkeypatch.setenv("TELEGRAM_AGENT_BOT_AGENT_TYPE", "claude")
        monkeypatch.setenv("HOME", "/custom/home")
        monkeypatch.setenv("CODEX_HOME", "/wrong/codex/home")
        cfg = Config()
        assert cfg.codex_projects_path == Path("/custom/home/.claude/projects")

    def test_codex_projects_path_takes_priority(self, monkeypatch):
        """TELEGRAM_AGENT_BOT_CODEX_PROJECTS_PATH takes priority over CODEX_HOME."""
        monkeypatch.setenv("TELEGRAM_AGENT_BOT_CODEX_PROJECTS_PATH", "/priority/path")
        monkeypatch.setenv("CODEX_HOME", "/lower/priority")
        cfg = Config()
        assert cfg.codex_projects_path == Path("/priority/path")


@pytest.mark.usefixtures("_base_env")
class TestConfigDefaultProjectsPath:
    def test_default_projects_path(self, monkeypatch):
        monkeypatch.delenv("TELEGRAM_AGENT_BOT_DEFAULT_PROJECTS_PATH", raising=False)
        cfg = Config()
        assert cfg.default_projects_path == Path.home() / "Projects"

    def test_custom_default_projects_path(self, monkeypatch):
        monkeypatch.setenv("TELEGRAM_AGENT_BOT_DEFAULT_PROJECTS_PATH", "/srv/projects")
        cfg = Config()
        assert cfg.default_projects_path == Path("/srv/projects")

    def test_default_project_roots_follow_default_path(self):
        cfg = Config()
        assert cfg.project_roots_configured is False
        assert len(cfg.project_roots) == 1
        assert cfg.project_roots[0].label == "Default"
        assert cfg.project_roots[0].path == Path.home() / "Projects"

    def test_named_project_roots(self, monkeypatch):
        monkeypatch.setenv(
            "TELEGRAM_AGENT_BOT_PROJECT_ROOTS",
            "Primary=/srv/projects,Secondary=/mnt/secondary-projects",
        )
        cfg = Config()
        assert cfg.project_roots_configured is True
        assert [(root.label, root.path) for root in cfg.project_roots] == [
            ("Primary", Path("/srv/projects")),
            ("Secondary", Path("/mnt/secondary-projects")),
        ]


@pytest.mark.usefixtures("_base_env")
class TestConfigTranscription:
    def test_transcription_defaults(self, monkeypatch):
        monkeypatch.delenv("AI_TRANSCRIPTION_PROVIDERS", raising=False)
        monkeypatch.delenv("AI_TRANSCRIPTION_OPENAI_API_KEY", raising=False)
        monkeypatch.delenv("AI_TRANSCRIPTION_OPENAI_BASE_URL", raising=False)
        monkeypatch.delenv("AI_TRANSCRIPTION_OPENAI_MODEL", raising=False)
        monkeypatch.delenv("AI_TRANSCRIPTION_GOOGLE_API_KEY", raising=False)
        monkeypatch.delenv("AI_TRANSCRIPTION_GOOGLE_MODEL", raising=False)
        cfg = Config()
        assert cfg.transcription_providers == ("openai",)
        assert cfg.transcription_openai_api_key == ""
        assert cfg.transcription_openai_base_url == "https://api.openai.com/v1"
        assert cfg.transcription_openai_model == "gpt-4o-transcribe"
        assert cfg.transcription_google_api_key == ""
        assert cfg.transcription_google_model == "gemini-2.0-flash-lite"

    def test_transcription_providers(self, monkeypatch):
        monkeypatch.setenv("AI_TRANSCRIPTION_PROVIDERS", "openai,google")
        cfg = Config()
        assert cfg.transcription_providers == ("openai", "google")

    def test_transcription_openai_api_key(self, monkeypatch):
        monkeypatch.setenv("AI_TRANSCRIPTION_OPENAI_API_KEY", "sk-test-123")
        cfg = Config()
        assert cfg.transcription_openai_api_key == "sk-test-123"

    def test_transcription_google_api_key(self, monkeypatch):
        monkeypatch.setenv("AI_TRANSCRIPTION_GOOGLE_API_KEY", "google-test-key")
        cfg = Config()
        assert cfg.transcription_google_api_key == "google-test-key"

    def test_transcription_openai_model(self, monkeypatch):
        monkeypatch.setenv("AI_TRANSCRIPTION_OPENAI_MODEL", "whisper-1")
        cfg = Config()
        assert cfg.transcription_openai_model == "whisper-1"

    def test_transcription_google_model(self, monkeypatch):
        monkeypatch.setenv("AI_TRANSCRIPTION_GOOGLE_MODEL", "gemini-2.0-flash-001")
        cfg = Config()
        assert cfg.transcription_google_model == "gemini-2.0-flash-001"

    def test_transcription_openai_api_key_scrubbed_from_env(self, monkeypatch):
        import os

        monkeypatch.setenv("AI_TRANSCRIPTION_OPENAI_API_KEY", "sk-secret")
        Config()
        assert os.environ.get("AI_TRANSCRIPTION_OPENAI_API_KEY") is None
