# GitHub issue bridge

English | [简体中文](github-codex-bridge.zh-CN.md)

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
| `retry_attempts` | `3` | Attempts for transient `gh` and read-only tmux failures; paste and Enter are attempted once |
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

## Dispatch recovery

The CLI holds an exclusive lock beside its state file for the entire invocation,
including watch mode. Use one state file for every executor targeting the same
tmux destinations; separate files do not provide a shared lock. Programmatic
callers must hold `state_lock` and pass `state_file` to `run_once` for the same
guarantees. A dry run sends no input and writes no dispatch reservation.

Before sending input, the bridge durably saves `dispatch_pending`, syncing the
state directory and its ancestors so newly created directories are persisted.
If any sync fails, it does not send input. An interrupted
or uncertain paste/Enter leaves that reservation in place and blocks further
dispatch to that target, including `--force`. Paste and Enter are each attempted
once because a timeout does not establish that the operation failed. A confirmed
missing tmux target clears the reservation, since no input was sent.

Any existing `dispatch_pending` key is treated as an unknown outcome, even when
its value is empty or malformed. The bridge preserves that evidence and blocks
dispatch until it has been reconciled; `--force` does not bypass this check.
The CLI checks all records in the shared state file before processing any target,
including targets excluded by `--target`. If any reservation is present, it exits
nonzero without rewriting the file or dispatching. This applies to the default
single pass, `--once`, both CLI/configured watch mode, and `--dry-run`.

Watch mode checks before each subsequent pass and stops if an uncertain send left a
reservation behind.

After a confirmed send, the bridge saves the dispatch receipt immediately. This
records input delivery, not successful execution or business recovery. If a
reservation remains, stop the bridge, reconcile the actual destination and job
receipts, and back up state before clearing only the reconciled reservation. If
delivery occurred, also record its fingerprint as the last dispatched fingerprint
before restarting. Never clear a reservation or resubmit a mutating job merely
because a completion callback is missing. Keep state and its backups private.

This bridge has no authenticated completion-callback endpoint. It does not claim
callback delivery, operation completion or deployment acceptance. Integrating
those results requires the existing executor's authenticated receipt contract;
dispatch state is not a substitute for it.

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
