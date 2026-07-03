"""Lifecycle tests for Telegram bot runtime cleanup."""

from unittest.mock import MagicMock

import pytest
from telegram.ext import CommandHandler

from telegram_agent_bot import bot as bot_module


@pytest.mark.asyncio
async def test_post_stop_stops_producers_before_draining_message_workers(monkeypatch):
    calls: list[tuple[str, bool | None]] = []

    class DummyBackend:
        async def stop(self) -> None:
            calls.append(("backend", None))

    async def fake_shutdown_workers(*, drain: bool = True) -> None:
        calls.append(("workers", drain))

    async def fake_close_transcribe_client() -> None:
        calls.append(("transcribe", None))

    monkeypatch.setattr(bot_module, "_runtime_stopped", False)
    monkeypatch.setattr(bot_module, "_status_poll_task", None)
    monkeypatch.setattr(bot_module, "_auto_update_task", None)
    monkeypatch.setattr(bot_module, "agent_backend", DummyBackend())
    monkeypatch.setattr(bot_module, "session_monitor", object())
    monkeypatch.setattr(bot_module, "shutdown_workers", fake_shutdown_workers)
    monkeypatch.setattr(
        bot_module,
        "close_transcribe_client",
        fake_close_transcribe_client,
    )

    await bot_module.post_stop(MagicMock())

    assert calls == [
        ("backend", None),
        ("workers", True),
        ("transcribe", None),
    ]
    assert bot_module.agent_backend is None
    assert bot_module.session_monitor is None


@pytest.mark.asyncio
async def test_post_shutdown_fallback_cancels_workers_without_draining(monkeypatch):
    calls: list[tuple[str, bool | None]] = []

    async def fake_shutdown_workers(*, drain: bool = True) -> None:
        calls.append(("workers", drain))

    async def fake_close_transcribe_client() -> None:
        calls.append(("transcribe", None))

    monkeypatch.setattr(bot_module, "_runtime_stopped", False)
    monkeypatch.setattr(bot_module, "_status_poll_task", None)
    monkeypatch.setattr(bot_module, "_auto_update_task", None)
    monkeypatch.setattr(bot_module, "agent_backend", None)
    monkeypatch.setattr(bot_module, "session_monitor", None)
    monkeypatch.setattr(bot_module, "shutdown_workers", fake_shutdown_workers)
    monkeypatch.setattr(
        bot_module,
        "close_transcribe_client",
        fake_close_transcribe_client,
    )

    await bot_module.post_shutdown(MagicMock())

    assert calls == [
        ("workers", False),
        ("transcribe", None),
    ]


def test_create_bot_registers_post_stop_before_shutdown():
    application = bot_module.create_bot()

    assert application.post_stop is bot_module.post_stop
    assert application.post_shutdown is bot_module.post_shutdown


def test_create_bot_registers_kill_command_before_forwarder():
    application = bot_module.create_bot()

    group_handlers = application.handlers[0]
    kill_index = next(
        index
        for index, handler in enumerate(group_handlers)
        if isinstance(handler, CommandHandler) and "kill" in handler.commands
    )
    forward_index = next(
        index
        for index, handler in enumerate(group_handlers)
        if handler.callback is bot_module.forward_command_handler
    )

    assert group_handlers[kill_index].callback is bot_module.topic_closed_handler
    assert kill_index < forward_index
