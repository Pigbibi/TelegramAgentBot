import asyncio

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
