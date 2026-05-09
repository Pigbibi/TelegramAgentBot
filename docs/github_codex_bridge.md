# GitHub -> Codex bridge

This bridge polls GitHub issues and injects a structured task into a tmux
window where Codex is already running.

The intended flow is:

1. Monthly review / optimization issues are created in GitHub.
2. `ccbot-bridge` polls those issues with `gh`.
3. Matching issues are converted into a Codex task prompt.
4. The prompt is pasted into a configured tmux window.
5. ccbot keeps listening to the Codex transcript and streams the result back
   to Telegram as usual.

The bridge does not replace ccbot. It is only the task injector.
It only touches the repositories listed in its config file, so it will not
affect other repositories unless you add them explicitly.

## Configuration

Commit only the template file in the repo. Keep your real config local at
`~/.ccbot/github_codex_bridge.json` and never add it to Git.

Any repository list, including any targets that you want to auto-merge, must
stay only in the local config. Do not add those repository names or target
settings to the open-source template.

Template file:

- `docs/github_codex_bridge.sample.json`

Create `~/.ccbot/github_codex_bridge.json`:

```json
{
  "bridge_mode": "targets",
  "dispatch_mode": "poll",
  "tmux_socket": null,
  "issue_limit": 50,
  "body_limit": 4000,
  "comment_limit": 3,
  "poll_interval_seconds": 300,
  "retry_attempts": 3,
  "retry_base_delay_seconds": 1.0,
  "source_repo": "owner/control-plane-repo",
  "source_label": "monthly-review",
  "source_query": "Monthly Audit Review",
  "runner_window": "@42",
  "runner_workspace": "/home/ubuntu/Projects/runner",
  "runner_extra_instructions": "Treat the monthly issue as the contract and keep changes minimal.",
  "targets": [
    {
      "name": "snapshot-audit",
      "repo": "owner/example-snapshot-repo",
      "window": "@12",
      "workspace": "/home/ubuntu/Projects/example-snapshot-repo",
      "labels": ["codex-bridge"],
      "query": "monthly review",
      "extra_instructions": "Only make low-risk changes."
    },
    {
      "name": "execution-audit",
      "repo": "owner/example-execution-repo",
      "window": "@13",
      "workspace": "/home/ubuntu/Projects/example-execution-repo",
      "labels": ["codex-bridge"],
      "query": "monthly review",
      "extra_instructions": "Focus on execution quality, monthly audit findings, and low-risk fixes."
    }
  ]
}
```

Fields:

- `bridge_mode`: `targets` (legacy per-repository bridge, the default) or
  `orchestrator` (consume the monthly issue published by a GitHub Actions
  control plane such as `AuditOrchestrator`).
- `dispatch_mode`: `poll` (run once per invocation, the default) or `watch`
  (keep polling on `poll_interval_seconds`).
- `source_repo`: repository that publishes the monthly issue when `bridge_mode`
  is `orchestrator`.
- `source_label`: label used to identify the monthly issue when `bridge_mode`
  is `orchestrator`.
- `source_query`: optional title/body filter used to select the monthly issue
  when `bridge_mode` is `orchestrator`.
- `source_issue_number`: optional explicit issue number to consume in
  orchestrator mode.
- `runner_window`: tmux window that receives the orchestrator task.
- `runner_workspace`: optional local workspace path to include in the
  orchestrator task prompt.
- `runner_extra_instructions`: optional extra guardrails appended to the
  orchestrator task prompt.
- `repo`: GitHub repository in `owner/name` form.
- `window`: tmux window id or target accepted by `tmux -t`.
- `workspace`: optional local repo path to include in the instruction text.
- `labels`: optional labels that must all be present on an issue before it is
  dispatched.
- `query`: optional case-insensitive substring search over issue title/body.
- `issue_number`: optional explicit issue number to dispatch.
- `merge_mode`: `manual` (default) or `auto`.
- `merge_label`: label required before auto-merge is permitted.
- `retry_attempts`: bounded retry count for transient `gh` and `tmux` failures.
- `retry_base_delay_seconds`: base delay for retry backoff.
- `extra_instructions`: optional repo-specific guardrails appended to the task.

## Bridge modes

### Legacy target mode

Use `bridge_mode: "targets"` when you want the bridge to poll one or more
repositories directly and inject a separate Codex task for each target.

This is the original mode and it still works as before.

### Orchestrator mode

Use `bridge_mode: "orchestrator"` when you want the bridge to consume a
monthly issue from a control-plane repository and hand the work to one tmux
runner window.

The monthly issue should already contain the machine-readable payload defined by
the control-plane repository. The bridge just relays that contract into the
runner session.

## Automatic merge

Automatic merge is possible, but it should remain opt-in and label-gated.
Recommended guardrails:

- Only allow merge for low-risk maintenance targets.
- Require a dedicated label such as `auto-merge-ok`.
- Require tests to pass in the Codex session before merging.
- Require GitHub CI checks to be green before merging.
- Require review comments to be resolved before merging.
- Never auto-merge if the prompt asked for architectural changes or touched
  production risk paths.

The preferred flow is:

1. Codex creates a draft PR.
2. GitHub checks run.
3. A second small automation step merges only if the issue/PR satisfies the
   merge gate.

This keeps the Codex execution loop simple and leaves the final merge decision
in a narrow, auditable gate.

## Suggested target setup

If you only want the two AI-audited repositories, start with `targets` mode:

- `snapshot-audit` for your monthly snapshot/reporting repo
- `execution-audit` for your monthly execution/audit repo

If you want the GitHub Actions control plane to drive a single Codex runner,
switch to `orchestrator` mode and point `source_repo` at that control plane
repository.

Do not use this bridge to self-update `ccbot`; keep the bridge focused on the
external repositories listed in your local config file.

## Usage

Run once:

```bash
ccbot-bridge --config ~/.ccbot/github_codex_bridge.json --once
```

Watch continuously:

```bash
ccbot-bridge --config ~/.ccbot/github_codex_bridge.json --watch --interval 300
```

Dispatch a specific issue:

```bash
ccbot-bridge --config ~/.ccbot/github_codex_bridge.json --issue-number 123
```

Dry run:

```bash
ccbot-bridge --config ~/.ccbot/github_codex_bridge.json --dry-run
```

## Operational notes

- `gh` must already be authenticated on the VPS.
- The tmux window must be running a Codex session.
- The bridge tracks the last dispatched issue fingerprint in
  `~/.ccbot/github_codex_bridge_state.json` so it does not resend the same
  issue repeatedly.
- `gh` and `tmux` calls are retried only for transient subprocess failures.
  Logical failures still fail fast.
- For a true event listener, use a webhook receiver plus a public endpoint.
  This bridge intentionally stays polling-first for VPS simplicity.
