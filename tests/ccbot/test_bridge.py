from __future__ import annotations

import json
from pathlib import Path
import subprocess

from ccbot.bridge import (
    BridgeConfig,
    BridgeTarget,
    GitHubIssue,
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


def test_load_config(tmp_path: Path) -> None:
    cfg_path = tmp_path / "bridge.json"
    cfg_path.write_text(
        json.dumps(
            {
                "bridge_mode": "targets",
                "tmux_socket": "ccbot",
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
    assert cfg.tmux_socket == "ccbot"
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
                "tmux_socket": "ccbot",
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
        _make_issue(2, "match later", labels=["codex-bridge"], updated_at="2026-05-09T01:00:00Z"),
        _make_issue(3, "match earlier", labels=["codex-bridge"], updated_at="2026-05-09T00:30:00Z"),
    ]

    selected = select_issue(issues, labels=["codex-bridge"])
    assert selected is not None
    assert selected.number == 2


def test_select_issue_matches_query_terms() -> None:
    issues = [
        _make_issue(1, "build bridge", body="monthly review ready", labels=["codex-bridge"]),
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

    def fake_run(argv, *, input=None, check=None, capture_output=None, text=None):
        calls.append((list(argv), input if isinstance(input, bytes) else None))

        class Result:
            stdout = ""

        return Result()

    monkeypatch.setattr("ccbot.bridge.subprocess.run", fake_run)

    dispatch_to_tmux("@1", "hello\nworld\n", socket_name="ccbot")

    assert calls[0][0][:4] == ["tmux", "-L", "ccbot", "load-buffer"]
    assert calls[1][0][:4] == ["tmux", "-L", "ccbot", "paste-buffer"]
    assert calls[2][0][:4] == ["tmux", "-L", "ccbot", "send-keys"]


def test_process_target_skips_duplicate_issue(monkeypatch, tmp_path: Path) -> None:
    target = BridgeTarget(name="alpha", repo="owner/repo", window="@1")
    cfg = BridgeConfig(targets=[target])
    issue = _make_issue(4, "duplicate")
    state = {"targets": {"alpha": {"last_fingerprint": "4:2026-05-09T00:00:00Z"}}}

    monkeypatch.setattr(
        "ccbot.bridge.list_open_issues",
        lambda repo, limit, **kwargs: [issue],
    )
    monkeypatch.setattr(
        "ccbot.bridge.fetch_issue",
        lambda repo, issue_number, **kwargs: issue,
    )
    monkeypatch.setattr(
        "ccbot.bridge.dispatch_to_tmux",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("should not send")),
    )

    dispatched = process_target(target, cfg, state)
    assert dispatched is False


def test_process_target_retries_retryable_gh_failure(monkeypatch) -> None:
    target = BridgeTarget(name="alpha", repo="owner/repo", window="@1")
    cfg = BridgeConfig(targets=[target], retry_attempts=2, retry_base_delay_seconds=0.0)
    issue = _make_issue(5, "retry")
    state: dict = {}
    calls = {"count": 0}

    def fake_run(argv, *, input=None, check=None, capture_output=None, text=None):
        if argv[0] == "gh":
            calls["count"] += 1
            if calls["count"] == 1:
                raise subprocess.CalledProcessError(returncode=1, cmd=argv)
            return type("Result", (), {"stdout": json.dumps([{
                "number": issue.number,
                "title": issue.title,
                "url": issue.url,
                "updatedAt": issue.updated_at,
                "labels": [{"name": "codex-bridge"}],
            }])})()
        return type("Result", (), {"stdout": ""})()

    monkeypatch.setattr("ccbot.bridge.subprocess.run", fake_run)
    monkeypatch.setattr("ccbot.bridge.time.sleep", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("ccbot.bridge.random.uniform", lambda *_args, **_kwargs: 0.0)
    monkeypatch.setattr(
        "ccbot.bridge.fetch_issue",
        lambda repo, issue_number, **kwargs: issue,
    )
    monkeypatch.setattr("ccbot.bridge.dispatch_to_tmux", lambda *args, **kwargs: None)

    dispatched = process_target(
        target,
        cfg,
        state,
        dry_run=True,
    )

    assert dispatched is True
    assert calls["count"] == 2


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
        "ccbot.bridge.list_open_issues",
        lambda repo, limit, **kwargs: [issue],
    )
    monkeypatch.setattr(
        "ccbot.bridge.fetch_issue",
        lambda repo, issue_number, **kwargs: full_issue if issue_number == 88 else issue,
    )
    sent: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "ccbot.bridge.dispatch_to_tmux",
        lambda window, text, **kwargs: sent.append((window, text)),
    )

    dispatched = process_orchestrator(cfg, state)

    assert dispatched is True
    assert sent[0][0] == "@42"
    assert "Monthly Audit Review: 2026-05" in sent[0][1]
    assert "owner/repo-a" in sent[0][1]
    assert "@ops" in sent[0][1]
    assert state["orchestrator"]["last_issue_number"] == 88


def test_process_orchestrator_fetches_full_issue_payload(monkeypatch) -> None:
    cfg = BridgeConfig(
        bridge_mode="orchestrator",
        targets=[],
        source_repo="owner/control-plane",
        runner_window="@42",
        source_label="monthly-review",
        source_query="Monthly Audit Review",
    )
    summary_issue = _make_issue(90, "Monthly Audit Review: 2026-05", labels=["monthly-review"], body="")
    full_issue = _make_issue(
        90,
        "Monthly Audit Review: 2026-05",
        body='{"month":"2026-05","targets":["owner/repo-a"]}',
        labels=["monthly-review"],
        comments=[{"author": {"login": "ops"}, "body": "keep it scoped"}],
    )
    state: dict = {}

    monkeypatch.setattr(
        "ccbot.bridge.list_open_issues",
        lambda repo, limit, **kwargs: [summary_issue],
    )
    monkeypatch.setattr(
        "ccbot.bridge.fetch_issue",
        lambda repo, issue_number, **kwargs: full_issue,
    )
    sent: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "ccbot.bridge.dispatch_to_tmux",
        lambda window, text, **kwargs: sent.append((window, text)),
    )

    dispatched = process_orchestrator(cfg, state)

    assert dispatched is True
    assert "owner/repo-a" in sent[0][1]
    assert "@ops" in sent[0][1]
