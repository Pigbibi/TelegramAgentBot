"""Bound API-backed Claude sessions using one guarded print run per input."""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, ROUND_CEILING
from pathlib import Path


class BudgetDenied(ValueError):
    """No API request may be made after this accounting failure."""


def _positive_int(value: str) -> int:
    if not value.isdecimal() or int(value) <= 0:
        raise BudgetDenied("API limits must be positive integers")
    return int(value)


def _usd_units(value: str) -> int:
    amount = Decimal(value)
    units = amount * 1_000_000
    if not amount.is_finite() or amount <= 0 or units != units.to_integral_value():
        raise BudgetDenied("API budgets must be positive USD values")
    return int(units)


@dataclass(frozen=True)
class Limits:
    max_tokens: int
    max_turns: int
    run_budget: int
    daily_budget: int

    @classmethod
    def from_env(cls, values: Mapping[str, str] | None = None) -> Limits:
        prefix = "TELEGRAM_AGENT_BOT_CLAUDE_API_"
        values = os.environ if values is None else values
        limits = cls(
            _positive_int(values.get(prefix + "MAX_TOKENS", "4096")),
            _positive_int(values.get(prefix + "MAX_TURNS", "8")),
            _usd_units(values.get(prefix + "RUN_BUDGET_USD", "0.50")),
            _usd_units(values.get(prefix + "DAILY_BUDGET_USD", "0")),
        )
        if limits.run_budget > limits.daily_budget:
            raise BudgetDenied("API run budget exceeds daily budget")
        return limits


class Ledger:
    """Serialize reservations across sessions/processes; never refund daily quota."""

    def __init__(self, path: Path):
        if path.is_symlink() or not path.parent.is_dir():
            raise BudgetDenied("API budget state is unavailable")
        fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        os.close(fd)
        self.db = sqlite3.connect(path, timeout=5)
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS days "
            "(day TEXT PRIMARY KEY, reserved INTEGER NOT NULL, blocked INTEGER NOT NULL)"
        )
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS sessions "
            "(id TEXT PRIMARY KEY, turns INTEGER NOT NULL, blocked INTEGER NOT NULL)"
        )
        self.db.commit()

    def reserve(
        self, session_id: str, limits: Limits, *, new_session: bool, day: str
    ) -> tuple[int, int]:
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            if self.db.execute(
                "SELECT 1 FROM days WHERE blocked != 0 LIMIT 1"
            ).fetchone():
                raise BudgetDenied("An API run has unresolved daily accounting")
            row = self.db.execute(
                "SELECT turns, blocked FROM sessions WHERE id=?", (session_id,)
            ).fetchone()
            if row is None:
                if not new_session:
                    raise BudgetDenied("Resumed API session has no trusted accounting")
                self.db.execute("INSERT INTO sessions VALUES (?, 0, 0)", (session_id,))
                row = (0, 0)
            used, blocked = row
            if blocked or not isinstance(used, int) or not 0 <= used < limits.max_turns:
                raise BudgetDenied(
                    "API session turn limit or unresolved run blocks input"
                )
            row = self.db.execute(
                "SELECT reserved FROM days WHERE day=?", (day,)
            ).fetchone()
            reserved = row[0] if row else 0
            if not isinstance(reserved, int) or reserved < 0:
                raise BudgetDenied("Invalid API budget accounting")
            if reserved + limits.run_budget > limits.daily_budget:
                raise BudgetDenied("Daily API budget exhausted")
            self.db.execute(
                "INSERT INTO days VALUES (?, ?, 1) ON CONFLICT(day) "
                "DO UPDATE SET reserved=excluded.reserved, blocked=1",
                (day, reserved + limits.run_budget),
            )
            # Reserve every remaining turn before execution. A crash, timeout,
            # or malformed result leaves the session blocked, including on resume.
            self.db.execute(
                "UPDATE sessions SET turns=?, blocked=1 WHERE id=?",
                (limits.max_turns, session_id),
            )
            return used, limits.max_turns - used

    def settle(
        self,
        session_id: str,
        previous: int,
        remaining: int,
        result: dict,
        *,
        day: str,
        run_budget: int,
    ) -> None:
        turns = result.get("num_turns")
        if (
            result.get("session_id") != session_id
            or result.get("is_error") is not False
            or type(turns) is not int
            or not 0 < turns <= remaining
        ):
            raise BudgetDenied("API result accounting is missing or invalid")
        cost = result.get("total_cost_usd")
        if type(cost) not in (int, float):
            raise BudgetDenied("API cost accounting is missing")
        amount = Decimal(str(cost))
        if not amount.is_finite() or amount <= 0:
            raise BudgetDenied("API cost accounting is invalid")
        charged = int((amount * 1_000_000).to_integral_value(rounding=ROUND_CEILING))
        if charged > run_budget:
            raise BudgetDenied(
                "API cost exceeded its reservation; further calls blocked"
            )
        with self.db:
            self.db.execute(
                "UPDATE sessions SET turns=?, blocked=0 WHERE id=?",
                (previous + turns, session_id),
            )
            self.db.execute("UPDATE days SET blocked=0 WHERE day=?", (day,))


def _command_options(command: list[str]) -> tuple[list[str], str, bool]:
    """Allow launch options, but reject flags that could replace the guard."""
    if not command or Path(command[0]).name != "claude":
        raise BudgetDenied("API mode requires the Claude CLI executable")
    command = list(command)
    session_id = str(uuid.uuid4())
    new_session = True
    if "--resume" in command:
        index = command.index("--resume")
        if index + 1 >= len(command):
            raise BudgetDenied("API resume requires an accounted session ID")
        session_id = str(uuid.UUID(command[index + 1]))
        del command[index : index + 2]
        new_session = False
    valued = {"--model", "--effort", "--permission-mode"}
    flags: set[str] = set()
    index = 1
    while index < len(command):
        if command[index] in valued and index + 1 < len(command):
            value = command[index + 1]
            if value.startswith("-") or (
                command[index] == "--permission-mode" and value != "default"
            ):
                raise BudgetDenied("Unsupported API launch option")
            index += 2
        elif command[index] in flags:
            index += 1
        else:
            raise BudgetDenied("Unsupported API launch option")
    return command, session_id, new_session


def run_session(command: list[str], state_path: Path, limits: Limits) -> int:
    command, session_id, new_session = _command_options(command)
    base_url = os.getenv("ANTHROPIC_BASE_URL", "https://api.anthropic.com").rstrip("/")
    if base_url != "https://api.anthropic.com" or any(
        os.getenv(name, "").lower() not in {"", "0", "false"}
        for name in (
            "CLAUDE_CODE_USE_BEDROCK",
            "CLAUDE_CODE_USE_VERTEX",
            "CLAUDE_CODE_USE_FOUNDRY",
        )
    ):
        raise BudgetDenied("Custom API route has no verified cost accounting")
    version = subprocess.run(
        [command[0], "--version"],
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    )
    match = re.search(r"\b(\d+)\.(\d+)\.(\d+)\b", version.stdout)
    if not match or tuple(map(int, match.groups())) < (2, 1, 217):
        raise BudgetDenied("API mode requires Claude Code 2.1.217 or newer")
    ledger = Ledger(state_path)
    try:
        while True:
            try:
                prompt = input("❯ ")
            except EOFError:
                return 0
            if not prompt.strip():
                continue
            day = datetime.now(timezone.utc).date().isoformat()
            previous, remaining = ledger.reserve(
                session_id, limits, new_session=new_session, day=day
            )
            args = command + [
                "--print",
                "--input-format",
                "text",
                "--output-format",
                "json",
                "--max-turns",
                str(remaining),
                "--max-budget-usd",
                str(Decimal(limits.run_budget) / 1_000_000),
                "--disallowedTools",
                "Agent",
                "Task",
                "--session-id" if new_session else "--resume",
                session_id,
            ]
            env = os.environ.copy()
            env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] = str(limits.max_tokens)
            env["MAX_THINKING_TOKENS"] = str(max(1, limits.max_tokens - 1))
            # Auxiliary compaction/title requests and subagent runs must not
            # create work outside the main session's configured limits.
            env["DISABLE_COMPACT"] = "1"
            env["CLAUDE_CODE_DISABLE_TERMINAL_TITLE"] = "1"
            env["CLAUDE_CODE_DISABLE_BACKGROUND_TASKS"] = "1"
            completed = subprocess.run(
                args, input=prompt, capture_output=True, text=True, env=env, timeout=600
            )
            if completed.returncode != 0:
                raise BudgetDenied("API run failed; accounting remains blocked")
            result = json.loads(completed.stdout)
            if not isinstance(result, dict):
                raise BudgetDenied("API result accounting is invalid")
            ledger.settle(
                session_id,
                previous,
                remaining,
                result,
                day=day,
                run_budget=limits.run_budget,
            )
            new_session = False
            print(result.get("result", ""), flush=True)
    finally:
        ledger.db.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-path", type=Path, required=True)
    for option in ("max-tokens", "max-turns", "run-budget-usd", "daily-budget-usd"):
        parser.add_argument("--" + option)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    values = dict(os.environ)
    for name in ("MAX_TOKENS", "MAX_TURNS", "RUN_BUDGET_USD", "DAILY_BUDGET_USD"):
        value = getattr(args, name.lower())
        if value is not None:
            values["TELEGRAM_AGENT_BOT_CLAUDE_API_" + name] = value
    try:
        return run_session(command, args.state_path, Limits.from_env(values))
    except BudgetDenied as exc:
        print(f"API session blocked: {exc}", file=sys.stderr)
        return 3
    except Exception:
        # Never forward command lines, environment, provider stderr, or raw
        # error payloads: those can contain credentials or private prompts.
        print(
            "API session blocked by budget/turn guard. Check local accounting.",
            file=sys.stderr,
        )
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
