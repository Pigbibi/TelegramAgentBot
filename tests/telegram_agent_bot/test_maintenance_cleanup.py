from __future__ import annotations

import os
import time
from pathlib import Path

from telegram_agent_bot import maintenance_cleanup as cleanup


def _config(tmp_path: Path, *, dry_run: bool = False) -> cleanup.CleanupConfig:
    home = tmp_path / "home"
    home.mkdir()
    protected = (home / ".telegram-agent-bot", home / "Projects" / "TelegramAgentBot")
    for path in protected:
        path.mkdir(parents=True)
    return cleanup.CleanupConfig(
        home=home,
        dry_run=dry_run,
        yes=not dry_run,
        force=True,
        tmp_path=tmp_path / "tmp",
        protected_paths=tuple(path.resolve() for path in protected),
    )


def test_remove_path_refuses_protected_runtime_dir(tmp_path: Path) -> None:
    config = _config(tmp_path)
    stats = cleanup.CleanupStats()
    protected_file = config.home / ".telegram-agent-bot" / "state.json"
    protected_file.write_text("state")

    cleanup.remove_path(config.home / ".telegram-agent-bot", config, stats)

    assert protected_file.exists()
    assert stats.removed_paths == []
    assert stats.skipped_paths
    assert "protected" in stats.skipped_paths[0]


def test_clean_action_runner_skips_work_when_worker_is_active(tmp_path: Path) -> None:
    config = _config(tmp_path)
    runner = config.home / "actions-runner-demo"
    work = runner / "_work"
    diag = runner / "_diag"
    work.mkdir(parents=True)
    diag.mkdir()
    (runner / ".runner").write_text("{}")
    (work / "checkout").mkdir()
    (work / "checkout" / "file.txt").write_text("keep while active")
    (diag / "old.log").write_text("keep while active")

    stats = cleanup.CleanupStats()
    cleanup.clean_action_runner(
        runner,
        config,
        stats,
        process_lines=[f"123 Runner.Worker {runner}/bin/Runner.Worker spawnclient"],
    )

    assert (work / "checkout" / "file.txt").exists()
    assert (diag / "old.log").exists()
    assert any("active runner worker" in item for item in stats.skipped_paths)


def test_clean_action_runner_removes_work_and_old_versions(tmp_path: Path) -> None:
    config = _config(tmp_path)
    runner = config.home / "actions-runner-demo"
    runner.mkdir(parents=True)
    (runner / ".runner").write_text("{}")
    work = runner / "_work"
    work.mkdir()
    (work / "checkout").mkdir()
    (work / "checkout" / "file.txt").write_text("remove")

    current_bin = runner / "bin.2.335.1"
    old_bin = runner / "bin.2.334.0"
    current_ext = runner / "externals.2.335.1"
    old_ext = runner / "externals.2.334.0"
    for path in (current_bin, old_bin, current_ext, old_ext):
        path.mkdir()
        (path / "marker").write_text(path.name)
    os.symlink(current_bin, runner / "bin")
    os.symlink(current_ext, runner / "externals")

    stats = cleanup.CleanupStats()
    cleanup.clean_action_runner(runner, config, stats, process_lines=[])

    assert not (work / "checkout").exists()
    assert current_bin.exists()
    assert current_ext.exists()
    assert not old_bin.exists()
    assert not old_ext.exists()


def test_clean_tmp_keeps_bot_locks_and_removes_old_artifacts(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config.tmp_path.mkdir()
    lock_dir = config.tmp_path / "telegram-agent-bot-lock-active"
    old_artifact = config.tmp_path / "old-artifact"
    lock_dir.mkdir()
    old_artifact.mkdir()
    cutoff = time.time() - 5 * 24 * 60 * 60
    os.utime(lock_dir, (cutoff, cutoff))
    os.utime(old_artifact, (cutoff, cutoff))

    stats = cleanup.CleanupStats()
    cleanup.clean_tmp(config, stats)

    assert lock_dir.exists()
    assert not old_artifact.exists()
