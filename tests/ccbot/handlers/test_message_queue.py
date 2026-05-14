import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ccbot.handlers import message_queue


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
    from ccbot.handlers import working_status

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
        message_queue.tmux_manager,
        "find_window_by_id",
        AsyncMock(return_value=SimpleNamespace(window_id="@9")),
    )
    monkeypatch.setattr(
        message_queue.tmux_manager,
        "capture_pane",
        AsyncMock(return_value="output\nstill running without prompt chrome\n"),
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
