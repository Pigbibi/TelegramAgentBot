"""Telegram update processing with ordering scoped to one forum topic.

Long-running agent startup and submit-confirmation waits must not stall every
Telegram topic.  Python Telegram Bot otherwise processes updates globally in
sequence.  This processor allows different topics to make progress in parallel
while preserving arrival order within each topic.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Hashable
from typing import Any

from telegram.ext import BaseUpdateProcessor

from .durable_state import DurableRuntimeStore


logger = logging.getLogger(__name__)


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

    def __init__(
        self,
        max_concurrent_updates: int,
        *,
        update_store: DurableRuntimeStore | None = None,
    ) -> None:
        super().__init__(max_concurrent_updates=max_concurrent_updates)
        self._update_store = update_store
        self._topic_locks: dict[Hashable, asyncio.Lock] = {}
        self._topic_users: dict[Hashable, int] = {}
        self._registry_lock = asyncio.Lock()

    async def initialize(self) -> None:
        """Release interrupted claims while retaining completed update IDs."""
        if self._update_store is not None:
            try:
                self._update_store.initialize(reset_inflight_updates=True)
            except Exception:
                logger.exception(
                    "Telegram update dedupe store unavailable at startup; "
                    "continuing without durable duplicate suppression"
                )

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
                update_id = getattr(update, "update_id", None)
                claimed_update_id: int | None = None
                if self._update_store is not None and isinstance(update_id, int):
                    try:
                        claimed = self._update_store.claim_telegram_update(update_id)
                    except Exception:
                        logger.exception(
                            "Telegram update dedupe store unavailable; processing "
                            "update %d without durable deduplication",
                            update_id,
                        )
                    else:
                        if not claimed:
                            close = getattr(coroutine, "close", None)
                            if callable(close):
                                close()
                            logger.info(
                                "Skipping duplicate Telegram update %d", update_id
                            )
                            return
                        claimed_update_id = update_id

                try:
                    await coroutine
                except BaseException:
                    if claimed_update_id is not None and self._update_store is not None:
                        try:
                            self._update_store.abandon_telegram_update(
                                claimed_update_id
                            )
                        except Exception:
                            logger.exception(
                                "Failed to release Telegram update claim %d",
                                claimed_update_id,
                            )
                    raise
                else:
                    if claimed_update_id is not None and self._update_store is not None:
                        try:
                            self._update_store.complete_telegram_update(
                                claimed_update_id
                            )
                        except Exception:
                            logger.exception(
                                "Failed to persist completed Telegram update %d",
                                claimed_update_id,
                            )
        finally:
            async with self._registry_lock:
                remaining = self._topic_users.get(key, 1) - 1
                if remaining <= 0:
                    self._topic_users.pop(key, None)
                    self._topic_locks.pop(key, None)
                else:
                    self._topic_users[key] = remaining
