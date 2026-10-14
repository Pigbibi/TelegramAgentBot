"""Offline guard regressions: all CLI calls and prompts are synthetic."""

import json
import subprocess
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import pytest

from telegram_agent_bot import claude_api_session as guard
from telegram_agent_bot.agent_profile import AgentProfile
from telegram_agent_bot.tmux_manager import _agent_command_for_launch


@pytest.fixture(autouse=True)
def isolated_route(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://api.anthropic.com")
    for name in (
        "CLAUDE_CODE_USE_BEDROCK",
        "CLAUDE_CODE_USE_VERTEX",
        "CLAUDE_CODE_USE_FOUNDRY",
    ):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def limits():
    return guard.Limits(4096, 8, 500_000, 1_000_000)


def result(session="one", turns=2, cost=0.1):
    return {
        "session_id": session,
        "num_turns": turns,
        "total_cost_usd": cost,
        "is_error": False,
        "result": "synthetic answer",
    }


def settle(ledger, limits, *, session="one", day="2026-10-15", previous=0, turns=2):
    ledger.settle(
        session,
        previous,
        limits.max_turns - previous,
        result(session, turns),
        day=day,
        run_budget=limits.run_budget,
    )


def test_daily_budget_persists_and_rolls_over(tmp_path, limits):
    path = tmp_path / "budget.sqlite3"
    ledger = guard.Ledger(path)
    ledger.reserve("one", limits, new_session=True, day="2026-10-15")
    settle(ledger, limits)
    ledger.db.close()
    ledger = guard.Ledger(path)
    assert ledger.reserve("one", limits, new_session=False, day="2026-10-15") == (2, 6)
    settle(ledger, limits, previous=2)
    with pytest.raises(guard.BudgetDenied, match="Daily API budget exhausted"):
        ledger.reserve("two", limits, new_session=True, day="2026-10-15")
    assert ledger.reserve("two", limits, new_session=True, day="2026-10-16") == (0, 8)
    ledger.db.close()


def test_unresolved_call_blocks_all_sessions_even_after_restart(tmp_path, limits):
    path = tmp_path / "budget.sqlite3"
    ledger = guard.Ledger(path)
    ledger.reserve("one", limits, new_session=True, day="2026-10-15")
    ledger.db.close()
    ledger = guard.Ledger(path)
    for session, day in (("one", "2026-10-15"), ("two", "2026-10-16")):
        with pytest.raises(guard.BudgetDenied, match="unresolved daily accounting"):
            ledger.reserve(session, limits, new_session=True, day=day)
    ledger.db.close()


def test_unknown_resumed_session_denied(tmp_path, limits):
    ledger = guard.Ledger(tmp_path / "budget.sqlite3")
    with pytest.raises(guard.BudgetDenied, match="no trusted accounting"):
        ledger.reserve("unknown", limits, new_session=False, day="2026-10-15")
    ledger.db.close()


@pytest.mark.parametrize(
    "changes",
    [
        {"num_turns": 0},
        {"num_turns": 9},
        {"num_turns": True},
        {"session_id": "other"},
        {"is_error": True},
        {"total_cost_usd": None},
        {"total_cost_usd": -1},
        {"total_cost_usd": 0},
        {"total_cost_usd": float("nan")},
        {"total_cost_usd": 0.6},
    ],
)
def test_invalid_result_keeps_daily_quota_blocked(tmp_path, limits, changes):
    ledger = guard.Ledger(tmp_path / "budget.sqlite3")
    ledger.reserve("one", limits, new_session=True, day="2026-10-15")
    with pytest.raises(guard.BudgetDenied):
        ledger.settle(
            "one",
            0,
            8,
            result() | changes,
            day="2026-10-15",
            run_budget=limits.run_budget,
        )
    with pytest.raises(guard.BudgetDenied):
        ledger.reserve("two", limits, new_session=True, day="2026-10-15")
    ledger.db.close()


def test_parallel_process_connections_cannot_double_admit(tmp_path, limits):
    path = tmp_path / "budget.sqlite3"
    initial = guard.Ledger(path)
    initial.db.close()

    def reserve(session):
        ledger = guard.Ledger(path)
        try:
            ledger.reserve(session, limits, new_session=True, day="2026-10-15")
            return True
        except guard.BudgetDenied:
            return False
        finally:
            ledger.db.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(reserve, ("one", "two"))) == [False, True]


def test_corrupt_or_symlink_ledger_denied(tmp_path):
    corrupt = tmp_path / "bad.sqlite3"
    corrupt.write_text("invalid accounting")
    with pytest.raises(Exception):
        guard.Ledger(corrupt)
    link = tmp_path / "link.sqlite3"
    link.symlink_to(corrupt)
    with pytest.raises(guard.BudgetDenied):
        guard.Ledger(link)


@pytest.mark.parametrize(
    "name,value",
    [
        ("MAX_TOKENS", "0"),
        ("MAX_TURNS", "-1"),
        ("MAX_TURNS", "bad"),
        ("RUN_BUDGET_USD", "NaN"),
        ("DAILY_BUDGET_USD", "Infinity"),
        ("DAILY_BUDGET_USD", "0"),
        ("DAILY_BUDGET_USD", "0.0000001"),
    ],
)
def test_invalid_limits_fail_closed(monkeypatch, name, value):
    monkeypatch.setenv("TELEGRAM_AGENT_BOT_CLAUDE_API_DAILY_BUDGET_USD", "5")
    monkeypatch.setenv("TELEGRAM_AGENT_BOT_CLAUDE_API_" + name, value)
    with pytest.raises(Exception):
        guard.Limits.from_env()


def test_default_budget_disables_api_mode(monkeypatch):
    monkeypatch.delenv("TELEGRAM_AGENT_BOT_CLAUDE_API_DAILY_BUDGET_USD", raising=False)
    with pytest.raises(guard.BudgetDenied):
        guard.Limits.from_env()


def test_calls_use_token_cap_remaining_session_turns_and_budget(tmp_path, limits):
    calls = []

    def run(args, **kwargs):
        calls.append((args, kwargs))
        if args[-1] == "--version":
            return subprocess.CompletedProcess(args, 0, "2.1.217 (Claude Code)", "")
        session = args[-1]
        turns = 3 if "--session-id" in args else 5
        return subprocess.CompletedProcess(
            args, 0, json.dumps(result(session, turns)), ""
        )

    with (
        patch.object(guard.subprocess, "run", side_effect=run),
        patch("builtins.input", side_effect=["first", "second", "third"]),
    ):
        with pytest.raises(guard.BudgetDenied, match="turn limit"):
            guard.run_session(["claude"], tmp_path / "budget.sqlite3", limits)
    assert len(calls) == 3  # version probe, then exactly two API print runs
    for index, expected in ((1, "8"), (2, "5")):
        args, kwargs = calls[index]
        assert args[args.index("--max-turns") + 1] == expected
        assert args[args.index("--max-budget-usd") + 1] == "0.5"
        assert kwargs["env"]["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] == "4096"
        assert "--print" in args and "stream-json" not in args
        assert kwargs["input"] in {"first", "second"}


@pytest.mark.parametrize(
    "options",
    [
        ["--max-turns", "999"],
        ["--max-budget-usd=999"],
        ["--settings", "{}"],
        ["-c"],
        ["--print"],
        ["--input-format", "stream-json"],
    ],
)
def test_conflicting_command_cannot_bypass_guard(options):
    with pytest.raises(guard.BudgetDenied):
        guard._command_options(["claude", *options])


def test_old_cli_fails_before_ledger_or_api(tmp_path, limits):
    version = subprocess.CompletedProcess([], 0, "2.1.216 (Claude Code)", "")
    with patch.object(guard.subprocess, "run", return_value=version) as run:
        with pytest.raises(guard.BudgetDenied, match="2.1.217"):
            guard.run_session(["claude"], tmp_path / "budget.sqlite3", limits)
    run.assert_called_once()
    assert not (tmp_path / "budget.sqlite3").exists()


def test_api_launch_is_guarded_but_subscription_launch_is_unchanged():
    api = _agent_command_for_launch(AgentProfile(agent_type="claude"))
    subscription = _agent_command_for_launch(AgentProfile(agent_type="claudeofficial"))
    assert "telegram_agent_bot.claude_api_session" in api
    assert "telegram_agent_bot.claude_api_session" not in subscription


@pytest.mark.parametrize(
    "base_url",
    [
        "https://proxy.example/anthropic",
        "http://api.anthropic.com",
        "https://api.anthropic.com.example",
    ],
)
def test_custom_billing_routes_fail_before_cli(tmp_path, limits, monkeypatch, base_url):
    monkeypatch.setenv("ANTHROPIC_BASE_URL", base_url)
    with patch.object(guard.subprocess, "run") as run:
        with pytest.raises(guard.BudgetDenied, match="verified cost"):
            guard.run_session(["claude"], tmp_path / "budget.sqlite3", limits)
    run.assert_not_called()


@pytest.mark.parametrize("daily", ["0", "bad", "NaN"])
def test_main_invalid_budget_never_starts_cli(tmp_path, monkeypatch, daily):
    monkeypatch.setattr(
        guard.sys,
        "argv",
        [
            "guard",
            "--state-path",
            str(tmp_path / "budget.sqlite3"),
            "--daily-budget-usd",
            daily,
            "--",
            "claude",
        ],
    )
    with patch.object(guard.subprocess, "run") as run:
        assert guard.main() == 3
    run.assert_not_called()


def test_launcher_passes_current_bot_limits_explicitly(monkeypatch):
    monkeypatch.setenv("TELEGRAM_AGENT_BOT_CLAUDE_API_MAX_TOKENS", "2048")
    monkeypatch.setenv("TELEGRAM_AGENT_BOT_CLAUDE_API_DAILY_BUDGET_USD", "3")
    launch = _agent_command_for_launch(AgentProfile(agent_type="claude"))
    assert "--max-tokens 2048" in launch and "--daily-budget-usd 3" in launch
