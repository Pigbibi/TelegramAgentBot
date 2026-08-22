import asyncio
from collections import deque
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from telegram_agent_bot import bot as bot_module
from telegram_agent_bot.durable_state import DurableRuntimeStore
from telegram_agent_bot.turn_admission import turn_admission


@pytest.fixture(autouse=True)
def clear_agent_input_queue_state(monkeypatch, tmp_path):
    store = DurableRuntimeStore(tmp_path / "runtime.sqlite3")
    store.initialize()
    monkeypatch.setattr(bot_module, "_runtime_store", store)
    bot_module._agent_input_queues.clear()
    bot_module._agent_input_tasks.clear()
    bot_module._agent_input_locks.clear()
    turn_admission.reset()
    yield
    bot_module._agent_input_queues.clear()
    bot_module._agent_input_tasks.clear()
    bot_module._agent_input_locks.clear()
    turn_admission.reset()


def test_restore_agent_input_queues_after_restart(monkeypatch):
    record = bot_module._runtime_store.enqueue_agent_input(
        user_id=12345,
        thread_id=42,
        window_id="@1",
        text="queued before restart",
        max_pending=20,
        created_at_epoch=1000.0,
    )
    assert record is not None
    ensured: list[tuple[int, int, str]] = []
    monkeypatch.setattr(
        bot_module,
        "_ensure_agent_input_drain_task",
        lambda _bot, key: ensured.append(key),
    )

    restored = bot_module._restore_agent_input_queues(MagicMock())

    assert restored == 1
    assert [
        item.text for item in bot_module._agent_input_queues[(12345, 42, "@1")]
    ] == ["queued before restart"]
    assert ensured == [(12345, 42, "@1")]


@pytest.mark.asyncio
async def test_runtime_shutdown_keeps_persisted_agent_inputs(monkeypatch):
    monkeypatch.setattr(
        bot_module,
        "_ensure_agent_input_drain_task",
        lambda _bot, _key: None,
    )
    ok, _message = await bot_module._queue_agent_input_after_interrupt(
        MagicMock(),
        12345,
        42,
        "@1",
        "survive shutdown",
    )
    assert ok is True

    await bot_module._cancel_agent_input_drain_tasks()

    assert bot_module._agent_input_queues == {}
    assert [
        item.text for item in bot_module._runtime_store.list_pending_agent_inputs()
    ] == ["survive shutdown"]


@pytest.mark.asyncio
async def test_queue_agent_input_after_interrupt_queues_and_starts_drain(monkeypatch):
    ensured: list[tuple[int, int, str]] = []
    monkeypatch.setattr(
        bot_module,
        "_ensure_agent_input_drain_task",
        lambda _bot, key: ensured.append(key),
    )

    ok, message = await bot_module._queue_agent_input_after_interrupt(
        MagicMock(),
        12345,
        42,
        "@1",
        "replacement prompt",
    )

    assert ok is True
    assert (
        message == "Interrupt requested; queued message until the agent is ready (1/20)"
    )
    assert [
        item.text for item in bot_module._agent_input_queues[(12345, 42, "@1")]
    ] == ["replacement prompt"]
    assert ensured == [(12345, 42, "@1")]


@pytest.mark.asyncio
async def test_send_or_queue_agent_input_uses_bot_side_queue_when_busy(
    monkeypatch,
):
    capture = SimpleNamespace(
        text="• Working (12s • esc to interrupt)\n\n  gpt-5.5 · ~/repo",
        missing=False,
    )
    send_message = AsyncMock(return_value=(True, "Sent"))
    ensured: list[tuple[int, int, str]] = []

    monkeypatch.setattr(
        bot_module, "capture_agent_output", AsyncMock(return_value=capture)
    )
    monkeypatch.setattr(bot_module, "_send_message_to_agent", send_message)
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
        "next prompt",
    )

    assert ok is True
    assert queued is True
    assert message == "Agent is busy; queued until ready (1/20)"
    send_message.assert_not_awaited()
    assert [
        item.text for item in bot_module._agent_input_queues[(12345, 42, "@1")]
    ] == ["next prompt"]
    assert ensured == [(12345, 42, "@1")]


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
    assert message.startswith("Interrupted agent prompt and queued")
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
async def test_send_or_queue_agent_input_queues_at_server_turn_limit(monkeypatch):
    capture = SimpleNamespace(
        text="previous output\n\n›\n\n  gpt-5.5 · ~/repo", missing=False
    )
    send_message = AsyncMock(return_value=(True, "Sent"))
    monkeypatch.setattr(
        bot_module, "capture_agent_output", AsyncMock(return_value=capture)
    )
    monkeypatch.setattr(bot_module, "_send_message_to_agent", send_message)
    monkeypatch.setattr(bot_module.config, "agent_max_active_turns", 2)
    monkeypatch.setattr(bot_module, "_ensure_agent_input_drain_task", lambda *_: None)
    turn_admission.observe("@2", active=True)
    turn_admission.observe("@3", active=True)

    ok, message, queued = await bot_module._send_or_queue_agent_input(
        MagicMock(),
        12345,
        42,
        "@1",
        "prompt",
    )

    assert (ok, queued) == (True, True)
    assert "active-task limit" in message
    send_message.assert_not_awaited()
    assert turn_admission.has_pending("@1") is True


@pytest.mark.asyncio
async def test_cancelled_send_releases_turn_reservation(monkeypatch):
    capture = SimpleNamespace(
        text="previous output\n\n›\n\n  gpt-5.5 · ~/repo", missing=False
    )
    monkeypatch.setattr(
        bot_module, "capture_agent_output", AsyncMock(return_value=capture)
    )
    monkeypatch.setattr(
        bot_module,
        "_send_message_to_agent",
        AsyncMock(side_effect=asyncio.CancelledError),
    )

    with pytest.raises(asyncio.CancelledError):
        await bot_module._send_or_queue_agent_input(
            MagicMock(),
            12345,
            42,
            "@1",
            "prompt",
        )

    assert turn_admission.snapshot().reserved_windows == frozenset()


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
    assert "not confirmed" in notify.await_args.args[3]
    assert "avoid a duplicate" in notify.await_args.args[3]
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
async def test_send_to_window_when_ready_uses_configured_startup_timeout(monkeypatch):
    capture = SimpleNamespace(
        text="starting agent",
        missing=False,
    )
    monkeypatch.setattr(
        bot_module, "capture_agent_output", AsyncMock(return_value=capture)
    )
    monkeypatch.setattr(bot_module, "_send_message_to_agent", AsyncMock())
    monkeypatch.setattr(bot_module.config, "agent_startup_timeout_seconds", 30.0)
    monkeypatch.setattr(bot_module.asyncio, "sleep", AsyncMock())
    clock = iter((0.0, 0.0, 31.0))
    monkeypatch.setattr(
        bot_module.asyncio,
        "get_event_loop",
        lambda: SimpleNamespace(time=lambda: next(clock, 31.0)),
    )

    ok, message = await bot_module._send_to_window_when_codex_ready(
        12345, 42, "@1", "queued prompt"
    )

    assert (ok, message) == (False, "Agent UI is not ready for input")


@pytest.mark.asyncio
async def test_send_to_window_when_ready_confirms_startup_directory_trust(
    monkeypatch,
):
    trust_prompt = SimpleNamespace(
        text=(
            "  Do you trust the contents of this directory?\n"
            "\n"
            "› 1. Yes, continue\n"
            "  2. No, quit\n"
            "\n"
            "  Press enter to continue\n"
        ),
        missing=False,
    )
    ready_prompt = SimpleNamespace(
        text="previous output\n\n› \n\n  gpt-5.5 · ~/repo",
        missing=False,
    )
    send_control = AsyncMock(return_value=True)
    send_message = AsyncMock(return_value=(True, "Sent"))

    monkeypatch.setattr(
        bot_module,
        "capture_agent_output",
        AsyncMock(side_effect=[trust_prompt, ready_prompt]),
    )
    monkeypatch.setattr(bot_module.tmux_manager, "send_control_key", send_control)
    monkeypatch.setattr(bot_module, "_send_message_to_agent", send_message)
    monkeypatch.setattr(bot_module.asyncio, "sleep", AsyncMock())

    ok, message = await bot_module._send_to_window_when_codex_ready(
        12345,
        42,
        "@1",
        "first prompt",
        timeout=1.0,
        auto_confirm_startup_trust=True,
    )

    assert (ok, message) == (True, "Sent")
    send_control.assert_awaited_once_with("@1", "Enter")
    send_message.assert_awaited_once_with(12345, 42, "@1", "first prompt")


@pytest.mark.asyncio
async def test_send_to_window_when_ready_reports_auth_error(monkeypatch):
    capture = SimpleNamespace(
        text=(
            "› hi\n\n"
            "■ Your access token could not be refreshed because your refresh token "
            "was revoked. Please log out and sign in again.\n\n"
            "›\n\n"
            "  gpt-5.5 · ~/repo"
        ),
        missing=False,
    )
    send_message = AsyncMock()

    monkeypatch.setattr(
        bot_module, "capture_agent_output", AsyncMock(return_value=capture)
    )
    monkeypatch.setattr(bot_module, "_send_message_to_agent", send_message)

    ok, message = await bot_module._send_to_window_when_codex_ready(
        12345,
        42,
        "@1",
        "queued prompt",
        timeout=1.0,
    )

    assert ok is False
    assert "Use /agentlogin" in message
    send_message.assert_not_awaited()


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


@pytest.mark.asyncio
async def test_recovery_detects_active_writer_without_rebinding(monkeypatch):
    old_state = SimpleNamespace(
        session_id="sid-1",
        cwd="/tmp/repo",
        window_name="Repo",
        account_name="",
    )
    session_manager = MagicMock()
    session_manager.window_states = {"@8": old_state}
    session_manager.user_window_offsets = {}
    session_manager.user_window_offset_sessions = {}
    session_manager.iter_thread_bindings.return_value = [(12345, 42, "@8")]
    create_window = AsyncMock(return_value=(True, "created", "Repo", "@9", None))
    wait_for_process = AsyncMock(
        return_value=(
            False,
            "the previous session is still active in another Codex process",
        )
    )
    kill_window = AsyncMock(return_value=True)
    remove_map = AsyncMock()

    monkeypatch.setattr(bot_module, "session_manager", session_manager)
    monkeypatch.setattr(bot_module, "_create_agent_local_window", create_window)
    monkeypatch.setattr(
        bot_module, "_wait_for_recovered_agent_process", wait_for_process
    )
    monkeypatch.setattr(bot_module.tmux_manager, "kill_window", kill_window)
    session_manager.remove_session_map_entry = remove_map

    ok, message = await bot_module._recover_missing_bound_window(
        user_id=12345,
        thread_id=42,
        old_window_id="@8",
        text="pending prompt",
    )

    assert ok is False
    assert "still active" in message
    kill_window.assert_awaited_once_with("@9")
    remove_map.assert_awaited_once_with("@9")
    session_manager.remove_window_state.assert_called_once_with("@9")
    session_manager.prepare_window_launch.assert_not_called()
    session_manager.bind_thread.assert_not_called()
    assert session_manager.window_states["@8"] is old_state


@pytest.mark.asyncio
async def test_recovery_skips_window_id_reserved_by_another_topic(monkeypatch):
    old_state = SimpleNamespace(
        session_id="sid-1",
        cwd="/tmp/repo",
        window_name="Repo",
        account_name="",
    )
    new_state = SimpleNamespace(session_id="", account_name="")
    session_manager = MagicMock()
    session_manager.window_states = {
        "@8": old_state,
        "@9": SimpleNamespace(session_id="other-session", cwd="/tmp/other"),
    }
    session_manager.user_window_offsets = {}
    session_manager.user_window_offset_sessions = {}
    session_manager.iter_thread_bindings.return_value = [
        (12345, 42, "@8"),
        (12345, 99, "@9"),
    ]
    session_manager.wait_for_session_map_entry = AsyncMock(return_value=True)
    session_manager.remove_session_map_entry = AsyncMock()
    session_manager.get_window_state.return_value = new_state
    create_window = AsyncMock(
        side_effect=[
            (True, "created", "Repo", "@9", None),
            (True, "created", "Repo", "@10", None),
        ]
    )
    kill_window = AsyncMock(return_value=True)

    monkeypatch.setattr(bot_module, "session_manager", session_manager)
    monkeypatch.setattr(bot_module, "_create_agent_local_window", create_window)
    monkeypatch.setattr(bot_module.tmux_manager, "kill_window", kill_window)
    monkeypatch.setattr(
        bot_module,
        "_wait_for_recovered_agent_process",
        AsyncMock(return_value=(True, "")),
    )
    monkeypatch.setattr(
        bot_module,
        "_recovered_agent_process_status",
        AsyncMock(return_value=(True, "")),
    )
    monkeypatch.setattr(
        bot_module,
        "_send_to_window_when_codex_ready",
        AsyncMock(return_value=(True, "Sent")),
    )
    monkeypatch.setattr(
        bot_module,
        "_refresh_session_map_after_first_prompt",
        AsyncMock(),
    )

    ok, message = await bot_module._recover_missing_bound_window(
        user_id=12345,
        thread_id=42,
        old_window_id="@8",
        text="pending prompt",
    )

    assert ok is True
    assert "Recovered window" in message
    assert create_window.await_count == 2
    kill_window.assert_awaited_once_with("@9")
    session_manager.prepare_window_launch.assert_called_once_with(
        "@10", cwd="/tmp/repo", window_name="Repo", account_name=""
    )
    session_manager.bind_thread.assert_called_once_with(
        12345, 42, "@10", window_name="Repo"
    )
    session_manager.remove_session_map_entry.assert_awaited_once_with("@8")
    session_manager.remove_window_state.assert_called_once_with("@8")


@pytest.mark.asyncio
async def test_recovery_accepts_original_window_id_after_tmux_server_restart(
    monkeypatch,
):
    old_state = SimpleNamespace(
        session_id="sid-1",
        cwd="/tmp/repo",
        window_name="Repo",
        account_name="primary",
    )
    session_manager = MagicMock()
    session_manager.window_states = {"@8": old_state}
    session_manager.user_window_offsets = {12345: {"@8": 17}}
    session_manager.user_window_offset_sessions = {12345: {"@8": "sid-1"}}
    session_manager.iter_thread_bindings.return_value = [(12345, 42, "@8")]
    session_manager.wait_for_session_map_entry = AsyncMock(return_value=True)
    session_manager.remove_session_map_entry = AsyncMock()
    session_manager.get_window_state.return_value = old_state

    monkeypatch.setattr(bot_module, "session_manager", session_manager)
    monkeypatch.setattr(
        bot_module,
        "_create_agent_local_window",
        AsyncMock(return_value=(True, "created", "Repo", "@8", None)),
    )
    monkeypatch.setattr(
        bot_module,
        "_wait_for_recovered_agent_process",
        AsyncMock(return_value=(True, "")),
    )
    monkeypatch.setattr(
        bot_module,
        "_recovered_agent_process_status",
        AsyncMock(return_value=(True, "")),
    )
    monkeypatch.setattr(
        bot_module,
        "_send_to_window_when_codex_ready",
        AsyncMock(return_value=(True, "Sent")),
    )
    monkeypatch.setattr(
        bot_module,
        "_refresh_session_map_after_first_prompt",
        AsyncMock(),
    )

    ok, message = await bot_module._recover_missing_bound_window(
        user_id=12345,
        thread_id=42,
        old_window_id="@8",
        text="pending prompt",
    )

    assert ok is True
    assert "Recovered window" in message
    session_manager.bind_thread.assert_called_once_with(
        12345, 42, "@8", window_name="Repo"
    )
    session_manager.remove_session_map_entry.assert_not_awaited()
    session_manager.remove_window_state.assert_not_called()
    assert session_manager.user_window_offsets == {12345: {"@8": 17}}
    assert session_manager.user_window_offset_sessions == {12345: {"@8": "sid-1"}}


@pytest.mark.asyncio
async def test_recovery_failure_after_original_id_reuse_keeps_saved_state(monkeypatch):
    old_state = SimpleNamespace(
        session_id="sid-1",
        cwd="/tmp/repo",
        window_name="Repo",
        account_name="",
    )
    session_manager = MagicMock()
    session_manager.window_states = {"@8": old_state}
    session_manager.user_window_offsets = {}
    session_manager.user_window_offset_sessions = {}
    session_manager.iter_thread_bindings.return_value = [(12345, 42, "@8")]
    session_manager.remove_session_map_entry = AsyncMock()
    kill_window = AsyncMock(return_value=True)

    monkeypatch.setattr(bot_module, "session_manager", session_manager)
    monkeypatch.setattr(
        bot_module,
        "_create_agent_local_window",
        AsyncMock(return_value=(True, "created", "Repo", "@8", None)),
    )
    monkeypatch.setattr(
        bot_module,
        "_wait_for_recovered_agent_process",
        AsyncMock(return_value=(False, "resume failed")),
    )
    monkeypatch.setattr(bot_module.tmux_manager, "kill_window", kill_window)

    ok, message = await bot_module._recover_missing_bound_window(
        user_id=12345,
        thread_id=42,
        old_window_id="@8",
        text="pending prompt",
    )

    assert ok is False
    assert message == "resume failed"
    kill_window.assert_awaited_once_with("@8")
    assert session_manager.window_states["@8"] is old_state
    session_manager.remove_session_map_entry.assert_not_awaited()
    session_manager.remove_window_state.assert_not_called()
    session_manager._save_state.assert_called_once()


@pytest.mark.asyncio
async def test_recovery_commits_binding_only_after_validation(monkeypatch):
    old_state = SimpleNamespace(
        session_id="sid-1",
        cwd="/tmp/repo",
        window_name="Repo",
        account_name="",
    )
    new_state = SimpleNamespace(session_id="", account_name="")
    session_manager = MagicMock()
    session_manager.window_states = {"@8": old_state}
    session_manager.user_window_offsets = {}
    session_manager.user_window_offset_sessions = {}
    session_manager.iter_thread_bindings.return_value = [(12345, 42, "@8")]
    session_manager.wait_for_session_map_entry = AsyncMock(return_value=True)
    session_manager.remove_session_map_entry = AsyncMock()
    session_manager.get_window_state.return_value = new_state

    monkeypatch.setattr(bot_module, "session_manager", session_manager)
    monkeypatch.setattr(
        bot_module,
        "_create_agent_local_window",
        AsyncMock(return_value=(True, "created", "Repo", "@9", None)),
    )
    monkeypatch.setattr(
        bot_module,
        "_wait_for_recovered_agent_process",
        AsyncMock(return_value=(True, "")),
    )
    monkeypatch.setattr(
        bot_module,
        "_recovered_agent_process_status",
        AsyncMock(return_value=(True, "")),
    )
    monkeypatch.setattr(
        bot_module,
        "_send_to_window_when_codex_ready",
        AsyncMock(return_value=(True, "Sent")),
    )
    refresh_map = AsyncMock()
    monkeypatch.setattr(
        bot_module,
        "_refresh_session_map_after_first_prompt",
        refresh_map,
    )

    ok, message = await bot_module._recover_missing_bound_window(
        user_id=12345,
        thread_id=42,
        old_window_id="@8",
        text="pending prompt",
    )

    assert ok is True
    assert "Recovered window" in message
    session_manager.wait_for_session_map_entry.assert_awaited_once_with(
        "@9", timeout=15.0, expected_session_id="sid-1", apply=False
    )
    session_manager.prepare_window_launch.assert_called_once_with(
        "@9", cwd="/tmp/repo", window_name="Repo", account_name=""
    )
    session_manager.register_session_to_window.assert_called_once_with(
        "@9",
        "sid-1",
        "/tmp/repo",
        window_name="Repo",
        persist_session_map=True,
    )
    session_manager.bind_thread.assert_called_once_with(
        12345, 42, "@9", window_name="Repo"
    )
    session_manager.remove_session_map_entry.assert_awaited_once_with("@8")
    session_manager.remove_window_state.assert_called_once_with("@8")
    refresh_map.assert_awaited_once_with(
        "@9", text="pending prompt", confirm_existing_session=True
    )


@pytest.mark.asyncio
async def test_recovered_agent_status_reports_active_writer(monkeypatch):
    window = SimpleNamespace(window_id="@9", pane_current_command="bash")
    monkeypatch.setattr(
        bot_module.tmux_manager,
        "find_window_by_id",
        AsyncMock(return_value=window),
    )
    monkeypatch.setattr(
        bot_module.tmux_manager,
        "capture_pane",
        AsyncMock(return_value="thread sid-1 already has an active writer"),
    )

    ok, message = await bot_module._recovered_agent_process_status("@9")

    assert ok is False
    assert "still active in another Codex process" in message


@pytest.mark.asyncio
async def test_handle_auth_error_bound_window_recovers_resumable_session(monkeypatch):
    update_message = MagicMock()
    session_manager = MagicMock()
    session_manager.get_display_name.return_value = "Repo"
    session_manager.window_states = {
        "@1": SimpleNamespace(session_id="sid-1", cwd="/tmp/repo")
    }
    kill_window = AsyncMock(return_value=True)
    recover = AsyncMock(return_value=(True, "Recovered"))
    safe_reply = AsyncMock()
    pane_text = (
        "› hi\n\n"
        "■ Your access token could not be refreshed because you have since "
        "logged out or signed in to another account. Please sign in again.\n\n"
        "›\n"
    )

    monkeypatch.setattr(bot_module, "session_manager", session_manager)
    monkeypatch.setattr(bot_module.tmux_manager, "kill_window", kill_window)
    monkeypatch.setattr(bot_module, "_recover_missing_bound_window", recover)
    monkeypatch.setattr(bot_module, "safe_reply", safe_reply)

    handled = await bot_module._handle_auth_error_bound_window(
        update_message=update_message,
        user_id=12345,
        thread_id=42,
        window_id="@1",
        pane_text=pane_text,
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
