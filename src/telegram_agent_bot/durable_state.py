"""Small SQLite-backed runtime state for restart-safe message handling.

The bot intentionally keeps this store narrow: prompt submission lifecycles,
bounded continuation retries, and Telegram update IDs used for duplicate
suppression. It does not replace the existing session or transcript state files.
"""

from __future__ import annotations

import os
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path


_COMPLETED_UPDATE_RETENTION_SECONDS = 7 * 24 * 60 * 60
AGENT_INPUT_QUEUED = "queued"
AGENT_INPUT_SUBMITTED_UNCONFIRMED = "submitted_unconfirmed"
TRANSIENT_RETRY_SCHEDULED = "scheduled"
TRANSIENT_RETRY_WAITING_RESULT = "waiting_result"


@dataclass(frozen=True, slots=True)
class PendingAgentInput:
    """One prompt accepted by Telegram and still awaiting durable confirmation."""

    id: int
    user_id: int
    thread_id: int
    window_id: str
    text: str
    created_at_epoch: float
    state: str = AGENT_INPUT_QUEUED
    submitted_at_epoch: float | None = None


@dataclass(frozen=True, slots=True)
class TransientAgentRetry:
    """Restart-safe continuation retry for one agent window."""

    user_id: int
    thread_id: int
    window_id: str
    attempt: int
    state: str
    not_before_epoch: float
    created_at_epoch: float


@dataclass(frozen=True, slots=True)
class PendingAgentInputStats:
    """Small aggregate used by health checks without loading prompt text."""

    count: int
    oldest_created_at_epoch: float | None


@dataclass(frozen=True, slots=True)
class HealthAlertState:
    """Persisted alert activity and cooldown timestamp for one health issue."""

    active: bool
    last_sent_at_epoch: float


class DurableRuntimeStore:
    """Persist a bounded prompt spool and recently completed Telegram updates."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=2.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 2000")
        return connection

    def initialize(self, *, reset_inflight_updates: bool = False) -> None:
        """Create the schema and optionally release claims left by a dead process."""
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS agent_input_queue (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    thread_id INTEGER NOT NULL,
                    window_id TEXT NOT NULL,
                    text TEXT NOT NULL,
                    created_at_epoch REAL NOT NULL,
                    state TEXT NOT NULL DEFAULT 'queued'
                        CHECK (state IN ('queued', 'submitted_unconfirmed')),
                    submitted_at_epoch REAL
                );
                CREATE INDEX IF NOT EXISTS agent_input_queue_target_idx
                    ON agent_input_queue(user_id, thread_id, window_id, id);

                CREATE TABLE IF NOT EXISTS transient_agent_retries (
                    window_id TEXT PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    thread_id INTEGER NOT NULL,
                    attempt INTEGER NOT NULL,
                    state TEXT NOT NULL
                        CHECK (state IN ('scheduled', 'waiting_result')),
                    not_before_epoch REAL NOT NULL,
                    created_at_epoch REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS telegram_updates (
                    update_id INTEGER PRIMARY KEY,
                    state TEXT NOT NULL CHECK (state IN ('inflight', 'completed')),
                    updated_at_epoch REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS telegram_updates_completed_idx
                    ON telegram_updates(state, updated_at_epoch);

                CREATE TABLE IF NOT EXISTS health_alert_state (
                    issue_key TEXT PRIMARY KEY,
                    active INTEGER NOT NULL CHECK (active IN (0, 1)),
                    last_sent_at_epoch REAL NOT NULL
                );
                """
            )
            agent_input_columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(agent_input_queue)")
            }
            if "state" not in agent_input_columns:
                connection.execute(
                    "ALTER TABLE agent_input_queue ADD COLUMN state TEXT NOT NULL "
                    "DEFAULT 'queued' CHECK "
                    "(state IN ('queued', 'submitted_unconfirmed'))"
                )
            if "submitted_at_epoch" not in agent_input_columns:
                connection.execute(
                    "ALTER TABLE agent_input_queue ADD COLUMN submitted_at_epoch REAL"
                )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS agent_input_queue_state_idx "
                "ON agent_input_queue(state, created_at_epoch)"
            )
            if reset_inflight_updates:
                connection.execute(
                    "DELETE FROM telegram_updates WHERE state = 'inflight'"
                )
            connection.execute(
                "DELETE FROM telegram_updates "
                "WHERE state = 'completed' AND updated_at_epoch < ?",
                (time.time() - _COMPLETED_UPDATE_RETENTION_SECONDS,),
            )
        os.chmod(self.path, 0o600)

    @staticmethod
    def _record_from_row(row: sqlite3.Row) -> PendingAgentInput:
        return PendingAgentInput(
            id=int(row["id"]),
            user_id=int(row["user_id"]),
            thread_id=int(row["thread_id"]),
            window_id=str(row["window_id"]),
            text=str(row["text"]),
            created_at_epoch=float(row["created_at_epoch"]),
            state=str(row["state"]),
            submitted_at_epoch=(
                float(row["submitted_at_epoch"])
                if row["submitted_at_epoch"] is not None
                else None
            ),
        )

    @staticmethod
    def _retry_from_row(row: sqlite3.Row) -> TransientAgentRetry:
        return TransientAgentRetry(
            user_id=int(row["user_id"]),
            thread_id=int(row["thread_id"]),
            window_id=str(row["window_id"]),
            attempt=int(row["attempt"]),
            state=str(row["state"]),
            not_before_epoch=float(row["not_before_epoch"]),
            created_at_epoch=float(row["created_at_epoch"]),
        )

    def enqueue_agent_input(
        self,
        *,
        user_id: int,
        thread_id: int,
        window_id: str,
        text: str,
        max_pending: int,
        created_at_epoch: float | None = None,
    ) -> PendingAgentInput | None:
        """Atomically append a prompt unless the target queue is already full."""
        created_at = time.time() if created_at_epoch is None else created_at_epoch
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            pending = connection.execute(
                "SELECT COUNT(*) FROM agent_input_queue "
                "WHERE user_id = ? AND thread_id = ? AND window_id = ?",
                (user_id, thread_id, window_id),
            ).fetchone()[0]
            if int(pending) >= max_pending:
                return None
            cursor = connection.execute(
                "INSERT INTO agent_input_queue "
                "(user_id, thread_id, window_id, text, created_at_epoch) "
                "VALUES (?, ?, ?, ?, ?)",
                (user_id, thread_id, window_id, text, created_at),
            )
            if cursor.lastrowid is None:
                raise RuntimeError("SQLite did not return an agent input row id")
            record_id = int(cursor.lastrowid)
        return PendingAgentInput(
            id=record_id,
            user_id=user_id,
            thread_id=thread_id,
            window_id=window_id,
            text=text,
            created_at_epoch=created_at,
        )

    def list_pending_agent_inputs(self) -> list[PendingAgentInput]:
        """Return queued and submitted-unconfirmed prompts in FIFO order."""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT id, user_id, thread_id, window_id, text, created_at_epoch, "
                "state, submitted_at_epoch "
                "FROM agent_input_queue ORDER BY id"
            ).fetchall()
        return [self._record_from_row(row) for row in rows]

    def get_agent_input(self, record_id: int) -> PendingAgentInput | None:
        """Return one active input lifecycle record, if it still exists."""
        with self._connect() as connection:
            row = connection.execute(
                "SELECT id, user_id, thread_id, window_id, text, created_at_epoch, "
                "state, submitted_at_epoch FROM agent_input_queue WHERE id = ?",
                (record_id,),
            ).fetchone()
        return self._record_from_row(row) if row is not None else None

    def pending_agent_input_stats(self) -> PendingAgentInputStats:
        """Return active lifecycle depth and oldest age without reading text."""
        with self._connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS count, MIN(created_at_epoch) AS oldest "
                "FROM agent_input_queue"
            ).fetchone()
        if row is None:
            return PendingAgentInputStats(0, None)
        oldest = row["oldest"]
        return PendingAgentInputStats(
            count=int(row["count"]),
            oldest_created_at_epoch=float(oldest) if oldest is not None else None,
        )

    def load_health_alert_states(self) -> dict[str, HealthAlertState]:
        """Load issue cooldown state so restarts do not repeat alerts."""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT issue_key, active, last_sent_at_epoch FROM health_alert_state"
            ).fetchall()
        return {
            str(row["issue_key"]): HealthAlertState(
                active=bool(row["active"]),
                last_sent_at_epoch=float(row["last_sent_at_epoch"]),
            )
            for row in rows
        }

    def save_health_alert_state(
        self,
        issue_key: str,
        *,
        active: bool,
        last_sent_at_epoch: float,
    ) -> None:
        """Upsert one issue's active flag and last notification timestamp."""
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO health_alert_state "
                "(issue_key, active, last_sent_at_epoch) VALUES (?, ?, ?) "
                "ON CONFLICT(issue_key) DO UPDATE SET "
                "active = excluded.active, "
                "last_sent_at_epoch = excluded.last_sent_at_epoch",
                (issue_key, int(active), last_sent_at_epoch),
            )

    def retarget_agent_input(self, record_id: int, window_id: str) -> None:
        """Update a restored prompt after a persisted thread binding is re-resolved."""
        with self._connect() as connection:
            connection.execute(
                "UPDATE agent_input_queue SET window_id = ? WHERE id = ?",
                (window_id, record_id),
            )

    def mark_agent_input_submitted(
        self,
        record_id: int,
        *,
        submitted_at_epoch: float | None = None,
    ) -> bool:
        """Move a queued prompt to the no-auto-resend confirmation state."""
        submitted_at = time.time() if submitted_at_epoch is None else submitted_at_epoch
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE agent_input_queue SET state = ?, submitted_at_epoch = ? "
                "WHERE id = ? AND state = ?",
                (
                    AGENT_INPUT_SUBMITTED_UNCONFIRMED,
                    submitted_at,
                    record_id,
                    AGENT_INPUT_QUEUED,
                ),
            )
        return cursor.rowcount == 1

    def confirm_agent_input(self, record_id: int) -> None:
        """Remove a submitted prompt after its transcript entry is observed."""
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM agent_input_queue WHERE id = ? AND state = ?",
                (record_id, AGENT_INPUT_SUBMITTED_UNCONFIRMED),
            )

    def delete_agent_inputs(self, record_ids: list[int]) -> None:
        """Remove expired, discarded, or terminally failed queued prompts."""
        if not record_ids:
            return
        placeholders = ",".join("?" for _ in record_ids)
        with self._connect() as connection:
            connection.execute(
                f"DELETE FROM agent_input_queue WHERE id IN ({placeholders})",
                tuple(record_ids),
            )

    def delete_agent_inputs_for_target(
        self, user_id: int, thread_id: int, window_id: str
    ) -> None:
        """Discard all queued prompts for one explicit Telegram/agent target."""
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM agent_input_queue "
                "WHERE user_id = ? AND thread_id = ? AND window_id = ?",
                (user_id, thread_id, window_id),
            )

    def save_transient_agent_retry(
        self,
        *,
        user_id: int,
        thread_id: int,
        window_id: str,
        attempt: int,
        state: str,
        not_before_epoch: float,
        created_at_epoch: float | None = None,
    ) -> TransientAgentRetry:
        """Upsert one restart-safe continuation retry per agent window."""
        if state not in {TRANSIENT_RETRY_SCHEDULED, TRANSIENT_RETRY_WAITING_RESULT}:
            raise ValueError(f"Unsupported transient retry state: {state}")
        created_at = time.time() if created_at_epoch is None else created_at_epoch
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO transient_agent_retries "
                "(window_id, user_id, thread_id, attempt, state, "
                "not_before_epoch, created_at_epoch) VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(window_id) DO UPDATE SET "
                "user_id = excluded.user_id, thread_id = excluded.thread_id, "
                "attempt = excluded.attempt, state = excluded.state, "
                "not_before_epoch = excluded.not_before_epoch",
                (
                    window_id,
                    user_id,
                    thread_id,
                    attempt,
                    state,
                    not_before_epoch,
                    created_at,
                ),
            )
        return TransientAgentRetry(
            user_id=user_id,
            thread_id=thread_id,
            window_id=window_id,
            attempt=attempt,
            state=state,
            not_before_epoch=not_before_epoch,
            created_at_epoch=created_at,
        )

    def get_transient_agent_retry(self, window_id: str) -> TransientAgentRetry | None:
        """Return the retry lifecycle for one window."""
        with self._connect() as connection:
            row = connection.execute(
                "SELECT user_id, thread_id, window_id, attempt, state, "
                "not_before_epoch, created_at_epoch "
                "FROM transient_agent_retries WHERE window_id = ?",
                (window_id,),
            ).fetchone()
        return self._retry_from_row(row) if row is not None else None

    def list_transient_agent_retries(self) -> list[TransientAgentRetry]:
        """Return all retry lifecycles for restart recovery."""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT user_id, thread_id, window_id, attempt, state, "
                "not_before_epoch, created_at_epoch "
                "FROM transient_agent_retries ORDER BY created_at_epoch, window_id"
            ).fetchall()
        return [self._retry_from_row(row) for row in rows]

    def mark_transient_agent_retry_waiting(self, window_id: str, attempt: int) -> bool:
        """Prevent an already-due retry from being resubmitted after restart."""
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE transient_agent_retries SET state = ? "
                "WHERE window_id = ? AND attempt = ? AND state = ?",
                (
                    TRANSIENT_RETRY_WAITING_RESULT,
                    window_id,
                    attempt,
                    TRANSIENT_RETRY_SCHEDULED,
                ),
            )
        return cursor.rowcount == 1

    def delete_transient_agent_retry(self, window_id: str) -> None:
        """Clear retry history when a real user input supersedes it."""
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM transient_agent_retries WHERE window_id = ?",
                (window_id,),
            )

    def claim_telegram_update(self, update_id: int) -> bool:
        """Claim a Telegram update, returning False when it was already accepted."""
        try:
            with self._connect() as connection:
                connection.execute(
                    "INSERT INTO telegram_updates "
                    "(update_id, state, updated_at_epoch) VALUES (?, 'inflight', ?)",
                    (update_id, time.time()),
                )
        except sqlite3.IntegrityError:
            return False
        return True

    def complete_telegram_update(self, update_id: int) -> None:
        """Keep a short-lived durable record after handler completion."""
        with self._connect() as connection:
            connection.execute(
                "UPDATE telegram_updates "
                "SET state = 'completed', updated_at_epoch = ? WHERE update_id = ?",
                (time.time(), update_id),
            )

    def abandon_telegram_update(self, update_id: int) -> None:
        """Release an update claim when its handler raises before completion."""
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM telegram_updates "
                "WHERE update_id = ? AND state = 'inflight'",
                (update_id,),
            )
