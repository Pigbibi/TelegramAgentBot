"""Tests for restart-safe AgentBot input and update state."""

import sqlite3

from telegram_agent_bot.durable_state import (
    AGENT_INPUT_MODE_QUEUE,
    AGENT_INPUT_MODE_TURN,
    AGENT_INPUT_QUEUED,
    AGENT_INPUT_SUBMITTED_UNCONFIRMED,
    TRANSIENT_RETRY_SCHEDULED,
    TRANSIENT_RETRY_WAITING_RESULT,
    ConversationDeliveryOffset,
    ConversationRoute,
    DurableRuntimeStore,
)


def test_agent_input_survives_store_reopen(tmp_path):
    path = tmp_path / "runtime.sqlite3"
    first = DurableRuntimeStore(path)
    first.initialize()

    record = first.enqueue_agent_input(
        user_id=12345,
        thread_id=42,
        window_id="@4",
        text="keep this prompt",
        max_pending=20,
        created_at_epoch=1000.0,
    )

    assert record is not None
    second = DurableRuntimeStore(path)
    second.initialize(reset_inflight_updates=True)
    assert second.list_pending_agent_inputs() == [record]

    assert second.mark_agent_input_submitted(
        record.id,
        submitted_at_epoch=1001.0,
        transcript_session_id="sid-4",
        transcript_offset=2048,
    )
    submitted = second.list_pending_agent_inputs()[0]
    assert submitted.state == AGENT_INPUT_SUBMITTED_UNCONFIRMED
    assert submitted.submitted_at_epoch == 1001.0
    assert submitted.transcript_session_id == "sid-4"
    assert submitted.transcript_offset == 2048
    assert second.has_unconfirmed_agent_input(12345, 42) is True
    assert second.has_unconfirmed_agent_input(12345, 42, window_id="@4") is True
    assert second.has_unconfirmed_agent_input(12345, 42, window_id="@new") is False

    third = DurableRuntimeStore(path)
    third.initialize()
    assert third.list_pending_agent_inputs() == [submitted]
    third.confirm_agent_input(record.id)
    assert third.list_pending_agent_inputs() == []


def test_legacy_agent_input_schema_migrates_without_losing_queue(tmp_path):
    path = tmp_path / "runtime.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE agent_input_queue ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, "
            "thread_id INTEGER NOT NULL, window_id TEXT NOT NULL, "
            "text TEXT NOT NULL, created_at_epoch REAL NOT NULL)"
        )
        connection.execute(
            "INSERT INTO agent_input_queue "
            "(user_id, thread_id, window_id, text, created_at_epoch) "
            "VALUES (12345, 42, '@4', 'legacy prompt', 1000.0)"
        )

    store = DurableRuntimeStore(path)
    store.initialize()

    record = store.list_pending_agent_inputs()[0]
    assert record.text == "legacy prompt"
    assert record.state == AGENT_INPUT_QUEUED
    assert record.submitted_at_epoch is None
    assert record.transcript_session_id is None
    assert record.transcript_offset is None
    assert record.submission_mode == AGENT_INPUT_MODE_TURN


def test_native_queue_confirmation_does_not_block_later_input(tmp_path):
    store = DurableRuntimeStore(tmp_path / "runtime.sqlite3")
    store.initialize()
    record = store.enqueue_agent_input(
        user_id=12345,
        thread_id=42,
        window_id="@4",
        text="next turn",
        max_pending=20,
        submission_mode=AGENT_INPUT_MODE_QUEUE,
    )

    assert record is not None
    assert store.mark_agent_input_submitted(record.id)
    assert store.has_unconfirmed_agent_input(12345, 42) is False
    assert (
        store.list_pending_agent_inputs()[0].submission_mode == AGENT_INPUT_MODE_QUEUE
    )


def test_queued_native_input_can_downgrade_to_idle_turn(tmp_path):
    store = DurableRuntimeStore(tmp_path / "runtime.sqlite3")
    store.initialize()
    record = store.enqueue_agent_input(
        user_id=12345,
        thread_id=42,
        window_id="@4",
        text="run now",
        max_pending=20,
        submission_mode=AGENT_INPUT_MODE_QUEUE,
    )

    assert record is not None
    assert store.set_agent_input_submission_mode(
        record.id,
        AGENT_INPUT_MODE_TURN,
    )
    assert store.get_agent_input(record.id).submission_mode == AGENT_INPUT_MODE_TURN


def test_pending_input_choice_is_owned_one_shot_and_expiring(tmp_path):
    store = DurableRuntimeStore(tmp_path / "runtime.sqlite3")
    store.initialize()
    choice = store.create_pending_input_choice(
        user_id=12345,
        thread_id=42,
        window_id="@4",
        text="guide this",
        max_pending=20,
        created_at_epoch=1000.0,
    )

    assert choice is not None
    assert (
        store.claim_pending_input_choice(
            choice.id,
            user_id=99999,
            thread_id=42,
            max_age_seconds=900.0,
            now_epoch=1001.0,
        )
        is None
    )
    assert (
        store.claim_pending_input_choice(
            choice.id,
            user_id=12345,
            thread_id=42,
            max_age_seconds=900.0,
            now_epoch=1001.0,
        )
        == choice
    )
    assert (
        store.claim_pending_input_choice(
            choice.id,
            user_id=12345,
            thread_id=42,
            max_age_seconds=900.0,
            now_epoch=1001.0,
        )
        is None
    )

    expired = store.create_pending_input_choice(
        user_id=12345,
        thread_id=42,
        window_id="@4",
        text="too old",
        max_pending=20,
        created_at_epoch=1000.0,
    )
    assert expired is not None
    assert (
        store.claim_pending_input_choice(
            expired.id,
            user_id=12345,
            thread_id=42,
            max_age_seconds=900.0,
            now_epoch=2000.0,
        )
        is None
    )


def test_transient_agent_retry_survives_state_transitions_and_reopen(tmp_path):
    path = tmp_path / "runtime.sqlite3"
    first = DurableRuntimeStore(path)
    first.initialize()
    scheduled = first.save_transient_agent_retry(
        user_id=12345,
        thread_id=42,
        window_id="@4",
        attempt=1,
        state=TRANSIENT_RETRY_SCHEDULED,
        not_before_epoch=1015.0,
        created_at_epoch=1000.0,
    )

    second = DurableRuntimeStore(path)
    second.initialize()
    assert second.list_transient_agent_retries() == [scheduled]
    assert second.mark_transient_agent_retry_waiting("@4", 1) is True
    waiting = second.get_transient_agent_retry("@4")
    assert waiting is not None
    assert waiting.state == TRANSIENT_RETRY_WAITING_RESULT

    second.delete_transient_agent_retry("@4")
    assert second.list_transient_agent_retries() == []
    assert second.list_pending_agent_inputs() == []


def test_agent_input_limit_is_enforced_transactionally(tmp_path):
    store = DurableRuntimeStore(tmp_path / "runtime.sqlite3")
    store.initialize()
    first = store.enqueue_agent_input(
        user_id=12345,
        thread_id=42,
        window_id="@4",
        text="first",
        max_pending=1,
    )
    second = store.enqueue_agent_input(
        user_id=12345,
        thread_id=42,
        window_id="@4",
        text="second",
        max_pending=1,
    )

    assert first is not None
    assert second is None
    assert [item.text for item in store.list_pending_agent_inputs()] == ["first"]

    assert store.mark_agent_input_submitted(first.id)
    still_blocked = store.enqueue_agent_input(
        user_id=12345,
        thread_id=42,
        window_id="@4",
        text="third",
        max_pending=1,
    )
    assert still_blocked is None


def test_completed_telegram_update_is_deduplicated_across_restarts(tmp_path):
    path = tmp_path / "runtime.sqlite3"
    first = DurableRuntimeStore(path)
    first.initialize()

    assert first.claim_telegram_update(987) is True
    first.complete_telegram_update(987)

    second = DurableRuntimeStore(path)
    second.initialize(reset_inflight_updates=True)
    assert second.claim_telegram_update(987) is False


def test_inflight_telegram_update_is_recoverable_after_restart(tmp_path):
    path = tmp_path / "runtime.sqlite3"
    first = DurableRuntimeStore(path)
    first.initialize()
    assert first.claim_telegram_update(987) is True

    second = DurableRuntimeStore(path)
    second.initialize(reset_inflight_updates=True)
    assert second.claim_telegram_update(987) is True


def test_pending_input_stats_do_not_load_prompt_text(tmp_path):
    store = DurableRuntimeStore(tmp_path / "runtime.sqlite3")
    store.initialize()
    store.enqueue_agent_input(
        user_id=12345,
        thread_id=42,
        window_id="@4",
        text="first",
        max_pending=20,
        created_at_epoch=1000.0,
    )
    store.enqueue_agent_input(
        user_id=12345,
        thread_id=42,
        window_id="@4",
        text="second",
        max_pending=20,
        created_at_epoch=2000.0,
    )

    stats = store.pending_agent_input_stats()

    assert stats.count == 2
    assert stats.oldest_created_at_epoch == 1000.0


def test_health_alert_cooldown_state_survives_reopen(tmp_path):
    path = tmp_path / "runtime.sqlite3"
    first = DurableRuntimeStore(path)
    first.initialize()
    first.save_health_alert_state(
        "disk",
        active=True,
        last_sent_at_epoch=1234.5,
    )

    second = DurableRuntimeStore(path)
    second.initialize()

    state = second.load_health_alert_states()["disk"]
    assert state.active is True
    assert state.last_sent_at_epoch == 1234.5


def test_conversation_registry_preserves_generation_and_delivery_identity(tmp_path):
    store = DurableRuntimeStore(tmp_path / "runtime.sqlite3")
    store.initialize()
    route = ConversationRoute(
        user_id=12345,
        thread_id=42,
        backend_id="local",
        node_id="local",
        window_id="@4",
        session_id="sid-4",
    )
    offset = ConversationDeliveryOffset(
        user_id=12345,
        window_id="@4",
        offset=2048,
        session_id="sid-4",
    )

    store.replace_conversation_registry([route], [offset])
    first = store.load_conversation_registry()
    assert first is not None
    assert first.routes == [route.with_generation(1)]
    assert first.delivery_offsets == [offset]

    store.replace_conversation_registry([route], [offset])
    assert store.load_conversation_registry().routes == [route.with_generation(1)]

    resumed = ConversationRoute(
        user_id=12345,
        thread_id=42,
        backend_id="local",
        node_id="local",
        window_id="@4",
        session_id="sid-5",
    )
    store.replace_conversation_registry(
        [resumed],
        [
            ConversationDeliveryOffset(
                user_id=12345,
                window_id="@4",
                offset=0,
                session_id="sid-5",
            )
        ],
    )

    second = store.load_conversation_registry()
    assert second is not None
    assert second.routes == [resumed.with_generation(2)]
    assert second.delivery_offsets == [
        ConversationDeliveryOffset(
            user_id=12345,
            window_id="@4",
            offset=0,
            session_id="sid-5",
        )
    ]
