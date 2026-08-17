"""Bot command safeguards for a local window bound to multiple topics."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _make_update(text: str, user_id: int = 12345, thread_id: int = 42) -> MagicMock:
    update = MagicMock()
    update.effective_user.id = user_id
    update.effective_chat.type = "private"
    update.message.text = text
    update.message.message_thread_id = thread_id
    return update


def _make_context() -> MagicMock:
    context = MagicMock()
    context.bot = AsyncMock()
    context.user_data = {}
    return context


class TestAmbiguousWindowBindings:
    @pytest.mark.asyncio
    async def test_text_handler_pauses_cross_topic_delivery(self) -> None:
        update = _make_update("hello")
        context = _make_context()

        with (
            patch("telegram_agent_bot.bot.is_user_allowed", return_value=True),
            patch("telegram_agent_bot.bot._get_thread_id", return_value=42),
            patch("telegram_agent_bot.bot.session_manager") as manager,
            patch(
                "telegram_agent_bot.bot.safe_reply", new_callable=AsyncMock
            ) as safe_reply,
            patch(
                "telegram_agent_bot.bot._send_message_to_agent",
                new_callable=AsyncMock,
            ) as send_message,
        ):
            manager.get_ambiguous_window_for_thread.return_value = "@1"

            from telegram_agent_bot.bot import text_handler

            await text_handler(update, context)

        send_message.assert_not_awaited()
        safe_reply.assert_awaited_once()
        assert "cross-topic delivery" in safe_reply.await_args.args[1]

    @pytest.mark.asyncio
    async def test_escape_pauses_ambiguous_control(self) -> None:
        update = _make_update("/esc")
        context = _make_context()

        with (
            patch("telegram_agent_bot.bot.is_user_allowed", return_value=True),
            patch("telegram_agent_bot.bot._get_thread_id", return_value=42),
            patch("telegram_agent_bot.bot.session_manager") as manager,
            patch(
                "telegram_agent_bot.bot.safe_reply", new_callable=AsyncMock
            ) as safe_reply,
            patch(
                "telegram_agent_bot.bot.send_agent_control", new_callable=AsyncMock
            ) as send_control,
        ):
            manager.get_ambiguous_window_for_thread.return_value = "@1"

            from telegram_agent_bot.bot import esc_command

            await esc_command(update, context)

        manager.resolve_window_for_thread.assert_not_called()
        send_control.assert_not_awaited()
        assert "Control is paused" in safe_reply.await_args.args[1]

    @pytest.mark.asyncio
    async def test_interrupt_pauses_ambiguous_control(self) -> None:
        update = _make_update("/interrupt replace this")
        context = _make_context()

        with (
            patch("telegram_agent_bot.bot.is_user_allowed", return_value=True),
            patch("telegram_agent_bot.bot._get_thread_id", return_value=42),
            patch("telegram_agent_bot.bot.session_manager") as manager,
            patch(
                "telegram_agent_bot.bot.safe_reply", new_callable=AsyncMock
            ) as safe_reply,
            patch("telegram_agent_bot.bot.tmux_manager") as tmux_manager,
        ):
            manager.get_ambiguous_window_for_thread.return_value = "@1"

            from telegram_agent_bot.bot import interrupt_command

            await interrupt_command(update, context)

        manager.resolve_window_for_thread.assert_not_called()
        tmux_manager.find_window_by_id.assert_not_called()
        assert "Interrupt is paused" in safe_reply.await_args.args[1]

    @pytest.mark.asyncio
    async def test_unbind_removes_only_current_ambiguous_binding(self) -> None:
        update = _make_update("/unbind")
        context = _make_context()

        with (
            patch("telegram_agent_bot.bot.is_user_allowed", return_value=True),
            patch("telegram_agent_bot.bot._get_thread_id", return_value=42),
            patch("telegram_agent_bot.bot.session_manager") as manager,
            patch(
                "telegram_agent_bot.bot.clear_topic_state", new_callable=AsyncMock
            ) as clear_topic_state,
            patch(
                "telegram_agent_bot.bot.safe_reply", new_callable=AsyncMock
            ) as safe_reply,
        ):
            manager.get_ambiguous_window_for_thread.return_value = "@1"

            from telegram_agent_bot.bot import unbind_command

            await unbind_command(update, context)

        manager.unbind_thread.assert_called_once_with(12345, 42)
        clear_topic_state.assert_awaited_once_with(
            12345, 42, context.bot, context.user_data
        )
        assert "Removed the ambiguous topic binding" in safe_reply.await_args.args[1]
