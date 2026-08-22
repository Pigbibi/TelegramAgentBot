# GitHub issue bridge

`telegram-agent-bridge` polls GitHub issues and sends selected issue content to
a Codex session already running in tmux. It is a separate process from the
Telegram bot and does not replace TelegramAgentBot's session monitor.

Use the bridge for asynchronous, repository-scoped work. It is not intended as
a low-latency chat transport.

## Requirements

- `gh` installed and authenticated as an account that can read the configured
  repositories;
- tmux access as the user running the bridge;
- a live Codex session in every configured destination window;
- a local JSON configuration file outside the Git checkout.

Confirm access before starting the bridge:

```bash
gh auth status
gh repo view owner/repository
tmux list-windows -a
```

## Configuration

Copy the placeholder template:

```bash
cp docs/github_codex_bridge.sample.json \
  ~/.telegram-agent-bot/github_codex_bridge.json
chmod 600 ~/.telegram-agent-bot/github_codex_bridge.json
```

Do not commit the real file. It may contain private repository names, local
paths, tmux targets, and operational instructions.

### Target mode

Use `bridge_mode: "targets"` to poll repositories independently:

```json
{
  "bridge_mode": "targets",
  "dispatch_mode": "poll",
  "poll_interval_seconds": 300,
  "retry_attempts": 3,
  "retry_base_delay_seconds": 1.0,
  "targets": [
    {
      "name": "maintenance",
      "repo": "owner/repository",
      "window": "@12",
      "workspace": "/srv/work/repository",
      "labels": ["agent-task"],
      "query": "maintenance",
      "merge_mode": "manual",
      "extra_instructions": "Keep the change focused and open a draft PR."
    }
  ]
}
```

Each target is evaluated independently. The bridge records the dispatched issue
fingerprint and does not resend unchanged work unless `--force` is supplied.

Target fields:

| Field | Required | Description |
| --- | --- | --- |
| `name` | yes | Stable local target identifier |
| `repo` | yes | GitHub repository in `owner/name` form |
| `window` | yes | Destination accepted by `tmux -t` |
| `workspace` | no | Local path included in the generated task |
| `labels` | no | Labels that must all be present |
| `query` | no | Case-insensitive title/body substring |
| `issue_number` | no | Dispatch one explicit issue |
| `merge_mode` | no | `manual` or `auto`; default is `manual` |
| `merge_label` | no | Required label for automatic merge eligibility |
| `extra_instructions` | no | Target-specific prompt constraints |

### Orchestrator mode

Use `bridge_mode: "orchestrator"` when one control-plane repository publishes a
complete task for a single runner window:

```json
{
  "bridge_mode": "orchestrator",
  "dispatch_mode": "watch",
  "poll_interval_seconds": 300,
  "source_repo": "owner/control-plane",
  "source_label": "agent-task",
  "source_query": "scheduled review",
  "runner_window": "@42",
  "runner_workspace": "/srv/work/runner",
  "runner_extra_instructions": "Treat the issue body as the task contract."
}
```

Set `source_issue_number` to consume one explicit issue instead of selecting by
label and query.

## Limits and retry behavior

Top-level limits keep each poll bounded:

| Field | Default | Description |
| --- | --- | --- |
| `issue_limit` | `50` | Maximum issues returned by one GitHub query |
| `body_limit` | `4000` | Maximum issue-body characters added to the prompt |
| `comment_limit` | `3` | Maximum recent comments added to the prompt |
| `poll_interval_seconds` | `300` | Watch-mode interval |
| `retry_attempts` | `3` | Attempts for transient `gh` and tmux subprocess failures |
| `retry_base_delay_seconds` | `1.0` | Exponential-backoff base delay |
| `tmux_socket` | unset | Optional tmux socket path or name used by the bridge |

Authentication failures, invalid configuration, missing repositories, and
missing tmux targets are logical errors and should be fixed instead of retried
indefinitely.

## Commands

Preview the generated prompt without writing to tmux:

```bash
telegram-agent-bridge \
  --config ~/.telegram-agent-bot/github_codex_bridge.json \
  --dry-run
```

Run one dispatch pass:

```bash
telegram-agent-bridge \
  --config ~/.telegram-agent-bot/github_codex_bridge.json \
  --once
```

Poll continuously:

```bash
telegram-agent-bridge \
  --config ~/.telegram-agent-bot/github_codex_bridge.json \
  --watch --interval 300
```

Limit execution to one named target or issue:

```bash
telegram-agent-bridge --target maintenance --issue-number 123 --once
```

The default state file is:

```text
~/.telegram-agent-bot/github_codex_bridge_state.json
```

## Automatic merge

Keep `merge_mode` set to `manual` unless the destination repository has a
narrow, independently enforced merge policy. A safe automatic path should
require all of the following:

- an explicit opt-in label;
- a pull request rather than a direct push;
- required GitHub checks;
- no unresolved review requests;
- a restricted set of repositories and change types;
- an auditable merge gate outside the agent prompt.

Do not use prompt text as the only authorization for merging or deployment.

## Running as a service

Use a separate service instance per independent configuration. Set a bounded
restart delay and inspect repeated failures instead of restarting continuously.
The service user must own the tmux server and `gh` authentication state used by
the bridge.

Start with `--dry-run` and `--once`. Enable `--watch` only after repository
access, issue filters, destination windows, and generated prompts have been
verified.
