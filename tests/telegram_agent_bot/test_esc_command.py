"""Tests for the bot-level Escape/interrupt command."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from telegram_agent_bot.agent_io import ControlResult
from telegram_agent_bot.backends.base import AgentTarget


def _make_update(user_id: int = 1, thread_id: int = 42) -> MagicMock:
    update = MagicMock()
    update.effective_user = MagicMock()
    update.effective_user.id = user_id
    update.message = MagicMock()
    update.message.message_thread_id = thread_id
    return update


def _make_context() -> MagicMock:
    context = MagicMock()
    context.bot = AsyncMock()
    context.user_data = {}
    return context


class TestEscCommand:
    @pytest.mark.asyncio
    async def test_esc_command_sends_tmux_escape_key(self):
        update = _make_update()
        context = _make_context()

        with (
            patch("telegram_agent_bot.bot.is_user_allowed", return_value=True),
            patch("telegram_agent_bot.bot._get_thread_id", return_value=42),
            patch("telegram_agent_bot.bot.session_manager") as mock_sm,
            patch(
                "telegram_agent_bot.bot.send_agent_control",
                new_callable=AsyncMock,
            ) as mock_send_control,
            patch(
                "telegram_agent_bot.bot.safe_reply", new_callable=AsyncMock
            ) as safe_reply,
            patch("telegram_agent_bot.bot.clear_window_working") as clear_working,
            patch(
                "telegram_agent_bot.bot.enqueue_status_update", new_callable=AsyncMock
            ) as enqueue_status,
        ):
            mock_sm.resolve_window_for_thread.return_value = "@5"
            mock_sm.resolve_target_for_thread.return_value = AgentTarget(
                "local", "local", window_id="@5"
            )
            mock_send_control.return_value = ControlResult(
                target=AgentTarget("local", "local", window_id="@5"),
                ok=True,
            )

            from telegram_agent_bot.bot import esc_command

            await esc_command(update, context)

        mock_send_control.assert_awaited_once_with(1, 42, "@5", "Escape")
        clear_working.assert_called_once_with(1, "@5", 42)
        enqueue_status.assert_awaited_once_with(
            context.bot,
            1,
            "@5",
            None,
            thread_id=42,
        )
        safe_reply.assert_awaited_once()
        assert safe_reply.await_args.args[1].endswith("Sent Escape")

    @pytest.mark.asyncio
    async def test_esc_command_reports_send_failure(self):
        update = _make_update()
        context = _make_context()

        with (
            patch("telegram_agent_bot.bot.is_user_allowed", return_value=True),
            patch("telegram_agent_bot.bot._get_thread_id", return_value=42),
            patch("telegram_agent_bot.bot.session_manager") as mock_sm,
            patch(
                "telegram_agent_bot.bot.send_agent_control",
                new_callable=AsyncMock,
            ) as mock_send_control,
            patch(
                "telegram_agent_bot.bot.safe_reply", new_callable=AsyncMock
            ) as safe_reply,
            patch("telegram_agent_bot.bot.clear_window_working") as clear_working,
            patch(
                "telegram_agent_bot.bot.enqueue_status_update", new_callable=AsyncMock
            ) as enqueue_status,
        ):
            mock_sm.resolve_window_for_thread.return_value = "@5"
            mock_sm.resolve_target_for_thread.return_value = AgentTarget(
                "local", "local", window_id="@5"
            )
            mock_send_control.return_value = ControlResult(
                target=AgentTarget("local", "local", window_id="@5"),
                ok=False,
                message="failed",
            )

            from telegram_agent_bot.bot import esc_command

            await esc_command(update, context)

        safe_reply.assert_awaited_once()
        assert "Failed to send Escape" in safe_reply.await_args.args[1]
        clear_working.assert_not_called()
        enqueue_status.assert_not_awaited()
