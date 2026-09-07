"""Cross-group topic collisions must stop before any business handler runs."""

from unittest.mock import AsyncMock

import pytest
from telegram import Update
from telegram.ext import TypeHandler

from telegram_agent_bot import bot as bot_module
from telegram_agent_bot.config import config
from telegram_agent_bot.durable_state import DurableRuntimeStore
from telegram_agent_bot.handlers import message_queue
from telegram_agent_bot.session import SessionManager


def make_update(application, kind, chat_id):
    user = {"id": 12345, "is_bot": False, "first_name": "Synthetic"}
    message = {
        "message_id": 9,
        "date": 1,
        "from": user,
        "chat": {"id": chat_id, "type": "supergroup", "title": "Synthetic"},
        "message_thread_id": 42,
        "text": "synthetic message",
    }
    body = {"update_id": 10}
    if kind == "callback":
        body["callback_query"] = {
            "id": "synthetic-callback",
            "from": user,
            "chat_instance": "synthetic",
            "message": message,
            "data": "synthetic",
        }
    else:
        body["message"] = message
    return Update.de_json(body, application.bot)


@pytest.fixture
def route_app(monkeypatch, tmp_path):
    monkeypatch.setattr(SessionManager, "_load_state", lambda self: None)
    monkeypatch.setattr(config, "state_file", tmp_path / "state.json")
    manager = SessionManager()
    store = DurableRuntimeStore(tmp_path / "runtime.sqlite3")
    store.initialize()
    manager.activate_durable_registry(store)
    manager.set_group_chat_id(12345, 42, -100001)
    manager.bind_thread(12345, 42, "@1")
    monkeypatch.setattr(bot_module, "session_manager", manager)
    monkeypatch.setattr(message_queue, "session_manager", manager)
    application = bot_module.create_bot()
    # Only exercise application dispatch: no initialize/polling/network calls.
    application._initialized = True
    health = AsyncMock()
    business = AsyncMock()
    application.handlers[-1] = [TypeHandler(Update, health)]
    application.handlers[0] = [TypeHandler(Update, business)]
    return application, manager, health, business


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["message", "callback"])
async def test_conflicting_group_stops_before_health_and_business(route_app, kind):
    application, manager, health, business = route_app
    identity = manager.get_route_delivery_identity(12345, 42, "@1")
    await application.process_update(make_update(application, kind, -100002))

    health.assert_not_awaited()
    business.assert_not_awaited()
    assert manager.resolve_chat_id(12345, 42) == -100001
    assert manager.get_route_delivery_identity(12345, 42, "@1") == identity
    task = message_queue.MessageTask(
        task_type="content",
        thread_id=42,
        window_id="@1",
        route_generation=identity.generation,
        route_session_id=identity.session_id,
    )
    assert message_queue._task_matches_current_route(12345, task)
    # Queued output remains owned by the original group after rejection.
    assert manager.resolve_chat_id(12345, task.thread_id) == -100001


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["message", "callback"])
async def test_same_group_continues_to_existing_handlers(route_app, kind):
    application, manager, health, business = route_app
    await application.process_update(make_update(application, kind, -100001))
    health.assert_awaited_once()
    business.assert_awaited_once()
    assert manager.resolve_chat_id(12345, 42) == -100001


@pytest.mark.asyncio
async def test_guard_reserves_new_route_before_later_handlers(route_app):
    application, manager, health, business = route_app
    manager.clear_group_chat_id(12345, 42)
    await application.process_update(make_update(application, "callback", -100002))
    assert manager.resolve_chat_id(12345, 42) == -100002
    health.assert_awaited_once()
    business.assert_awaited_once()
