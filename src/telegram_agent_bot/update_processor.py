"""Telegram update processing with ordering scoped to one forum topic.

Long-running agent startup and submit-confirmation waits must not stall every
Telegram topic.  Python Telegram Bot otherwise processes updates globally in
sequence.  This processor allows different topics to make progress in parallel
while preserving arrival order within each topic.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Hashable
from typing import Any

from telegram.ext import BaseUpdateProcessor


def update_topic_key(update: object) -> Hashable:
    """Return a stable serialization key for a Telegram update."""
    chat = getattr(update, "effective_chat", None)
    chat_id = getattr(chat, "id", None)
    message = getattr(update, "effective_message", None)
    thread_id = getattr(message, "message_thread_id", None) or 0

    if chat_id is not None:
        return ("chat", int(chat_id), int(thread_id))

    user = getattr(update, "effective_user", None)
    user_id = getattr(user, "id", None)
    if user_id is not None:
        return ("user", int(user_id))

    update_id = getattr(update, "update_id", None)
    return ("update", update_id if update_id is not None else id(update))


class TopicUpdateProcessor(BaseUpdateProcessor):
    """Run different topics concurrently and one topic sequentially."""

    def __init__(self, max_concurrent_updates: int) -> None:
        super().__init__(max_concurrent_updates=max_concurrent_updates)
        self._topic_locks: dict[Hashable, asyncio.Lock] = {}
        self._topic_users: dict[Hashable, int] = {}
        self._registry_lock = asyncio.Lock()

    async def initialize(self) -> None:
        """No external resources are required."""

    async def shutdown(self) -> None:
        """Release idle lock bookkeeping after update processing stops."""
        async with self._registry_lock:
            self._topic_locks.clear()
            self._topic_users.clear()

    async def do_process_update(
        self,
        update: object,
        coroutine: Awaitable[Any],
    ) -> None:
        key = update_topic_key(update)
        async with self._registry_lock:
            topic_lock = self._topic_locks.setdefault(key, asyncio.Lock())
            self._topic_users[key] = self._topic_users.get(key, 0) + 1

        try:
            async with topic_lock:
                await coroutine
        finally:
            async with self._registry_lock:
                remaining = self._topic_users.get(key, 1) - 1
                if remaining <= 0:
                    self._topic_users.pop(key, None)
                    self._topic_locks.pop(key, None)
                else:
                    self._topic_users[key] = remaining
