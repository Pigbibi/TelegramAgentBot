"""Small SQLite-backed runtime state for restart-safe message handling.

The bot intentionally keeps this store narrow: queued prompts that have not yet
reached an agent, plus Telegram update IDs used for duplicate suppression.  It
does not replace the existing session or transcript state files.
"""

from __future__ import annotations

import os
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path


_COMPLETED_UPDATE_RETENTION_SECONDS = 7 * 24 * 60 * 60


@dataclass(frozen=True, slots=True)
class PendingAgentInput:
    """One prompt accepted by Telegram but not yet submitted to the agent."""

    id: int
    user_id: int
    thread_id: int
    window_id: str
    text: str
    created_at_epoch: float


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
                    created_at_epoch REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS agent_input_queue_target_idx
                    ON agent_input_queue(user_id, thread_id, window_id, id);

                CREATE TABLE IF NOT EXISTS telegram_updates (
                    update_id INTEGER PRIMARY KEY,
                    state TEXT NOT NULL CHECK (state IN ('inflight', 'completed')),
                    updated_at_epoch REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS telegram_updates_completed_idx
                    ON telegram_updates(state, updated_at_epoch);
                """
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
        """Return all queued prompts in stable FIFO order."""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT id, user_id, thread_id, window_id, text, created_at_epoch "
                "FROM agent_input_queue ORDER BY id"
            ).fetchall()
        return [self._record_from_row(row) for row in rows]

    def retarget_agent_input(self, record_id: int, window_id: str) -> None:
        """Update a restored prompt after a persisted thread binding is re-resolved."""
        with self._connect() as connection:
            connection.execute(
                "UPDATE agent_input_queue SET window_id = ? WHERE id = ?",
                (window_id, record_id),
            )

    def mark_agent_input_submitted(self, record_id: int) -> None:
        """Remove a prompt only after it has been accepted by the agent pane."""
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM agent_input_queue WHERE id = ?", (record_id,)
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
