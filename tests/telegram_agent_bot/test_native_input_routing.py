from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from telegram_agent_bot import bot as bot_module
from telegram_agent_bot.durable_state import (
    AGENT_INPUT_MODE_QUEUE,
    AGENT_INPUT_MODE_STEER,
    AGENT_INPUT_SUBMITTED_UNCONFIRMED,
    DurableRuntimeStore,
)


def _make_text_update(text: str, user_id: int = 12345, thread_id: int = 42):
    update = MagicMock()
    update.effective_user = MagicMock(id=user_id)
    update.message = MagicMock()
    update.message.text = text
    update.message.message_thread_id = thread_id
    update.message.chat = MagicMock()
    update.message.chat.type = "supergroup"
    update.message.chat.send_action = AsyncMock()
    update.effective_chat = MagicMock(type="supergroup", id=-1001234567890)
    return update


def _make_context():
    context = MagicMock()
    context.bot = AsyncMock()
    context.user_data = {}
    return context


@pytest.fixture
def runtime_store(monkeypatch, tmp_path):
    store = DurableRuntimeStore(tmp_path / "runtime.sqlite3")
    store.initialize()
    monkeypatch.setattr(bot_module, "_runtime_store", store)
    bot_module._agent_input_queues.clear()
    bot_module._agent_input_tasks.clear()
    bot_module._agent_input_confirmation_tasks.clear()
    bot_module._agent_input_confirmation_targets.clear()
    bot_module._agent_input_confirmation_modes.clear()
    yield store
    bot_module._agent_input_queues.clear()
    bot_module._agent_input_tasks.clear()
    bot_module._agent_input_confirmation_tasks.clear()
    bot_module._agent_input_confirmation_targets.clear()
    bot_module._agent_input_confirmation_modes.clear()


@pytest.mark.asyncio
async def test_native_queue_is_persisted_before_tab_submission(
    monkeypatch,
    runtime_store,
):
    send_native = AsyncMock(return_value=(True, "sent"))
    confirmations: list[tuple[int, str]] = []
    monkeypatch.setattr(bot_module, "_send_native_input_to_agent", send_native)
    monkeypatch.setattr(
        bot_module,
        "capture_agent_output",
        AsyncMock(
            return_value=SimpleNamespace(
                text="• Working (12s • esc to interrupt)",
                missing=False,
            )
        ),
    )
    monkeypatch.setattr(
        bot_module.session_manager,
        "transcript_confirmation_baseline",
        AsyncMock(return_value=("sid-1", 256)),
    )
    monkeypatch.setattr(
        bot_module,
        "_ensure_agent_input_confirmation_task",
        lambda _bot, record_id, _key, **kwargs: confirmations.append(
            (record_id, kwargs["submission_mode"])
        ),
    )
    monkeypatch.setattr(bot_module, "_reset_transient_agent_retry", lambda _wid: None)

    ok, message = await bot_module._send_native_agent_input(
        MagicMock(),
        12345,
        42,
        "@1",
        "next task",
        mode=AGENT_INPUT_MODE_QUEUE,
    )

    assert ok is True
    assert "Tab" in message
    send_native.assert_awaited_once_with(
        12345,
        42,
        "@1",
        "next task",
        mode=AGENT_INPUT_MODE_QUEUE,
    )
    records = runtime_store.list_pending_agent_inputs()
    assert len(records) == 1
    assert records[0].state == AGENT_INPUT_SUBMITTED_UNCONFIRMED
    assert records[0].submission_mode == AGENT_INPUT_MODE_QUEUE
    assert confirmations == [(records[0].id, AGENT_INPUT_MODE_QUEUE)]


@pytest.mark.asyncio
async def test_native_queue_degrades_to_enter_when_codex_became_idle(
    monkeypatch,
    runtime_store,
):
    send_or_queue = AsyncMock(return_value=(True, "sent", False))
    send_native = AsyncMock()
    monkeypatch.setattr(
        bot_module,
        "capture_agent_output",
        AsyncMock(
            return_value=SimpleNamespace(
                text="previous output\n\n›\n\n  gpt-5.5 · ~/repo",
                missing=False,
            )
        ),
    )
    monkeypatch.setattr(bot_module, "_send_or_queue_agent_input", send_or_queue)
    monkeypatch.setattr(bot_module, "_send_native_input_to_agent", send_native)
    monkeypatch.setattr(bot_module, "_reset_transient_agent_retry", lambda _wid: None)

    ok, message = await bot_module._send_native_agent_input(
        MagicMock(),
        12345,
        42,
        "@1",
        "next task",
        mode=AGENT_INPUT_MODE_QUEUE,
    )

    assert ok is True
    assert "Enter" in message
    send_or_queue.assert_awaited_once()
    send_native.assert_not_awaited()
    assert runtime_store.list_pending_agent_inputs() == []


@pytest.mark.asyncio
async def test_claude_busy_command_uses_enter_and_persists_submission(
    monkeypatch,
    runtime_store,
):
    send_native = AsyncMock(return_value=(True, "sent"))
    confirmations: list[tuple[int, str]] = []
    monkeypatch.setattr(bot_module, "_window_agent_type", lambda _wid: "claude")
    monkeypatch.setattr(bot_module, "_send_native_input_to_agent", send_native)
    monkeypatch.setattr(
        bot_module,
        "capture_agent_output",
        AsyncMock(
            return_value=SimpleNamespace(
                text="Claude is working…\nEsc to interrupt",
                missing=False,
            )
        ),
    )
    monkeypatch.setattr(
        bot_module.session_manager,
        "transcript_confirmation_baseline",
        AsyncMock(return_value=("sid-1", 256)),
    )
    monkeypatch.setattr(
        bot_module,
        "_ensure_agent_input_confirmation_task",
        lambda _bot, record_id, _key, **kwargs: confirmations.append(
            (record_id, kwargs["submission_mode"])
        ),
    )
    monkeypatch.setattr(bot_module, "_reset_transient_agent_retry", lambda _wid: None)

    ok, message = await bot_module._send_native_agent_input(
        MagicMock(),
        12345,
        42,
        "@1",
        "/plugin",
        mode=AGENT_INPUT_MODE_STEER,
    )

    assert ok is True
    assert "Claude Code will run it after the current turn" in message
    send_native.assert_awaited_once_with(
        12345,
        42,
        "@1",
        "/plugin",
        mode=AGENT_INPUT_MODE_STEER,
    )
    records = runtime_store.list_pending_agent_inputs()
    assert len(records) == 1
    assert records[0].submission_mode == AGENT_INPUT_MODE_STEER
    assert confirmations == [(records[0].id, AGENT_INPUT_MODE_STEER)]


@pytest.mark.asyncio
async def test_busy_codex_text_offers_native_routing_choice(monkeypatch):
    update = _make_text_update("change direction")
    context = _make_context()
    window = SimpleNamespace(
        window_id="@1",
        window_name="Project",
        cwd="/tmp/project",
        pane_current_command="codex",
    )
    offer_choice = AsyncMock(return_value=True)
    send_or_queue = AsyncMock()

    monkeypatch.setattr(bot_module, "is_user_allowed", lambda _user_id: True)
    monkeypatch.setattr(bot_module, "_get_thread_id", lambda _update: 42)
    monkeypatch.setattr(
        bot_module.session_manager,
        "get_ambiguous_window_for_thread",
        lambda _user_id, _thread_id: None,
    )
    monkeypatch.setattr(
        bot_module.session_manager,
        "get_window_for_thread",
        lambda _user_id, _thread_id: "@1",
    )
    monkeypatch.setattr(
        bot_module.session_manager,
        "window_has_usage_limit_exceeded",
        AsyncMock(return_value=False),
    )
    monkeypatch.setattr(
        bot_module.tmux_manager,
        "find_window_by_id",
        AsyncMock(return_value=window),
    )
    monkeypatch.setattr(
        bot_module,
        "_handle_non_codex_bound_window",
        AsyncMock(return_value=False),
    )
    monkeypatch.setattr(
        bot_module,
        "capture_agent_output",
        AsyncMock(
            return_value=SimpleNamespace(
                text="• Working (12s • esc to interrupt)\n\n  gpt-5.5 · ~/repo",
                missing=False,
            )
        ),
    )
    monkeypatch.setattr(
        bot_module,
        "_handle_auth_error_bound_window",
        AsyncMock(return_value=False),
    )
    monkeypatch.setattr(
        bot_module, "_window_supports_native_input_routing", lambda _wid: True
    )
    monkeypatch.setattr(bot_module, "_route_can_use_native_input", lambda _key: True)
    monkeypatch.setattr(bot_module, "_offer_input_route_choice", offer_choice)
    monkeypatch.setattr(bot_module, "_send_or_queue_agent_input", send_or_queue)
    monkeypatch.setattr(bot_module, "enqueue_status_update", AsyncMock())
    monkeypatch.setattr(bot_module, "_cancel_bash_capture", lambda *_args: None)

    await bot_module.text_handler(update, context)

    offer_choice.assert_awaited_once_with(
        update.message,
        user_id=12345,
        thread_id=42,
        window_id="@1",
        text="change direction",
    )
    send_or_queue.assert_not_awaited()


@pytest.mark.asyncio
async def test_queue_callback_claims_choice_and_uses_native_queue(
    monkeypatch,
    runtime_store,
):
    choice = runtime_store.create_pending_input_choice(
        user_id=12345,
        thread_id=42,
        window_id="@1",
        text="next task",
        max_pending=20,
    )
    assert choice is not None
    update = MagicMock()
    update.effective_user = MagicMock(id=12345)
    update.callback_query = MagicMock()
    update.callback_query.answer = AsyncMock()
    context = _make_context()
    send_native = AsyncMock(return_value=(True, "queued with Tab"))
    safe_edit = AsyncMock()

    monkeypatch.setattr(bot_module, "_get_thread_id", lambda _update: 42)
    monkeypatch.setattr(
        bot_module.session_manager,
        "resolve_window_for_thread",
        lambda _user_id, _thread_id: "@1",
    )
    monkeypatch.setattr(
        bot_module, "_window_supports_native_input_routing", lambda _wid: True
    )
    monkeypatch.setattr(
        bot_module,
        "capture_agent_output",
        AsyncMock(
            return_value=SimpleNamespace(
                text="• Working (12s • esc to interrupt)",
                missing=False,
            )
        ),
    )
    monkeypatch.setattr(bot_module, "_send_native_agent_input", send_native)
    monkeypatch.setattr(bot_module, "mark_window_working", AsyncMock())
    monkeypatch.setattr(bot_module, "safe_edit", safe_edit)

    await bot_module._handle_input_routing_callback(
        update,
        context,
        mode="queue",
        record_id=choice.id,
    )

    send_native.assert_awaited_once_with(
        context.bot,
        12345,
        42,
        "@1",
        "next task",
        mode=AGENT_INPUT_MODE_QUEUE,
    )
    safe_edit.assert_awaited_once_with(update.callback_query, "✅ queued with Tab")
    assert (
        runtime_store.claim_pending_input_choice(
            choice.id,
            user_id=12345,
            thread_id=42,
            max_age_seconds=900.0,
        )
        is None
    )


@pytest.mark.asyncio
async def test_busy_codex_command_offers_native_routing_choice(monkeypatch):
    offer_choice = AsyncMock(return_value=True)
    send_native = AsyncMock()
    monkeypatch.setattr(
        bot_module,
        "capture_agent_output",
        AsyncMock(
            return_value=SimpleNamespace(
                text="• Working (12s • esc to interrupt)",
                missing=False,
            )
        ),
    )
    monkeypatch.setattr(bot_module, "_window_agent_type", lambda _wid: "codex")
    monkeypatch.setattr(
        bot_module, "_window_supports_native_input_routing", lambda _wid: True
    )
    monkeypatch.setattr(bot_module, "_route_can_use_native_input", lambda _key: True)
    monkeypatch.setattr(bot_module, "_offer_input_route_choice", offer_choice)
    monkeypatch.setattr(bot_module, "_send_native_agent_input", send_native)

    ok, message, delivery = await bot_module._send_local_forwarded_command(
        MagicMock(),
        MagicMock(),
        user_id=12345,
        thread_id=42,
        window_id="@1",
        command_text="/plugins",
    )

    assert (ok, message, delivery) == (True, "", "choice")
    offer_choice.assert_awaited_once()
    send_native.assert_not_awaited()


@pytest.mark.asyncio
async def test_busy_claude_command_uses_claude_native_command_queue(monkeypatch):
    send_native = AsyncMock(return_value=(True, "queued by Claude Code"))
    mark_working = AsyncMock()
    bot_client = MagicMock()
    monkeypatch.setattr(
        bot_module,
        "capture_agent_output",
        AsyncMock(
            return_value=SimpleNamespace(
                text="Claude is working…\nEsc to interrupt",
                missing=False,
            )
        ),
    )
    monkeypatch.setattr(bot_module, "_window_agent_type", lambda _wid: "claude")
    monkeypatch.setattr(
        bot_module, "_window_supports_native_input_routing", lambda _wid: True
    )
    monkeypatch.setattr(bot_module, "_route_can_use_native_input", lambda _key: True)
    monkeypatch.setattr(bot_module, "_send_native_agent_input", send_native)
    monkeypatch.setattr(bot_module, "mark_window_working", mark_working)

    ok, message, delivery = await bot_module._send_local_forwarded_command(
        bot_client,
        MagicMock(),
        user_id=12345,
        thread_id=42,
        window_id="@1",
        command_text="/plugin",
    )

    assert (ok, message, delivery) == (True, "queued by Claude Code", "native")
    send_native.assert_awaited_once_with(
        bot_client,
        12345,
        42,
        "@1",
        "/plugin",
        mode=AGENT_INPUT_MODE_STEER,
    )
    mark_working.assert_awaited_once()


@pytest.mark.asyncio
async def test_claude_queue_callback_uses_durable_bot_fifo(
    monkeypatch,
    runtime_store,
):
    choice = runtime_store.create_pending_input_choice(
        user_id=12345,
        thread_id=42,
        window_id="@1",
        text="next task",
        max_pending=20,
    )
    assert choice is not None
    update = MagicMock()
    update.effective_user = MagicMock(id=12345)
    update.callback_query = MagicMock()
    update.callback_query.answer = AsyncMock()
    context = _make_context()
    send_or_queue = AsyncMock(return_value=(True, "Queued (1/20)", True))
    send_native = AsyncMock()
    safe_edit = AsyncMock()

    monkeypatch.setattr(bot_module, "_get_thread_id", lambda _update: 42)
    monkeypatch.setattr(
        bot_module.session_manager,
        "resolve_window_for_thread",
        lambda _user_id, _thread_id: "@1",
    )
    monkeypatch.setattr(
        bot_module, "_window_supports_native_input_routing", lambda _wid: True
    )
    monkeypatch.setattr(bot_module, "_window_agent_type", lambda _wid: "claude")
    monkeypatch.setattr(
        bot_module,
        "capture_agent_output",
        AsyncMock(
            return_value=SimpleNamespace(
                text="Claude is working…\nEsc to interrupt",
                missing=False,
            )
        ),
    )
    monkeypatch.setattr(bot_module, "_send_or_queue_agent_input", send_or_queue)
    monkeypatch.setattr(bot_module, "_send_native_agent_input", send_native)
    monkeypatch.setattr(bot_module, "safe_edit", safe_edit)

    await bot_module._handle_input_routing_callback(
        update,
        context,
        mode="queue",
        record_id=choice.id,
    )

    send_or_queue.assert_awaited_once_with(
        context.bot,
        12345,
        42,
        "@1",
        "next task",
    )
    send_native.assert_not_awaited()
    safe_edit.assert_awaited_once_with(update.callback_query, "⏳ Queued (1/20)")
