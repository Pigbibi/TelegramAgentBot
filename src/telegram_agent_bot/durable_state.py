"""Small SQLite-backed runtime state for restart-safe message handling.

The bot keeps prompt submission lifecycles, bounded continuation retries,
Telegram update IDs, and authoritative conversation routing in this store.
The legacy JSON session file remains a compatibility mirror for non-routing
settings and rollback.
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
AGENT_INPUT_MODE_TURN = "turn"
AGENT_INPUT_MODE_STEER = "steer"
AGENT_INPUT_MODE_QUEUE = "queue"
AGENT_INPUT_MODES = frozenset(
    {AGENT_INPUT_MODE_TURN, AGENT_INPUT_MODE_STEER, AGENT_INPUT_MODE_QUEUE}
)
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
    transcript_session_id: str | None = None
    transcript_offset: int | None = None
    submission_mode: str = AGENT_INPUT_MODE_TURN


@dataclass(frozen=True, slots=True)
class PendingInputChoice:
    """One busy-turn Telegram message waiting for a routing choice."""

    id: int
    user_id: int
    thread_id: int
    window_id: str
    text: str
    created_at_epoch: float


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


@dataclass(frozen=True, slots=True)
class ConversationRoute:
    """Durable identity for one Telegram topic's agent destination."""

    user_id: int
    thread_id: int
    backend_id: str
    node_id: str
    window_id: str = ""
    session_id: str = ""
    generation: int = 0

    def with_generation(self, generation: int) -> "ConversationRoute":
        """Return the same route with a persisted generation number."""
        return ConversationRoute(
            user_id=self.user_id,
            thread_id=self.thread_id,
            backend_id=self.backend_id,
            node_id=self.node_id,
            window_id=self.window_id,
            session_id=self.session_id,
            generation=generation,
        )


@dataclass(frozen=True, slots=True)
class ConversationDeliveryOffset:
    """Transcript cursor guarded by the session identity that created it."""

    user_id: int
    window_id: str
    offset: int
    session_id: str = ""


@dataclass(frozen=True, slots=True)
class ConversationRegistrySnapshot:
    """One consistent routing and delivery-cursor read."""

    routes: list[ConversationRoute]
    delivery_offsets: list[ConversationDeliveryOffset]


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
                    submitted_at_epoch REAL,
                    transcript_session_id TEXT,
                    transcript_offset INTEGER,
                    submission_mode TEXT NOT NULL DEFAULT 'turn'
                        CHECK (submission_mode IN ('turn', 'steer', 'queue'))
                );
                CREATE INDEX IF NOT EXISTS agent_input_queue_target_idx
                    ON agent_input_queue(user_id, thread_id, window_id, id);

                CREATE TABLE IF NOT EXISTS pending_input_choices (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    thread_id INTEGER NOT NULL,
                    window_id TEXT NOT NULL,
                    text TEXT NOT NULL,
                    created_at_epoch REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS pending_input_choices_route_idx
                    ON pending_input_choices(user_id, thread_id, id);

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

                CREATE TABLE IF NOT EXISTS conversation_routes (
                    user_id INTEGER NOT NULL,
                    thread_id INTEGER NOT NULL,
                    backend_id TEXT NOT NULL,
                    node_id TEXT NOT NULL,
                    window_id TEXT NOT NULL DEFAULT '',
                    session_id TEXT NOT NULL DEFAULT '',
                    generation INTEGER NOT NULL CHECK (generation >= 1),
                    PRIMARY KEY (user_id, thread_id)
                );

                CREATE TABLE IF NOT EXISTS conversation_delivery_offsets (
                    user_id INTEGER NOT NULL,
                    window_id TEXT NOT NULL,
                    offset INTEGER NOT NULL CHECK (offset >= 0),
                    session_id TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY (user_id, window_id)
                );

                CREATE TABLE IF NOT EXISTS conversation_registry_metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
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
            if "transcript_session_id" not in agent_input_columns:
                connection.execute(
                    "ALTER TABLE agent_input_queue ADD COLUMN transcript_session_id TEXT"
                )
            if "transcript_offset" not in agent_input_columns:
                connection.execute(
                    "ALTER TABLE agent_input_queue ADD COLUMN transcript_offset INTEGER"
                )
            if "submission_mode" not in agent_input_columns:
                connection.execute(
                    "ALTER TABLE agent_input_queue ADD COLUMN submission_mode "
                    "TEXT NOT NULL DEFAULT 'turn' CHECK "
                    "(submission_mode IN ('turn', 'steer', 'queue'))"
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

    def load_conversation_registry(self) -> ConversationRegistrySnapshot | None:
        """Load the authoritative registry, or None before its first migration."""
        with self._connect() as connection:
            marker = connection.execute(
                "SELECT value FROM conversation_registry_metadata "
                "WHERE key = 'conversation_registry_v1'"
            ).fetchone()
            if marker is None:
                return None
            route_rows = connection.execute(
                "SELECT user_id, thread_id, backend_id, node_id, window_id, "
                "session_id, generation FROM conversation_routes "
                "ORDER BY user_id, thread_id"
            ).fetchall()
            offset_rows = connection.execute(
                "SELECT user_id, window_id, offset, session_id "
                "FROM conversation_delivery_offsets ORDER BY user_id, window_id"
            ).fetchall()
        return ConversationRegistrySnapshot(
            routes=[
                ConversationRoute(
                    user_id=int(row["user_id"]),
                    thread_id=int(row["thread_id"]),
                    backend_id=str(row["backend_id"]),
                    node_id=str(row["node_id"]),
                    window_id=str(row["window_id"]),
                    session_id=str(row["session_id"]),
                    generation=int(row["generation"]),
                )
                for row in route_rows
            ],
            delivery_offsets=[
                ConversationDeliveryOffset(
                    user_id=int(row["user_id"]),
                    window_id=str(row["window_id"]),
                    offset=int(row["offset"]),
                    session_id=str(row["session_id"]),
                )
                for row in offset_rows
            ],
        )

    def replace_conversation_registry(
        self,
        routes: list[ConversationRoute],
        delivery_offsets: list[ConversationDeliveryOffset],
    ) -> list[ConversationRoute]:
        """Atomically replace the registry and return routes with generations."""
        route_keys = {(route.user_id, route.thread_id) for route in routes}
        offset_keys = {
            (offset.user_id, offset.window_id) for offset in delivery_offsets
        }
        if len(route_keys) != len(routes):
            raise ValueError("Conversation routes must be unique per Telegram topic")
        if len(offset_keys) != len(delivery_offsets):
            raise ValueError("Conversation delivery offsets must be unique per window")

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing_rows = connection.execute(
                "SELECT user_id, thread_id, backend_id, node_id, window_id, "
                "session_id, generation FROM conversation_routes"
            ).fetchall()
            existing = {
                (int(row["user_id"]), int(row["thread_id"])): row
                for row in existing_rows
            }
            persisted_routes: list[ConversationRoute] = []
            for route in routes:
                previous = existing.get((route.user_id, route.thread_id))
                if previous is None:
                    generation = 1
                elif (
                    str(previous["backend_id"]),
                    str(previous["node_id"]),
                    str(previous["window_id"]),
                    str(previous["session_id"]),
                ) == (
                    route.backend_id,
                    route.node_id,
                    route.window_id,
                    route.session_id,
                ):
                    generation = int(previous["generation"])
                else:
                    generation = int(previous["generation"]) + 1
                persisted_routes.append(route.with_generation(generation))

            connection.execute("DELETE FROM conversation_routes")
            connection.executemany(
                "INSERT INTO conversation_routes "
                "(user_id, thread_id, backend_id, node_id, window_id, session_id, "
                "generation) VALUES (?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        route.user_id,
                        route.thread_id,
                        route.backend_id,
                        route.node_id,
                        route.window_id,
                        route.session_id,
                        route.generation,
                    )
                    for route in persisted_routes
                ],
            )
            connection.execute("DELETE FROM conversation_delivery_offsets")
            connection.executemany(
                "INSERT INTO conversation_delivery_offsets "
                "(user_id, window_id, offset, session_id) VALUES (?, ?, ?, ?)",
                [
                    (offset.user_id, offset.window_id, offset.offset, offset.session_id)
                    for offset in delivery_offsets
                ],
            )
            connection.execute(
                "INSERT INTO conversation_registry_metadata (key, value) "
                "VALUES ('conversation_registry_v1', 'ready') "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value"
            )
        return persisted_routes

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
            transcript_session_id=(
                str(row["transcript_session_id"])
                if row["transcript_session_id"] is not None
                else None
            ),
            transcript_offset=(
                int(row["transcript_offset"])
                if row["transcript_offset"] is not None
                else None
            ),
            submission_mode=str(row["submission_mode"]),
        )

    @staticmethod
    def _choice_from_row(row: sqlite3.Row) -> PendingInputChoice:
        return PendingInputChoice(
            id=int(row["id"]),
            user_id=int(row["user_id"]),
            thread_id=int(row["thread_id"]),
            window_id=str(row["window_id"]),
            text=str(row["text"]),
            created_at_epoch=float(row["created_at_epoch"]),
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
        submission_mode: str = AGENT_INPUT_MODE_TURN,
    ) -> PendingAgentInput | None:
        """Atomically append a prompt unless the target queue is already full."""
        if submission_mode not in AGENT_INPUT_MODES:
            raise ValueError(f"Unsupported agent input mode: {submission_mode}")
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
                "(user_id, thread_id, window_id, text, created_at_epoch, "
                "submission_mode) VALUES (?, ?, ?, ?, ?, ?)",
                (user_id, thread_id, window_id, text, created_at, submission_mode),
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
            submission_mode=submission_mode,
        )

    def list_pending_agent_inputs(self) -> list[PendingAgentInput]:
        """Return queued and submitted-unconfirmed prompts in FIFO order."""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT id, user_id, thread_id, window_id, text, created_at_epoch, "
                "state, submitted_at_epoch, transcript_session_id, transcript_offset, "
                "submission_mode "
                "FROM agent_input_queue ORDER BY id"
            ).fetchall()
        return [self._record_from_row(row) for row in rows]

    def get_agent_input(self, record_id: int) -> PendingAgentInput | None:
        """Return one active input lifecycle record, if it still exists."""
        with self._connect() as connection:
            row = connection.execute(
                "SELECT id, user_id, thread_id, window_id, text, created_at_epoch, "
                "state, submitted_at_epoch, transcript_session_id, transcript_offset, "
                "submission_mode "
                "FROM agent_input_queue WHERE id = ?",
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

    def has_unconfirmed_agent_input(
        self,
        user_id: int,
        thread_id: int,
        *,
        window_id: str | None = None,
    ) -> bool:
        """Return whether a route or explicit target has an unresolved submission."""
        window_filter = ""
        parameters: list[object] = [
            user_id,
            thread_id,
            AGENT_INPUT_SUBMITTED_UNCONFIRMED,
            AGENT_INPUT_MODE_QUEUE,
        ]
        if window_id is not None:
            window_filter = "AND window_id = ? "
            parameters.append(window_id)
        with self._connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM agent_input_queue "
                "WHERE user_id = ? AND thread_id = ? AND state = ? "
                f"AND submission_mode != ? {window_filter}LIMIT 1",
                tuple(parameters),
            ).fetchone()
        return row is not None

    def create_pending_input_choice(
        self,
        *,
        user_id: int,
        thread_id: int,
        window_id: str,
        text: str,
        max_pending: int,
        created_at_epoch: float | None = None,
    ) -> PendingInputChoice | None:
        """Persist a busy-turn message until the user chooses its input mode."""
        created_at = time.time() if created_at_epoch is None else created_at_epoch
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            pending = connection.execute(
                "SELECT COUNT(*) FROM pending_input_choices "
                "WHERE user_id = ? AND thread_id = ?",
                (user_id, thread_id),
            ).fetchone()[0]
            if int(pending) >= max_pending:
                return None
            cursor = connection.execute(
                "INSERT INTO pending_input_choices "
                "(user_id, thread_id, window_id, text, created_at_epoch) "
                "VALUES (?, ?, ?, ?, ?)",
                (user_id, thread_id, window_id, text, created_at),
            )
            if cursor.lastrowid is None:
                raise RuntimeError("SQLite did not return an input choice row id")
            record_id = int(cursor.lastrowid)
        return PendingInputChoice(
            id=record_id,
            user_id=user_id,
            thread_id=thread_id,
            window_id=window_id,
            text=text,
            created_at_epoch=created_at,
        )

    def claim_pending_input_choice(
        self,
        record_id: int,
        *,
        user_id: int,
        thread_id: int,
        max_age_seconds: float,
        now_epoch: float | None = None,
    ) -> PendingInputChoice | None:
        """Atomically consume one owned, unexpired routing choice."""
        now = time.time() if now_epoch is None else now_epoch
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT id, user_id, thread_id, window_id, text, created_at_epoch "
                "FROM pending_input_choices WHERE id = ? AND user_id = ? "
                "AND thread_id = ?",
                (record_id, user_id, thread_id),
            ).fetchone()
            if row is None:
                return None
            connection.execute(
                "DELETE FROM pending_input_choices WHERE id = ?",
                (record_id,),
            )
            record = self._choice_from_row(row)
            if now - record.created_at_epoch > max_age_seconds:
                return None
            return record

    def delete_pending_input_choices_for_route(
        self,
        user_id: int,
        thread_id: int,
    ) -> int:
        """Delete unchosen busy-turn messages for one Telegram topic."""
        with self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM pending_input_choices WHERE user_id = ? AND thread_id = ?",
                (user_id, thread_id),
            )
        return cursor.rowcount

    def delete_expired_input_choices(self, before_epoch: float) -> int:
        """Delete routing choices older than the supplied epoch."""
        with self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM pending_input_choices WHERE created_at_epoch < ?",
                (before_epoch,),
            )
        return cursor.rowcount

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

    def set_agent_input_submission_mode(self, record_id: int, mode: str) -> bool:
        """Change the mode of an input that has not touched the agent yet."""
        if mode not in AGENT_INPUT_MODES:
            raise ValueError(f"Unsupported agent input mode: {mode}")
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE agent_input_queue SET submission_mode = ? "
                "WHERE id = ? AND state = ?",
                (mode, record_id, AGENT_INPUT_QUEUED),
            )
        return cursor.rowcount == 1

    def mark_agent_input_submitted(
        self,
        record_id: int,
        *,
        submitted_at_epoch: float | None = None,
        transcript_session_id: str | None = None,
        transcript_offset: int | None = None,
    ) -> bool:
        """Move a queued prompt to the no-auto-resend confirmation state."""
        submitted_at = time.time() if submitted_at_epoch is None else submitted_at_epoch
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE agent_input_queue SET state = ?, submitted_at_epoch = ?, "
                "transcript_session_id = ?, transcript_offset = ? "
                "WHERE id = ? AND state = ?",
                (
                    AGENT_INPUT_SUBMITTED_UNCONFIRMED,
                    submitted_at,
                    transcript_session_id or None,
                    transcript_offset,
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

    def delete_agent_inputs_for_route(self, user_id: int, thread_id: int) -> None:
        """Discard all prompt lifecycles for one Telegram topic."""
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM agent_input_queue WHERE user_id = ? AND thread_id = ?",
                (user_id, thread_id),
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
