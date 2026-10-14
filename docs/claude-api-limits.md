# Claude API session limits

The `claude` API profile now launches a guarded print-session process instead
of an unrestricted interactive Claude CLI. Each submitted input runs one
`claude --print` invocation; subsequent inputs resume the same accounted
session. `claudeofficial` subscription sessions keep their existing launcher.
This is a source change only: existing running sessions are not retrofitted,
restarted, or declared safe by this PR.

## Configuration

| Environment variable | Default | Meaning |
| --- | --- | --- |
| `TELEGRAM_AGENT_BOT_CLAUDE_API_MAX_TOKENS` | `4096` | Output-token setting per model request |
| `TELEGRAM_AGENT_BOT_CLAUDE_API_MAX_TURNS` | `8` | Total main-agent turns across an accounted session, including resume |
| `TELEGRAM_AGENT_BOT_CLAUDE_API_RUN_BUDGET_USD` | `0.50` | Estimated USD reservation for each print run |
| `TELEGRAM_AGENT_BOT_CLAUDE_API_DAILY_BUDGET_USD` | `0` | UTC admission budget; zero disables API sessions until configured |
| `AI_TRANSCRIPTION_MAX_DURATION_SECONDS` | `120` | Voice duration cap, checked before download and again before provider calls |

Limits are taken from the bot configuration and passed explicitly to the
wrapper, so an old tmux server environment does not silently drop them.
Token/turn limits and enabled budgets must be positive and finite. Run budget
must not exceed daily budget. Missing duration, invalid limits, unavailable or
corrupt state, unaccounted resume, and unknown cost/turn results fail closed.
Custom Anthropic-compatible endpoints (including DeepSeek), Bedrock, Vertex,
and Foundry are blocked because this guard cannot verify their billing from
Claude's native cost estimate. No provider credentials are inspected or logged.

## Persistent accounting

The owner-local bot configuration directory holds `claude-api-budget.sqlite3`.
Its path is passed explicitly so account HOME changes do not split the budget.
SQLite serializes admission across bot processes. Before starting a model run,
the guard reserves its whole estimated USD budget and all remaining session
turns. Daily reservations are never refunded, even after cheap runs. A valid
result releases unused session turns. A failed, timed-out, interrupted,
malformed, zero-cost, or over-budget result blocks further API admission across
sessions, including after restart and date rollover. Concurrent input while a
run is unresolved is refused. Resolve uncertain accounting locally; never
reset/delete the ledger merely to retry a paid call.

Only guard-created sessions can be resumed. A completed session cannot exceed
its turn allowance by restarting the bot or changing Telegram topics. Each
new session also consumes the shared daily budget. Prompts go through stdin;
provider stderr, raw errors, credentials, and command arguments are not logged.

## Claude CLI boundaries

Claude Code 2.1.217 or newer is required. Each run receives `--max-turns` with
its remaining session allowance and `--max-budget-usd` with its reservation.
The guard rejects custom CLI options that could replace these flags. It sets
`CLAUDE_CODE_MAX_OUTPUT_TOKENS` and a bounded thinking setting. Compaction,
auxiliary title generation, background tasks, and Agent/Task subagent tools are
disabled to keep work inside the main session's limits. Context exhaustion
therefore fails rather than creating an unaccounted compaction request.

The [official CLI reference](https://code.claude.com/docs/en/cli-reference)
limits turn/spend flags to print mode and states that the spend cap is an
estimate which can overshoot. The [environment variable reference](https://code.claude.com/docs/en/env-vars)
describes the output-token setting as applying to most requests. These are
CLI-side controls, not a guarantee of an exact provider invoice or proof of a
hard cap on every possible auxiliary API request. Keep billing headroom and
retain provider-side spend controls; no provider configuration is changed here.
No live or paid API call has been made to validate billing or CLI integration.

Guarded API sessions accept submitted text lines; native interactive slash
commands/steer, multiline paste, and permission-prompt dialogs are not promised
by this print adapter. The normal permission setting is preserved; the wrapper
does not add an approval bypass. Unknown/oversized voice messages are rejected
before Telegram download and before either transcription provider is invoked.
