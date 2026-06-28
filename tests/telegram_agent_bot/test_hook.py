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
        monkeypatch.setenv("CODEX_HOME", str(codex_dir))
        return config_file, hooks_file

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
                                "statusMessage": "Registering Codex session",
                                "timeout": 5,
                            }
                        ],
                    }
                ]
            }
        }
        assert "Hook installed successfully" in capsys.readouterr().out

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
                                        "statusMessage": "Registering Codex session",
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
                                        "statusMessage": "Registering Codex session",
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
