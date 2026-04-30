"""Tests for status_polling — Settings UI detection via the poller path.

Simulates the user workflow: /model is sent to Codex, the Settings
model picker renders in the terminal, and the status poller detects it
on its next 1s tick.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ccbot.handlers.status_polling import status_poll_loop, update_status_message


@pytest.fixture
def mock_bot():
    bot = AsyncMock()
    sent_msg = MagicMock()
    sent_msg.message_id = 999
    bot.send_message.return_value = sent_msg
    return bot


@pytest.fixture
def _clear_interactive_state():
    """Ensure interactive state is clean before and after each test."""
    from ccbot.handlers.interactive_ui import _interactive_mode, _interactive_msgs

    _interactive_mode.clear()
    _interactive_msgs.clear()
    yield
    _interactive_mode.clear()
    _interactive_msgs.clear()


@pytest.mark.usefixtures("_clear_interactive_state")
class TestStatusPollerSettingsDetection:
    """Simulate the status poller detecting a Settings UI in the terminal.

    This is the actual code path for /model: no JSONL tool_use entry exists,
    so the status poller (update_status_message) is the only detector.
    """

    @pytest.mark.asyncio
    async def test_settings_ui_detected_and_keyboard_sent(
        self, mock_bot: AsyncMock, sample_pane_settings: str
    ):
        """Poller captures Settings pane → handle_interactive_ui sends keyboard."""
        window_id = "@5"
        mock_window = MagicMock()
        mock_window.window_id = window_id

        with (
            patch("ccbot.handlers.status_polling.tmux_manager") as mock_tmux,
            patch(
                "ccbot.handlers.status_polling.handle_interactive_ui",
                new_callable=AsyncMock,
            ) as mock_handle_ui,
        ):
            mock_tmux.find_window_by_id = AsyncMock(return_value=mock_window)
            mock_tmux.capture_pane = AsyncMock(return_value=sample_pane_settings)
            mock_handle_ui.return_value = True

            await update_status_message(
                mock_bot, user_id=1, window_id=window_id, thread_id=42
            )

            mock_handle_ui.assert_called_once_with(mock_bot, 1, window_id, 42)

    @pytest.mark.asyncio
    async def test_normal_pane_no_interactive_ui(self, mock_bot: AsyncMock):
        """Normal pane text → no handle_interactive_ui call, just status check."""
        window_id = "@5"
        mock_window = MagicMock()
        mock_window.window_id = window_id
        normal_pane = (
            "some output\n"
            "✻ Reading file\n"
            "──────────────────────────────────────\n"
            "❯ \n"
            "──────────────────────────────────────\n"
            "  [Opus 4.6] Context: 50%\n"
        )

        with (
            patch("ccbot.handlers.status_polling.tmux_manager") as mock_tmux,
            patch(
                "ccbot.handlers.status_polling.handle_interactive_ui",
                new_callable=AsyncMock,
            ) as mock_handle_ui,
            patch(
                "ccbot.handlers.status_polling.enqueue_status_update",
                new_callable=AsyncMock,
            ),
        ):
            mock_tmux.find_window_by_id = AsyncMock(return_value=mock_window)
            mock_tmux.capture_pane = AsyncMock(return_value=normal_pane)

            await update_status_message(
                mock_bot, user_id=1, window_id=window_id, thread_id=42
            )

            mock_handle_ui.assert_not_called()

    @pytest.mark.asyncio
    async def test_idle_pane_clears_stale_status(self, mock_bot: AsyncMock):
        """Idle panes clear any old Working status message."""
        window_id = "@5"
        mock_window = MagicMock()
        mock_window.window_id = window_id
        idle_pane = (
            "─ Worked for 2m 04s ─────────────────────────\n\n"
            "• Final answer already rendered\n\n"
            "› Run /review on my current changes\n"
            "──────────────────────────────────────\n"
            "  [Opus 4.6] Context: 50%\n"
        )

        with (
            patch("ccbot.handlers.status_polling.tmux_manager") as mock_tmux,
            patch(
                "ccbot.handlers.status_polling.handle_interactive_ui",
                new_callable=AsyncMock,
            ) as mock_handle_ui,
            patch(
                "ccbot.handlers.status_polling.enqueue_status_update",
                new_callable=AsyncMock,
            ) as mock_enqueue_status,
        ):
            mock_tmux.find_window_by_id = AsyncMock(return_value=mock_window)
            mock_tmux.capture_pane = AsyncMock(return_value=idle_pane)

            await update_status_message(
                mock_bot, user_id=1, window_id=window_id, thread_id=42
            )

            mock_handle_ui.assert_not_called()
            mock_enqueue_status.assert_awaited_once_with(
                mock_bot,
                1,
                window_id,
                None,
                thread_id=42,
            )

    @pytest.mark.asyncio
    async def test_public_progress_block_is_sent_as_status(self, mock_bot: AsyncMock):
        """Recent public progress should show up in Telegram status updates."""
        window_id = "@5"
        mock_window = MagicMock()
        mock_window.window_id = window_id
        pane = (
            '• Searched site:msci.com \\"MSCI USA Momentum Index\\"\n'
            "✻ Searching the web\n"
            "──────────────────────────────────────\n"
            "❯ \n"
            "──────────────────────────────────────\n"
            "  [Opus 4.6] Context: 50%\n"
        )

        with (
            patch("ccbot.handlers.status_polling.tmux_manager") as mock_tmux,
            patch(
                "ccbot.handlers.status_polling.handle_interactive_ui",
                new_callable=AsyncMock,
            ) as mock_handle_ui,
            patch(
                "ccbot.handlers.status_polling.enqueue_status_update",
                new_callable=AsyncMock,
            ) as mock_enqueue_status,
        ):
            mock_tmux.find_window_by_id = AsyncMock(return_value=mock_window)
            mock_tmux.capture_pane = AsyncMock(return_value=pane)

            await update_status_message(
                mock_bot, user_id=1, window_id=window_id, thread_id=42
            )

            mock_handle_ui.assert_not_called()
            mock_enqueue_status.assert_called_once()
            assert (
                mock_enqueue_status.call_args.args[3]
                == '• Searched site:msci.com \\"MSCI USA Momentum Index\\"\n\n'
                "⏳ Searching the web"
            )

    @pytest.mark.asyncio
    async def test_working_status_is_sent_even_when_queue_is_busy(
        self, mock_bot: AsyncMock
    ):
        """Active Working status should still display while content is queued."""
        window_id = "@5"
        mock_window = MagicMock()
        mock_window.window_id = window_id
        pane = (
            "• Working (1m 07s • esc to interrupt)\n"
            "──────────────────────────────────────\n"
            "❯ \n"
            "──────────────────────────────────────\n"
            "  [Opus 4.6] Context: 50%\n"
        )

        with (
            patch("ccbot.handlers.status_polling.tmux_manager") as mock_tmux,
            patch(
                "ccbot.handlers.status_polling.handle_interactive_ui",
                new_callable=AsyncMock,
            ) as mock_handle_ui,
            patch(
                "ccbot.handlers.status_polling.enqueue_status_update",
                new_callable=AsyncMock,
            ) as mock_enqueue_status,
        ):
            mock_tmux.find_window_by_id = AsyncMock(return_value=mock_window)
            mock_tmux.capture_pane = AsyncMock(return_value=pane)

            await update_status_message(
                mock_bot,
                user_id=1,
                window_id=window_id,
                thread_id=42,
                skip_status=True,
            )

            mock_handle_ui.assert_not_called()
            mock_enqueue_status.assert_awaited_once_with(
                mock_bot,
                1,
                window_id,
                "• Working (1m 07s • esc to interrupt)",
                thread_id=42,
            )

    @pytest.mark.asyncio
    async def test_non_working_status_is_skipped_when_queue_is_busy(
        self, mock_bot: AsyncMock
    ):
        """Queue-busy suppression remains for less important status updates."""
        window_id = "@5"
        mock_window = MagicMock()
        mock_window.window_id = window_id
        pane = (
            "✻ Reading file\n"
            "──────────────────────────────────────\n"
            "❯ \n"
            "──────────────────────────────────────\n"
            "  [Opus 4.6] Context: 50%\n"
        )

        with (
            patch("ccbot.handlers.status_polling.tmux_manager") as mock_tmux,
            patch(
                "ccbot.handlers.status_polling.handle_interactive_ui",
                new_callable=AsyncMock,
            ) as mock_handle_ui,
            patch(
                "ccbot.handlers.status_polling.enqueue_status_update",
                new_callable=AsyncMock,
            ) as mock_enqueue_status,
        ):
            mock_tmux.find_window_by_id = AsyncMock(return_value=mock_window)
            mock_tmux.capture_pane = AsyncMock(return_value=pane)

            await update_status_message(
                mock_bot,
                user_id=1,
                window_id=window_id,
                thread_id=42,
                skip_status=True,
            )

            mock_handle_ui.assert_not_called()
            mock_enqueue_status.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_settings_ui_end_to_end_sends_telegram_keyboard(
        self, mock_bot: AsyncMock, sample_pane_settings: str
    ):
        """Full end-to-end: poller → is_interactive_ui → handle_interactive_ui
        → bot.send_message with keyboard.

        Uses real handle_interactive_ui (not mocked) to verify the full path.
        """
        window_id = "@5"
        mock_window = MagicMock()
        mock_window.window_id = window_id

        with (
            patch("ccbot.handlers.status_polling.tmux_manager") as mock_tmux_poll,
            patch("ccbot.handlers.interactive_ui.tmux_manager") as mock_tmux_ui,
            patch("ccbot.handlers.interactive_ui.session_manager") as mock_sm,
        ):
            mock_tmux_poll.find_window_by_id = AsyncMock(return_value=mock_window)
            mock_tmux_poll.capture_pane = AsyncMock(return_value=sample_pane_settings)
            mock_tmux_ui.find_window_by_id = AsyncMock(return_value=mock_window)
            mock_tmux_ui.capture_pane = AsyncMock(return_value=sample_pane_settings)
            mock_sm.resolve_chat_id.return_value = 100

            await update_status_message(
                mock_bot, user_id=1, window_id=window_id, thread_id=42
            )

            # Verify bot.send_message was called with keyboard
            mock_bot.send_message.assert_called_once()
            call_kwargs = mock_bot.send_message.call_args.kwargs
            assert call_kwargs["chat_id"] == 100
            assert call_kwargs["message_thread_id"] == 42
            keyboard = call_kwargs["reply_markup"]
            assert keyboard is not None
            # Verify the message text contains model picker content
            assert "Select model" in call_kwargs["text"]

    @pytest.mark.asyncio
    async def test_missing_bound_window_is_kept_for_recovery(self, mock_bot: AsyncMock):
        """A vanished tmux window should not immediately erase topic binding state."""
        with (
            patch("ccbot.handlers.status_polling.tmux_manager") as mock_tmux,
            patch("ccbot.handlers.status_polling.session_manager") as mock_sm,
            patch(
                "ccbot.handlers.status_polling.enqueue_status_update",
                new_callable=AsyncMock,
            ) as mock_enqueue_status,
            patch(
                "ccbot.handlers.status_polling.clear_topic_state",
                new_callable=AsyncMock,
            ) as mock_clear_topic_state,
            patch(
                "ccbot.handlers.status_polling.asyncio.sleep",
                new_callable=AsyncMock,
                side_effect=asyncio.CancelledError,
            ),
        ):
            mock_sm.iter_thread_bindings.return_value = [(1, 42, "@2")]
            mock_sm.resolve_chat_id.return_value = -100123
            mock_tmux.find_window_by_id = AsyncMock(return_value=None)

            with pytest.raises(asyncio.CancelledError):
                await status_poll_loop(mock_bot)

        mock_sm.unbind_thread.assert_not_called()
        mock_sm.hide_session.assert_not_called()
        mock_sm.remove_session_map_entry.assert_not_called()
        mock_sm.remove_window_state.assert_not_called()
        mock_clear_topic_state.assert_not_awaited()
        mock_enqueue_status.assert_awaited_once_with(
            mock_bot, 1, "@2", None, thread_id=42
        )
