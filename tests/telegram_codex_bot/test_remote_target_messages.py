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
        tool_name=None,
        content_type="text",
        role="assistant",
        text="remote response",
        thread_id=42,
        image_data=None,
        wait_until_sent=True,
    )


@pytest.mark.asyncio
async def test_handle_new_message_uses_source_offset_for_delivered_local_message() -> (
    None
):
    bot = AsyncMock()
    msg = NewMessage(
        session_id="local-1",
        text="local response",
        is_complete=True,
        source_offset=123,
    )

    with (
        patch("telegram_codex_bot.bot.session_manager") as mock_sm,
        patch(
            "telegram_codex_bot.bot.enqueue_content_message",
            new_callable=AsyncMock,
        ),
        patch(
            "telegram_codex_bot.bot.build_response_parts",
            return_value=["local response"],
        ),
    ):
        mock_sm.find_users_for_session = AsyncMock(return_value=[(12345, "@1", 42)])
        mock_sm.find_users_for_target_session.return_value = []
        mock_sm.resolve_session_for_window = AsyncMock()

        from telegram_codex_bot.bot import handle_new_message

        await handle_new_message(msg, bot)

    mock_sm.update_user_window_offset.assert_called_once_with(12345, "@1", 123)
    mock_sm.resolve_session_for_window.assert_not_awaited()


@pytest.mark.asyncio
async def test_handle_new_message_skips_already_delivered_local_recipient() -> None:
    bot = AsyncMock()
    msg = NewMessage(
        session_id="local-1",
        text="local response",
        is_complete=True,
        source_offset=123,
    )

    with (
        patch("telegram_codex_bot.bot.session_manager") as mock_sm,
        patch(
            "telegram_codex_bot.bot.enqueue_content_message",
            new_callable=AsyncMock,
            return_value=True,
        ) as enqueue_content_message,
        patch(
            "telegram_codex_bot.bot.build_response_parts",
            return_value=["local response"],
        ),
    ):
        mock_sm.find_users_for_session = AsyncMock(
            return_value=[(11111, "@1", 41), (22222, "@1", 42)]
        )
        mock_sm.find_users_for_target_session.return_value = []
        mock_sm.user_window_offsets = {
            11111: {"@1": 123},
            22222: {"@1": 100},
        }
        mock_sm.resolve_session_for_window = AsyncMock()

        from telegram_codex_bot.bot import handle_new_message

        await handle_new_message(msg, bot)

    enqueue_content_message.assert_awaited_once_with(
        bot=bot,
        user_id=22222,
        window_id="@1",
        parts=["local response"],
        tool_use_id=None,
        tool_name=None,
        content_type="text",
        role="assistant",
        text="local response",
        thread_id=42,
        image_data=None,
        wait_until_sent=True,
    )
    mock_sm.update_user_window_offset.assert_called_once_with(22222, "@1", 123)
    mock_sm.resolve_session_for_window.assert_not_awaited()


@pytest.mark.asyncio
async def test_handle_new_message_keeps_offset_when_delivery_fails() -> None:
    bot = AsyncMock()
    msg = NewMessage(
        session_id="local-1",
        text="local response",
        is_complete=True,
        source_offset=123,
    )

    with (
        patch("telegram_codex_bot.bot.session_manager") as mock_sm,
        patch(
            "telegram_codex_bot.bot.enqueue_content_message",
            new_callable=AsyncMock,
            return_value=False,
        ) as enqueue_content_message,
        patch(
            "telegram_codex_bot.bot.build_response_parts",
            return_value=["local response"],
        ),
    ):
        mock_sm.find_users_for_session = AsyncMock(return_value=[(12345, "@1", 42)])
        mock_sm.find_users_for_target_session.return_value = []

        from telegram_codex_bot.bot import handle_new_message

        with pytest.raises(RuntimeError, match="Telegram content deliveries failed"):
            await handle_new_message(msg, bot)

    enqueue_content_message.assert_awaited_once()
    mock_sm.update_user_window_offset.assert_not_called()


@pytest.mark.asyncio
async def test_handle_new_message_renders_encrypted_reasoning_as_status() -> None:
    bot = AsyncMock()
    msg = NewMessage(
        session_id="local-1",
        text="EXPQUOTE_STARTWorking on it…EXPQUOTE_END",
        is_complete=True,
        content_type="thinking",
    )

    with (
        patch("telegram_codex_bot.bot.session_manager") as mock_sm,
        patch(
            "telegram_codex_bot.bot.enqueue_status_update",
            new_callable=AsyncMock,
        ) as enqueue_status_update,
        patch(
            "telegram_codex_bot.bot.enqueue_content_message",
            new_callable=AsyncMock,
        ) as enqueue_content_message,
    ):
        mock_sm.find_users_for_session = AsyncMock(return_value=[(12345, "@1", 42)])
        mock_sm.find_users_for_target_session.return_value = []

        from telegram_codex_bot.bot import handle_new_message

        await handle_new_message(msg, bot)

    enqueue_status_update.assert_awaited_once_with(
        bot,
        12345,
        "@1",
        "💭 Thinking…\n◦ Working on it…",
        thread_id=42,
    )
    enqueue_content_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_handle_new_message_renders_wait_tool_use_as_status() -> None:
    bot = AsyncMock()
    msg = NewMessage(
        session_id="local-1",
        text="**Wait**(background terminal)",
        is_complete=True,
        content_type="tool_use",
        tool_use_id="call_wait",
        tool_name="Wait",
        source_offset=123,
    )

    with (
        patch("telegram_codex_bot.bot.session_manager") as mock_sm,
        patch(
            "telegram_codex_bot.bot.enqueue_status_update",
            new_callable=AsyncMock,
        ) as enqueue_status_update,
        patch(
            "telegram_codex_bot.bot.enqueue_content_message",
            new_callable=AsyncMock,
        ) as enqueue_content_message,
        patch("telegram_codex_bot.bot.build_response_parts") as build_response_parts,
    ):
        mock_sm.find_users_for_session = AsyncMock(return_value=[(12345, "@1", 42)])
        mock_sm.find_users_for_target_session.return_value = []

        from telegram_codex_bot.bot import handle_new_message

        await handle_new_message(msg, bot)

    enqueue_status_update.assert_awaited_once_with(
        bot,
        12345,
        "@1",
        "💭 Thinking…\n◦ Working in background terminal…",
        thread_id=42,
    )
    enqueue_content_message.assert_not_awaited()
    build_response_parts.assert_not_called()
    mock_sm.update_user_window_offset.assert_called_once_with(12345, "@1", 123)


@pytest.mark.asyncio
async def test_handle_new_message_hides_wait_tool_result() -> None:
    bot = AsyncMock()
    msg = NewMessage(
        session_id="local-1",
        text="background terminal output",
        is_complete=True,
        content_type="tool_result",
        tool_use_id="call_wait",
        tool_name="Wait",
        source_offset=456,
    )

    with (
        patch("telegram_codex_bot.bot.session_manager") as mock_sm,
        patch(
            "telegram_codex_bot.bot.enqueue_status_update",
            new_callable=AsyncMock,
        ) as enqueue_status_update,
        patch(
            "telegram_codex_bot.bot.enqueue_content_message",
            new_callable=AsyncMock,
        ) as enqueue_content_message,
        patch("telegram_codex_bot.bot.build_response_parts") as build_response_parts,
    ):
        mock_sm.find_users_for_session = AsyncMock(return_value=[(12345, "@1", 42)])
        mock_sm.find_users_for_target_session.return_value = []

        from telegram_codex_bot.bot import handle_new_message

        await handle_new_message(msg, bot)

    enqueue_status_update.assert_not_awaited()
    enqueue_content_message.assert_not_awaited()
    build_response_parts.assert_not_called()
    mock_sm.update_user_window_offset.assert_called_once_with(12345, "@1", 456)


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
        patch(
            "telegram_codex_bot.bot.config",
            SimpleNamespace(enable_account_rotation=False),
        ),
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
            "\nThe window is already marked as exhausted."
            "\nAutomatic account rotation is disabled. Use /codexlogin to "
            "refresh the current login, or /codexaccount to choose a saved account."
        ),
        message_thread_id=42,
    )


@pytest.mark.asyncio
async def test_handle_new_message_reports_auto_rotation_when_enabled() -> None:
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
        patch(
            "telegram_codex_bot.bot.config",
            SimpleNamespace(enable_account_rotation=True),
        ),
    ):
        mock_sm.find_users_for_session = AsyncMock(return_value=[(12345, "@1", 42)])
        mock_sm.find_users_for_target_session.return_value = []
        mock_sm.resolve_chat_id.return_value = -1001234567890
        mock_sm.mark_window_usage_limit_exceeded.return_value = True
        mock_sm.get_window_state.return_value = SimpleNamespace(account_name="main")

        from telegram_codex_bot.bot import handle_new_message

        await handle_new_message(msg, bot)

    safe_send.assert_awaited_once_with(
        bot,
        -1001234567890,
        (
            "⚠️ This session has hit its usage limit."
            "\nThe window is now marked as exhausted."
            " On your next message, TelegramAgentBot will open a new "
            "`backup` session automatically."
        ),
        message_thread_id=42,
    )
