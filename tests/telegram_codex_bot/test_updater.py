from __future__ import annotations

import json
from pathlib import Path
from typing import Sequence

from telegram_codex_bot.updater import (
    CodexUpdateSettings,
    CommandResult,
    UpdateSettings,
    check_codex_update,
    check_and_apply_update,
    load_codex_update_settings,
    load_update_settings,
)


class FakeRunner:
    def __init__(self, outputs: dict[tuple[str, ...], CommandResult | str]) -> None:
        self.outputs = outputs
        self.calls: list[tuple[str, ...]] = []

    def __call__(
        self, args: Sequence[str], cwd: Path, timeout_seconds: int
    ) -> CommandResult:
        command = tuple(args)
        self.calls.append(command)
        output = self.outputs.get(command, "")
        if isinstance(output, CommandResult):
            return output
        return CommandResult(args=command, returncode=0, stdout=output)


def test_load_update_settings_reads_env(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("TELEGRAM_CODEX_BOT_DIR", str(tmp_path))
    monkeypatch.setenv("TELEGRAM_CODEX_BOT_AUTO_UPDATE", "true")
    monkeypatch.setenv("TELEGRAM_CODEX_BOT_UPDATE_INTERVAL_SECONDS", "42")
    monkeypatch.setenv("TELEGRAM_CODEX_BOT_UPDATE_REQUIRE_IDLE", "false")
    monkeypatch.setenv("TELEGRAM_CODEX_BOT_UPDATE_BUSY_RETRY_SECONDS", "90")
    monkeypatch.setenv("TELEGRAM_CODEX_BOT_UPDATE_REMOTE", "origin")
    monkeypatch.setenv("TELEGRAM_CODEX_BOT_UPDATE_BRANCH", "main")
    monkeypatch.setenv("TELEGRAM_CODEX_BOT_UPDATE_RUN_UV_SYNC", "false")

    settings = load_update_settings()

    assert settings.enabled is True
    assert settings.interval_seconds == 42
    assert settings.require_idle is False
    assert settings.busy_retry_seconds == 90
    assert settings.remote == "origin"
    assert settings.branch == "main"
    assert settings.run_uv_sync is False
    assert settings.state_file == tmp_path / "update_state.json"


def test_load_codex_update_settings_reads_env(monkeypatch) -> None:
    monkeypatch.setenv("TELEGRAM_CODEX_BOT_CODEX_UPDATE_CHECK", "true")
    monkeypatch.setenv("TELEGRAM_CODEX_BOT_CODEX_AUTO_UPDATE", "true")
    monkeypatch.setenv("TELEGRAM_CODEX_BOT_CODEX_UPDATE_PACKAGE", "@openai/codex")
    monkeypatch.setenv(
        "TELEGRAM_CODEX_BOT_CODEX_COMMAND", "IS_SANDBOX=1 codex --danger"
    )
    monkeypatch.setenv("TELEGRAM_CODEX_BOT_CODEX_UPDATE_NPM", "npm")

    settings = load_codex_update_settings()

    assert settings.enabled is True
    assert settings.auto_update is True
    assert settings.package == "@openai/codex"
    assert settings.codex_executable
    assert settings.codex_executable.endswith("codex")
    assert settings.npm_executable == "npm"


def test_update_skips_non_git_checkout(tmp_path: Path) -> None:
    result = check_and_apply_update(UpdateSettings(), repo_dir=tmp_path, force=True)

    assert result.checked is True
    assert result.supported is False
    assert result.skipped_reason == "not_git_checkout"


def test_auto_check_respects_interval(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    state_file = tmp_path / "update_state.json"
    state_file.write_text(json.dumps({"last_checked_at": 100}), encoding="utf-8")
    runner = FakeRunner({})

    result = check_and_apply_update(
        UpdateSettings(interval_seconds=60, state_file=state_file),
        repo_dir=tmp_path,
        runner=runner,
        now=120,
    )

    assert result.checked is False
    assert result.skipped_reason == "interval"
    assert runner.calls == []


def test_check_only_reports_available_update(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    runner = FakeRunner(
        {
            ("git", "status", "--porcelain"): "",
            ("git", "rev-parse", "--short", "HEAD"): "abc123\n",
            (
                "git",
                "rev-parse",
                "--abbrev-ref",
                "--symbolic-full-name",
                "@{u}",
            ): "origin/main\n",
            ("git", "fetch", "--quiet", "--prune", "origin"): "",
            ("git", "rev-list", "--count", "HEAD..origin/main"): "2\n",
        }
    )

    result = check_and_apply_update(
        UpdateSettings(state_file=tmp_path / "state.json"),
        repo_dir=tmp_path,
        check_only=True,
        force=True,
        runner=runner,
    )

    assert result.update_available is True
    assert result.updated is False
    assert result.commits_behind == 2
    assert ("git", "pull", "--ff-only") not in runner.calls


def test_update_pulls_and_runs_uv_sync(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    rev_parse_short = ("git", "rev-parse", "--short", "HEAD")
    runner = FakeRunner(
        {
            ("git", "status", "--porcelain"): "",
            rev_parse_short: "new456\n",
            (
                "git",
                "rev-parse",
                "--abbrev-ref",
                "--symbolic-full-name",
                "@{u}",
            ): "origin/main\n",
            ("git", "fetch", "--quiet", "--prune", "origin"): "",
            ("git", "rev-list", "--count", "HEAD..origin/main"): "3\n",
            ("git", "pull", "--ff-only"): "",
            ("uv", "sync"): "",
        }
    )

    result = check_and_apply_update(
        UpdateSettings(
            state_file=tmp_path / "state.json",
            uv_executable="uv",
            run_uv_sync=True,
        ),
        repo_dir=tmp_path,
        force=True,
        runner=runner,
    )

    assert result.updated is True
    assert result.restart_required is True
    assert result.commits_behind == 3
    assert ("git", "pull", "--ff-only") in runner.calls
    assert ("uv", "sync") in runner.calls


def test_dirty_worktree_skips_update(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    runner = FakeRunner(
        {("git", "status", "--porcelain"): " M src/telegram_codex_bot/main.py\n"}
    )

    result = check_and_apply_update(
        UpdateSettings(state_file=tmp_path / "state.json"),
        repo_dir=tmp_path,
        force=True,
        runner=runner,
    )

    assert result.updated is False
    assert result.skipped_reason == "dirty_worktree"
    assert ("git", "fetch", "--quiet", "--prune", "origin") not in runner.calls


def test_codex_update_check_reports_available_update(tmp_path: Path) -> None:
    runner = FakeRunner(
        {
            ("codex", "--version"): "codex-cli 0.125.0\n",
            ("npm", "view", "@openai/codex", "version"): "0.126.0\n",
        }
    )

    result = check_codex_update(
        CodexUpdateSettings(
            codex_executable="codex",
            npm_executable="npm",
            package="@openai/codex",
        ),
        runner=runner,
        cwd=tmp_path,
    )

    assert result.update_available is True
    assert result.updated is False
    assert result.current_version == "0.125.0"
    assert result.latest_version == "0.126.0"


def test_codex_update_applies_global_npm_update(tmp_path: Path) -> None:
    runner = FakeRunner(
        {
            ("codex", "--version"): "codex-cli 0.125.0\n",
            ("npm", "view", "@openai/codex", "version"): "0.126.0\n",
            (
                "npm",
                "list",
                "-g",
                "--depth=0",
                "@openai/codex",
                "--json",
            ): '{"dependencies":{"@openai/codex":{"version":"0.125.0"}}}\n',
            ("npm", "install", "-g", "@openai/codex@latest"): "",
        }
    )

    result = check_codex_update(
        CodexUpdateSettings(
            codex_executable="codex",
            npm_executable="npm",
            package="@openai/codex",
        ),
        apply_update=True,
        runner=runner,
        cwd=tmp_path,
    )

    assert result.updated is True
    assert ("npm", "install", "-g", "@openai/codex@latest") in runner.calls
