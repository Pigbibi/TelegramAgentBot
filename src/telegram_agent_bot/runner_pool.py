"""On-demand supervisor for repository-scoped GitHub Actions runners.

The controller is intentionally a short-lived polling command instead of a
resident daemon. A systemd timer invokes it periodically; queued workflow runs
start their repository runner, and listeners stop after an idle grace period.
Any GitHub API uncertainty fails open by keeping an online listener running.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Sequence
from urllib.parse import urlparse

from .utils import app_dir, atomic_write_json

logger = logging.getLogger(__name__)

_SERVICE_PATTERN = re.compile(r"^actions\.runner\.[A-Za-z0-9_.-]+\.service$")
_REPOSITORY_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


@dataclass(frozen=True, slots=True)
class RunnerTarget:
    root: Path
    service: str
    repository: str


@dataclass(frozen=True, slots=True)
class RunnerObservation:
    service_active: bool
    worker_active: bool
    queued_runs: int
    api_ok: bool = True


@dataclass(frozen=True, slots=True)
class RunnerDecision:
    action: str
    last_needed_at_epoch: float
    reason: str


@dataclass(slots=True)
class RunnerPoolStats:
    started: int = 0
    stopped: int = 0
    kept: int = 0
    errors: int = 0


def load_runner_target(root: Path) -> RunnerTarget:
    """Load and validate one runner's repository and systemd service metadata."""
    resolved = root.expanduser().resolve()
    service = (resolved / ".service").read_text(encoding="utf-8").strip()
    if not _SERVICE_PATTERN.fullmatch(service):
        raise ValueError(f"invalid runner service name in {resolved / '.service'}")

    metadata = json.loads((resolved / ".runner").read_text(encoding="utf-8-sig"))
    github_url = str(metadata.get("gitHubUrl") or "")
    parsed = urlparse(github_url)
    path_parts = [part for part in parsed.path.split("/") if part]
    if (
        parsed.scheme != "https"
        or parsed.netloc != "github.com"
        or len(path_parts) != 2
    ):
        raise ValueError(f"invalid repository URL in {resolved / '.runner'}")
    repository = "/".join(path_parts)
    if not _REPOSITORY_PATTERN.fullmatch(repository):
        raise ValueError(f"invalid repository name in {resolved / '.runner'}")
    return RunnerTarget(resolved, service, repository)


def decide_runner_action(
    observation: RunnerObservation,
    *,
    last_needed_at_epoch: float,
    now_epoch: float,
    idle_timeout_seconds: float,
) -> RunnerDecision:
    """Return a conservative start/stop/keep decision for one runner."""
    if not observation.api_ok:
        action = "keep" if observation.service_active else "start"
        return RunnerDecision(
            action,
            now_epoch,
            "GitHub queue status unavailable; failing open",
        )
    if observation.worker_active or observation.queued_runs > 0:
        action = "keep" if observation.service_active else "start"
        reason = "worker active" if observation.worker_active else "workflow queued"
        return RunnerDecision(action, now_epoch, reason)
    if not observation.service_active:
        return RunnerDecision("keep", last_needed_at_epoch, "idle and offline")
    if now_epoch - last_needed_at_epoch >= idle_timeout_seconds:
        return RunnerDecision("stop", last_needed_at_epoch, "idle grace elapsed")
    return RunnerDecision("keep", last_needed_at_epoch, "inside idle grace")


def _run_command(
    command: Sequence[str], *, timeout: float = 30.0
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(command),
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def queued_workflow_runs(
    repository: str,
    *,
    command_runner: Callable[..., subprocess.CompletedProcess[str]] = _run_command,
) -> tuple[bool, int]:
    """Return GitHub API availability and queued workflow-run count."""
    result = command_runner(
        [
            "gh",
            "api",
            "--method",
            "GET",
            f"repos/{repository}/actions/runs",
            "-f",
            "status=queued",
            "-f",
            "per_page=1",
            "--jq",
            ".total_count",
        ],
        timeout=30.0,
    )
    if result.returncode != 0:
        return False, 0
    try:
        return True, max(0, int(result.stdout.strip()))
    except ValueError:
        return False, 0


def service_is_active(
    service: str,
    *,
    command_runner: Callable[..., subprocess.CompletedProcess[str]] = _run_command,
) -> bool:
    result = command_runner(
        ["sudo", "-n", "systemctl", "is-active", "--quiet", service],
        timeout=15.0,
    )
    return result.returncode == 0


def _process_lines() -> list[str]:
    result = _run_command(["ps", "-eo", "pid=,args="], timeout=15.0)
    return result.stdout.splitlines() if result.returncode == 0 else []


def runner_worker_active(root: Path, process_lines: Sequence[str]) -> bool:
    """Conservatively detect a Runner.Worker owned by one configured root."""
    worker_lines = [line for line in process_lines if "Runner.Worker" in line]
    if not worker_lines:
        return False
    root_text = str(root.resolve())
    if any(root_text in line for line in worker_lines):
        return True
    # Worker command lines vary across runner releases. If ownership cannot be
    # proven, keep every managed listener online rather than stopping a job.
    return True


class RunnerPoolController:
    """Reconcile a bounded list of repository runner services once."""

    def __init__(
        self,
        *,
        state_file: Path,
        idle_timeout_seconds: float,
        dry_run: bool = False,
    ) -> None:
        self.state_file = state_file
        self.idle_timeout_seconds = max(60.0, idle_timeout_seconds)
        self.dry_run = dry_run

    def _load_state(self) -> dict[str, float]:
        try:
            payload = json.loads(self.state_file.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            return {}
        values = payload.get("last_needed_at_epoch", {})
        if not isinstance(values, dict):
            return {}
        result: dict[str, float] = {}
        for service, value in values.items():
            if isinstance(service, str) and isinstance(value, (int, float)):
                result[service] = float(value)
        return result

    def _save_state(self, state: dict[str, float]) -> None:
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(
            self.state_file,
            {"last_needed_at_epoch": state},
        )
        os.chmod(self.state_file, 0o600)

    def _service_action(self, action: str, target: RunnerTarget) -> bool:
        if self.dry_run:
            logger.info("DRY-RUN %s runner %s", action, target.service)
            return True
        result = _run_command(
            ["sudo", "-n", "systemctl", action, target.service],
            timeout=30.0,
        )
        if result.returncode == 0:
            return True
        logger.error(
            "Failed to %s runner %s: %s",
            action,
            target.service,
            result.stderr.strip(),
        )
        return False

    def run_once(
        self,
        targets: Sequence[RunnerTarget],
        *,
        now_epoch: float | None = None,
    ) -> RunnerPoolStats:
        current_time = time.time() if now_epoch is None else now_epoch
        state = self._load_state()
        processes = _process_lines()
        stats = RunnerPoolStats()

        for target in targets:
            active = service_is_active(target.service)
            api_ok, queued = queued_workflow_runs(target.repository)
            worker = runner_worker_active(target.root, processes)
            last_needed = state.get(target.service, current_time)
            decision = decide_runner_action(
                RunnerObservation(active, worker, queued, api_ok),
                last_needed_at_epoch=last_needed,
                now_epoch=current_time,
                idle_timeout_seconds=self.idle_timeout_seconds,
            )
            state[target.service] = decision.last_needed_at_epoch
            if decision.action in {"start", "stop"}:
                if self._service_action(decision.action, target):
                    if decision.action == "start":
                        stats.started += 1
                    else:
                        stats.stopped += 1
                else:
                    stats.errors += 1
            else:
                stats.kept += 1
            logger.info(
                "Runner %s repo=%s action=%s reason=%s queued=%d active=%s worker=%s",
                target.service,
                target.repository,
                decision.action,
                decision.reason,
                queued,
                active,
                worker,
            )

        self._save_state(state)
        return stats


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="Run one reconciliation.")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--runner-root",
        action="append",
        type=Path,
        required=True,
        help="Explicit runner directory to manage (repeatable).",
    )
    parser.add_argument(
        "--idle-timeout-seconds",
        type=float,
        default=float(
            os.getenv("TELEGRAM_AGENT_BOT_RUNNER_IDLE_TIMEOUT_SECONDS", "600")
        ),
    )
    parser.add_argument(
        "--state-file",
        type=Path,
        default=app_dir() / "runner_pool_state.json",
    )
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    logging.basicConfig(
        format="%(asctime)s - %(levelname)s - %(message)s",
        level=logging.INFO,
    )
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    if not args.once:
        raise SystemExit("--once is required; use a timer for recurring checks")
    try:
        targets = [load_runner_target(root) for root in args.runner_root]
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        logger.error("Invalid runner configuration: %s", exc)
        return 2
    stats = RunnerPoolController(
        state_file=args.state_file,
        idle_timeout_seconds=args.idle_timeout_seconds,
        dry_run=args.dry_run,
    ).run_once(targets)
    logger.info(
        "Runner pool summary: started=%d stopped=%d kept=%d errors=%d",
        stats.started,
        stats.stopped,
        stats.kept,
        stats.errors,
    )
    return 1 if stats.errors else 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
