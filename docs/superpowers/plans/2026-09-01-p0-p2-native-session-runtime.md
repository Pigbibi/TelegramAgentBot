# P0-P2 Native Session Runtime Plan

> **Execution:** implement in the existing `feat/p0-routing-doctor` worktree; do not commit, push, deploy, or restart the VPS without explicit authorization.

## Goal

Prevent a Telegram topic from silently switching to a different agent conversation, while evolving the current local tmux implementation toward an explicit agent-host boundary and clear running-state UI.

## Scope and sequencing

1. **P0 — routing diagnostics and fail-closed stale IDs**
   - Add a read-only `doctor` command for live routing consistency checks.
   - Never rebind a persisted raw tmux window ID by display name.
   - Status: implemented and locally verified before this plan.

2. **P1 — durable conversation registry**
   - Add SQLite tables for `(user_id, thread_id)` route identity, target session identity, route generation, and transcript delivery offsets.
   - On first successful startup, import the current JSON state; on subsequent starts, hydrate route/offset fields from SQLite before tmux reconciliation.
   - Persist each SessionManager mutation through one SQLite transaction before writing the compatibility JSON mirror.
   - Keep `state.json` for rollback-compatible ancillary settings; SQLite is authoritative only for route/offset fields once initialized.
   - Characterize migration, generation increment, stale JSON override, and offset-session identity with unit tests.

3. **P2 — conversation-run delivery fence**
   - Use the existing `AgentBackend` target boundary as the logical agent host; do not add an unreviewed process transport in this slice.
   - Attach the durable route generation and session identity to queued Telegram content, working, waiting, and clear-status events.
   - Drop a queued event if its topic was rebound, recovered, or advanced to a new session before Telegram delivery.
   - Do not split into another process or change systemd/tmux lifecycle in this slice; that requires a separately reviewed transport and deployment migration.

## Verification

Run focused durable-state/session/backend tests first, then ruff and pyright for touched modules. Run the full test suite; report any pre-existing environment failure separately. No production validation is implied by local tests.

## Rollback

The SQLite registry is additive. Removing its activation restores the existing JSON loading path; no service or state-file format removal is required.
