"""Self-update helpers for source-checkout telegram-agent-bot installs.

The updater intentionally supports only git checkouts.  That matches the
bootstrap scripts and avoids trying to mutate package-manager controlled
installations such as pipx or uv tool environments.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import shlex
import subprocess
import sys
import time
from collections.abc import Awaitable
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

from dotenv import load_dotenv

from .utils import atomic_write_json, app_dir

logger = logging.getLogger(__name__)

DEFAULT_UPDATE_INTERVAL_SECONDS = 24 * 60 * 60
DEFAULT_BUSY_RETRY_SECONDS = 5 * 60
DEFAULT_CHECK_TIMEOUT_SECONDS = 30
DEFAULT_UPDATE_TIMEOUT_SECONDS = 180
DEFAULT_CODEX_PACKAGE = "@openai/codex"
DEFAULT_CLAUDE_PACKAGE = "@anthropic/claude-code"
TRUTHY_VALUES = {"1", "true", "yes", "on", "y"}
FALSY_VALUES = {"0", "false", "no", "off", "n", ""}
_SEMVER_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?")


@dataclass(frozen=True)
class UpdateSettings:
    """Runtime settings for telegram-agent-bot self-update checks."""

    enabled: bool = False
    require_idle: bool = True
    interval_seconds: int = DEFAULT_UPDATE_INTERVAL_SECONDS
    busy_retry_seconds: int = DEFAULT_BUSY_RETRY_SECONDS
    remote: str | None = None
    branch: str | None = None
    state_file: Path | None = None
    run_uv_sync: bool = True
    uv_executable: str | None = None
    check_timeout_seconds: int = DEFAULT_CHECK_TIMEOUT_SECONDS
    update_timeout_seconds: int = DEFAULT_UPDATE_TIMEOUT_SECONDS


@dataclass(frozen=True)
class CodexUpdateSettings:
    """Runtime settings for agent CLI update checks.

    Supports both Codex CLI and Claude Code CLI.
    The ``package`` field determines which npm package to check:

    - codex:  @openai/codex
    - claude: @anthropic/claude-code
    """

    enabled: bool = False
    auto_update: bool = False
    package: str = DEFAULT_CODEX_PACKAGE
    codex_executable: str | None = None
    npm_executable: str | None = None
    agent_type: str = "codex"
    check_timeout_seconds: int = DEFAULT_CHECK_TIMEOUT_SECONDS
    update_timeout_seconds: int = DEFAULT_UPDATE_TIMEOUT_SECONDS


@dataclass(frozen=True)
class CommandResult:
    """Small subprocess result wrapper used by the updater and tests."""

    args: tuple[str, ...]
    returncode: int
    stdout: str = ""
    stderr: str = ""


@dataclass(frozen=True)
class UpdateResult:
    """Result of a self-update check."""

    checked: bool
    supported: bool
    updated: bool = False
    update_available: bool = False
    restart_required: bool = False
    skipped_reason: str | None = None
    message: str = ""
    ref: str | None = None
    commits_behind: int = 0
    old_revision: str | None = None
    new_revision: str | None = None


@dataclass(frozen=True)
class CodexUpdateResult:
    """Result of a Codex CLI update check."""

    checked: bool
    supported: bool
    updated: bool = False
    update_available: bool = False
    skipped_reason: str | None = None
    message: str = ""
    current_version: str | None = None
    latest_version: str | None = None


CommandRunner = Callable[[Sequence[str], Path, int], CommandResult]
CodexUpdateNotifier = Callable[[CodexUpdateResult], Awaitable[None]]


class UpdateError(RuntimeError):
    """Raised when an update command fails."""


def _parse_bool(value: str | None, *, default: bool = False) -> bool:
    if value is None:
        return default

    normalized = value.strip().lower()
    if normalized in TRUTHY_VALUES:
        return True
    if normalized in FALSY_VALUES:
        return False
    return default


def _parse_int(value: str | None, *, default: int) -> int:
    if value is None:
        return default
    try:
        return int(value.strip())
    except ValueError:
        return default


def load_update_env() -> None:
    """Load .env files needed before Config is constructed.

    Config performs the same load later for the normal bot settings.  The
    updater runs earlier so it can update code before importing the rest of the
    application.
    """

    local_env = Path(".env")
    global_env = app_dir() / ".env"
    if local_env.is_file():
        load_dotenv(local_env)
    if global_env.is_file():
        load_dotenv(global_env)


def load_update_settings() -> UpdateSettings:
    """Build updater settings from environment variables."""

    config_dir = app_dir()
    state_path = os.getenv("TELEGRAM_AGENT_BOT_UPDATE_STATE_FILE")
    uv_executable = os.getenv("TELEGRAM_AGENT_BOT_UPDATE_UV") or shutil.which("uv")
    return UpdateSettings(
        enabled=_parse_bool(os.getenv("TELEGRAM_AGENT_BOT_AUTO_UPDATE"), default=False),
        require_idle=_parse_bool(
            os.getenv("TELEGRAM_AGENT_BOT_UPDATE_REQUIRE_IDLE"), default=True
        ),
        interval_seconds=max(
            0,
            _parse_int(
                os.getenv("TELEGRAM_AGENT_BOT_UPDATE_INTERVAL_SECONDS"),
                default=DEFAULT_UPDATE_INTERVAL_SECONDS,
            ),
        ),
        busy_retry_seconds=max(
            60,
            _parse_int(
                os.getenv("TELEGRAM_AGENT_BOT_UPDATE_BUSY_RETRY_SECONDS"),
                default=DEFAULT_BUSY_RETRY_SECONDS,
            ),
        ),
        remote=(os.getenv("TELEGRAM_AGENT_BOT_UPDATE_REMOTE") or "").strip() or None,
        branch=(os.getenv("TELEGRAM_AGENT_BOT_UPDATE_BRANCH") or "").strip() or None,
        state_file=Path(state_path).expanduser()
        if state_path
        else config_dir / "update_state.json",
        run_uv_sync=_parse_bool(
            os.getenv("TELEGRAM_AGENT_BOT_UPDATE_RUN_UV_SYNC"), default=True
        ),
        uv_executable=uv_executable,
    )


def _extract_executable(command: str) -> str | None:
    """Best-effort extraction of the executable from a shell-ish command string."""

    try:
        parts = shlex.split(command)
    except ValueError:
        return None

    for part in parts:
        if part == "env":
            continue
        if "=" in part and not part.startswith(("/", "./", "../")):
            name, _, _value = part.partition("=")
            if name.replace("_", "").isalnum():
                continue
        return part
    return None


def _split_command(command: str | None) -> list[str]:
    """Split a configured command string into argv parts."""
    if not command:
        return []
    try:
        parts = shlex.split(command)
    except ValueError:
        return [command]
    return parts or [command]


def load_codex_update_settings() -> CodexUpdateSettings:
    """Build agent CLI updater settings from environment variables.

    Agent type is read from config to determine:
    - codex:  @openai/codex (default)
    - claude: @anthropic/claude-code
    """
    from .config import config

    agent_type = getattr(config, "agent_type", "codex")
    default_package = (
        DEFAULT_CLAUDE_PACKAGE if agent_type == "claude" else DEFAULT_CODEX_PACKAGE
    )
    default_executable = "claude" if agent_type == "claude" else "codex"

    codex_command = os.getenv(
        "TELEGRAM_AGENT_BOT_CODEX_COMMAND", default_executable
    )
    codex_executable = _extract_executable(codex_command) or default_executable
    return CodexUpdateSettings(
        enabled=_parse_bool(
            os.getenv("TELEGRAM_AGENT_BOT_CODEX_UPDATE_CHECK"), default=False
        ),
        auto_update=_parse_bool(
            os.getenv("TELEGRAM_AGENT_BOT_CODEX_AUTO_UPDATE"), default=False
        ),
        package=(
            os.getenv("TELEGRAM_AGENT_BOT_CODEX_UPDATE_PACKAGE")
            or default_package
        ),
        codex_executable=shutil.which(codex_executable) or codex_executable,
        npm_executable=os.getenv("TELEGRAM_AGENT_BOT_CODEX_UPDATE_NPM")
        or shutil.which("npm"),
        agent_type=agent_type,
    )


def find_git_repo(start: Path | None = None) -> Path | None:
    """Find the nearest parent directory containing a .git entry."""

    current = (start or Path(__file__)).resolve()
    if current.is_file():
        current = current.parent

    for path in (current, *current.parents):
        if (path / ".git").exists():
            return path
    return None


def run_command(args: Sequence[str], cwd: Path, timeout_seconds: int) -> CommandResult:
    """Run a command and capture output."""

    completed = subprocess.run(
        list(args),
        cwd=str(cwd),
        text=True,
        capture_output=True,
        timeout=timeout_seconds,
        check=False,
    )
    return CommandResult(
        args=tuple(args),
        returncode=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
    )


def _run_checked(
    runner: CommandRunner,
    args: Sequence[str],
    cwd: Path,
    timeout_seconds: int,
) -> CommandResult:
    result = runner(args, cwd, timeout_seconds)
    if result.returncode != 0:
        stderr = result.stderr.strip()
        stdout = result.stdout.strip()
        detail = stderr or stdout or f"exit code {result.returncode}"
        raise UpdateError(f"{' '.join(args)} failed: {detail}")
    return result


def _run_git(
    runner: CommandRunner,
    repo_dir: Path,
    git_args: Sequence[str],
    timeout_seconds: int,
) -> CommandResult:
    return _run_checked(runner, ["git", *git_args], repo_dir, timeout_seconds)


def _read_update_state(path: Path | None) -> dict[str, object]:
    if path is None or not path.is_file():
        return {}
    try:
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_update_state(path: Path | None, now: float, result: UpdateResult) -> None:
    if path is None:
        return

    payload = {
        "last_checked_at": int(now),
        "last_result": {
            "checked": result.checked,
            "supported": result.supported,
            "updated": result.updated,
            "update_available": result.update_available,
            "skipped_reason": result.skipped_reason,
            "message": result.message,
            "ref": result.ref,
            "commits_behind": result.commits_behind,
            "old_revision": result.old_revision,
            "new_revision": result.new_revision,
        },
    }
    try:
        atomic_write_json(path, payload)
    except OSError as exc:
        logger.debug("Failed to write update state %s: %s", path, exc)


def _check_due(settings: UpdateSettings, now: float) -> bool:
    if settings.interval_seconds <= 0:
        return True

    state = _read_update_state(settings.state_file)
    last_checked = state.get("last_checked_at")
    if not isinstance(last_checked, int | float):
        return True

    return now - float(last_checked) >= settings.interval_seconds


def _short_revision(
    runner: CommandRunner,
    repo_dir: Path,
    timeout_seconds: int,
) -> str | None:
    try:
        result = _run_git(
            runner, repo_dir, ["rev-parse", "--short", "HEAD"], timeout_seconds
        )
    except UpdateError:
        return None
    revision = result.stdout.strip()
    return revision or None


def _working_tree_clean(
    runner: CommandRunner,
    repo_dir: Path,
    timeout_seconds: int,
) -> bool:
    result = _run_git(runner, repo_dir, ["status", "--porcelain"], timeout_seconds)
    return result.stdout.strip() == ""


def _resolve_ref(
    runner: CommandRunner,
    repo_dir: Path,
    settings: UpdateSettings,
) -> tuple[str, str, str | None]:
    """Return (remote, ref, branch) for the update target."""

    if settings.remote and settings.branch:
        return settings.remote, f"{settings.remote}/{settings.branch}", settings.branch

    upstream = ""
    try:
        result = _run_git(
            runner,
            repo_dir,
            ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"],
            settings.check_timeout_seconds,
        )
        upstream = result.stdout.strip()
    except UpdateError:
        upstream = ""

    if upstream and upstream != "@{u}":
        remote, _, branch = upstream.partition("/")
        if settings.remote:
            remote = settings.remote
            upstream = f"{remote}/{branch}" if branch else remote
        if settings.branch:
            branch = settings.branch
            upstream = f"{remote}/{branch}" if remote else branch
        return remote or "origin", upstream, branch or None

    remote = settings.remote or "origin"
    branch = settings.branch
    if not branch:
        try:
            result = _run_git(
                runner,
                repo_dir,
                ["branch", "--show-current"],
                settings.check_timeout_seconds,
            )
            branch = result.stdout.strip() or "main"
        except UpdateError:
            branch = "main"
    return remote, f"{remote}/{branch}", branch


def _fetch_ref(
    runner: CommandRunner,
    repo_dir: Path,
    settings: UpdateSettings,
    remote: str,
    _branch: str | None,
) -> None:
    args = ["fetch", "--quiet", "--prune", remote]
    _run_git(runner, repo_dir, args, settings.check_timeout_seconds)


def _commits_behind(
    runner: CommandRunner,
    repo_dir: Path,
    ref: str,
    timeout_seconds: int,
) -> int:
    result = _run_git(
        runner, repo_dir, ["rev-list", "--count", f"HEAD..{ref}"], timeout_seconds
    )
    try:
        return int(result.stdout.strip())
    except ValueError as exc:
        raise UpdateError(
            f"Unable to parse git rev-list output: {result.stdout!r}"
        ) from exc


def _run_uv_sync(
    runner: CommandRunner,
    repo_dir: Path,
    settings: UpdateSettings,
) -> None:
    if not settings.run_uv_sync or not settings.uv_executable:
        return
    _run_checked(
        runner,
        [settings.uv_executable, "sync"],
        repo_dir,
        settings.update_timeout_seconds,
    )


def _extract_version(text: str) -> str | None:
    match = _SEMVER_RE.search(text)
    return match.group(0) if match else None


def _version_parts(version: str) -> tuple[int, int, int, str] | None:
    match = _SEMVER_RE.fullmatch(version)
    if not match:
        return None
    major, minor, patch, prerelease = match.groups()
    return int(major), int(minor), int(patch), prerelease or ""


def _version_is_newer(candidate: str, current: str) -> bool:
    candidate_parts = _version_parts(candidate)
    current_parts = _version_parts(current)
    if candidate_parts is None or current_parts is None:
        return candidate != current

    candidate_base = candidate_parts[:3]
    current_base = current_parts[:3]
    if candidate_base != current_base:
        return candidate_base > current_base

    candidate_pre = candidate_parts[3]
    current_pre = current_parts[3]
    if candidate_pre == current_pre:
        return False
    if not candidate_pre and current_pre:
        return True
    if candidate_pre and not current_pre:
        return False
    return candidate_pre > current_pre


def _npm_global_package_installed(
    settings: CodexUpdateSettings,
    runner: CommandRunner,
    cwd: Path,
) -> bool:
    npm_command = _split_command(settings.npm_executable)
    if not npm_command:
        return False
    result = runner(
        [
            *npm_command,
            "list",
            "-g",
            "--depth=0",
            settings.package,
            "--json",
        ],
        cwd,
        settings.check_timeout_seconds,
    )
    if result.returncode != 0:
        return False
    try:
        payload = json.loads(result.stdout or "{}")
    except json.JSONDecodeError:
        return False
    dependencies = payload.get("dependencies")
    return isinstance(dependencies, dict) and settings.package in dependencies


def _agent_display_name(settings: CodexUpdateSettings) -> str:
    """Return the human-readable agent name for log messages."""
    return "Claude Code" if settings.agent_type == "claude" else "Codex CLI"


def check_codex_update(
    settings: CodexUpdateSettings,
    *,
    apply_update: bool = False,
    runner: CommandRunner = run_command,
    cwd: Path | None = None,
) -> CodexUpdateResult:
    """Check the installed agent CLI against npm's latest package version.

    Supports both Codex CLI (``@openai/codex``) and Claude Code
    (``@anthropic/claude-code``) via ``settings.agent_type``.
    """

    agent_name = _agent_display_name(settings)
    work_dir = cwd or find_git_repo() or Path.cwd()
    npm_command = _split_command(settings.npm_executable)
    if not settings.codex_executable:
        return CodexUpdateResult(
            checked=True,
            supported=False,
            skipped_reason="missing_codex",
            message=f"{agent_name} update check skipped: executable not found.",
        )
    if not npm_command:
        return CodexUpdateResult(
            checked=True,
            supported=False,
            skipped_reason="missing_npm",
            message=f"{agent_name} update check skipped: npm executable not found.",
        )

    try:
        current_result = _run_checked(
            runner,
            [settings.codex_executable, "--version"],
            work_dir,
            settings.check_timeout_seconds,
        )
        current_version = _extract_version(current_result.stdout)
        if not current_version:
            return CodexUpdateResult(
                checked=True,
                supported=False,
                skipped_reason="unknown_current_version",
                message=(
                    f"{agent_name} update check skipped: unable to parse "
                    f"version output {current_result.stdout.strip()!r}."
                ),
            )

        latest_result = _run_checked(
            runner,
            [*npm_command, "view", settings.package, "version"],
            work_dir,
            settings.check_timeout_seconds,
        )
        latest_version = _extract_version(latest_result.stdout)
        if not latest_version:
            return CodexUpdateResult(
                checked=True,
                supported=False,
                skipped_reason="unknown_latest_version",
                current_version=current_version,
                message=(
                    f"{agent_name} update check skipped: unable to parse npm "
                    f"version output {latest_result.stdout.strip()!r}."
                ),
            )

        if not _version_is_newer(latest_version, current_version):
            return CodexUpdateResult(
                checked=True,
                supported=True,
                current_version=current_version,
                latest_version=latest_version,
                message=f"{agent_name} is up to date ({current_version}).",
            )

        if not apply_update:
            return CodexUpdateResult(
                checked=True,
                supported=True,
                update_available=True,
                current_version=current_version,
                latest_version=latest_version,
                message=(
                    f"{agent_name} update available: "
                    f"{current_version} -> {latest_version}."
                ),
            )

        if not _npm_global_package_installed(settings, runner, work_dir):
            return CodexUpdateResult(
                checked=True,
                supported=False,
                update_available=True,
                skipped_reason="unsupported_install",
                current_version=current_version,
                latest_version=latest_version,
                message=(
                    f"{agent_name} update skipped: installed "
                    f"{settings.codex_executable} is not detected "
                    f"as a global npm package ({settings.package})."
                ),
            )

        _run_checked(
            runner,
            [*npm_command, "install", "-g", f"{settings.package}@latest"],
            work_dir,
            settings.update_timeout_seconds,
        )
        refreshed = _run_checked(
            runner,
            [settings.codex_executable, "--version"],
            work_dir,
            settings.check_timeout_seconds,
        )
        new_version = _extract_version(refreshed.stdout) or latest_version
        return CodexUpdateResult(
            checked=True,
            supported=True,
            updated=True,
            update_available=True,
            current_version=new_version,
            latest_version=latest_version,
            message=f"Updated {agent_name} from {current_version} to {new_version}.",
        )
    except (OSError, subprocess.SubprocessError, UpdateError) as exc:
        return CodexUpdateResult(
            checked=True,
            supported=True,
            skipped_reason="error",
            message=f"{agent_name} update check failed: {exc}",
        )


def check_and_apply_update(
    settings: UpdateSettings,
    *,
    repo_dir: Path | None = None,
    check_only: bool = False,
    force: bool = False,
    runner: CommandRunner = run_command,
    now: float | None = None,
) -> UpdateResult:
    """Check for git updates and optionally fast-forward the source checkout."""

    timestamp = time.time() if now is None else now
    repo = repo_dir or find_git_repo()
    if repo is None or not (repo / ".git").exists():
        return UpdateResult(
            checked=True,
            supported=False,
            skipped_reason="not_git_checkout",
            message=(
                "Self-update is only supported when telegram-agent-bot runs from a git checkout."
            ),
        )

    if not force and not _check_due(settings, timestamp):
        return UpdateResult(
            checked=False,
            supported=True,
            skipped_reason="interval",
            message="Update check skipped because the interval has not elapsed.",
        )

    try:
        if not check_only and not _working_tree_clean(
            runner, repo, settings.check_timeout_seconds
        ):
            result = UpdateResult(
                checked=True,
                supported=True,
                skipped_reason="dirty_worktree",
                message="Update skipped because the git working tree has local changes.",
            )
            _write_update_state(settings.state_file, timestamp, result)
            return result

        old_revision = _short_revision(runner, repo, settings.check_timeout_seconds)
        remote, ref, branch = _resolve_ref(runner, repo, settings)
        _fetch_ref(runner, repo, settings, remote, branch)
        behind = _commits_behind(runner, repo, ref, settings.check_timeout_seconds)

        if behind <= 0:
            result = UpdateResult(
                checked=True,
                supported=True,
                ref=ref,
                old_revision=old_revision,
                new_revision=old_revision,
                message=f"telegram-agent-bot is already up to date with {ref}.",
            )
            _write_update_state(settings.state_file, timestamp, result)
            return result

        if check_only:
            result = UpdateResult(
                checked=True,
                supported=True,
                update_available=True,
                ref=ref,
                commits_behind=behind,
                old_revision=old_revision,
                message=f"Update available: {behind} commit(s) behind {ref}.",
            )
            _write_update_state(settings.state_file, timestamp, result)
            return result

        _run_git(runner, repo, ["pull", "--ff-only"], settings.update_timeout_seconds)
        _run_uv_sync(runner, repo, settings)
        new_revision = _short_revision(runner, repo, settings.check_timeout_seconds)
        result = UpdateResult(
            checked=True,
            supported=True,
            updated=True,
            update_available=True,
            restart_required=True,
            ref=ref,
            commits_behind=behind,
            old_revision=old_revision,
            new_revision=new_revision,
            message=f"Updated telegram-agent-bot from {old_revision or 'unknown'} to {new_revision or 'unknown'}.",
        )
        _write_update_state(settings.state_file, timestamp, result)
        return result
    except (OSError, subprocess.SubprocessError, UpdateError) as exc:
        result = UpdateResult(
            checked=True,
            supported=True,
            skipped_reason="error",
            message=f"Update check failed: {exc}",
        )
        _write_update_state(settings.state_file, timestamp, result)
        return result


def restart_current_process(argv: Sequence[str] | None = None) -> None:
    """Replace the current process with the same command."""

    command = list(argv or sys.argv)
    if not command:
        command = [sys.executable, "-m", "telegram_agent_bot.main"]
    os.execvp(command[0], command)


def maybe_auto_update(argv: Sequence[str] | None = None) -> UpdateResult | None:
    """Run the startup auto-update check when enabled."""

    load_update_env()
    settings = load_update_settings()
    if not settings.enabled:
        return None
    if settings.require_idle:
        logger.info("Startup auto-update deferred until telegram-agent-bot is idle")
        return None

    result = check_and_apply_update(settings)
    if result.skipped_reason == "error":
        logger.warning(result.message)
    elif result.checked:
        logger.info(result.message)
    else:
        logger.debug(result.message)

    if result.updated and result.restart_required:
        logger.warning("telegram-agent-bot updated; restarting current process")
        restart_current_process(argv)
    return result


async def get_update_blockers() -> list[str]:
    """Return reasons automatic update should wait for an idle window."""

    from .handlers.message_queue import has_pending_message_work
    from .session import _is_shell_pane_command
    from .terminal_parser import is_interactive_ui, parse_status_update
    from .tmux_manager import tmux_manager

    blockers: list[str] = []
    if has_pending_message_work():
        blockers.append("Telegram message queue is not empty")

    windows = await tmux_manager.list_windows()
    for window in windows:
        label = window.window_name or window.window_id
        pane_cmd = (window.pane_current_command or "").strip()
        if _is_shell_pane_command(pane_cmd):
            continue

        pane_text = await tmux_manager.capture_pane(window.window_id)
        if pane_text is None:
            blockers.append(f"{label}: pane capture failed")
            continue
        if not pane_text.strip():
            blockers.append(f"{label}: pane state is unclear")
            continue
        if is_interactive_ui(pane_text):
            blockers.append(f"{label}: waiting for interactive input")
            continue

        status_text = parse_status_update(pane_text)
        if status_text:
            blockers.append(f"{label}: {status_text.splitlines()[0]}")

    return blockers


async def auto_update_loop(argv: Sequence[str] | None = None) -> None:
    """Periodically check for updates while the bot is running."""

    await auto_update_loop_with_notifier(argv)


async def _report_codex_update_result(
    codex_result: CodexUpdateResult,
    codex_settings: CodexUpdateSettings,
    *,
    codex_update_notifier: CodexUpdateNotifier | None = None,
) -> None:
    """Log agent CLI update status and optionally notify for manual approval."""
    if codex_result.skipped_reason == "error":
        logger.warning(codex_result.message)
    elif codex_result.update_available and not codex_result.updated:
        logger.warning(codex_result.message)
        if not codex_settings.auto_update and codex_update_notifier is not None:
            await codex_update_notifier(codex_result)
    elif codex_result.checked:
        logger.info(codex_result.message)


async def auto_update_loop_with_notifier(
    argv: Sequence[str] | None = None,
    *,
    codex_update_notifier: CodexUpdateNotifier | None = None,
) -> None:
    """Periodically check for updates and notify when the agent CLI needs approval.

    Supports both Codex CLI and Claude Code CLI via the configured agent_type.
    """

    load_update_env()
    settings = load_update_settings()
    codex_settings = load_codex_update_settings()
    if (
        not settings.enabled
        and not codex_settings.enabled
        or settings.interval_seconds <= 0
    ):
        return

    interval_seconds = max(settings.interval_seconds, 60)
    next_delay = 10.0
    while True:
        await asyncio.sleep(next_delay)
        next_delay = interval_seconds

        if settings.require_idle:
            blockers = await get_update_blockers()
            if blockers:
                logger.info(
                    "Auto-update deferred; telegram-agent-bot is not idle: %s",
                    "; ".join(blockers[:5]),
                )
                next_delay = settings.busy_retry_seconds
                continue

        if settings.enabled:
            result = await asyncio.to_thread(
                check_and_apply_update,
                settings,
            )
            if result.skipped_reason == "error":
                logger.warning(result.message)
            elif result.checked:
                logger.info(result.message)

            if result.updated and result.restart_required:
                logger.warning("telegram-agent-bot updated; restarting current process")
                restart_current_process(argv)

        if codex_settings.enabled:
            codex_result = await asyncio.to_thread(
                check_codex_update,
                codex_settings,
                apply_update=codex_settings.auto_update,
            )
            await _report_codex_update_result(
                codex_result,
                codex_settings,
                codex_update_notifier=codex_update_notifier,
            )


def _print_update_usage() -> None:
    print(
        "Usage:\n"
        "  telegram-agent-bot update           Fast-forward the git checkout if an update exists\n"
        "  telegram-agent-bot update --check   Check only; do not change files\n"
        "  telegram-agent-bot update --help    Show this help message"
    )


def _agent_update_display_name() -> str:
    """Return the display name for the configured agent's update command."""
    try:
        from .config import config
        return "Claude Code" if config.agent_type == "claude" else "Codex CLI"
    except Exception:
        return "agent CLI"


def _print_codex_update_usage() -> None:
    agent_display = _agent_update_display_name()
    print(
        f"Usage:\n"
        f"  telegram-agent-bot codex-update           Update global npm {agent_display} if needed\n"
        f"  telegram-agent-bot codex-update --check   Check only; do not change files\n"
        f"  telegram-agent-bot codex-update --help    Show this help message"
    )


def update_main(argv: Sequence[str]) -> int:
    """CLI entry point for manual update checks."""

    if any(arg in {"-h", "--help", "help"} for arg in argv):
        _print_update_usage()
        return 0

    unknown = [arg for arg in argv if arg not in {"--check"}]
    if unknown:
        print(f"Unknown update arguments: {' '.join(unknown)}", file=sys.stderr)
        _print_update_usage()
        return 2

    load_update_env()
    settings = load_update_settings()
    result = check_and_apply_update(
        settings,
        check_only="--check" in argv,
        force=True,
    )
    print(result.message)
    if result.updated:
        print("Restart telegram-agent-bot to use the updated code.")
    return 0 if result.supported and result.skipped_reason != "error" else 1


def codex_update_main(argv: Sequence[str]) -> int:
    """CLI entry point for manual agent CLI update checks.

    Supports both Codex CLI and Claude Code CLI via the configured agent_type.
    """

    if any(arg in {"-h", "--help", "help"} for arg in argv):
        _print_codex_update_usage()
        return 0

    unknown = [arg for arg in argv if arg not in {"--check"}]
    if unknown:
        print(f"Unknown codex-update arguments: {' '.join(unknown)}", file=sys.stderr)
        _print_codex_update_usage()
        return 2

    load_update_env()
    settings = load_codex_update_settings()
    result = check_codex_update(
        settings,
        apply_update="--check" not in argv,
    )
    print(result.message)
    return 0 if result.supported and result.skipped_reason != "error" else 1
