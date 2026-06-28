import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from telegram_agent_bot import bot as bot_module
from telegram_agent_bot.handlers.callback_data import (
    CB_CODEX_UPDATE_APPLY,
    CB_CODEX_UPDATE_DISMISS,
)
from telegram_agent_bot.updater import CodexUpdateResult


def _make_callback_update(data: str) -> MagicMock:
    update = MagicMock()
    update.effective_user = MagicMock()
    update.effective_user.id = 12345
    update.effective_chat = MagicMock()
    update.effective_chat.type = "private"
    update.callback_query = MagicMock()
    update.callback_query.data = data
    update.callback_query.answer = AsyncMock()
    return update


@pytest.mark.asyncio
async def test_notify_codex_update_available_sends_private_prompts(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("TELEGRAM_AGENT_BOT_DIR", str(tmp_path))
    monkeypatch.setattr(bot_module.config, "allowed_users", {222, 111})
    monkeypatch.setattr(bot_module, "_codex_update_prompted_versions", set())
    safe_send = AsyncMock()
    monkeypatch.setattr(bot_module, "safe_send", safe_send)

    result = CodexUpdateResult(
        checked=True,
        supported=True,
        update_available=True,
        current_version="0.125.0",
        latest_version="0.126.0",
    )
    bot = MagicMock()

    await bot_module.notify_codex_update_available(bot, result)
    await bot_module.notify_codex_update_available(bot, result)

    assert safe_send.await_count == 2
    assert [call.args[1] for call in safe_send.await_args_list] == [111, 222]
    assert "0.125.0" in safe_send.await_args_list[0].args[2]
    assert "0.126.0" in safe_send.await_args_list[0].args[2]
    keyboard = safe_send.await_args_list[0].kwargs["reply_markup"]
    assert keyboard.inline_keyboard[0][0].callback_data == CB_CODEX_UPDATE_APPLY
    assert keyboard.inline_keyboard[0][1].callback_data == CB_CODEX_UPDATE_DISMISS
    state = json.loads((tmp_path / "codex_update_prompt_state.json").read_text())
    assert state == {"prompted_versions": ["0.126.0"]}


@pytest.mark.asyncio
async def test_notify_codex_update_available_skips_persisted_prompted_version(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("TELEGRAM_AGENT_BOT_DIR", str(tmp_path))
    monkeypatch.setattr(bot_module.config, "allowed_users", {222, 111})
    (tmp_path / "codex_update_prompt_state.json").write_text(
        json.dumps({"prompted_versions": ["0.126.0"]}),
        encoding="utf-8",
    )
    monkeypatch.setattr(bot_module, "_codex_update_prompted_versions", set())
    safe_send = AsyncMock()
    monkeypatch.setattr(bot_module, "safe_send", safe_send)

    result = CodexUpdateResult(
        checked=True,
        supported=True,
        update_available=True,
        current_version="0.125.0",
        latest_version="0.126.0",
    )

    await bot_module.notify_codex_update_available(MagicMock(), result)

    safe_send.assert_not_awaited()


@pytest.mark.asyncio
async def test_codex_update_apply_callback_runs_update(monkeypatch):
    update = _make_callback_update(CB_CODEX_UPDATE_APPLY)
    context = MagicMock()
    safe_edit = AsyncMock()
    monkeypatch.setattr(bot_module, "_get_thread_id", lambda _update: None)
    monkeypatch.setattr(bot_module, "_codex_update_apply_lock", None)
    monkeypatch.setattr(bot_module, "safe_edit", safe_edit)
    monkeypatch.setattr(bot_module, "load_update_env", lambda: None)
    monkeypatch.setattr(bot_module, "load_codex_update_settings", lambda: object())

    def fake_check_codex_update(_settings: object, *, apply_update: bool):
        assert apply_update is True
        return CodexUpdateResult(
            checked=True,
            supported=True,
            updated=True,
            update_available=True,
            current_version="0.126.0",
            latest_version="0.126.0",
            message="Updated Codex CLI from 0.125.0 to 0.126.0.",
        )

    monkeypatch.setattr(bot_module, "check_codex_update", fake_check_codex_update)

    await bot_module.callback_handler(update, context)

    update.callback_query.answer.assert_awaited_once_with("Updating Codex CLI...")
    assert safe_edit.await_args_list[0].args[1] == "⏳ Updating Codex CLI…"
    assert "Updated Codex CLI" in safe_edit.await_args_list[-1].args[1]


@pytest.mark.asyncio
async def test_codex_update_dismiss_callback_edits_prompt(monkeypatch):
    update = _make_callback_update(CB_CODEX_UPDATE_DISMISS)
    context = MagicMock()
    safe_edit = AsyncMock()
    monkeypatch.setattr(bot_module, "_get_thread_id", lambda _update: None)
    monkeypatch.setattr(bot_module, "safe_edit", safe_edit)

    await bot_module.callback_handler(update, context)

    update.callback_query.answer.assert_awaited_once_with("Dismissed")
    safe_edit.assert_awaited_once_with(
        update.callback_query,
        "Codex CLI update dismissed.",
    )
