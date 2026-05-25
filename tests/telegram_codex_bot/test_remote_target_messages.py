"""Tests for routing messages from non-local agent backends."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from telegram_codex_bot.session_monitor import NewMessage


@pytest.mark.asyncio
async def test_handle_new_message_routes_remote_target_session() -> None:
    bot = AsyncMock()
    msg = NewMessage(
        session_id="remote-1",
        text="remote response",
        is_complete=True,
    )

    with (
        patch("telegram_codex_bot.bot.session_manager") as mock_sm,
        patch(
            "telegram_codex_bot.bot.enqueue_content_message",
            new_callable=AsyncMock,
        ) as enqueue_content_message,
        patch(
            "telegram_codex_bot.bot.build_response_parts",
            return_value=["remote response"],
        ) as build_response_parts,
    ):
        mock_sm.find_users_for_session = AsyncMock(return_value=[])
        mock_sm.find_users_for_target_session.return_value = [(12345, "", 42)]
        mock_sm.resolve_chat_id.return_value = -1001234567890

        from telegram_codex_bot.bot import handle_new_message

        await handle_new_message(msg, bot)

    build_response_parts.assert_called_once_with(
        "remote response",
        True,
        "text",
        "assistant",
    )
    enqueue_content_message.assert_awaited_once_with(
        bot=bot,
        user_id=12345,
        window_id="",
        parts=["remote response"],
        tool_use_id=None,
        content_type="text",
        role="assistant",
        text="remote response",
        thread_id=42,
        image_data=None,
        wait_until_sent=True,
    )


@pytest.mark.asyncio
async def test_handle_new_message_reports_remote_usage_limit() -> None:
    bot = AsyncMock()
    msg = NewMessage(
        session_id="remote-1",
        text="",
        is_complete=True,
        content_type="usage_limit",
    )

    with (
        patch("telegram_codex_bot.bot.session_manager") as mock_sm,
        patch("telegram_codex_bot.bot.safe_send", new_callable=AsyncMock) as safe_send,
    ):
        mock_sm.find_users_for_session = AsyncMock(return_value=[])
        mock_sm.find_users_for_target_session.return_value = [(12345, "", 42)]
        mock_sm.resolve_chat_id.return_value = -1001234567890

        from telegram_codex_bot.bot import handle_new_message

        await handle_new_message(msg, bot)

    safe_send.assert_awaited_once_with(
        bot,
        -1001234567890,
        "⚠️ This remote session has hit its usage limit.",
        message_thread_id=42,
    )
    mock_sm.mark_window_usage_limit_exceeded.assert_not_called()


@pytest.mark.asyncio
async def test_handle_new_message_reports_repeated_local_usage_limit() -> None:
    bot = AsyncMock()
    msg = NewMessage(
        session_id="local-1",
        text="",
        is_complete=True,
        content_type="usage_limit",
    )

    with (
        patch("telegram_codex_bot.bot.session_manager") as mock_sm,
        patch("telegram_codex_bot.bot.safe_send", new_callable=AsyncMock) as safe_send,
        patch("telegram_codex_bot.bot.get_next_account_name", return_value="backup"),
    ):
        mock_sm.find_users_for_session = AsyncMock(return_value=[(12345, "@1", 42)])
        mock_sm.find_users_for_target_session.return_value = []
        mock_sm.resolve_chat_id.return_value = -1001234567890
        mock_sm.mark_window_usage_limit_exceeded.return_value = False
        mock_sm.get_window_state.return_value = SimpleNamespace(account_name="main")

        from telegram_codex_bot.bot import handle_new_message

        await handle_new_message(msg, bot)

    safe_send.assert_awaited_once_with(
        bot,
        -1001234567890,
        (
            "⚠️ This session has hit its usage limit."
            "\nThe window is already marked as exhausted. On your next "
            "message, TelegramCodexBot will open a new `backup` session automatically."
        ),
        message_thread_id=42,
    )
