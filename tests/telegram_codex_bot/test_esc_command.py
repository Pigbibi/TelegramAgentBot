"""Tests for the bot-level Escape/interrupt command."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


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
        window = SimpleNamespace(window_id="@5")

        with (
            patch("telegram_codex_bot.bot.is_user_allowed", return_value=True),
            patch("telegram_codex_bot.bot._get_thread_id", return_value=42),
            patch("telegram_codex_bot.bot.session_manager") as mock_sm,
            patch("telegram_codex_bot.bot.tmux_manager") as mock_tmux,
            patch(
                "telegram_codex_bot.bot.safe_reply", new_callable=AsyncMock
            ) as safe_reply,
        ):
            mock_sm.resolve_window_for_thread.return_value = "@5"
            mock_tmux.find_window_by_id = AsyncMock(return_value=window)
            mock_tmux.send_keys = AsyncMock(return_value=True)

            from telegram_codex_bot.bot import esc_command

            await esc_command(update, context)

        mock_tmux.send_keys.assert_awaited_once_with(
            "@5", "Escape", enter=False, literal=False
        )
        safe_reply.assert_awaited_once()
        assert safe_reply.await_args.args[1].endswith("Sent Escape")

    @pytest.mark.asyncio
    async def test_esc_command_reports_send_failure(self):
        update = _make_update()
        context = _make_context()
        window = SimpleNamespace(window_id="@5")

        with (
            patch("telegram_codex_bot.bot.is_user_allowed", return_value=True),
            patch("telegram_codex_bot.bot._get_thread_id", return_value=42),
            patch("telegram_codex_bot.bot.session_manager") as mock_sm,
            patch("telegram_codex_bot.bot.tmux_manager") as mock_tmux,
            patch(
                "telegram_codex_bot.bot.safe_reply", new_callable=AsyncMock
            ) as safe_reply,
        ):
            mock_sm.resolve_window_for_thread.return_value = "@5"
            mock_tmux.find_window_by_id = AsyncMock(return_value=window)
            mock_tmux.send_keys = AsyncMock(return_value=False)

            from telegram_codex_bot.bot import esc_command

            await esc_command(update, context)

        safe_reply.assert_awaited_once()
        assert "Failed to send Escape" in safe_reply.await_args.args[1]
