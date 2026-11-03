from __future__ import annotations

from contextlib import nullcontext
import json
from pathlib import Path
import stat
import subprocess

import pytest

from telegram_agent_bot import bridge
from telegram_agent_bot.bridge import (
    BridgeConfig,
    BridgeTarget,
    GitHubIssue,
    TmuxTargetNotFoundError,
    build_orchestrator_message,
    build_task_message,
    dispatch_to_tmux,
    load_config,
    process_target,
    process_orchestrator,
    select_issue,
)


def _make_issue(
    number: int,
    title: str,
    *,
    body: str = "body",
    updated_at: str = "2026-05-09T00:00:00Z",
    labels: list[str] | None = None,
    comments: list[dict] | None = None,
) -> GitHubIssue:
    return GitHubIssue(
        number=number,
        title=title,
        body=body,
        url=f"https://github.com/org/repo/issues/{number}",
        updated_at=updated_at,
        labels=labels or [],
        comments=comments or [],
    )


def test_dispatch_reservation_survives_uncertain_send_and_blocks_force(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    issue = _make_issue(1, "Synthetic maintenance")
    target = BridgeTarget(name="alpha", repo="org/repo", window="@1", issue_number=1)
    config = BridgeConfig(targets=[target])
    path = tmp_path / "state.json"
    monkeypatch.setattr(bridge, "fetch_issue", lambda *a, **k: issue)

    def uncertain_send(*args, **kwargs):
        saved = bridge.load_state(path)
        assert saved["targets"]["alpha"]["dispatch_pending"]
        raise subprocess.TimeoutExpired("tmux", 10)

    monkeypatch.setattr(bridge, "dispatch_to_tmux", uncertain_send)
    with pytest.raises(subprocess.TimeoutExpired):
        bridge.run_once(config, {}, state_file=path)
    restarted = bridge.load_state(path)
    monkeypatch.setattr(
        bridge,
        "dispatch_to_tmux",
        lambda *a, **k: pytest.fail("replayed uncertain dispatch"),
    )
    assert bridge.run_once(config, restarted, state_file=path, force=True) == 0


def test_successful_dispatch_receipt_is_persisted_before_next_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    issue = _make_issue(1, "Synthetic maintenance")
    target = BridgeTarget(name="alpha", repo="org/repo", window="@1", issue_number=1)
    path = tmp_path / "state.json"
    monkeypatch.setattr(bridge, "fetch_issue", lambda *a, **k: issue)
    monkeypatch.setattr(bridge, "dispatch_to_tmux", lambda *a, **k: None)
    assert bridge.run_once(BridgeConfig(targets=[target]), {}, state_file=path) == 1
    receipt = bridge.load_state(path)["targets"]["alpha"]
    assert receipt["last_issue_number"] == 1
    assert "dispatch_pending" not in receipt


def test_state_lock_rejects_competing_executor(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    with bridge.state_lock(path):
        with pytest.raises(BlockingIOError):
            with bridge.state_lock(path):
                pytest.fail("second executor acquired active state")


def test_cli_reports_competing_executor_without_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "state.json"
    monkeypatch.setattr(bridge, "_load_config", lambda _: BridgeConfig(targets=[]))
    monkeypatch.setattr(bridge, "_run_config", lambda *_: pytest.fail("dispatched"))
    with bridge.state_lock(path):
        assert bridge.main(["--state-file", str(path)]) != 0
    assert "already running" in caplog.text


def test_reservation_write_failure_prevents_send(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    issue = _make_issue(1, "Synthetic maintenance")
    target = BridgeTarget(name="alpha", repo="org/repo", window="@1", issue_number=1)
    monkeypatch.setattr(bridge, "fetch_issue", lambda *a, **k: issue)
    monkeypatch.setattr(
        bridge, "save_state", lambda *a: (_ for _ in ()).throw(OSError("disk full"))
    )
    monkeypatch.setattr(
        bridge,
        "dispatch_to_tmux",
        lambda *a, **k: pytest.fail("sent before durable reservation"),
    )
    with pytest.raises(OSError):
        bridge.run_once(
            BridgeConfig(targets=[target]), {}, state_file=tmp_path / "state.json"
        )


@pytest.mark.parametrize("mode", ["targets", "orchestrator"])
@pytest.mark.parametrize("created_by_lock", [False, True])
@pytest.mark.parametrize("relative", [False, True])
def test_new_state_ancestors_are_durable_before_send(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    created_by_lock: bool,
    relative: bool,
) -> None:
    monkeypatch.chdir(tmp_path)
    path = Path("new/nested/state.json")
    if not relative:
        path = tmp_path / path
    target = BridgeTarget(name="alpha", repo="org/repo", window="@1", issue_number=1)
    config = BridgeConfig(bridge_mode=mode, targets=[target])
    monkeypatch.setattr(bridge, "fetch_issue", lambda *a, **k: _make_issue(1, "Test"))
    synced: set[tuple[int, int]] = set()
    real_fsync = bridge.os.fsync

    def record_fsync(fd: int) -> None:
        real_fsync(fd)
        info = bridge.os.fstat(fd)
        if stat.S_ISDIR(info.st_mode):
            synced.add((info.st_dev, info.st_ino))

    monkeypatch.setattr(bridge.os, "fsync", record_fsync)
    sends = []

    def send(*args, **kwargs) -> None:
        parent = path.parent.resolve()
        for ancestor in (parent, *parent.parents):
            info = ancestor.stat()
            assert (info.st_dev, info.st_ino) in synced
        sends.append(True)

    monkeypatch.setattr(bridge, "dispatch_to_tmux", send)
    with bridge.state_lock(path) if created_by_lock else nullcontext():
        assert bridge.run_once(config, {}, state_file=path, issue_number=1) == 1
    assert sends == [True]


@pytest.mark.parametrize("mode", ["targets", "orchestrator"])
@pytest.mark.parametrize("created_by_lock", [False, True])
def test_ancestor_sync_failure_prevents_send(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    created_by_lock: bool,
) -> None:
    path = tmp_path / "new/nested/state.json"
    target = BridgeTarget(name="alpha", repo="org/repo", window="@1", issue_number=1)
    config = BridgeConfig(bridge_mode=mode, targets=[target])
    monkeypatch.setattr(bridge, "fetch_issue", lambda *a, **k: _make_issue(1, "Test"))
    ancestor = tmp_path.stat()
    real_fsync = bridge.os.fsync

    def fail_ancestor_sync(fd: int) -> None:
        info = bridge.os.fstat(fd)
        if (info.st_dev, info.st_ino) == (ancestor.st_dev, ancestor.st_ino):
            raise OSError("synthetic ancestor sync failure")
        real_fsync(fd)

    monkeypatch.setattr(bridge.os, "fsync", fail_ancestor_sync)
    monkeypatch.setattr(
        bridge,
        "dispatch_to_tmux",
        lambda *a, **k: pytest.fail("sent before ancestor metadata was durable"),
    )
    with bridge.state_lock(path) if created_by_lock else nullcontext():
        with pytest.raises(OSError, match="synthetic ancestor sync failure"):
            bridge.run_once(config, {}, state_file=path, issue_number=1)


def test_orchestrator_blocks_uncertain_dispatch_and_dry_run_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    issue = _make_issue(1, "Synthetic maintenance")
    config = BridgeConfig(bridge_mode="orchestrator")
    path = tmp_path / "state.json"
    monkeypatch.setattr(bridge, "fetch_issue", lambda *a, **k: issue)
    monkeypatch.setattr(
        bridge,
        "dispatch_to_tmux",
        lambda *a, **k: pytest.fail("uncertain send replayed"),
    )
    state = {"orchestrator": {"dispatch_pending": {"fingerprint": "previous"}}}
    assert (
        bridge.run_once(config, state, state_file=path, issue_number=1, force=True) == 0
    )
    assert (
        bridge.run_once(config, {}, state_file=path, issue_number=1, dry_run=True) == 1
    )
    assert not path.exists()


def test_input_side_effects_are_never_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bridge, "_tmux_target_exists", lambda *a, **k: True)
    calls = []
    monkeypatch.setattr(
        bridge,
        "_run_subprocess_with_retry",
        lambda args, **kwargs: calls.append((args, kwargs)),
    )
    monkeypatch.setattr(bridge.subprocess, "run", lambda *a, **k: None)
    dispatch_to_tmux("@1", "synthetic", attempts=5)
    assert [
        kwargs["attempts"]
        for args, kwargs in calls
        if "paste-buffer" in args or "send-keys" in args
    ] == [1, 1]


def test_load_config(tmp_path: Path) -> None:
    cfg_path = tmp_path / "bridge.json"
    cfg_path.write_text(
        json.dumps(
            {
                "bridge_mode": "targets",
                "tmux_socket": "telegram-agent-bot",
                "issue_limit": 20,
                "body_limit": 100,
                "comment_limit": 2,
                "poll_interval_seconds": 60,
                "targets": [
                    {
                        "name": "alpha",
                        "repo": "owner/repo",
                        "window": "@1",
                        "workspace": "/tmp/repo",
                        "labels": ["codex-bridge"],
                        "query": "monthly review",
                        "merge_mode": "auto",
                        "merge_label": "auto-merge-ok",
                        "extra_instructions": "Keep it small.",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    cfg = load_config(cfg_path)
    assert cfg.tmux_socket == "telegram-agent-bot"
    assert cfg.issue_limit == 20
    assert cfg.body_limit == 100
    assert cfg.comment_limit == 2
    assert cfg.poll_interval_seconds == 60
    assert cfg.targets[0].name == "alpha"
    assert cfg.targets[0].repo == "owner/repo"
    assert cfg.targets[0].query == "monthly review"
    assert cfg.targets[0].merge_mode == "auto"
    assert cfg.targets[0].merge_label == "auto-merge-ok"
    assert cfg.runner_window == "Ubuntu"


def test_load_config_orchestrator_mode(tmp_path: Path) -> None:
    cfg_path = tmp_path / "bridge.json"
    cfg_path.write_text(
        json.dumps(
            {
                "bridge_mode": "orchestrator",
                "source_repo": "owner/control-plane",
                "source_label": "monthly-review",
                "source_query": "Monthly Audit Review",
                "runner_window": "@42",
                "runner_workspace": "/tmp/runner",
                "runner_extra_instructions": "Keep it minimal.",
                "tmux_socket": "telegram-agent-bot",
            }
        ),
        encoding="utf-8",
    )

    cfg = load_config(cfg_path)
    assert cfg.bridge_mode == "orchestrator"
    assert cfg.source_repo == "owner/control-plane"
    assert cfg.source_label == "monthly-review"
    assert cfg.source_query == "Monthly Audit Review"
    assert cfg.runner_window == "@42"
    assert cfg.runner_workspace == "/tmp/runner"
    assert cfg.runner_extra_instructions == "Keep it minimal."


def test_select_issue_prefers_latest_matching_issue() -> None:
    issues = [
        _make_issue(1, "skip me", labels=["other"], updated_at="2026-05-09T00:00:00Z"),
        _make_issue(
            2, "match later", labels=["codex-bridge"], updated_at="2026-05-09T01:00:00Z"
        ),
        _make_issue(
            3,
            "match earlier",
            labels=["codex-bridge"],
            updated_at="2026-05-09T00:30:00Z",
        ),
    ]

    selected = select_issue(issues, labels=["codex-bridge"])
    assert selected is not None
    assert selected.number == 2


def test_select_issue_matches_query_terms() -> None:
    issues = [
        _make_issue(
            1, "build bridge", body="monthly review ready", labels=["codex-bridge"]
        ),
        _make_issue(2, "unrelated", body="different text", labels=["codex-bridge"]),
    ]

    selected = select_issue(issues, labels=["codex-bridge"], query="monthly review")
    assert selected is not None
    assert selected.number == 1


def test_build_task_message_includes_guardrails_and_comments() -> None:
    target = BridgeTarget(
        name="alpha",
        repo="owner/repo",
        window="@1",
        workspace="/home/ubuntu/Projects/repo",
        labels=["codex-bridge"],
        extra_instructions="Only touch the bridge script.",
    )
    issue = _make_issue(
        9,
        "Improve bridge",
        body="x" * 5000,
        comments=[
            {"author": {"login": "alice"}, "body": "please keep it minimal"},
            {"author": {"login": "bob"}, "body": "add tests"},
        ],
    )
    cfg = BridgeConfig(targets=[target], body_limit=200, comment_limit=1)

    text = build_task_message(target, issue, cfg)
    assert "Target: alpha" in text
    assert "Workspace: /home/ubuntu/Projects/repo" in text
    assert "Only touch the bridge script." in text
    assert "x" * 200 not in text
    assert "Latest comments:" in text
    assert "@alice" in text
    assert "@bob" not in text


def test_build_task_message_mentions_auto_merge_gate() -> None:
    target = BridgeTarget(
        name="alpha",
        repo="owner/repo",
        window="@1",
        merge_mode="auto",
        merge_label="auto-merge-ok",
    )
    issue = _make_issue(10, "Auto merge")
    cfg = BridgeConfig(targets=[target])

    text = build_task_message(target, issue, cfg)
    assert "Add the `auto-merge-ok` label" in text
    assert "Do not merge unless `auto-merge-ok` is present" in text


def test_build_orchestrator_message_includes_monthly_contract() -> None:
    issue = _make_issue(
        17,
        "Monthly Audit Review: 2026-05",
        body='{"month":"2026-05","targets":["owner/repo-a"]}',
        comments=[{"author": {"login": "ops"}, "body": "please keep it scoped"}],
    )
    cfg = BridgeConfig(
        bridge_mode="orchestrator",
        targets=[],
        source_repo="owner/control-plane",
        runner_window="@42",
        runner_workspace="/home/ubuntu/Projects/runner",
        runner_extra_instructions="Keep output concise.",
        body_limit=200,
        comment_limit=1,
    )

    text = build_orchestrator_message(issue, cfg)
    assert "Mode: orchestrator" in text
    assert "Source repo: owner/control-plane" in text
    assert "Treat the issue body and payload as the current contract" in text
    assert "Workspace: /home/ubuntu/Projects/runner" in text
    assert "Keep output concise." in text
    assert "@ops" in text


def test_dispatch_to_tmux_uses_paste_buffer(monkeypatch) -> None:
    calls: list[tuple[list[str], bytes | None]] = []

    def fake_run(
        argv, *, input=None, check=None, capture_output=None, text=None, timeout=None
    ):
        calls.append((list(argv), input if isinstance(input, bytes) else None))

        class Result:
            returncode = 0
            stdout = ""

        return Result()

    monkeypatch.setattr("telegram_agent_bot.bridge.subprocess.run", fake_run)

    dispatch_to_tmux("@1", "hello\nworld\n", socket_name="telegram-agent-bot")

    assert calls[0][0][:4] == ["tmux", "-L", "telegram-agent-bot", "list-panes"]
    assert calls[1][0][:4] == ["tmux", "-L", "telegram-agent-bot", "load-buffer"]
    assert calls[2][0][:4] == ["tmux", "-L", "telegram-agent-bot", "paste-buffer"]
    assert calls[3][0][:4] == ["tmux", "-L", "telegram-agent-bot", "send-keys"]
    assert calls[3][0][-1] == "C-m"


def test_dispatch_to_tmux_raises_when_target_missing(monkeypatch) -> None:
    def fake_run(
        argv, *, input=None, check=None, capture_output=None, text=None, timeout=None
    ):
        class Result:
            stdout = ""
            returncode = 1 if "list-panes" in argv else 0

        return Result()

    monkeypatch.setattr("telegram_agent_bot.bridge.subprocess.run", fake_run)

    try:
        dispatch_to_tmux("Ubuntu", "hello\n", socket_name="telegram-agent-bot")
    except TmuxTargetNotFoundError as exc:
        assert "Ubuntu" in str(exc)
    else:
        raise AssertionError("expected missing tmux target")


def test_process_target_skips_duplicate_issue(monkeypatch, tmp_path: Path) -> None:
    target = BridgeTarget(name="alpha", repo="owner/repo", window="@1")
    cfg = BridgeConfig(targets=[target])
    issue = _make_issue(4, "duplicate")
    state = {"targets": {"alpha": {"last_fingerprint": "4:2026-05-09T00:00:00Z"}}}

    monkeypatch.setattr(
        "telegram_agent_bot.bridge.list_open_issues",
        lambda repo, limit, **kwargs: [issue],
    )
    monkeypatch.setattr(
        "telegram_agent_bot.bridge.fetch_issue",
        lambda repo, issue_number, **kwargs: issue,
    )
    monkeypatch.setattr(
        "telegram_agent_bot.bridge.dispatch_to_tmux",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("should not send")
        ),
    )

    dispatched = process_target(target, cfg, state)
    assert dispatched is False


def test_process_target_skips_missing_tmux_target(monkeypatch) -> None:
    target = BridgeTarget(name="alpha", repo="owner/repo", window="Ubuntu")
    cfg = BridgeConfig(targets=[target])
    issue = _make_issue(4, "dispatch me", labels=["codex-bridge"])
    state: dict = {}

    monkeypatch.setattr(
        "telegram_agent_bot.bridge.list_open_issues",
        lambda repo, limit, **kwargs: [issue],
    )
    monkeypatch.setattr(
        "telegram_agent_bot.bridge.fetch_issue",
        lambda repo, issue_number, **kwargs: issue,
    )
    monkeypatch.setattr(
        "telegram_agent_bot.bridge.dispatch_to_tmux",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            TmuxTargetNotFoundError("tmux target not found: Ubuntu")
        ),
    )

    dispatched = process_target(target, cfg, state)

    assert dispatched is False
    assert state["targets"]["alpha"] == {}


def test_process_target_retries_retryable_gh_failure(monkeypatch) -> None:
    target = BridgeTarget(name="alpha", repo="owner/repo", window="@1")
    cfg = BridgeConfig(targets=[target], retry_attempts=2, retry_base_delay_seconds=0.0)
    issue = _make_issue(5, "retry")
    state: dict = {}
    calls = {"count": 0}

    timeouts: list[float | None] = []

    def fake_run(
        argv, *, input=None, check=None, capture_output=None, text=None, timeout=None
    ):
        if argv[0] == "gh":
            timeouts.append(timeout)
            calls["count"] += 1
            if calls["count"] == 1:
                raise subprocess.CalledProcessError(returncode=1, cmd=argv)
            return type(
                "Result",
                (),
                {
                    "stdout": json.dumps(
                        [
                            {
                                "number": issue.number,
                                "title": issue.title,
                                "url": issue.url,
                                "updatedAt": issue.updated_at,
                                "labels": [{"name": "codex-bridge"}],
                            }
                        ]
                    )
                },
            )()
        return type("Result", (), {"stdout": ""})()

    monkeypatch.setattr("telegram_agent_bot.bridge.subprocess.run", fake_run)
    monkeypatch.setattr(
        "telegram_agent_bot.bridge.time.sleep", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(
        "telegram_agent_bot.bridge.random.uniform", lambda *_args, **_kwargs: 0.0
    )
    monkeypatch.setattr(
        "telegram_agent_bot.bridge.fetch_issue",
        lambda repo, issue_number, **kwargs: issue,
    )
    monkeypatch.setattr(
        "telegram_agent_bot.bridge.dispatch_to_tmux", lambda *args, **kwargs: None
    )

    dispatched = process_target(
        target,
        cfg,
        state,
        dry_run=True,
    )

    assert dispatched is True
    assert calls["count"] == 2
    assert timeouts == [60.0, 60.0]


def test_watch_waits_after_transient_cli_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    class StopWatch(Exception):
        pass

    failure = subprocess.CalledProcessError(returncode=1, cmd=["gh", "issue", "list"])
    monkeypatch.setattr(
        bridge, "run_once", lambda *args, **kwargs: (_ for _ in ()).throw(failure)
    )
    slept: list[int] = []

    def stop_after_sleep(interval: int) -> None:
        slept.append(interval)
        raise StopWatch

    monkeypatch.setattr(bridge.time, "sleep", stop_after_sleep)

    with pytest.raises(StopWatch):
        bridge._watch(
            BridgeConfig(),
            {},
            state_file=tmp_path / "state.json",
            interval=300,
        )

    assert slept == [300]
    assert "watcher will retry in 300 seconds" in caplog.text


def test_process_orchestrator_dispatches_monthly_issue(monkeypatch) -> None:
    cfg = BridgeConfig(
        bridge_mode="orchestrator",
        targets=[],
        source_repo="owner/control-plane",
        runner_window="@42",
        source_label="monthly-review",
        source_query="Monthly Audit Review",
    )
    issue = _make_issue(88, "Monthly Audit Review: 2026-05", labels=["monthly-review"])
    full_issue = _make_issue(
        88,
        "Monthly Audit Review: 2026-05",
        body='{"month":"2026-05","targets":["owner/repo-a"]}',
        labels=["monthly-review"],
        comments=[{"author": {"login": "ops"}, "body": "keep it scoped"}],
    )
    state: dict = {}

    monkeypatch.setattr(
        "telegram_agent_bot.bridge.list_open_issues",
        lambda repo, limit, **kwargs: [issue],
    )
    monkeypatch.setattr(
        "telegram_agent_bot.bridge.fetch_issue",
        lambda repo, issue_number, **kwargs: (
            full_issue if issue_number == 88 else issue
        ),
    )
    sent: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "telegram_agent_bot.bridge.dispatch_to_tmux",
        lambda window, text, **kwargs: sent.append((window, text)),
    )

    dispatched = process_orchestrator(cfg, state)

    assert dispatched is True
    assert sent[0][0] == "@42"
    assert "Monthly Audit Review: 2026-05" in sent[0][1]
    assert "owner/repo-a" in sent[0][1]
    assert "@ops" in sent[0][1]
    assert state["orchestrator"]["last_issue_number"] == 88


def test_process_orchestrator_skips_missing_tmux_target(monkeypatch) -> None:
    cfg = BridgeConfig(
        bridge_mode="orchestrator",
        targets=[],
        source_repo="owner/control-plane",
        runner_window="Ubuntu",
        source_label="monthly-review",
        source_query="Monthly Audit Review",
    )
    issue = _make_issue(88, "Monthly Audit Review: 2026-05", labels=["monthly-review"])
    state: dict = {}

    monkeypatch.setattr(
        "telegram_agent_bot.bridge.list_open_issues",
        lambda repo, limit, **kwargs: [issue],
    )
    monkeypatch.setattr(
        "telegram_agent_bot.bridge.fetch_issue",
        lambda repo, issue_number, **kwargs: issue,
    )
    monkeypatch.setattr(
        "telegram_agent_bot.bridge.dispatch_to_tmux",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            TmuxTargetNotFoundError("tmux target not found: Ubuntu")
        ),
    )

    dispatched = process_orchestrator(cfg, state)

    assert dispatched is False
    assert state["orchestrator"] == {}


def test_process_orchestrator_fetches_full_issue_payload(monkeypatch) -> None:
    cfg = BridgeConfig(
        bridge_mode="orchestrator",
        targets=[],
        source_repo="owner/control-plane",
        runner_window="@42",
        source_label="monthly-review",
        source_query="Monthly Audit Review",
    )
    summary_issue = _make_issue(
        90, "Monthly Audit Review: 2026-05", labels=["monthly-review"], body=""
    )
    full_issue = _make_issue(
        90,
        "Monthly Audit Review: 2026-05",
        body='{"month":"2026-05","targets":["owner/repo-a"]}',
        labels=["monthly-review"],
        comments=[{"author": {"login": "ops"}, "body": "keep it scoped"}],
    )
    state: dict = {}

    monkeypatch.setattr(
        "telegram_agent_bot.bridge.list_open_issues",
        lambda repo, limit, **kwargs: [summary_issue],
    )
    monkeypatch.setattr(
        "telegram_agent_bot.bridge.fetch_issue",
        lambda repo, issue_number, **kwargs: full_issue,
    )
    sent: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "telegram_agent_bot.bridge.dispatch_to_tmux",
        lambda window, text, **kwargs: sent.append((window, text)),
    )

    dispatched = process_orchestrator(cfg, state)

    assert dispatched is True
    assert "owner/repo-a" in sent[0][1]
    assert "@ops" in sent[0][1]


@pytest.mark.parametrize("mode", ["targets", "orchestrator"])
@pytest.mark.parametrize("force", [False, True])
@pytest.mark.parametrize("pending", [None, {}, [], "", False, 0, "synthetic-malformed"])
def test_present_pending_reservation_blocks_and_preserves_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    mode: str,
    force: bool,
    pending: object,
) -> None:
    issue = _make_issue(1, "Synthetic maintenance")
    target = BridgeTarget(name="alpha", repo="org/repo", window="@1", issue_number=1)
    config = BridgeConfig(bridge_mode=mode, targets=[target])
    record = {"dispatch_pending": pending, "synthetic_evidence": "retain"}
    state = (
        {"targets": {"alpha": record}}
        if mode == "targets"
        else {"orchestrator": record}
    )
    path = tmp_path / "state.json"
    bridge.save_state(path, state)
    evidence = path.read_bytes()
    monkeypatch.setattr(bridge, "fetch_issue", lambda *a, **k: issue)
    monkeypatch.setattr(
        bridge,
        "dispatch_to_tmux",
        lambda *a, **k: pytest.fail("dispatched over an unresolved reservation"),
    )
    monkeypatch.setattr(
        bridge,
        "save_state",
        lambda *a, **k: pytest.fail("overwrote unresolved reservation evidence"),
    )
    restarted = bridge.load_state(path)
    assert (
        bridge.run_once(
            config,
            restarted,
            state_file=path,
            issue_number=1,
            force=force,
        )
        == 0
    )
    assert restarted == state
    assert path.read_bytes() == evidence
    assert "Dispatch outcome is unknown" in caplog.text


@pytest.mark.parametrize("mode", ["targets", "orchestrator"])
@pytest.mark.parametrize("force", [False, True])
@pytest.mark.parametrize(
    "pending",
    [
        None,
        {},
        [],
        "",
        False,
        0,
        "synthetic-malformed",
        True,
        1,
        ["synthetic"],
        {"unknown": "synthetic"},
        {"fingerprint": "previous"},
        {"fingerprint": "previous", "reserved_at": "2026-01-01T00:00:00Z"},
    ],
)
@pytest.mark.parametrize(
    "entry", ["default", "once", "watch", "config-watch", "dry-run"]
)
def test_cli_pending_reservation_fails_without_touching_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    mode: str,
    force: bool,
    pending: object,
    entry: str,
) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "bridge_mode": mode,
                "dispatch_mode": "watch" if entry == "config-watch" else "once",
                "source_repo": "org/repo",
                "targets": [{"name": "alpha", "repo": "org/repo", "window": "@1"}],
            }
        )
    )
    record = {"dispatch_pending": pending, "synthetic_evidence": "retain"}
    state = (
        {"targets": {"alpha": record}}
        if mode == "targets"
        else {"orchestrator": record}
    )
    path = tmp_path / "state.json"
    # Non-canonical JSON exposes even a rewrite that preserves the decoded value.
    path.write_text(json.dumps(state, separators=(",", ":")) + "\n\n")
    evidence = path.read_bytes()
    original_stat = path.stat()
    issue = _make_issue(1, "Synthetic maintenance")
    monkeypatch.setattr(bridge, "fetch_issue", lambda *a, **k: issue)
    monkeypatch.setattr(bridge, "list_open_issues", lambda *a, **k: [issue])
    monkeypatch.setattr(
        bridge, "dispatch_to_tmux", lambda *a, **k: pytest.fail("dispatched")
    )
    monkeypatch.setattr(
        bridge.time, "sleep", lambda *a: pytest.fail("retried pending state")
    )
    argv = [
        "--config",
        str(config_path),
        "--state-file",
        str(path),
        "--issue-number",
        "1",
    ]
    if entry in {"once", "watch", "dry-run"}:
        argv.append(f"--{entry}")
    if force:
        argv.append("--force")

    assert bridge.main(argv) != 0
    assert path.read_bytes() == evidence
    assert path.stat().st_ino == original_stat.st_ino
    assert path.stat().st_mtime_ns == original_stat.st_mtime_ns
    assert "Dispatch outcome is unknown" in caplog.text


@pytest.mark.parametrize("mode", ["targets", "orchestrator"])
@pytest.mark.parametrize("force", [False, True])
@pytest.mark.parametrize("pending_location", ["unselected-target", "inactive-mode"])
def test_cli_checks_entire_shared_state_before_processing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    force: bool,
    pending_location: str,
) -> None:
    target = BridgeTarget(name="alpha", repo="org/repo", window="@1", issue_number=1)
    config = BridgeConfig(bridge_mode=mode, targets=[target])
    state: dict = {"targets": {"alpha": {}}}
    if pending_location == "unselected-target" or mode == "orchestrator":
        state["targets"]["beta"] = {"dispatch_pending": None}
    else:
        state["orchestrator"] = {"dispatch_pending": None}
    path = tmp_path / "state.json"
    path.write_text(json.dumps(state, separators=(",", ":")))
    evidence = path.read_bytes()
    monkeypatch.setattr(bridge, "_load_config", lambda *a: config)
    monkeypatch.setattr(
        bridge, "run_once", lambda *a, **k: pytest.fail("processed state")
    )
    monkeypatch.setattr(
        bridge, "save_state", lambda *a, **k: pytest.fail("saved state")
    )
    argv = ["--state-file", str(path), "--target", "alpha"]
    if force:
        argv.append("--force")
    assert bridge.main(argv) != 0
    assert path.read_bytes() == evidence


@pytest.mark.parametrize("mode", ["targets", "orchestrator"])
@pytest.mark.parametrize("force", [False, True])
def test_cli_watch_stops_after_uncertain_send_without_rewriting_reservation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str, force: bool
) -> None:
    target = BridgeTarget(name="alpha", repo="org/repo", window="@1", issue_number=1)
    config = BridgeConfig(bridge_mode=mode, targets=[target])
    path = tmp_path / "state.json"
    issue = _make_issue(1, "Synthetic maintenance")
    monkeypatch.setattr(bridge, "_load_config", lambda *a: config)
    monkeypatch.setattr(bridge, "fetch_issue", lambda *a, **k: issue)
    evidence: list[bytes] = []
    sleeps: list[int] = []

    def uncertain_send(*args, **kwargs):
        evidence.append(path.read_bytes())
        if len(evidence) > 1:
            pytest.fail("retried uncertain dispatch")
        raise subprocess.TimeoutExpired("synthetic-tmux", 10)

    def sleep(interval):
        sleeps.append(interval)
        if len(sleeps) > 1:
            pytest.fail("watch did not stop")

    monkeypatch.setattr(bridge, "dispatch_to_tmux", uncertain_send)
    monkeypatch.setattr(bridge.time, "sleep", sleep)
    argv = ["--state-file", str(path), "--watch", "--issue-number", "1"]
    if force:
        argv.append("--force")
    assert bridge.main(argv) != 0
    assert len(evidence) == len(sleeps) == 1
    assert path.read_bytes() == evidence[0]
