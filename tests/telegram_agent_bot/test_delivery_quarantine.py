from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from telegram_agent_bot import bot as bot_module


@pytest.mark.asyncio
async def test_inbound_topic_message_clears_delivery_quarantine(monkeypatch):
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=12345),
        effective_chat=SimpleNamespace(id=-100123, type="supergroup"),
        effective_message=SimpleNamespace(
            message_thread_id=42,
            forum_topic_closed=None,
        ),
    )
    update.message = update.effective_message
    set_group_chat_id = MagicMock()
    clear_failure = MagicMock(return_value=True)
    monitor = SimpleNamespace(clear_permanent_delivery_block=MagicMock())
    monkeypatch.setattr(
        bot_module.session_manager,
        "set_group_chat_id",
        set_group_chat_id,
    )
    monkeypatch.setattr(
        bot_module,
        "clear_permanent_delivery_failure",
        clear_failure,
    )
    monkeypatch.setattr(bot_module, "session_monitor", monitor)

    await bot_module.inbound_route_health_handler(update, MagicMock())

    set_group_chat_id.assert_called_once_with(12345, 42, -100123)
    clear_failure.assert_called_once_with(12345, 42, chat_id=-100123)
    monitor.clear_permanent_delivery_block.assert_called_once_with(12345, 42)
