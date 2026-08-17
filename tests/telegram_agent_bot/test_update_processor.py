"""Tests for topic-scoped Telegram update concurrency."""

import asyncio
from types import SimpleNamespace

import pytest

from telegram_agent_bot.update_processor import (
    TopicUpdateProcessor,
    update_topic_key,
)


def _update(update_id: int, *, chat_id: int, thread_id: int):
    return SimpleNamespace(
        update_id=update_id,
        effective_chat=SimpleNamespace(id=chat_id),
        effective_message=SimpleNamespace(message_thread_id=thread_id),
        effective_user=SimpleNamespace(id=12345),
    )


def test_update_topic_key_separates_forum_topics() -> None:
    assert update_topic_key(_update(1, chat_id=-100, thread_id=10)) != update_topic_key(
        _update(2, chat_id=-100, thread_id=20)
    )


@pytest.mark.asyncio
async def test_different_topics_process_concurrently() -> None:
    processor = TopicUpdateProcessor(max_concurrent_updates=4)
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    second_started = asyncio.Event()

    async def first() -> None:
        first_started.set()
        await release_first.wait()

    async def second() -> None:
        second_started.set()

    first_task = asyncio.create_task(
        processor.process_update(_update(1, chat_id=-100, thread_id=10), first())
    )
    await first_started.wait()
    second_task = asyncio.create_task(
        processor.process_update(_update(2, chat_id=-100, thread_id=20), second())
    )

    await asyncio.wait_for(second_started.wait(), timeout=1.0)
    release_first.set()
    await asyncio.gather(first_task, second_task)


@pytest.mark.asyncio
async def test_same_topic_preserves_update_order() -> None:
    processor = TopicUpdateProcessor(max_concurrent_updates=4)
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    second_started = asyncio.Event()
    order: list[str] = []

    async def first() -> None:
        order.append("first-start")
        first_started.set()
        await release_first.wait()
        order.append("first-end")

    async def second() -> None:
        order.append("second")
        second_started.set()

    first_task = asyncio.create_task(
        processor.process_update(_update(1, chat_id=-100, thread_id=10), first())
    )
    await first_started.wait()
    second_task = asyncio.create_task(
        processor.process_update(_update(2, chat_id=-100, thread_id=10), second())
    )

    await asyncio.sleep(0)
    assert not second_started.is_set()
    release_first.set()
    await asyncio.gather(first_task, second_task)

    assert order == ["first-start", "first-end", "second"]
    assert processor._topic_locks == {}
