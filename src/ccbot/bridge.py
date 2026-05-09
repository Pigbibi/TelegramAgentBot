"""GitHub issue to Codex bridge for tmux-backed execution.

This module is intentionally independent from the main Telegram bot runtime.
It polls GitHub issues via `gh`, selects actionable issues based on a target
configuration, and injects a structured task into a tmux window where Codex is
already running.

Typical usage:

    ccbot-bridge --config ~/.ccbot/github_codex_bridge.json --watch

The configuration file is a JSON object with a `targets` array. Each target may
define:

    {
      "name": "crypto-snapshot",
      "repo": "owner/repo",
      "window": "@12",
      "workspace": "/home/ubuntu/Projects/repo",
      "labels": ["codex-bridge"],
      "issue_number": 123,
      "extra_instructions": "Only make low-risk changes."
    }

Targets are processed independently. A target is dispatched at most once per
issue number unless `--force` is used.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_CONFIG_PATH = Path.home() / ".ccbot" / "github_codex_bridge.json"
DEFAULT_STATE_PATH = Path.home() / ".ccbot" / "github_codex_bridge_state.json"
DEFAULT_POLL_INTERVAL_SECONDS = 300
DEFAULT_ISSUE_LIMIT = 50
DEFAULT_BODY_LIMIT = 4000
DEFAULT_COMMENT_LIMIT = 3
DEFAULT_DISPATCH_MODE = "poll"
DEFAULT_MERGE_MODE = "manual"
DEFAULT_MERGE_LABEL = "auto-merge-ok"


@dataclass(slots=True)
class BridgeTarget:
    """A single GitHub repo -> tmux window bridge target."""

    name: str
    repo: str
    window: str
    workspace: str | None = None
    labels: list[str] = field(default_factory=list)
    query: str | None = None
    issue_number: int | None = None
    merge_mode: str = DEFAULT_MERGE_MODE
    merge_label: str | None = DEFAULT_MERGE_LABEL
    extra_instructions: str | None = None


@dataclass(slots=True)
class BridgeConfig:
    """Top-level bridge configuration loaded from JSON."""

    targets: list[BridgeTarget]
    dispatch_mode: str = DEFAULT_DISPATCH_MODE
    tmux_socket: str | None = None
    issue_limit: int = DEFAULT_ISSUE_LIMIT
    body_limit: int = DEFAULT_BODY_LIMIT
    comment_limit: int = DEFAULT_COMMENT_LIMIT
    poll_interval_seconds: int = DEFAULT_POLL_INTERVAL_SECONDS


@dataclass(slots=True)
class GitHubIssue:
    """Normalized GitHub issue payload."""

    number: int
    title: str
    body: str
    url: str
    updated_at: str
    labels: list[str]
    comments: list[dict[str, Any]]


def _run_json_command(argv: list[str]) -> Any:
    """Run a command that returns JSON."""
    result = subprocess.run(
        argv,
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


def _truncate(text: str, limit: int) -> str:
    """Truncate text to a sensible prompt size."""
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[: limit - 20].rstrip() + "\n[... truncated ...]"


def _parse_labels(raw_labels: list[dict[str, Any]] | list[str]) -> list[str]:
    labels: list[str] = []
    for item in raw_labels:
        if isinstance(item, str):
            labels.append(item)
            continue
        name = item.get("name")
        if isinstance(name, str) and name:
            labels.append(name)
    return labels


def _parse_issue(raw: dict[str, Any]) -> GitHubIssue:
    return GitHubIssue(
        number=int(raw["number"]),
        title=str(raw.get("title", "")),
        body=str(raw.get("body", "")),
        url=str(raw.get("url", "")),
        updated_at=str(raw.get("updatedAt", "")),
        labels=_parse_labels(raw.get("labels", [])),
        comments=list(raw.get("comments", []) or []),
    )


def load_config(path: Path) -> BridgeConfig:
    """Load bridge configuration from JSON."""
    raw = json.loads(path.read_text(encoding="utf-8"))
    targets: list[BridgeTarget] = []
    for item in raw.get("targets", []):
        labels = item.get("labels", []) or []
        if not isinstance(labels, list):
            raise ValueError(f"Target {item.get('name', '<unnamed>')} labels must be a list")
        targets.append(
            BridgeTarget(
                name=str(item["name"]),
                repo=str(item["repo"]),
                window=str(item["window"]),
                workspace=item.get("workspace"),
                labels=[str(label) for label in labels if str(label).strip()],
                query=(str(item["query"]).strip() if item.get("query") else None),
                issue_number=(
                    int(item["issue_number"])
                    if item.get("issue_number") is not None
                    else None
                ),
                merge_mode=(
                    str(item.get("merge_mode", DEFAULT_MERGE_MODE)).strip()
                    or DEFAULT_MERGE_MODE
                ),
                merge_label=(
                    str(item.get("merge_label", DEFAULT_MERGE_LABEL)).strip()
                    if item.get("merge_label", DEFAULT_MERGE_LABEL) is not None
                    else None
                )
                or None,
                extra_instructions=item.get("extra_instructions"),
            )
        )
    return BridgeConfig(
        targets=targets,
        dispatch_mode=str(raw.get("dispatch_mode", DEFAULT_DISPATCH_MODE)).strip()
        or DEFAULT_DISPATCH_MODE,
        tmux_socket=raw.get("tmux_socket"),
        issue_limit=int(raw.get("issue_limit", DEFAULT_ISSUE_LIMIT)),
        body_limit=int(raw.get("body_limit", DEFAULT_BODY_LIMIT)),
        comment_limit=int(raw.get("comment_limit", DEFAULT_COMMENT_LIMIT)),
        poll_interval_seconds=int(
            raw.get("poll_interval_seconds", DEFAULT_POLL_INTERVAL_SECONDS)
        ),
    )


def load_state(path: Path) -> dict[str, Any]:
    """Load bridge state; missing file yields an empty state."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}


def save_state(path: Path, state: dict[str, Any]) -> None:
    """Persist bridge state atomically."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(
        json.dumps(state, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    tmp_path.replace(path)


def list_open_issues(repo: str, limit: int) -> list[GitHubIssue]:
    """List open issues for a repo using gh."""
    raw = _run_json_command(
        [
            "gh",
            "issue",
            "list",
            "--repo",
            repo,
            "--state",
            "open",
            "--limit",
            str(limit),
            "--json",
            "number,title,url,updatedAt,labels",
        ]
    )
    return [_parse_issue({**item, "body": "", "comments": []}) for item in raw]


def fetch_issue(repo: str, issue_number: int) -> GitHubIssue:
    """Fetch full issue details for a repo/issue number."""
    raw = _run_json_command(
        [
            "gh",
            "issue",
            "view",
            str(issue_number),
            "--repo",
            repo,
            "--json",
            "number,title,body,url,updatedAt,labels,comments",
        ]
    )
    return _parse_issue(raw)


def _issue_matches_labels(issue: GitHubIssue, labels: list[str]) -> bool:
    """Return whether all required labels are present."""
    if not labels:
        return True
    label_set = {label.lower() for label in issue.labels}
    return all(label.lower() in label_set for label in labels)


def _issue_text_matches(issue: GitHubIssue, query: str | None) -> bool:
    """Simple case-insensitive substring match over title and body."""
    if not query:
        return True
    haystack = f"{issue.title}\n{issue.body}".lower()
    terms = [term.strip().lower() for term in query.split() if term.strip()]
    return all(term in haystack for term in terms)


def select_issue(
    issues: list[GitHubIssue],
    *,
    labels: list[str] | None = None,
    query: str | None = None,
    issue_number: int | None = None,
) -> GitHubIssue | None:
    """Select the newest matching issue, if any."""
    if issue_number is not None:
        for issue in issues:
            if issue.number == issue_number:
                return issue
        return None

    matching = [
        issue
        for issue in issues
        if _issue_matches_labels(issue, labels or [])
        and _issue_text_matches(issue, query)
    ]
    if not matching:
        return None
    matching.sort(key=lambda issue: issue.updated_at, reverse=True)
    return matching[0]


def _issue_fingerprint(issue: GitHubIssue) -> str:
    """Stable fingerprint used to avoid duplicate dispatches."""
    return f"{issue.number}:{issue.updated_at}"


def build_task_message(target: BridgeTarget, issue: GitHubIssue, config: BridgeConfig) -> str:
    """Build the Codex task message for a selected issue."""
    comments: list[dict[str, Any]] = issue.comments[: config.comment_limit]
    comment_lines: list[str] = []
    for comment in comments:
        author = comment.get("author", {}) if isinstance(comment, dict) else {}
        author_login = ""
        if isinstance(author, dict):
            author_login = str(author.get("login", "")).strip()
        body = str(comment.get("body", "")) if isinstance(comment, dict) else ""
        body = _truncate(body, 800)
        prefix = f"- @{author_login}: " if author_login else "- "
        comment_lines.append(prefix + body.replace("\n", "\n  "))

    body_text = _truncate(issue.body, config.body_limit)
    lines = [
        "[Codex bridge task]",
        f"Target: {target.name}",
        f"Repo: {target.repo}",
        f"Issue: #{issue.number} {issue.title}",
        f"URL: {issue.url}",
    ]
    if target.workspace:
        lines.append(f"Workspace: {target.workspace}")
    if target.extra_instructions:
        lines.append("")
        lines.append("Target instructions:")
        lines.append(target.extra_instructions.strip())
    lines.extend(
        [
            "",
            "Process this GitHub issue end-to-end.",
            "Constraints:",
            "- Read the issue and its latest comments before editing.",
            "- Make the smallest safe change that satisfies the issue.",
            "- Prefer targeted tests over broad builds.",
            "- Open a draft PR if code changes are needed.",
            "- Report the PR link and a short summary back in the issue.",
            "",
            "Issue body:",
            body_text or "(empty)",
        ]
    )
    if target.merge_mode.lower() == "auto":
        lines.insert(
            -2,
            "- Treat merge as opt-in: only enable auto-merge or merge after CI and review pass.",
        )
        if target.merge_label:
            lines.insert(
                -2,
                f"- Add the `{target.merge_label}` label before enabling merge.",
            )
            lines.insert(
                -2,
                f"- Do not merge unless `{target.merge_label}` is present and checks are green.",
            )
        lines.insert(
            -2,
            "- If review asks for changes, continue fixing in the same PR until it is clean.",
        )
    else:
        lines.insert(
            -2,
            "- Leave the PR open unless the issue explicitly asks for merge.",
        )
    if comment_lines:
        lines.extend(["", "Latest comments:"])
        lines.extend(comment_lines)
    return "\n".join(lines).strip() + "\n"


def _tmux_prefix(socket_name: str | None) -> list[str]:
    cmd = ["tmux"]
    if socket_name:
        cmd.extend(["-L", socket_name])
    return cmd


def dispatch_to_tmux(window: str, text: str, *, socket_name: str | None = None) -> None:
    """Paste text into a tmux window and press Enter."""
    buffer_name = f"ccbot-bridge-{os.getpid()}-{time.time_ns()}"
    prefix = _tmux_prefix(socket_name)
    subprocess.run(
        [*prefix, "load-buffer", "-b", buffer_name, "-"],
        input=text.encode("utf-8"),
        check=True,
    )
    try:
        subprocess.run(
            [*prefix, "paste-buffer", "-b", buffer_name, "-t", window, "-d"],
            check=True,
        )
        subprocess.run([*prefix, "send-keys", "-t", window, "Enter"], check=True)
    finally:
        subprocess.run(
            [*prefix, "delete-buffer", "-b", buffer_name],
            check=False,
            capture_output=True,
        )


def process_target(
    target: BridgeTarget,
    config: BridgeConfig,
    state: dict[str, Any],
    *,
    force: bool = False,
    dry_run: bool = False,
) -> bool:
    """Poll one target and dispatch a new issue if needed."""
    target_state = state.setdefault("targets", {}).setdefault(target.name, {})

    if target.issue_number is not None:
        candidate = fetch_issue(target.repo, target.issue_number)
        if candidate:
            issues = [candidate]
        else:
            issues = []
    else:
        issues = list_open_issues(target.repo, config.issue_limit)
        candidate = select_issue(
            issues,
            labels=target.labels,
            query=target.query,
            issue_number=None,
        )

    if candidate is None:
        logger.info("No matching issue for target=%s", target.name)
        return False

    fingerprint = _issue_fingerprint(candidate)
    if not force and target_state.get("last_fingerprint") == fingerprint:
        logger.info(
            "Target %s already dispatched issue #%d (%s)",
            target.name,
            candidate.number,
            fingerprint,
        )
        return False

    message = build_task_message(target, candidate, config)
    if dry_run:
        print(message, end="")
        return True

    dispatch_to_tmux(candidate_target_window(target), message, socket_name=config.tmux_socket)
    target_state["last_fingerprint"] = fingerprint
    target_state["last_issue_number"] = candidate.number
    target_state["last_issue_url"] = candidate.url
    target_state["last_dispatched_at"] = datetime.now(tz=UTC).isoformat()
    logger.info(
        "Dispatched issue #%d to target=%s window=%s",
        candidate.number,
        target.name,
        target.window,
    )
    return True


def candidate_target_window(target: BridgeTarget) -> str:
    """Return the tmux target for the bridge task."""
    return target.window


def _load_targets(path: Path) -> BridgeConfig:
    """Load config and normalize defaults."""
    config = load_config(path)
    if not config.targets:
        raise ValueError("bridge config has no targets")
    return config


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG_PATH,
        help=f"Path to bridge config JSON (default: {DEFAULT_CONFIG_PATH})",
    )
    parser.add_argument(
        "--state-file",
        type=Path,
        default=DEFAULT_STATE_PATH,
        help=f"Path to persistent bridge state JSON (default: {DEFAULT_STATE_PATH})",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run one poll/dispatch pass and exit (default behavior)",
    )
    parser.add_argument(
        "--watch",
        action="store_true",
        help="Continuously poll on an interval",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=DEFAULT_POLL_INTERVAL_SECONDS,
        help="Polling interval in seconds for --watch",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Dispatch even if the issue fingerprint matches the last run",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the generated prompt instead of sending it to tmux",
    )
    parser.add_argument(
        "--target",
        action="append",
        dest="targets",
        help="Only process the named target(s). Can be repeated.",
    )
    parser.add_argument(
        "--issue-number",
        type=int,
        help="Dispatch a specific issue number for every selected target",
    )
    return parser.parse_args(argv)


def _selected_targets(config: BridgeConfig, names: list[str] | None) -> list[BridgeTarget]:
    if not names:
        return config.targets
    wanted = {name for name in names}
    return [target for target in config.targets if target.name in wanted]


def run_once(
    config: BridgeConfig,
    state: dict[str, Any],
    *,
    force: bool = False,
    dry_run: bool = False,
    target_names: list[str] | None = None,
    issue_number: int | None = None,
) -> int:
    """Run one bridge pass and return the number of dispatches."""
    dispatched = 0
    for target in _selected_targets(config, target_names):
        if issue_number is not None:
            target = BridgeTarget(
                name=target.name,
                repo=target.repo,
                window=target.window,
                workspace=target.workspace,
                labels=target.labels,
                issue_number=issue_number,
                merge_mode=target.merge_mode,
                merge_label=target.merge_label,
                extra_instructions=target.extra_instructions,
            )
        if process_target(target, config, state, force=force, dry_run=dry_run):
            dispatched += 1
    return dispatched


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint."""
    args = _parse_args(argv or sys.argv[1:])
    config = _load_targets(args.config)
    state = load_state(args.state_file)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    watch_mode = args.watch or config.dispatch_mode.lower() == "watch"
    if watch_mode:
        interval = args.interval or config.poll_interval_seconds
        logger.info(
            "Watching %d bridge target(s) every %d seconds",
            len(config.targets),
            interval,
        )
        while True:
            dispatched = run_once(
                config,
                state,
                force=args.force,
                dry_run=args.dry_run,
                target_names=args.targets,
                issue_number=args.issue_number,
            )
            save_state(args.state_file, state)
            logger.info("Bridge pass complete: dispatched=%d", dispatched)
            time.sleep(interval)

    dispatched = run_once(
        config,
        state,
        force=args.force,
        dry_run=args.dry_run,
        target_names=args.targets,
        issue_number=args.issue_number,
    )
    save_state(args.state_file, state)
    logger.info("Bridge pass complete: dispatched=%d", dispatched)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
