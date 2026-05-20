"""Tests for forward_command_handler — command forwarding to Codex."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from telegram_codex_bot.agent_io import MessageResult
from telegram_codex_bot.backends.base import AgentTarget


def _make_update(text: str, user_id: int = 1, thread_id: int = 42) -> MagicMock:
    """Build a minimal mock Update with message text in a forum topic."""
    update = MagicMock()
    update.effective_user = MagicMock()
    update.effective_user.id = user_id
    update.message = MagicMock()
    update.message.text = text
    update.message.message_thread_id = thread_id
    update.message.chat = MagicMock()
    update.message.chat.send_action = AsyncMock()
    update.effective_chat = MagicMock()
    update.effective_chat.type = "supergroup"
    update.effective_chat.id = 100
    return update


def _make_context() -> MagicMock:
    """Build a minimal mock context."""
    context = MagicMock()
    context.bot = AsyncMock()
    context.user_data = {}
    return context


class TestForwardCommand:
    @pytest.mark.asyncio
    async def test_model_sends_command_to_tmux(self):
        """/model → send_to_window called with "/model"."""
        update = _make_update("/model")
        context = _make_context()

        with (
            patch("telegram_codex_bot.bot.is_user_allowed", return_value=True),
            patch("telegram_codex_bot.bot._get_thread_id", return_value=42),
            patch("telegram_codex_bot.bot.session_manager") as mock_sm,
            patch(
                "telegram_codex_bot.bot.send_agent_message", new_callable=AsyncMock
            ) as mock_send,
            patch("telegram_codex_bot.bot.safe_reply", new_callable=AsyncMock),
        ):
            mock_sm.resolve_window_for_thread.return_value = "@5"
            mock_sm.resolve_target_for_thread.return_value = AgentTarget(
                "local", "local", window_id="@5"
            )
            mock_sm.get_display_name.return_value = "project"
            mock_send.return_value = MessageResult(
                AgentTarget("local", "local", window_id="@5"), True, "ok"
            )

            from telegram_codex_bot.bot import forward_command_handler

            await forward_command_handler(update, context)

            mock_send.assert_awaited_once_with(1, 42, "@5", "/model")

    @pytest.mark.asyncio
    async def test_cost_sends_command_to_tmux(self):
        """/cost → send_to_window called with "/cost"."""
        update = _make_update("/cost")
        context = _make_context()

        with (
            patch("telegram_codex_bot.bot.is_user_allowed", return_value=True),
            patch("telegram_codex_bot.bot._get_thread_id", return_value=42),
            patch("telegram_codex_bot.bot.session_manager") as mock_sm,
            patch(
                "telegram_codex_bot.bot.send_agent_message", new_callable=AsyncMock
            ) as mock_send,
            patch("telegram_codex_bot.bot.safe_reply", new_callable=AsyncMock),
        ):
            mock_sm.resolve_window_for_thread.return_value = "@5"
            mock_sm.resolve_target_for_thread.return_value = AgentTarget(
                "local", "local", window_id="@5"
            )
            mock_sm.get_display_name.return_value = "project"
            mock_send.return_value = MessageResult(
                AgentTarget("local", "local", window_id="@5"), True, "ok"
            )

            from telegram_codex_bot.bot import forward_command_handler

            await forward_command_handler(update, context)

            mock_send.assert_awaited_once_with(1, 42, "@5", "/cost")

    @pytest.mark.asyncio
    async def test_clear_clears_session(self):
        """/clear → send_to_window + clear_window_session."""
        update = _make_update("/clear")
        context = _make_context()

        with (
            patch("telegram_codex_bot.bot.is_user_allowed", return_value=True),
            patch("telegram_codex_bot.bot._get_thread_id", return_value=42),
            patch("telegram_codex_bot.bot.session_manager") as mock_sm,
            patch(
                "telegram_codex_bot.bot.send_agent_message", new_callable=AsyncMock
            ) as mock_send,
            patch("telegram_codex_bot.bot.safe_reply", new_callable=AsyncMock),
        ):
            mock_sm.resolve_window_for_thread.return_value = "@5"
            mock_sm.resolve_target_for_thread.return_value = AgentTarget(
                "local", "local", window_id="@5"
            )
            mock_sm.get_display_name.return_value = "project"
            mock_send.return_value = MessageResult(
                AgentTarget("local", "local", window_id="@5"), True, "ok"
            )

            from telegram_codex_bot.bot import forward_command_handler

            await forward_command_handler(update, context)

            mock_send.assert_awaited_once_with(1, 42, "@5", "/clear")
            mock_sm.clear_window_session.assert_called_once_with("@5")
