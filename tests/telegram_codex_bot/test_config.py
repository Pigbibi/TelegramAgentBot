"""Unit tests for Config — env var loading, validation, and user access."""

from pathlib import Path

import pytest

from telegram_codex_bot.config import Config


@pytest.fixture
def _base_env(monkeypatch, tmp_path):
    # chdir to tmp_path so load_dotenv won't find the real .env in repo root
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("TELEGRAM_CODEX_BOT_CODEX_PROJECTS_PATH", raising=False)
    monkeypatch.delenv("TELEGRAM_CODEX_BOT_DEFAULT_PROJECTS_PATH", raising=False)
    monkeypatch.delenv("TELEGRAM_CODEX_BOT_PROJECT_ROOTS", raising=False)
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.delenv("TELEGRAM_CODEX_BOT_CODEX_COMMAND", raising=False)
    monkeypatch.delenv("TELEGRAM_CODEX_BOT_ENABLE_ACCOUNT_ROTATION", raising=False)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test:token")
    monkeypatch.setenv("ALLOWED_USERS", "12345")
    monkeypatch.setenv("TELEGRAM_CODEX_BOT_DIR", str(tmp_path))


@pytest.mark.usefixtures("_base_env")
class TestConfigValid:
    def test_valid_config(self):
        cfg = Config()
        assert cfg.telegram_bot_token == "test:token"
        assert cfg.allowed_users == {12345}

    def test_default_command_is_codex(self, monkeypatch):
        monkeypatch.delenv("TELEGRAM_CODEX_BOT_CODEX_COMMAND", raising=False)
        cfg = Config()
        assert cfg.codex_command == "codex"

    def test_custom_tmux_session_name(self, monkeypatch):
        monkeypatch.setenv("TELEGRAM_CODEX_BOT_TMUX_SESSION_NAME", "mysession")
        cfg = Config()
        assert cfg.tmux_session_name == "mysession"

    def test_default_agent_backend_is_local(self):
        cfg = Config()
        assert cfg.agent_backend == "local"
        assert cfg.backend_plugins == ()

    def test_custom_agent_backend_and_plugins(self, monkeypatch):
        monkeypatch.setenv("TELEGRAM_CODEX_BOT_BACKEND", "cluster")
        monkeypatch.setenv(
            "TELEGRAM_CODEX_BOT_BACKEND_PLUGINS",
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
        monkeypatch.setenv("TELEGRAM_CODEX_BOT_MONITOR_POLL_INTERVAL", "5.0")
        cfg = Config()
        assert cfg.monitor_poll_interval == 5.0

    def test_custom_status_poll_interval(self, monkeypatch):
        monkeypatch.setenv("TELEGRAM_CODEX_BOT_STATUS_POLL_INTERVAL", "0.5")
        cfg = Config()
        assert cfg.status_poll_interval == 0.5

    def test_account_rotation_defaults_disabled(self):
        cfg = Config()
        assert cfg.enable_account_rotation is False

    def test_account_rotation_can_be_enabled(self, monkeypatch):
        monkeypatch.setenv("TELEGRAM_CODEX_BOT_ENABLE_ACCOUNT_ROTATION", "true")
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
        monkeypatch.delenv("TELEGRAM_CODEX_BOT_CODEX_PROJECTS_PATH", raising=False)
        monkeypatch.delenv("CODEX_HOME", raising=False)
        cfg = Config()
        assert cfg.codex_projects_path == Path.home() / ".codex"

    def test_custom_codex_projects_path(self, monkeypatch):
        """TELEGRAM_CODEX_BOT_CODEX_PROJECTS_PATH overrides the default path."""
        custom_path = "/custom/projects/path"
        monkeypatch.setenv("TELEGRAM_CODEX_BOT_CODEX_PROJECTS_PATH", custom_path)
        cfg = Config()
        assert cfg.codex_projects_path == Path(custom_path)

    def test_codex_home_sets_projects_path(self, monkeypatch):
        """CODEX_HOME becomes the transcript root when no explicit override is set."""
        custom_codex_home = "/custom/codex/home"
        monkeypatch.setenv("CODEX_HOME", custom_codex_home)
        cfg = Config()
        assert cfg.codex_projects_path == Path(custom_codex_home)

    def test_codex_projects_path_takes_priority(self, monkeypatch):
        """TELEGRAM_CODEX_BOT_CODEX_PROJECTS_PATH takes priority over CODEX_HOME."""
        monkeypatch.setenv("TELEGRAM_CODEX_BOT_CODEX_PROJECTS_PATH", "/priority/path")
        monkeypatch.setenv("CODEX_HOME", "/lower/priority")
        cfg = Config()
        assert cfg.codex_projects_path == Path("/priority/path")


@pytest.mark.usefixtures("_base_env")
class TestConfigDefaultProjectsPath:
    def test_default_projects_path(self, monkeypatch):
        monkeypatch.delenv("TELEGRAM_CODEX_BOT_DEFAULT_PROJECTS_PATH", raising=False)
        cfg = Config()
        assert cfg.default_projects_path == Path.home() / "Projects"

    def test_custom_default_projects_path(self, monkeypatch):
        monkeypatch.setenv("TELEGRAM_CODEX_BOT_DEFAULT_PROJECTS_PATH", "/srv/projects")
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
            "TELEGRAM_CODEX_BOT_PROJECT_ROOTS",
            "Primary=/srv/projects,Secondary=/mnt/secondary-projects",
        )
        cfg = Config()
        assert cfg.project_roots_configured is True
        assert [(root.label, root.path) for root in cfg.project_roots] == [
            ("Primary", Path("/srv/projects")),
            ("Secondary", Path("/mnt/secondary-projects")),
        ]


@pytest.mark.usefixtures("_base_env")
class TestConfigOpenAI:
    def test_openai_defaults(self, monkeypatch):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
        cfg = Config()
        assert cfg.openai_api_key == ""
        assert cfg.openai_base_url == "https://api.openai.com/v1"

    def test_openai_api_key(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test-123")
        cfg = Config()
        assert cfg.openai_api_key == "sk-test-123"

    def test_openai_base_url(self, monkeypatch):
        monkeypatch.setenv("OPENAI_BASE_URL", "https://proxy.example.com/v1")
        cfg = Config()
        assert cfg.openai_base_url == "https://proxy.example.com/v1"

    def test_openai_api_key_scrubbed_from_env(self, monkeypatch):
        import os

        monkeypatch.setenv("OPENAI_API_KEY", "sk-secret")
        Config()
        assert os.environ.get("OPENAI_API_KEY") is None
