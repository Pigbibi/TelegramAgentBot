import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram.error import BadRequest

from telegram_codex_bot.agent_io import CaptureResult
from telegram_codex_bot.backends.base import AgentTarget
from telegram_codex_bot.handlers import message_queue


def test_status_message_info_persists_for_restart(monkeypatch, tmp_path):
    """Tracked Working/Thinking bubbles survive process restarts."""
    status_file = tmp_path / "status_messages.json"
    monkeypatch.setattr(message_queue, "_STATUS_MESSAGES_FILE", status_file)
    message_queue._status_msg_info.clear()

    try:
        message_queue._set_status_info(
            (1, 42),
            (321, "@4", "💭 Thinking (0s) · esc to interrupt", 123.0),
        )

        assert json.loads(status_file.read_text()) == {
            "1:42": {
                "message_id": 321,
                "window_id": "@4",
                "text": "💭 Thinking (0s) · esc to interrupt",
                "created_at": 123.0,
            }
        }
        assert message_queue._load_status_msg_info(status_file) == {
            (1, 42): (321, "@4", "💭 Thinking (0s) · esc to interrupt", 123.0)
        }

        message_queue._pop_status_info((1, 42))
        assert json.loads(status_file.read_text()) == {}
    finally:
        message_queue._status_msg_info.clear()


@pytest.mark.asyncio
async def test_different_threads_process_concurrently(monkeypatch):
    await message_queue.shutdown_workers()
    started: list[int | None] = []
    completed: list[int | None] = []
    release_first = asyncio.Event()

    async def fake_process_content(bot, user_id, task):
        started.append(task.thread_id)
        if task.thread_id == 101:
            await release_first.wait()
        completed.append(task.thread_id)

    monkeypatch.setattr(message_queue, "_process_content_task", fake_process_content)

    try:
        await message_queue.enqueue_content_message(
            bot=object(),
            user_id=1,
            window_id="@1",
            parts=["first"],
            thread_id=101,
        )

        for _ in range(20):
            if 101 in started:
                break
            await asyncio.sleep(0.01)
        assert 101 in started

        await message_queue.enqueue_content_message(
            bot=object(),
            user_id=1,
            window_id="@2",
            parts=["second"],
            thread_id=202,
        )

        for _ in range(20):
            if 202 in completed:
                break
            await asyncio.sleep(0.01)

        assert 202 in completed
        assert 101 not in completed
    finally:
        release_first.set()
        queues = [q for q in message_queue._message_queues.values()]
        if queues:
            await asyncio.gather(*(q.join() for q in queues))
        await message_queue.shutdown_workers()


@pytest.mark.asyncio
async def test_enqueue_can_wait_until_content_is_processed(monkeypatch):
    await message_queue.shutdown_workers()
    completed: list[int | None] = []

    async def fake_process_content(bot, user_id, task):
        await asyncio.sleep(0.01)
        completed.append(task.thread_id)

    monkeypatch.setattr(message_queue, "_process_content_task", fake_process_content)

    await message_queue.enqueue_content_message(
        bot=object(),
        user_id=1,
        window_id="@1",
        parts=["final"],
        thread_id=303,
        wait_until_sent=True,
    )

    assert completed == [303]
    await message_queue.shutdown_workers()


@pytest.mark.asyncio
async def test_shutdown_drains_pending_content_before_cancelling(monkeypatch):
    await message_queue.shutdown_workers()
    completed: list[int | None] = []

    async def fake_process_content(bot, user_id, task):
        await asyncio.sleep(0.01)
        completed.append(task.thread_id)

    monkeypatch.setattr(message_queue, "_process_content_task", fake_process_content)

    await message_queue.enqueue_content_message(
        bot=object(),
        user_id=1,
        window_id="@1",
        parts=["final"],
        thread_id=404,
    )

    await message_queue.shutdown_workers()
    assert completed == [404]


@pytest.mark.asyncio
async def test_elapsed_working_status_edits_existing_status_message(monkeypatch):
    """Changing Working elapsed time should edit the Telegram status in place."""
    message_queue._status_msg_info.clear()

    bot = AsyncMock()
    monkeypatch.setattr(
        message_queue.session_manager,
        "resolve_chat_id",
        lambda user_id, thread_id=None: -100123,
    )

    async def fake_send_with_fallback(*args, **kwargs):
        return SimpleNamespace(message_id=321)

    monkeypatch.setattr(message_queue, "send_with_fallback", fake_send_with_fallback)

    await message_queue._process_status_update_task(
        bot,
        1,
        message_queue.MessageTask(
            task_type="status_update",
            text="• Working (1m 00s • esc to interrupt)",
            window_id="@4",
            thread_id=42,
        ),
    )
    await message_queue._process_status_update_task(
        bot,
        1,
        message_queue.MessageTask(
            task_type="status_update",
            text="• Working (1m 05s • esc to interrupt)",
            window_id="@4",
            thread_id=42,
        ),
    )

    bot.edit_message_text.assert_awaited_once()
    edit_kwargs = bot.edit_message_text.await_args.kwargs
    assert edit_kwargs["chat_id"] == -100123
    assert edit_kwargs["message_id"] == 321
    assert "1m 05s" in edit_kwargs["text"]
    assert message_queue._status_msg_info[(1, 42)][2] == (
        "• Working (1m 05s • esc to interrupt)"
    )

    message_queue._status_msg_info.clear()


@pytest.mark.asyncio
async def test_status_message_not_modified_keeps_existing_status(monkeypatch):
    """Telegram no-op edit errors should not create a duplicate status message."""
    message_queue._status_msg_info.clear()

    bot = AsyncMock()
    bot.edit_message_text.side_effect = BadRequest(
        "Message is not modified: specified new message content and reply markup "
        "are exactly the same as a current content and reply markup of the message"
    )
    monkeypatch.setattr(
        message_queue.session_manager,
        "resolve_chat_id",
        lambda user_id, thread_id=None: -100123,
    )

    send_status = AsyncMock()
    monkeypatch.setattr(message_queue, "_do_send_status_message", send_status)

    message_queue._status_msg_info[(1, 42)] = (
        321,
        "@4",
        "💭 Thinking (6m 06s) · esc to interrupt",
        123.0,
    )

    try:
        await message_queue._process_status_update_task(
            bot,
            1,
            message_queue.MessageTask(
                task_type="status_update",
                text="💭 Thinking (6m 07s) · esc to interrupt",
                window_id="@4",
                thread_id=42,
            ),
        )

        assert message_queue._status_msg_info[(1, 42)] == (
            321,
            "@4",
            "💭 Thinking (6m 07s) · esc to interrupt",
            123.0,
        )
        send_status.assert_not_awaited()
    finally:
        message_queue._status_msg_info.clear()


@pytest.mark.asyncio
async def test_failed_status_to_content_conversion_deletes_old_status(monkeypatch):
    """A failed status conversion must not leave a stale status bubble."""
    message_queue._status_msg_info.clear()

    bot = AsyncMock()
    bot.edit_message_text.side_effect = [
        BadRequest("Can't parse entities"),
        BadRequest("Message text is empty"),
    ]
    monkeypatch.setattr(
        message_queue.session_manager,
        "resolve_chat_id",
        lambda user_id, thread_id=None: -100123,
    )

    message_queue._status_msg_info[(1, 42)] = (
        321,
        "@4",
        "Status text",
        123.0,
    )

    try:
        converted = await message_queue._convert_status_to_content(
            bot,
            1,
            42,
            "@4",
            "final content",
        )

        assert converted is None
        bot.delete_message.assert_awaited_once_with(chat_id=-100123, message_id=321)
        assert (1, 42) not in message_queue._status_msg_info
    finally:
        message_queue._status_msg_info.clear()


@pytest.mark.asyncio
async def test_working_status_is_not_converted_to_content(monkeypatch):
    """Quick replies should be sent below Thinking instead of replacing it."""
    message_queue._status_msg_info.clear()
    bot = AsyncMock()
    sent_texts: list[str] = []
    monkeypatch.setattr(
        message_queue.session_manager,
        "resolve_chat_id",
        lambda user_id, thread_id=None: -100123,
    )
    monkeypatch.setattr(
        message_queue,
        "_check_and_send_status",
        AsyncMock(),
    )

    async def fake_send_with_fallback(bot, chat_id, text, **kwargs):
        sent_texts.append(text)
        return SimpleNamespace(message_id=654)

    monkeypatch.setattr(message_queue, "send_with_fallback", fake_send_with_fallback)
    message_queue._status_msg_info[(1, 42)] = (
        321,
        "@4",
        "💭 Thinking (0s) · esc to interrupt",
        0.0,
    )

    try:
        await message_queue._process_content_task(
            bot,
            1,
            message_queue.MessageTask(
                task_type="content",
                window_id="@4",
                parts=["final content"],
                thread_id=42,
            ),
        )

        assert sent_texts == ["final content"]
        bot.edit_message_text.assert_not_awaited()
        bot.delete_message.assert_awaited_once_with(chat_id=-100123, message_id=321)
        assert (1, 42) not in message_queue._status_msg_info
    finally:
        message_queue._status_msg_info.clear()


@pytest.mark.asyncio
async def test_working_status_clear_waits_for_minimum_visible_time(monkeypatch):
    """Very fast replies should leave Thinking visible briefly before deletion."""
    message_queue._status_msg_info.clear()
    bot = AsyncMock()
    sleep = AsyncMock()
    monkeypatch.setattr(message_queue.time, "monotonic", lambda: 100.5)
    monkeypatch.setattr(message_queue.asyncio, "sleep", sleep)
    monkeypatch.setattr(
        message_queue.session_manager,
        "resolve_chat_id",
        lambda user_id, thread_id=None: -100123,
    )
    message_queue._status_msg_info[(1, 42)] = (
        321,
        "@4",
        "💭 Thinking (0s) · esc to interrupt",
        100.0,
    )

    try:
        await message_queue._clear_working_status_after_min_visible(
            bot,
            1,
            42,
            "@4",
        )

        sleep.assert_awaited_once_with(pytest.approx(1.0))
        bot.delete_message.assert_awaited_once_with(chat_id=-100123, message_id=321)
    finally:
        message_queue._status_msg_info.clear()


@pytest.mark.asyncio
async def test_cancelled_status_to_content_conversion_keeps_tracking(monkeypatch):
    """Shutdown cancellation must not orphan a still-visible status bubble."""
    message_queue._status_msg_info.clear()

    bot = AsyncMock()
    bot.edit_message_text.side_effect = asyncio.CancelledError()
    monkeypatch.setattr(
        message_queue.session_manager,
        "resolve_chat_id",
        lambda user_id, thread_id=None: -100123,
    )

    message_queue._status_msg_info[(1, 42)] = (
        321,
        "@4",
        "Status text",
        123.0,
    )

    try:
        with pytest.raises(asyncio.CancelledError):
            await message_queue._convert_status_to_content(
                bot,
                1,
                42,
                "@4",
                "final content",
            )

        assert message_queue._status_msg_info[(1, 42)] == (
            321,
            "@4",
            "Status text",
            123.0,
        )
    finally:
        message_queue._status_msg_info.clear()


@pytest.mark.asyncio
async def test_cancelled_status_clear_keeps_tracking(monkeypatch):
    """A cancelled clear can be retried after restart if tracking is preserved."""
    message_queue._status_msg_info.clear()

    bot = AsyncMock()
    bot.delete_message.side_effect = asyncio.CancelledError()
    monkeypatch.setattr(
        message_queue.session_manager,
        "resolve_chat_id",
        lambda user_id, thread_id=None: -100123,
    )

    message_queue._status_msg_info[(1, 42)] = (
        321,
        "@4",
        "💭 Thinking (4m 28s) · esc to interrupt",
        123.0,
    )

    try:
        with pytest.raises(asyncio.CancelledError):
            await message_queue._do_clear_status_message(
                bot,
                1,
                42,
            )

        assert message_queue._status_msg_info[(1, 42)] == (
            321,
            "@4",
            "💭 Thinking (4m 28s) · esc to interrupt",
            123.0,
        )
    finally:
        message_queue._status_msg_info.clear()


@pytest.mark.asyncio
async def test_status_update_preserves_pending_clear():
    """A fresh Working bubble must not reuse an old bubble above the user."""
    queue: asyncio.Queue[message_queue.MessageTask] = asyncio.Queue()
    lock = asyncio.Lock()

    queue.put_nowait(
        message_queue.MessageTask(
            task_type="status_clear",
            thread_id=42,
        )
    )

    dropped = await message_queue._enqueue_coalesced_status_task(
        queue,
        message_queue.MessageTask(
            task_type="status_update",
            text="💭 Thinking (0s) · esc to interrupt",
            window_id="@1",
            thread_id=42,
        ),
        lock,
    )

    items = []
    while not queue.empty():
        items.append(queue.get_nowait())
        queue.task_done()

    assert dropped == 0
    assert [item.task_type for item in items] == ["status_clear", "status_update"]


@pytest.mark.asyncio
async def test_status_updates_are_coalesced_behind_content(monkeypatch):
    """Queued Working timer edits should not bury real Codex output."""
    await message_queue.shutdown_workers()
    message_queue._status_msg_info.clear()
    message_queue._flood_until.clear()

    processed: list[tuple[str, str | None]] = []
    first_status_started = asyncio.Event()
    release_first_status = asyncio.Event()
    status_calls = 0

    async def fake_process_status(bot, user_id, task):
        nonlocal status_calls
        status_calls += 1
        processed.append((task.task_type, task.text))
        if status_calls == 1:
            first_status_started.set()
            await release_first_status.wait()

    async def fake_process_content(bot, user_id, task):
        processed.append((task.task_type, task.parts[0]))

    monkeypatch.setattr(
        message_queue, "_process_status_update_task", fake_process_status
    )
    monkeypatch.setattr(message_queue, "_process_content_task", fake_process_content)

    try:
        await message_queue.enqueue_status_update(
            bot=object(),
            user_id=1,
            window_id="@1",
            status_text="Working 0",
            thread_id=505,
        )
        await asyncio.wait_for(first_status_started.wait(), timeout=1)

        for i in range(1, 20):
            await message_queue.enqueue_status_update(
                bot=object(),
                user_id=1,
                window_id="@1",
                status_text=f"Working {i}",
                thread_id=505,
            )

        await message_queue.enqueue_content_message(
            bot=object(),
            user_id=1,
            window_id="@1",
            parts=["final answer"],
            thread_id=505,
        )
        await message_queue.enqueue_status_update(
            bot=object(),
            user_id=1,
            window_id="@1",
            status_text="Working final",
            thread_id=505,
        )

        release_first_status.set()
        queue = message_queue.get_message_queue(1, 505)
        assert queue is not None
        await asyncio.wait_for(queue.join(), timeout=1)

        assert processed == [
            ("status_update", "Working 0"),
            ("content", "final answer"),
            ("status_update", "Working final"),
        ]
    finally:
        release_first_status.set()
        await message_queue.shutdown_workers()
        message_queue._status_msg_info.clear()
        message_queue._flood_until.clear()


@pytest.mark.asyncio
async def test_check_status_uses_synthetic_timer_after_content(monkeypatch):
    """Content delivery should immediately restore the bottom elapsed timer."""
    from telegram_codex_bot.handlers import working_status

    await message_queue.shutdown_workers()
    message_queue._status_msg_info.clear()
    working_status._synthetic_working_starts.clear()
    working_status._synthetic_working_starts[(1, 42, "@9")] = 100.0
    monkeypatch.setattr(working_status.time, "monotonic", lambda: 108.0)
    monkeypatch.setattr(
        message_queue.session_manager,
        "resolve_chat_id",
        lambda user_id, thread_id=None: -100123,
    )
    monkeypatch.setattr(
        message_queue,
        "capture_agent_output",
        AsyncMock(
            return_value=CaptureResult(
                target=AgentTarget("local", "local", window_id="@9"),
                text="output\nstill running without prompt chrome\n",
            )
        ),
    )

    sent_texts: list[str] = []

    async def fake_send_with_fallback(bot, chat_id, text, **kwargs):
        sent_texts.append(text)
        return SimpleNamespace(message_id=654)

    monkeypatch.setattr(message_queue, "send_with_fallback", fake_send_with_fallback)

    try:
        await message_queue._check_and_send_status(
            bot=AsyncMock(),
            user_id=1,
            window_id="@9",
            thread_id=42,
        )
    finally:
        message_queue._status_msg_info.clear()
        working_status._synthetic_working_starts.clear()

    assert sent_texts == ["💭 Thinking (8s) · esc to interrupt"]
