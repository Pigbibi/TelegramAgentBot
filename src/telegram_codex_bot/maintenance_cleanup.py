"""Conservative VPS cleanup helper for TelegramAgentBot hosts.

The cleanup policy is intentionally narrow: remove rebuildable caches and
GitHub Actions runner work products while protecting TelegramAgentBot runtime
state and skipping runner workspaces when a job worker is active.
"""

from __future__ import annotations

import argparse
import fnmatch
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Sequence


DEFAULT_MAX_USED_PERCENT = 85.0
DEFAULT_MIN_FREE_GB = 4.0
DEFAULT_TMP_RETENTION_DAYS = 2
DEFAULT_CODEX_SESSION_RETENTION_DAYS = 30
DEFAULT_RUNNER_DIAG_RETENTION_DAYS = 7
LOCK_PATH = Path("/tmp/telegram-codex-vps-cleanup.lock")

TMP_EXCLUDE_PATTERNS = (
    ".ICE-unix",
    ".X11-unix",
    ".XIM-unix",
    ".font-unix",
    "ccbot-lock-*",
    "codex-bwrap-*",
    "hsperfdata_*",
    "snap-private-tmp",
    "systemd-private-*",
    "telegram-codex-bot-lock-*",
)


@dataclass(slots=True)
class CleanupConfig:
    home: Path
    dry_run: bool = False
    yes: bool = False
    force: bool = False
    max_used_percent: float = DEFAULT_MAX_USED_PERCENT
    min_free_gb: float = DEFAULT_MIN_FREE_GB
    tmp_retention_days: int = DEFAULT_TMP_RETENTION_DAYS
    codex_session_retention_days: int = DEFAULT_CODEX_SESSION_RETENTION_DAYS
    runner_diag_retention_days: int = DEFAULT_RUNNER_DIAG_RETENTION_DAYS
    root_path: Path = Path("/")
    tmp_path: Path = Path("/tmp")
    protected_paths: tuple[Path, ...] = ()


@dataclass(slots=True)
class CleanupStats:
    removed_paths: list[Path] = field(default_factory=list)
    skipped_paths: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    estimated_bytes: int = 0

    def record_removed(self, path: Path, size: int) -> None:
        self.removed_paths.append(path)
        self.estimated_bytes += max(0, int(size))

    def record_skip(self, message: str) -> None:
        self.skipped_paths.append(message)

    def record_error(self, message: str) -> None:
        self.errors.append(message)


def _resolve(path: Path) -> Path:
    return path.expanduser().resolve(strict=False)


def _is_relative_to(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
    except ValueError:
        return False
    return True


def _conflicts_with_protected_path(path: Path, protected_paths: Sequence[Path]) -> bool:
    resolved = _resolve(path)
    for protected in protected_paths:
        protected_resolved = _resolve(protected)
        if resolved == protected_resolved:
            return True
        if _is_relative_to(resolved, protected_resolved):
            return True
        if _is_relative_to(protected_resolved, resolved):
            return True
    return False


def build_protected_paths(home: Path) -> tuple[Path, ...]:
    """Return runtime paths that this cleanup helper must never remove."""
    runtime_dir = Path(
        os.getenv("TELEGRAM_CODEX_BOT_DIR", str(home / ".telegram-codex-bot"))
    )
    explicit_paths = [
        runtime_dir,
        runtime_dir / "app" / "TelegramAgentBot",
        home / "Projects" / "TelegramAgentBot",
    ]
    extra = os.getenv("TELEGRAM_CODEX_CLEANUP_PROTECTED_PATHS", "")
    for raw_path in extra.split(","):
        raw_path = raw_path.strip()
        if raw_path:
            explicit_paths.append(Path(raw_path))
    return tuple(_resolve(path) for path in explicit_paths)


def path_size(path: Path) -> int:
    if not path.exists() and not path.is_symlink():
        return 0
    if path.is_file() or path.is_symlink():
        try:
            return path.lstat().st_size
        except OSError:
            return 0
    total = 0
    for root, dirs, files in os.walk(path, topdown=True, onerror=lambda _err: None):
        root_path = Path(root)
        for name in dirs:
            try:
                total += (root_path / name).lstat().st_size
            except OSError:
                continue
        for name in files:
            try:
                total += (root_path / name).lstat().st_size
            except OSError:
                continue
    return total


def remove_path(path: Path, config: CleanupConfig, stats: CleanupStats) -> None:
    path = _resolve(path)
    if _conflicts_with_protected_path(path, config.protected_paths):
        stats.record_skip(f"protected: {path}")
        return
    if not path.exists() and not path.is_symlink():
        return

    size = path_size(path)
    if config.dry_run:
        print(f"DRY-RUN remove {path} ({_format_bytes(size)})")
        stats.record_removed(path, size)
        return

    try:
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        else:
            path.unlink()
        print(f"removed {path} ({_format_bytes(size)})")
        stats.record_removed(path, size)
    except OSError as exc:
        stats.record_error(f"{path}: {exc}")


def clear_directory_contents(
    path: Path, config: CleanupConfig, stats: CleanupStats
) -> None:
    path = _resolve(path)
    if _conflicts_with_protected_path(path, config.protected_paths):
        stats.record_skip(f"protected directory: {path}")
        return
    if not path.is_dir():
        return
    for child in path.iterdir():
        remove_path(child, config, stats)


def _read_process_lines() -> list[str]:
    try:
        completed = subprocess.run(
            ["ps", "-eo", "pid=,comm=,args="],
            check=False,
            text=True,
            capture_output=True,
        )
    except OSError:
        return []
    return completed.stdout.splitlines()


def runner_worker_active(runner_dir: Path, process_lines: Sequence[str]) -> bool:
    """Return True if a GitHub Actions worker appears active for this runner."""
    runner = str(_resolve(runner_dir))
    worker_lines = [line for line in process_lines if "Runner.Worker" in line]
    if not worker_lines:
        return False
    for line in worker_lines:
        if runner in line:
            return True
    # Some runner versions omit the cwd in process args. Be conservative when a
    # worker exists and the owning runner cannot be identified.
    return True


def discover_action_runners(home: Path) -> list[Path]:
    roots_env = os.getenv("TELEGRAM_CODEX_CLEANUP_RUNNER_ROOTS", "")
    if roots_env.strip():
        return [
            _resolve(Path(raw.strip())) for raw in roots_env.split(",") if raw.strip()
        ]
    candidates = []
    for path in home.glob("actions-runner-*"):
        if not path.is_dir():
            continue
        if (path / ".runner").exists() or (path / "bin" / "Runner.Listener").exists():
            candidates.append(_resolve(path))
    return sorted(candidates)


def _current_runner_component(path: Path) -> Path | None:
    if not path.exists():
        return None
    try:
        return _resolve(path)
    except OSError:
        return None


def clean_action_runner(
    runner_dir: Path,
    config: CleanupConfig,
    stats: CleanupStats,
    process_lines: Sequence[str],
) -> None:
    active = runner_worker_active(runner_dir, process_lines)
    if active:
        stats.record_skip(f"active runner worker: {runner_dir}")
    else:
        clear_directory_contents(runner_dir / "_work", config, stats)
        clean_old_files(
            runner_dir / "_diag", config.runner_diag_retention_days, config, stats
        )

    current_bin = _current_runner_component(runner_dir / "bin")
    current_externals = _current_runner_component(runner_dir / "externals")
    for child in runner_dir.glob("bin.*"):
        if current_bin is not None and _resolve(child) == current_bin:
            continue
        remove_path(child, config, stats)
    for child in runner_dir.glob("externals.*"):
        if current_externals is not None and _resolve(child) == current_externals:
            continue
        remove_path(child, config, stats)


def clean_action_runners(config: CleanupConfig, stats: CleanupStats) -> None:
    process_lines = _read_process_lines()
    for runner_dir in discover_action_runners(config.home):
        clean_action_runner(runner_dir, config, stats, process_lines)


def clean_common_caches(config: CleanupConfig, stats: CleanupStats) -> None:
    cache_targets = (
        config.home / ".npm" / "_cacache",
        config.home / ".npm" / "_npx",
        config.home / ".gradle" / "caches",
        config.home / ".gradle" / "daemon",
        config.home / ".gradle" / "native",
        config.home / ".gradle" / ".tmp",
        config.home / ".cache" / "ms-playwright",
        config.home / ".cache" / "uv",
        config.home / ".cache" / "gd12345hotlinebot",
    )
    for target in cache_targets:
        remove_path(target, config, stats)
    clear_directory_contents(config.home / ".codex" / ".tmp", config, stats)
    clean_old_files(
        config.home / ".codex" / "sessions",
        config.codex_session_retention_days,
        config,
        stats,
        remove_empty_dirs=True,
    )


def clean_old_files(
    root: Path,
    retention_days: int,
    config: CleanupConfig,
    stats: CleanupStats,
    *,
    remove_empty_dirs: bool = False,
) -> None:
    root = _resolve(root)
    if not root.exists():
        return
    cutoff = time.time() - max(0, retention_days) * 24 * 60 * 60
    if root.is_file():
        try:
            if root.stat().st_mtime < cutoff:
                remove_path(root, config, stats)
        except OSError as exc:
            stats.record_error(f"{root}: {exc}")
        return

    for current, _dirs, files in os.walk(
        root, topdown=False, onerror=lambda _err: None
    ):
        current_path = Path(current)
        for name in files:
            file_path = current_path / name
            try:
                if file_path.stat().st_mtime < cutoff:
                    remove_path(file_path, config, stats)
            except OSError as exc:
                stats.record_error(f"{file_path}: {exc}")
        if remove_empty_dirs and current_path != root:
            try:
                if not any(current_path.iterdir()):
                    remove_path(current_path, config, stats)
            except OSError:
                pass


def _tmp_entry_excluded(path: Path) -> bool:
    name = path.name
    return any(fnmatch.fnmatch(name, pattern) for pattern in TMP_EXCLUDE_PATTERNS)


def clean_tmp(config: CleanupConfig, stats: CleanupStats) -> None:
    root = config.tmp_path
    if not root.is_dir():
        return
    cutoff = time.time() - max(0, config.tmp_retention_days) * 24 * 60 * 60
    for child in root.iterdir():
        if _tmp_entry_excluded(child):
            continue
        try:
            if child.stat().st_mtime >= cutoff:
                continue
        except OSError as exc:
            stats.record_error(f"{child}: {exc}")
            continue
        remove_path(child, config, stats)


def _run_best_effort(command: Sequence[str], *, dry_run: bool) -> None:
    if dry_run:
        print(f"DRY-RUN run {' '.join(command)}")
        return
    try:
        subprocess.run(
            command, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
    except OSError:
        return


def clean_system_caches(config: CleanupConfig) -> None:
    _run_best_effort(["sudo", "-n", "apt-get", "clean"], dry_run=config.dry_run)
    _run_best_effort(
        ["sudo", "-n", "journalctl", "--vacuum-size=100M"],
        dry_run=config.dry_run,
    )


def should_cleanup(config: CleanupConfig) -> tuple[bool, str]:
    if config.force:
        return True, "forced"
    usage = shutil.disk_usage(config.root_path)
    used_percent = 100.0 * (usage.total - usage.free) / usage.total
    free_gb = usage.free / (1024**3)
    if used_percent >= config.max_used_percent:
        return True, f"used_percent={used_percent:.1f} >= {config.max_used_percent:.1f}"
    if free_gb <= config.min_free_gb:
        return True, f"free_gb={free_gb:.1f} <= {config.min_free_gb:.1f}"
    return False, f"used_percent={used_percent:.1f}, free_gb={free_gb:.1f}"


def acquire_lock(path: Path = LOCK_PATH) -> int:
    import fcntl

    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        raise RuntimeError(f"cleanup already running: {path}") from None
    return fd


def release_lock(fd: int) -> None:
    import fcntl

    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def run_cleanup(config: CleanupConfig) -> CleanupStats:
    if not config.dry_run and not config.yes:
        raise SystemExit("Refusing to delete without --yes. Use --dry-run to preview.")

    stats = CleanupStats()
    should_run, reason = should_cleanup(config)
    print(f"cleanup trigger check: {reason}")
    if not should_run:
        return stats

    before = shutil.disk_usage(config.root_path)
    clean_action_runners(config, stats)
    clean_common_caches(config, stats)
    clean_tmp(config, stats)
    clean_system_caches(config)
    after = shutil.disk_usage(config.root_path)

    freed = after.free - before.free
    print(
        "cleanup summary: "
        f"paths={len(stats.removed_paths)} "
        f"estimated={_format_bytes(stats.estimated_bytes)} "
        f"free_delta={_format_bytes(freed)} "
        f"errors={len(stats.errors)} skips={len(stats.skipped_paths)}"
    )
    for message in stats.skipped_paths[:20]:
        print(f"skip: {message}")
    for message in stats.errors[:20]:
        print(f"error: {message}", file=sys.stderr)
    return stats


def _format_bytes(value: int) -> str:
    sign = "-" if value < 0 else ""
    value = abs(int(value))
    units = ("B", "K", "M", "G", "T")
    amount = float(value)
    for unit in units:
        if amount < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{sign}{int(amount)}{unit}"
            return f"{sign}{amount:.1f}{unit}"
        amount /= 1024
    return f"{sign}{amount:.1f}T"


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return float(raw)


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return int(raw)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run", action="store_true", help="Print removals without deleting."
    )
    parser.add_argument("--yes", action="store_true", help="Allow destructive cleanup.")
    parser.add_argument(
        "--force",
        action="store_true",
        help="Run even when disk thresholds are healthy.",
    )
    parser.add_argument(
        "--max-used-percent",
        type=float,
        default=_env_float(
            "TELEGRAM_CODEX_CLEANUP_MAX_USED_PERCENT", DEFAULT_MAX_USED_PERCENT
        ),
    )
    parser.add_argument(
        "--min-free-gb",
        type=float,
        default=_env_float("TELEGRAM_CODEX_CLEANUP_MIN_FREE_GB", DEFAULT_MIN_FREE_GB),
    )
    parser.add_argument(
        "--tmp-retention-days",
        type=int,
        default=_env_int(
            "TELEGRAM_CODEX_CLEANUP_TMP_RETENTION_DAYS", DEFAULT_TMP_RETENTION_DAYS
        ),
    )
    parser.add_argument(
        "--codex-session-retention-days",
        type=int,
        default=_env_int(
            "TELEGRAM_CODEX_CLEANUP_CODEX_SESSION_RETENTION_DAYS",
            DEFAULT_CODEX_SESSION_RETENTION_DAYS,
        ),
    )
    parser.add_argument(
        "--runner-diag-retention-days",
        type=int,
        default=_env_int(
            "TELEGRAM_CODEX_CLEANUP_RUNNER_DIAG_RETENTION_DAYS",
            DEFAULT_RUNNER_DIAG_RETENTION_DAYS,
        ),
    )
    parser.add_argument("--home", type=Path, default=Path.home())
    return parser


def config_from_args(args: argparse.Namespace) -> CleanupConfig:
    home = _resolve(args.home)
    return CleanupConfig(
        home=home,
        dry_run=bool(args.dry_run),
        yes=bool(args.yes),
        force=bool(args.force),
        max_used_percent=float(args.max_used_percent),
        min_free_gb=float(args.min_free_gb),
        tmp_retention_days=int(args.tmp_retention_days),
        codex_session_retention_days=int(args.codex_session_retention_days),
        runner_diag_retention_days=int(args.runner_diag_retention_days),
        protected_paths=build_protected_paths(home),
    )


def main(argv: Iterable[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    config = config_from_args(args)
    try:
        fd = acquire_lock()
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 75
    try:
        run_cleanup(config)
    finally:
        release_lock(fd)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
