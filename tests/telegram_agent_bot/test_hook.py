"""Tests for Codex session tracking hook."""

import io
import json
import sys
from types import SimpleNamespace
from pathlib import Path

import pytest

from telegram_agent_bot import hook as hook_module
from telegram_agent_bot.hook import (
    _ROLLOUT_SESSION_RE,
    _UUID_RE,
    _install_hook,
    _is_hook_installed,
    _is_non_interactive_session,
    hook_main,
)


class TestUuidRegex:
    @pytest.mark.parametrize(
        "value",
        [
            "550e8400-e29b-41d4-a716-446655440000",
            "00000000-0000-0000-0000-000000000000",
            "abcdef01-2345-6789-abcd-ef0123456789",
        ],
        ids=["standard", "all-zeros", "all-hex"],
    )
    def test_valid_uuid_matches(self, value: str) -> None:
        assert _UUID_RE.match(value) is not None

    @pytest.mark.parametrize(
        "value",
        [
            "not-a-uuid",
            "550e8400-e29b-41d4-a716",
            "550e8400-e29b-41d4-a716-44665544000g",
            "",
        ],
        ids=["gibberish", "truncated", "invalid-hex-char", "empty"],
    )
    def test_invalid_uuid_no_match(self, value: str) -> None:
        assert _UUID_RE.match(value) is None


class TestRolloutSessionRegex:
    @pytest.mark.parametrize(
        "value",
        [
            "rollout-2026-04-03T08-30-00-019d4610-438c-7c52-bf97-bc6f02747399",
            "rollout-2026-01-01T00-00-00-deadbeef-dead-beef-dead-beefdeadbeef",
        ],
        ids=["uuid-suffix", "hex-hyphen-suffix"],
    )
    def test_valid_rollout_session_matches(self, value: str) -> None:
        assert _ROLLOUT_SESSION_RE.match(value) is not None

    @pytest.mark.parametrize(
        "value",
        [
            "rollout-2026-04-03 08-30-00-019d4610-438c-7c52-bf97-bc6f02747399",
            "rollout-2026-04-03T08-30-00-",
            "rollout-2026-04-03T08:30:00-deadbeef",
        ],
        ids=["space-separated-time", "missing-suffix", "colon-time"],
    )
    def test_invalid_rollout_session_no_match(self, value: str) -> None:
        assert _ROLLOUT_SESSION_RE.match(value) is None


class TestIsHookInstalled:
    def test_hook_present(self) -> None:
        settings = {
            "hooks": {
                "SessionStart": [
                    {
                        "hooks": [
                            {
                                "type": "command",
                                "command": "telegram-agent-bot hook",
                                "timeout": 5,
                            }
                        ]
                    }
                ]
            }
        }
        assert _is_hook_installed(settings) is True

    def test_no_hooks_key(self) -> None:
        assert _is_hook_installed({}) is False

    def test_different_hook_command(self) -> None:
        settings = {
            "hooks": {
                "SessionStart": [
                    {"hooks": [{"type": "command", "command": "other-tool hook"}]}
                ]
            }
        }
        assert _is_hook_installed(settings) is False

    def test_full_path_matches(self) -> None:
        settings = {
            "hooks": {
                "SessionStart": [
                    {
                        "hooks": [
                            {
                                "type": "command",
                                "command": "/usr/bin/telegram-agent-bot hook",
                                "timeout": 5,
                            }
                        ]
                    }
                ]
            }
        }
        assert _is_hook_installed(settings) is True


class TestSessionPayloadFiltering:
    @pytest.mark.parametrize(
        "payload",
        [
            {"source": "exec"},
            {"originator": "codex_exec"},
            {"source": "EXEC", "originator": "codex-tui"},
        ],
    )
    def test_non_interactive_session_payload(self, payload: dict) -> None:
        assert _is_non_interactive_session(payload) is True

    @pytest.mark.parametrize(
        "payload",
        [
            {},
            {"source": "cli"},
            {"originator": "codex-tui"},
            {"source": "cli", "originator": "codex-tui"},
        ],
    )
    def test_interactive_session_payload(self, payload: dict) -> None:
        assert _is_non_interactive_session(payload) is False


class TestHookMainValidation:
    def _run_hook_main(
        self, monkeypatch: pytest.MonkeyPatch, payload: dict, *, tmux_pane: str = ""
    ) -> None:
        monkeypatch.setattr(sys, "argv", ["telegram-agent-bot", "hook"])
        monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
        if tmux_pane:
            monkeypatch.setenv("TMUX_PANE", tmux_pane)
        else:
            monkeypatch.delenv("TMUX_PANE", raising=False)
        hook_main()

    def test_missing_session_id(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        monkeypatch.setenv("TELEGRAM_AGENT_BOT_DIR", str(tmp_path))
        self._run_hook_main(
            monkeypatch,
            {"cwd": "/tmp", "hook_event_name": "SessionStart"},
        )
        assert not (tmp_path / "session_map.json").exists()

    def test_invalid_uuid_format(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        monkeypatch.setenv("TELEGRAM_AGENT_BOT_DIR", str(tmp_path))
        self._run_hook_main(
            monkeypatch,
            {
                "session_id": "not-a-uuid",
                "cwd": "/tmp",
                "hook_event_name": "SessionStart",
            },
        )
        assert not (tmp_path / "session_map.json").exists()

    def test_valid_rollout_session_writes_session_map(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        monkeypatch.setenv("TELEGRAM_AGENT_BOT_DIR", str(tmp_path))
        monkeypatch.setattr(
            hook_module.subprocess,
            "run",
            lambda *args, **kwargs: SimpleNamespace(
                stdout="telegram-agent-bot:@12:demo", returncode=0
            ),
        )
        self._run_hook_main(
            monkeypatch,
            {
                "session_id": "rollout-2026-04-03T08-30-00-019d4610-438c-7c52-bf97-bc6f02747399",
                "cwd": "/tmp",
                "hook_event_name": "SessionStart",
            },
            tmux_pane="%1",
        )

        session_map = json.loads((tmp_path / "session_map.json").read_text())
        assert session_map == {
            "telegram-agent-bot:@12": {
                "session_id": "rollout-2026-04-03T08-30-00-019d4610-438c-7c52-bf97-bc6f02747399",
                "cwd": "/tmp",
                "window_name": "demo",
                "agent_type": "codex",
            }
        }

    def test_relative_cwd(self, monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
        monkeypatch.setenv("TELEGRAM_AGENT_BOT_DIR", str(tmp_path))
        self._run_hook_main(
            monkeypatch,
            {
                "session_id": "550e8400-e29b-41d4-a716-446655440000",
                "cwd": "relative/path",
                "hook_event_name": "SessionStart",
            },
        )
        assert not (tmp_path / "session_map.json").exists()

    def test_non_session_start_event(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        monkeypatch.setenv("TELEGRAM_AGENT_BOT_DIR", str(tmp_path))
        self._run_hook_main(
            monkeypatch,
            {
                "session_id": "550e8400-e29b-41d4-a716-446655440000",
                "cwd": "/tmp",
                "hook_event_name": "Stop",
            },
        )
        assert not (tmp_path / "session_map.json").exists()

    def test_codex_exec_session_does_not_write_session_map(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        monkeypatch.setenv("TELEGRAM_AGENT_BOT_DIR", str(tmp_path))
        self._run_hook_main(
            monkeypatch,
            {
                "session_id": "550e8400-e29b-41d4-a716-446655440000",
                "cwd": "/tmp",
                "hook_event_name": "SessionStart",
                "source": "exec",
                "originator": "codex_exec",
            },
            tmux_pane="%1",
        )
        assert not (tmp_path / "session_map.json").exists()


class TestInstallHook:
    def _patch_codex_paths(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> tuple[Path, Path]:
        codex_dir = tmp_path / ".codex"
        config_file = codex_dir / "config.toml"
        hooks_file = codex_dir / "hooks.json"
        monkeypatch.delenv("TELEGRAM_AGENT_BOT_AGENT_TYPE", raising=False)
        monkeypatch.setenv("CODEX_HOME", str(codex_dir))
        return config_file, hooks_file

    def _patch_claude_path(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> Path:
        home_dir = tmp_path / "home"
        monkeypatch.setenv("TELEGRAM_AGENT_BOT_AGENT_TYPE", "claude")
        monkeypatch.delenv("CODEX_HOME", raising=False)
        monkeypatch.setenv("HOME", str(home_dir))
        return home_dir / ".claude" / "settings.json"

    def test_install_writes_config_and_hooks_json(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        config_file, hooks_file = self._patch_codex_paths(monkeypatch, tmp_path)
        monkeypatch.setattr(
            hook_module.shutil, "which", lambda _: "/usr/local/bin/telegram-agent-bot"
        )

        assert _install_hook() == 0

        assert config_file.read_text(encoding="utf-8") == "[features]\nhooks = true\n"
        hooks_payload = json.loads(hooks_file.read_text(encoding="utf-8"))
        assert hooks_payload == {
            "hooks": {
                "SessionStart": [
                    {
                        "matcher": "startup|resume",
                        "hooks": [
                            {
                                "type": "command",
                                "command": "/usr/local/bin/telegram-agent-bot hook",
                                "statusMessage": "Registering agent session",
                                "timeout": 5,
                            }
                        ],
                    }
                ]
            }
        }
        assert "Hook installed successfully" in capsys.readouterr().out

    def test_install_writes_claude_settings_json(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        settings_file = self._patch_claude_path(monkeypatch, tmp_path)
        monkeypatch.setattr(
            hook_module.shutil, "which", lambda _: "/usr/local/bin/telegram-agent-bot"
        )

        assert _install_hook() == 0

        settings_payload = json.loads(settings_file.read_text(encoding="utf-8"))
        assert settings_payload == {
            "hooks": {
                "SessionStart": [
                    {
                        "matcher": "startup|resume",
                        "hooks": [
                            {
                                "type": "command",
                                "command": "/usr/local/bin/telegram-agent-bot hook",
                                "statusMessage": "Registering agent session",
                                "timeout": 5,
                            }
                        ],
                    }
                ]
            }
        }
        assert not (settings_file.parent / "config.toml").exists()
        assert not (settings_file.parent / "hooks.json").exists()
        assert "Claude settings" in capsys.readouterr().out

    def test_hook_main_install_loads_agent_type_from_env_file(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        app_dir = tmp_path / "app"
        home_dir = tmp_path / "home"
        work_dir = tmp_path / "work"
        app_dir.mkdir()
        work_dir.mkdir()
        (app_dir / ".env").write_text(
            "TELEGRAM_AGENT_BOT_AGENT_TYPE=claude\n",
            encoding="utf-8",
        )
        monkeypatch.delenv("TELEGRAM_AGENT_BOT_AGENT_TYPE", raising=False)
        monkeypatch.delenv("CODEX_HOME", raising=False)
        monkeypatch.chdir(work_dir)
        monkeypatch.setenv("TELEGRAM_AGENT_BOT_DIR", str(app_dir))
        monkeypatch.setenv("HOME", str(home_dir))
        monkeypatch.setattr(sys, "argv", ["telegram-agent-bot", "hook", "--install"])
        monkeypatch.setattr(
            hook_module.shutil, "which", lambda _: "/usr/local/bin/telegram-agent-bot"
        )

        with pytest.raises(SystemExit) as exc:
            hook_main()

        assert exc.value.code == 0
        assert (home_dir / ".claude" / "settings.json").is_file()
        assert not (home_dir / ".codex" / "hooks.json").exists()

    def test_install_preserves_existing_claude_settings(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        settings_file = self._patch_claude_path(monkeypatch, tmp_path)
        monkeypatch.setattr(
            hook_module.shutil, "which", lambda _: "/opt/bin/telegram-agent-bot"
        )
        settings_file.parent.mkdir(parents=True, exist_ok=True)
        settings_file.write_text(
            json.dumps(
                {
                    "permissions": {"allow": ["Bash(pytest *)"]},
                    "env": {"ANTHROPIC_BASE_URL": "https://api.deepseek.com/anthropic"},
                    "hooks": {
                        "SessionStart": [
                            {
                                "matcher": "startup",
                                "hooks": [{"type": "command", "command": "other hook"}],
                            }
                        ]
                    },
                }
            ),
            encoding="utf-8",
        )

        assert _install_hook() == 0

        settings_payload = json.loads(settings_file.read_text(encoding="utf-8"))
        assert settings_payload["permissions"] == {"allow": ["Bash(pytest *)"]}
        assert settings_payload["env"] == {
            "ANTHROPIC_BASE_URL": "https://api.deepseek.com/anthropic"
        }
        assert len(settings_payload["hooks"]["SessionStart"]) == 2
        assert (
            settings_payload["hooks"]["SessionStart"][1]["hooks"][0]["command"]
            == "/opt/bin/telegram-agent-bot hook"
        )

    def test_install_is_idempotent_and_enables_feature(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        config_file, hooks_file = self._patch_codex_paths(monkeypatch, tmp_path)
        config_file.parent.mkdir(parents=True, exist_ok=True)
        config_file.write_text("[features]\ncodex_hooks = false\n", encoding="utf-8")
        hooks_file.write_text(
            json.dumps(
                {
                    "hooks": {
                        "SessionStart": [
                            {
                                "matcher": "startup|resume",
                                "hooks": [
                                    {
                                        "type": "command",
                                        "command": "telegram-agent-bot hook",
                                        "statusMessage": "Registering agent session",
                                        "timeout": 5,
                                    }
                                ],
                            }
                        ]
                    }
                }
            ),
            encoding="utf-8",
        )

        assert _install_hook() == 0

        assert config_file.read_text(encoding="utf-8") == "[features]\nhooks = true\n"
        hooks_payload = json.loads(hooks_file.read_text(encoding="utf-8"))
        assert len(hooks_payload["hooks"]["SessionStart"]) == 1
        assert "Hook already installed" in capsys.readouterr().out

    def test_install_repairs_stale_absolute_hook_path(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        config_file, hooks_file = self._patch_codex_paths(monkeypatch, tmp_path)
        monkeypatch.setattr(
            hook_module.shutil, "which", lambda _: "/opt/bin/telegram-agent-bot"
        )
        stale_cli = tmp_path / "deleted" / ".venv" / "bin" / "telegram-agent-bot"
        hooks_file.parent.mkdir(parents=True, exist_ok=True)
        hooks_file.write_text(
            json.dumps(
                {
                    "hooks": {
                        "SessionStart": [
                            {
                                "matcher": "startup|resume",
                                "hooks": [
                                    {
                                        "type": "command",
                                        "command": f"{stale_cli} hook",
                                        "statusMessage": "Registering agent session",
                                        "timeout": 5,
                                    }
                                ],
                            }
                        ]
                    }
                }
            ),
            encoding="utf-8",
        )

        assert _install_hook() == 0

        assert config_file.read_text(encoding="utf-8") == "[features]\nhooks = true\n"
        hooks_payload = json.loads(hooks_file.read_text(encoding="utf-8"))
        installed_hooks = hooks_payload["hooks"]["SessionStart"][0]["hooks"]
        assert len(installed_hooks) == 1
        assert installed_hooks[0]["command"] == "/opt/bin/telegram-agent-bot hook"
        assert "Hook command repaired" in capsys.readouterr().out

    def test_install_removes_missing_legacy_bot_hook(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        _, hooks_file = self._patch_codex_paths(monkeypatch, tmp_path)
        current_cli = tmp_path / "bin" / "telegram-agent-bot"
        current_cli.parent.mkdir(parents=True)
        current_cli.write_text("#!/bin/sh\n", encoding="utf-8")
        monkeypatch.setattr(hook_module.shutil, "which", lambda _: str(current_cli))
        stale_cli = tmp_path / "deleted" / ".venv" / "bin" / "telegram-codex-bot"
        hooks_file.parent.mkdir(parents=True, exist_ok=True)
        hooks_file.write_text(
            json.dumps(
                {
                    "hooks": {
                        "SessionStart": [
                            {
                                "matcher": "startup|resume",
                                "hooks": [
                                    {
                                        "type": "command",
                                        "command": f"{stale_cli} hook",
                                        "statusMessage": "Registering agent session",
                                        "timeout": 5,
                                    }
                                ],
                            },
                            {
                                "matcher": "startup|resume",
                                "hooks": [
                                    {
                                        "type": "command",
                                        "command": f"{current_cli} hook",
                                        "statusMessage": "Registering agent session",
                                        "timeout": 5,
                                    }
                                ],
                            },
                        ]
                    }
                }
            ),
            encoding="utf-8",
        )

        assert _install_hook() == 0

        hooks_payload = json.loads(hooks_file.read_text(encoding="utf-8"))
        session_start = hooks_payload["hooks"]["SessionStart"]
        assert len(session_start) == 1
        assert session_start[0]["hooks"][0]["command"] == f"{current_cli} hook"
        assert "Removed 1 stale bot hook command" in capsys.readouterr().out

    def test_install_preserves_existing_hooks(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _, hooks_file = self._patch_codex_paths(monkeypatch, tmp_path)
        monkeypatch.setattr(
            hook_module.shutil, "which", lambda _: "/opt/bin/telegram-agent-bot"
        )
        hooks_file.parent.mkdir(parents=True, exist_ok=True)
        hooks_file.write_text(
            json.dumps(
                {
                    "hooks": {
                        "SessionStart": [
                            {
                                "matcher": "startup",
                                "hooks": [
                                    {
                                        "type": "command",
                                        "command": "other-tool hook",
                                    }
                                ],
                            }
                        ]
                    }
                }
            ),
            encoding="utf-8",
        )

        assert _install_hook() == 0

        hooks_payload = json.loads(hooks_file.read_text(encoding="utf-8"))
        assert len(hooks_payload["hooks"]["SessionStart"]) == 2
        assert (
            hooks_payload["hooks"]["SessionStart"][0]["hooks"][0]["command"]
            == "other-tool hook"
        )
        assert (
            hooks_payload["hooks"]["SessionStart"][1]["hooks"][0]["command"]
            == "/opt/bin/telegram-agent-bot hook"
        )
