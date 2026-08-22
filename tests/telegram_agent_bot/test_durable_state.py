"""Tests for restart-safe AgentBot input and update state."""

from telegram_agent_bot.durable_state import DurableRuntimeStore


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

    second.mark_agent_input_submitted(record.id)
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
