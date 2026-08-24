import logging
from types import SimpleNamespace

import pytest
from telegram import Update
from telegram.error import BadRequest, NetworkError

from telegram_agent_bot.bot import application_error_handler


@pytest.mark.asyncio
async def test_polling_network_error_is_logged_as_transient_warning(caplog):
    context = SimpleNamespace(error=NetworkError("httpx.ReadError"))

    with caplog.at_level(logging.INFO, logger="telegram_agent_bot.bot"):
        await application_error_handler(None, context)

    assert "polling will retry automatically" in caplog.text
    assert not any(record.levelno >= logging.ERROR for record in caplog.records)


@pytest.mark.asyncio
async def test_expired_callback_is_ignored_without_error_trace(caplog):
    update = Update(update_id=123)
    context = SimpleNamespace(
        error=BadRequest(
            "Query is too old and response timeout expired or query id is invalid"
        )
    )

    with caplog.at_level(logging.INFO, logger="telegram_agent_bot.bot"):
        await application_error_handler(update, context)

    assert "Ignored expired Telegram callback query" in caplog.text
    assert not any(record.levelno >= logging.ERROR for record in caplog.records)
