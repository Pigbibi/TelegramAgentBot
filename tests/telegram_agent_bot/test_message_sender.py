"""Tests for Telegram message send retry behavior."""

from unittest.mock import AsyncMock

import pytest
from telegram.error import NetworkError

from telegram_agent_bot.handlers import message_sender


@pytest.mark.asyncio
async def test_safe_reply_retries_connect_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reply = AsyncMock(
        side_effect=[
            NetworkError("httpx.ConnectError: All connection attempts failed"),
            "ok",
        ]
    )
    message = AsyncMock()
    message.reply_text = reply

    sleep_mock = AsyncMock()
    monkeypatch.setattr(message_sender.asyncio, "sleep", sleep_mock)

    result = await message_sender.safe_reply(message, "hello")

    assert result == "ok"
    assert reply.await_count == 2
    sleep_mock.assert_awaited_once_with(0.5)


@pytest.mark.asyncio
async def test_safe_reply_does_not_retry_non_connect_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reply = AsyncMock(
        side_effect=[
            NetworkError("httpx.ReadTimeout"),
            "plain-ok",
        ]
    )
    message = AsyncMock()
    message.reply_text = reply

    sleep_mock = AsyncMock()
    monkeypatch.setattr(message_sender.asyncio, "sleep", sleep_mock)

    result = await message_sender.safe_reply(message, "hello")

    assert result == "plain-ok"
    assert reply.await_count == 2
    sleep_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_send_with_fallback_does_not_resend_after_read_timeout() -> None:
    bot = AsyncMock()
    bot.send_message.side_effect = NetworkError("httpx.ReadTimeout")

    with pytest.raises(NetworkError, match="ReadTimeout"):
        await message_sender.send_with_fallback(
            bot,
            -100123,
            "Thinking",
            at_most_once=True,
        )

    bot.send_message.assert_awaited_once()
