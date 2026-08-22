from unittest.mock import AsyncMock

import pytest
from telegram.error import BadRequest

from telegram_agent_bot.handlers.delivery_errors import PermanentDeliveryError
from telegram_agent_bot.handlers.message_sender import send_with_fallback


@pytest.mark.asyncio
async def test_message_thread_not_found_is_exposed_as_permanent_failure():
    bot = AsyncMock()
    bot.send_message.side_effect = BadRequest("Message thread not found")

    with pytest.raises(PermanentDeliveryError) as raised:
        await send_with_fallback(
            bot,
            -100123,
            "answer",
            message_thread_id=42,
        )

    assert raised.value.chat_id == -100123
    assert raised.value.thread_id == 42
    bot.send_message.assert_awaited_once()
