import asyncio
from collections import deque
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from telegram_codex_bot import bot as bot_module


@pytest.fixture(autouse=True)
def clear_agent_input_queue_state():
    bot_module._agent_input_queues.clear()
    bot_module._agent_input_tasks.clear()
    bot_module._agent_input_locks.clear()
    yield
    bot_module._agent_input_queues.clear()
    bot_module._agent_input_tasks.clear()
    bot_module._agent_input_locks.clear()


@pytest.mark.asyncio
async def test_send_or_queue_agent_input_sends_to_codex_native_queue_when_busy(
    monkeypatch,
):
    capture = SimpleNamespace(
        text="• Working (12s • esc to interrupt)\n\n  gpt-5.5 · ~/repo",
        missing=False,
    )
    send_message = AsyncMock(return_value=(True, "Sent"))

    monkeypatch.setattr(
        bot_module, "capture_agent_output", AsyncMock(return_value=capture)
    )
    monkeypatch.setattr(bot_module, "_send_message_to_agent", send_message)

    ok, message, queued = await bot_module._send_or_queue_agent_input(
        MagicMock(),
        12345,
        42,
        "@1",
        "next prompt",
    )

    assert (ok, message, queued) == (True, "Sent", False)
    send_message.assert_awaited_once_with(12345, 42, "@1", "next prompt")
    assert bot_module._agent_input_queues == {}


@pytest.mark.asyncio
async def test_send_or_queue_agent_input_interrupts_and_queues_during_interactive_ui(
    monkeypatch,
):
    capture = SimpleNamespace(
        text="  Do you want to proceed?\n  Some permission details\n  Esc to cancel\n",
        missing=False,
    )
    send_message = AsyncMock()
    send_control = AsyncMock(return_value=(True, ""))
    ensured: list[tuple[int, int, str]] = []

    monkeypatch.setattr(
        bot_module, "capture_agent_output", AsyncMock(return_value=capture)
    )
    monkeypatch.setattr(bot_module, "_send_message_to_agent", send_message)
    monkeypatch.setattr(bot_module, "_send_control_to_agent", send_control)
    monkeypatch.setattr(
        bot_module,
        "_ensure_agent_input_drain_task",
        lambda _bot, key: ensured.append(key),
    )

    ok, message, queued = await bot_module._send_or_queue_agent_input(
        MagicMock(),
        12345,
        42,
        "@1",
        "answer after prompt",
    )

    assert ok is True
    assert queued is True
    assert message.startswith("Interrupted Codex prompt and queued")
    assert (
        list(bot_module._agent_input_queues[(12345, 42, "@1")])[0].text
        == "answer after prompt"
    )
    assert ensured == [(12345, 42, "@1")]
    send_control.assert_awaited_once_with(12345, 42, "@1", "Escape")
    send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_send_or_queue_agent_input_sends_immediately_when_ready(monkeypatch):
    capture = SimpleNamespace(
        text="previous output\n\n›\n\n  gpt-5.5 · ~/repo", missing=False
    )
    send_message = AsyncMock(return_value=(True, "Sent"))

    monkeypatch.setattr(
        bot_module, "capture_agent_output", AsyncMock(return_value=capture)
    )
    monkeypatch.setattr(bot_module, "_send_message_to_agent", send_message)

    ok, message, queued = await bot_module._send_or_queue_agent_input(
        MagicMock(),
        12345,
        42,
        "@1",
        "prompt",
    )

    assert (ok, message, queued) == (True, "Sent", False)
    send_message.assert_awaited_once_with(12345, 42, "@1", "prompt")
    assert bot_module._agent_input_queues == {}
    assert bot_module._agent_input_locks == {}


@pytest.mark.asyncio
async def test_send_or_queue_agent_input_sends_when_idle_prompt_text_is_visible(
    monkeypatch,
):
    capture = SimpleNamespace(
        text="previous output\n\n› Improve documentation in @filename\n\n  gpt-5.5 · ~/repo",
        missing=False,
    )
    send_message = AsyncMock(return_value=(True, "Sent"))

    monkeypatch.setattr(
        bot_module, "capture_agent_output", AsyncMock(return_value=capture)
    )
    monkeypatch.setattr(bot_module, "_send_message_to_agent", send_message)

    ok, message, queued = await bot_module._send_or_queue_agent_input(
        MagicMock(),
        12345,
        42,
        "@1",
        "new prompt",
    )

    assert (ok, message, queued) == (True, "Sent", False)
    send_message.assert_awaited_once_with(12345, 42, "@1", "new prompt")
    assert bot_module._agent_input_queues == {}


@pytest.mark.asyncio
async def test_send_or_queue_agent_input_rejects_when_queue_is_full(monkeypatch):
    key = (12345, 42, "@1")
    bot_module._agent_input_queues[key] = deque(
        [bot_module._QueuedAgentInput(text="old prompt")]
    )
    monkeypatch.setattr(bot_module.config, "agent_input_queue_max_size", 1)
    monkeypatch.setattr(bot_module, "_ensure_agent_input_drain_task", lambda *_: None)

    ok, message, queued = await bot_module._send_or_queue_agent_input(
        MagicMock(),
        12345,
        42,
        "@1",
        "new prompt",
    )

    assert (ok, queued) == (False, False)
    assert "input queue is full" in message
    assert [item.text for item in bot_module._agent_input_queues[key]] == ["old prompt"]


@pytest.mark.asyncio
async def test_discard_queued_agent_input_clears_queue_and_cancels_task():
    key = (12345, 42, "@1")
    bot_module._agent_input_queues[key] = deque(
        [
            bot_module._QueuedAgentInput(text="old prompt"),
            bot_module._QueuedAgentInput(text="older prompt"),
        ]
    )
    started = asyncio.Event()

    async def sleeper():
        started.set()
        await asyncio.sleep(60)

    task = asyncio.create_task(sleeper())
    await started.wait()
    bot_module._agent_input_tasks[key] = task

    dropped = await bot_module._discard_queued_agent_input(12345, 42, "@1")

    assert dropped == 2
    assert key not in bot_module._agent_input_queues
    assert key not in bot_module._agent_input_tasks
    assert task.cancelled()


@pytest.mark.asyncio
async def test_drain_agent_input_queue_waits_until_ready(monkeypatch):
    key = (12345, 42, "@1")
    bot_module._agent_input_queues[key] = deque(
        [bot_module._QueuedAgentInput(text="queued prompt")]
    )
    capture_busy = SimpleNamespace(
        text="• Working (12s • esc to interrupt)\n\n  gpt-5.5 · ~/repo",
        missing=False,
    )
    capture_ready = SimpleNamespace(
        text="previous output\n\n›\n\n  gpt-5.5 · ~/repo", missing=False
    )
    send_message = AsyncMock(return_value=(True, "Sent"))
    mark_working = AsyncMock()
    refresh_session = AsyncMock(return_value=True)

    monkeypatch.setattr(
        bot_module,
        "capture_agent_output",
        AsyncMock(side_effect=[capture_busy, capture_ready]),
    )
    monkeypatch.setattr(bot_module, "_send_message_to_agent", send_message)
    monkeypatch.setattr(bot_module, "mark_window_working", mark_working)
    monkeypatch.setattr(
        bot_module,
        "_refresh_session_map_after_first_prompt",
        refresh_session,
    )
    monkeypatch.setattr(bot_module.asyncio, "sleep", AsyncMock())

    telegram_bot = MagicMock()
    await bot_module._drain_agent_input_queue(telegram_bot, key)

    send_message.assert_awaited_once_with(12345, 42, "@1", "queued prompt")
    mark_working.assert_awaited_once_with(telegram_bot, 12345, "@1", 42)
    refresh_session.assert_awaited_once_with(
        "@1",
        text="queued prompt",
        confirm_existing_session=True,
    )
    assert key not in bot_module._agent_input_queues


@pytest.mark.asyncio
async def test_drain_agent_input_queue_notifies_when_submit_confirmation_fails(
    monkeypatch,
):
    key = (12345, 42, "@1")
    bot_module._agent_input_queues[key] = deque(
        [bot_module._QueuedAgentInput(text="queued prompt")]
    )
    capture_ready = SimpleNamespace(
        text="previous output\n\n›\n\n  gpt-5.5 · ~/repo", missing=False
    )
    notify = AsyncMock()

    monkeypatch.setattr(
        bot_module,
        "capture_agent_output",
        AsyncMock(return_value=capture_ready),
    )
    monkeypatch.setattr(
        bot_module, "_send_message_to_agent", AsyncMock(return_value=(True, "Sent"))
    )
    monkeypatch.setattr(bot_module, "mark_window_working", AsyncMock())
    monkeypatch.setattr(
        bot_module,
        "_refresh_session_map_after_first_prompt",
        AsyncMock(return_value=False),
    )
    monkeypatch.setattr(bot_module, "_notify_queued_input_failure", notify)
    monkeypatch.setattr(bot_module.asyncio, "sleep", AsyncMock())

    await bot_module._drain_agent_input_queue(MagicMock(), key)

    notify.assert_awaited_once()
    assert "did not confirm" in notify.await_args.args[3]
    assert key not in bot_module._agent_input_queues


@pytest.mark.asyncio
async def test_drain_agent_input_queue_sends_when_idle_prompt_text_is_visible(
    monkeypatch,
):
    key = (12345, 42, "@1")
    bot_module._agent_input_queues[key] = deque(
        [bot_module._QueuedAgentInput(text="queued prompt")]
    )
    capture_ready_with_visible_prompt = SimpleNamespace(
        text="previous output\n\n› Improve documentation in @filename\n\n  gpt-5.5 · ~/repo",
        missing=False,
    )
    notify = AsyncMock()
    send_message = AsyncMock(return_value=(True, "Sent"))
    mark_working = AsyncMock()
    refresh_session = AsyncMock(return_value=True)
    sleep = AsyncMock()

    monkeypatch.setattr(
        bot_module,
        "capture_agent_output",
        AsyncMock(return_value=capture_ready_with_visible_prompt),
    )
    monkeypatch.setattr(bot_module, "_send_message_to_agent", send_message)
    monkeypatch.setattr(bot_module, "_notify_queued_input_failure", notify)
    monkeypatch.setattr(bot_module, "mark_window_working", mark_working)
    monkeypatch.setattr(
        bot_module,
        "_refresh_session_map_after_first_prompt",
        refresh_session,
    )
    monkeypatch.setattr(bot_module.asyncio, "sleep", sleep)

    await bot_module._drain_agent_input_queue(MagicMock(), key)

    notify.assert_not_awaited()
    sleep.assert_awaited_once_with(bot_module._AGENT_INPUT_POLL_INTERVAL_SECONDS)
    send_message.assert_awaited_once_with(12345, 42, "@1", "queued prompt")
    mark_working.assert_awaited_once()
    refresh_session.assert_awaited_once()
    assert key not in bot_module._agent_input_queues


@pytest.mark.asyncio
async def test_drain_agent_input_queue_drops_expired_items(monkeypatch):
    key = (12345, 42, "@1")
    bot_module._agent_input_queues[key] = deque(
        [bot_module._QueuedAgentInput(text="queued prompt", created_at=1.0)]
    )
    notify = AsyncMock()

    monkeypatch.setattr(bot_module.config, "agent_input_queue_max_wait_seconds", 10.0)
    monkeypatch.setattr(bot_module.time, "monotonic", lambda: 12.0)
    monkeypatch.setattr(bot_module, "_notify_queued_input_failure", notify)

    await bot_module._drain_agent_input_queue(MagicMock(), key)

    notify.assert_awaited_once()
    assert "expired" in notify.await_args.args[3]
    assert key not in bot_module._agent_input_queues


@pytest.mark.asyncio
async def test_drain_agent_input_queue_keeps_items_when_expiry_disabled(monkeypatch):
    key = (12345, 42, "@1")
    bot_module._agent_input_queues[key] = deque(
        [bot_module._QueuedAgentInput(text="queued prompt", created_at=1.0)]
    )
    notify = AsyncMock()
    capture_busy = SimpleNamespace(
        text="• Working (12s • esc to interrupt)\n\n  gpt-5.5 · ~/repo",
        missing=False,
    )

    monkeypatch.setattr(bot_module.config, "agent_input_queue_max_wait_seconds", 0.0)
    monkeypatch.setattr(bot_module.time, "monotonic", lambda: 9999.0)
    monkeypatch.setattr(bot_module, "_notify_queued_input_failure", notify)
    monkeypatch.setattr(
        bot_module,
        "capture_agent_output",
        AsyncMock(side_effect=[capture_busy, asyncio.CancelledError()]),
    )
    monkeypatch.setattr(bot_module.asyncio, "sleep", AsyncMock())

    with pytest.raises(asyncio.CancelledError):
        await bot_module._drain_agent_input_queue(MagicMock(), key)

    notify.assert_not_awaited()
    assert [item.text for item in bot_module._agent_input_queues[key]] == [
        "queued prompt"
    ]


@pytest.mark.asyncio
async def test_send_to_window_when_ready_sends_with_visible_idle_prompt(monkeypatch):
    capture = SimpleNamespace(
        text="previous output\n\n› Improve documentation in @filename\n\n  gpt-5.5 · ~/repo",
        missing=False,
    )
    send_message = AsyncMock(return_value=(True, "Sent"))

    monkeypatch.setattr(
        bot_module, "capture_agent_output", AsyncMock(return_value=capture)
    )
    monkeypatch.setattr(bot_module, "_send_message_to_agent", send_message)

    ok, message = await bot_module._send_to_window_when_codex_ready(
        12345,
        42,
        "@1",
        "queued prompt",
        timeout=0.1,
    )

    assert (ok, message) == (True, "Sent")
    send_message.assert_awaited_once_with(12345, 42, "@1", "queued prompt")


@pytest.mark.asyncio
async def test_handle_non_codex_bound_window_recovers_resumable_shell(monkeypatch):
    update_message = MagicMock()
    session_manager = MagicMock()
    session_manager.get_display_name.return_value = "Repo"
    session_manager.window_states = {
        "@1": SimpleNamespace(session_id="sid-1", cwd="/tmp/repo")
    }
    kill_window = AsyncMock(return_value=True)
    recover = AsyncMock(return_value=(True, "Recovered"))
    safe_reply = AsyncMock()

    monkeypatch.setattr(bot_module, "session_manager", session_manager)
    monkeypatch.setattr(bot_module.tmux_manager, "kill_window", kill_window)
    monkeypatch.setattr(bot_module, "_recover_missing_bound_window", recover)
    monkeypatch.setattr(bot_module, "safe_reply", safe_reply)

    handled = await bot_module._handle_non_codex_bound_window(
        update_message=update_message,
        user_id=12345,
        thread_id=42,
        window_id="@1",
        pane_command="bash",
        text="pending prompt",
        success_reply="sent",
    )

    assert handled is True
    kill_window.assert_awaited_once_with("@1")
    recover.assert_awaited_once_with(
        user_id=12345,
        thread_id=42,
        old_window_id="@1",
        text="pending prompt",
    )
    safe_reply.assert_any_await(update_message, "sent")
