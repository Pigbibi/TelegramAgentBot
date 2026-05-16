"""Tests for binding existing tmux windows to topics."""

from unittest.mock import AsyncMock, MagicMock, patch
from pathlib import Path

import pytest
from telegram.error import TelegramError

from ccbot.config import ProjectRoot
from ccbot.handlers.callback_data import CB_DIR_CONFIRM, CB_ROOT_SELECT, CB_WIN_BIND
from ccbot.handlers.directory_browser import (
    BROWSE_DIRS_KEY,
    BROWSE_PATH_KEY,
    BROWSE_ROOT_LABEL_KEY,
    BROWSE_ROOT_PATH_KEY,
    ROOTS_KEY,
    STATE_BROWSING_DIRECTORY,
    STATE_KEY,
    STATE_SELECTING_ROOT,
)
from ccbot.session import WindowState


def _make_text_update(text: str, user_id: int = 12345, thread_id: int = 42):
    update = MagicMock()
    update.effective_user = MagicMock(id=user_id)
    update.message = MagicMock()
    update.message.text = text
    update.message.message_thread_id = thread_id
    update.message.chat = MagicMock()
    update.message.chat.type = "supergroup"
    update.message.chat.send_action = AsyncMock()
    update.effective_chat = MagicMock()
    update.effective_chat.type = "supergroup"
    update.effective_chat.id = -1001234567890
    return update


def _make_callback_update(data: str, thread_id: int = 42, user_id: int = 12345):
    update = MagicMock()
    update.effective_user = MagicMock(id=user_id)
    update.effective_chat = MagicMock()
    update.effective_chat.type = "supergroup"
    update.effective_chat.id = -1001234567890
    query = MagicMock()
    query.data = data
    query.answer = AsyncMock()
    query.message = MagicMock()
    query.message.message_thread_id = thread_id
    update.callback_query = query
    return update, query


def _make_context():
    context = MagicMock()
    context.bot = AsyncMock()
    context.user_data = {}
    return context


class TestExistingWindowBinding:
    @pytest.mark.asyncio
    async def test_untracked_unbound_windows_fall_back_to_directory_browser(self):
        update = _make_text_update("hi")
        context = _make_context()

        fake_window = MagicMock()
        fake_window.window_id = "@1"
        fake_window.window_name = "Projects"
        fake_window.cwd = "/tmp/project"

        with (
            patch("ccbot.bot.is_user_allowed", return_value=True),
            patch("ccbot.bot._get_thread_id", return_value=42),
            patch("ccbot.bot.session_manager") as mock_sm,
            patch("ccbot.bot.tmux_manager") as mock_tmux,
            patch("ccbot.bot.config.project_roots_configured", False),
            patch("ccbot.bot.build_directory_browser") as build_directory_browser,
            patch("ccbot.bot.safe_reply", new_callable=AsyncMock) as safe_reply,
        ):
            mock_tmux.list_windows = AsyncMock(return_value=[fake_window])
            mock_sm.iter_thread_bindings.return_value = []
            mock_sm.get_window_for_thread.return_value = None
            mock_sm.window_states = {}
            build_directory_browser.return_value = ("pick dir", "kbd", ["src"])

            from ccbot.bot import text_handler

            await text_handler(update, context)

        safe_reply.assert_awaited_once_with(
            update.message, "pick dir", reply_markup="kbd"
        )
        assert context.user_data["_pending_thread_id"] == 42
        assert context.user_data["_pending_thread_text"] == "hi"

    @pytest.mark.asyncio
    async def test_unbound_topic_shows_root_picker_before_directory_browser(self):
        update = _make_text_update("hi")
        context = _make_context()

        with (
            patch("ccbot.bot.is_user_allowed", return_value=True),
            patch("ccbot.bot._get_thread_id", return_value=42),
            patch("ccbot.bot.session_manager") as mock_sm,
            patch("ccbot.bot.tmux_manager") as mock_tmux,
            patch("ccbot.bot.config") as mock_config,
            patch("ccbot.bot.safe_reply", new_callable=AsyncMock) as safe_reply,
        ):
            mock_tmux.list_windows = AsyncMock(return_value=[])
            mock_sm.iter_thread_bindings.return_value = []
            mock_sm.get_window_for_thread.return_value = None
            mock_config.project_roots_configured = True
            mock_config.project_roots = [
                ProjectRoot("Primary", Path("/srv/projects")),
            ]

            from ccbot.bot import text_handler

            await text_handler(update, context)

        safe_reply.assert_awaited_once()
        message_text = safe_reply.await_args.args[1]
        assert "Select Computer / VPS" in message_text
        assert "Primary" in message_text
        assert context.user_data[STATE_KEY] == STATE_SELECTING_ROOT
        assert context.user_data[ROOTS_KEY] == [
            ("Primary", "/srv/projects"),
        ]
        assert context.user_data["_pending_thread_text"] == "hi"

    @pytest.mark.asyncio
    async def test_configured_project_roots_take_priority_over_unbound_windows(self):
        update = _make_text_update("hi")
        context = _make_context()

        fake_window = MagicMock()
        fake_window.window_id = "@1"
        fake_window.window_name = "audit-runner"
        fake_window.cwd = "/srv/projects"

        with (
            patch("ccbot.bot.is_user_allowed", return_value=True),
            patch("ccbot.bot._get_thread_id", return_value=42),
            patch("ccbot.bot.session_manager") as mock_sm,
            patch("ccbot.bot.tmux_manager") as mock_tmux,
            patch("ccbot.bot.config") as mock_config,
            patch("ccbot.bot.safe_reply", new_callable=AsyncMock) as safe_reply,
        ):
            mock_tmux.list_windows = AsyncMock(return_value=[fake_window])
            mock_sm.iter_thread_bindings.return_value = []
            mock_sm.get_window_for_thread.return_value = None
            mock_sm.window_states = {
                "@1": WindowState(session_id="session-1", cwd="/srv/projects")
            }
            mock_config.project_roots_configured = True
            mock_config.project_roots = [
                ProjectRoot("Ubuntu", Path("/srv/projects")),
            ]

            from ccbot.bot import text_handler

            await text_handler(update, context)

        mock_tmux.list_windows.assert_not_awaited()
        safe_reply.assert_awaited_once()
        message_text = safe_reply.await_args.args[1]
        assert "Select Computer / VPS" in message_text
        assert "Ubuntu" in message_text
        assert "audit-runner" not in message_text
        assert context.user_data[STATE_KEY] == STATE_SELECTING_ROOT
        assert context.user_data[ROOTS_KEY] == [
            ("Ubuntu", "/srv/projects"),
        ]
        assert context.user_data["_pending_thread_text"] == "hi"

    @pytest.mark.asyncio
    async def test_root_picker_selection_enters_selected_directory(self, tmp_path):
        root_path = tmp_path / "primary"
        root_path.mkdir()
        (root_path / "repo").mkdir()
        update, query = _make_callback_update(f"{CB_ROOT_SELECT}0")
        context = _make_context()
        context.user_data = {
            STATE_KEY: STATE_SELECTING_ROOT,
            ROOTS_KEY: [("Primary", str(root_path))],
            "_pending_thread_id": 42,
            "_pending_thread_text": "hi",
        }

        with (
            patch("ccbot.bot.is_user_allowed", return_value=True),
            patch("ccbot.bot._get_thread_id", return_value=42),
            patch("ccbot.bot.safe_edit", new_callable=AsyncMock) as safe_edit,
        ):
            from ccbot.bot import callback_handler

            await callback_handler(update, context)

        safe_edit.assert_awaited_once()
        assert "Primary" in safe_edit.await_args.args[1]
        assert context.user_data[STATE_KEY] == STATE_BROWSING_DIRECTORY
        assert context.user_data[BROWSE_PATH_KEY] == str(root_path)
        assert context.user_data[BROWSE_ROOT_LABEL_KEY] == "Primary"
        assert context.user_data[BROWSE_ROOT_PATH_KEY] == str(root_path)
        assert context.user_data[BROWSE_DIRS_KEY] == ["repo"]
        assert ROOTS_KEY not in context.user_data

    @pytest.mark.asyncio
    async def test_directory_confirm_falls_back_to_callback_topic_when_pending_missing(
        self, tmp_path
    ):
        selected_path = tmp_path / "project"
        selected_path.mkdir()
        update, query = _make_callback_update(CB_DIR_CONFIRM)
        context = _make_context()
        context.user_data = {
            STATE_KEY: STATE_BROWSING_DIRECTORY,
            BROWSE_PATH_KEY: str(selected_path),
        }

        with (
            patch("ccbot.bot.is_user_allowed", return_value=True),
            patch("ccbot.bot._get_thread_id", return_value=42),
            patch("ccbot.bot.session_manager") as mock_sm,
            patch("ccbot.bot.safe_edit", new_callable=AsyncMock),
            patch(
                "ccbot.bot._create_and_bind_window",
                new_callable=AsyncMock,
            ) as create_and_bind,
        ):
            mock_sm.list_sessions_for_directory = AsyncMock(return_value=[])

            from ccbot.bot import callback_handler

            await callback_handler(update, context)

        create_and_bind.assert_awaited_once()
        assert create_and_bind.await_args.args[4] == 42
        assert create_and_bind.await_args.kwargs["answer_callback"] is False

    @pytest.mark.asyncio
    async def test_window_picker_rejects_untracked_window(self):
        update, query = _make_callback_update(f"{CB_WIN_BIND}0")
        context = _make_context()
        context.user_data = {
            "unbound_windows": ["@1"],
            "_pending_thread_id": 42,
            "_pending_thread_text": "hi",
        }

        fake_window = MagicMock()
        fake_window.window_id = "@1"
        fake_window.window_name = "Projects"

        with (
            patch("ccbot.bot.is_user_allowed", return_value=True),
            patch("ccbot.bot._get_thread_id", return_value=42),
            patch("ccbot.bot.session_manager") as mock_sm,
            patch("ccbot.bot.tmux_manager") as mock_tmux,
            patch("ccbot.bot.safe_edit", new_callable=AsyncMock) as safe_edit,
        ):
            mock_tmux.find_window_by_id = AsyncMock(return_value=fake_window)
            mock_sm.window_states = {}

            from ccbot.bot import callback_handler

            await callback_handler(update, context)

        safe_edit.assert_not_called()
        query.answer.assert_awaited_once()
        assert query.answer.await_args.args == (
            "This window has no tracked Codex session yet. Please choose New Session instead.",
        )
        assert query.answer.await_args.kwargs["show_alert"] is True

    @pytest.mark.asyncio
    async def test_window_picker_bind_does_not_rename_topic(self):
        update, query = _make_callback_update(f"{CB_WIN_BIND}0")
        context = _make_context()
        context.user_data = {
            "unbound_windows": ["@1"],
            "_pending_thread_id": 42,
        }

        fake_window = MagicMock()
        fake_window.window_id = "@1"
        fake_window.window_name = "Projects"

        with (
            patch("ccbot.bot.is_user_allowed", return_value=True),
            patch("ccbot.bot._get_thread_id", return_value=42),
            patch("ccbot.bot.session_manager") as mock_sm,
            patch("ccbot.bot.tmux_manager") as mock_tmux,
            patch("ccbot.bot.safe_edit", new_callable=AsyncMock),
        ):
            mock_tmux.find_window_by_id = AsyncMock(return_value=fake_window)
            mock_sm.window_states = {
                "@1": WindowState(session_id="session-1", cwd="/tmp/project")
            }
            mock_sm.resolve_chat_id.return_value = -1001234567890

            from ccbot.bot import callback_handler

            await callback_handler(update, context)

        context.bot.edit_forum_topic.assert_not_called()
        mock_sm.bind_thread.assert_called_once_with(
            12345, 42, "@1", window_name="Projects"
        )
        query.answer.assert_awaited_once_with("Bound")

    @pytest.mark.asyncio
    async def test_bound_topic_continues_when_typing_action_fails(self):
        update = _make_text_update("hi")
        update.message.chat.send_action = AsyncMock(
            side_effect=TelegramError("bad gateway")
        )
        context = _make_context()

        fake_window = MagicMock()
        fake_window.window_id = "@1"
        fake_window.window_name = "Projects"
        fake_window.cwd = "/tmp/project"

        with (
            patch("ccbot.bot.is_user_allowed", return_value=True),
            patch("ccbot.bot._get_thread_id", return_value=42),
            patch("ccbot.bot.session_manager") as mock_sm,
            patch("ccbot.bot.tmux_manager") as mock_tmux,
            patch(
                "ccbot.bot.enqueue_status_update", new_callable=AsyncMock
            ) as enqueue_status_update,
            patch("ccbot.bot.safe_reply", new_callable=AsyncMock) as safe_reply,
            patch(
                "ccbot.bot._send_to_window_when_codex_ready",
                new_callable=AsyncMock,
                return_value=(True, "Sent"),
            ) as send_when_ready,
            patch("ccbot.bot._cancel_bash_capture"),
        ):
            mock_sm.get_window_for_thread.return_value = "@1"
            mock_sm.window_has_usage_limit_exceeded = AsyncMock(return_value=False)
            mock_sm.send_to_window = AsyncMock(return_value=(True, "Sent"))
            mock_tmux.find_window_by_id = AsyncMock(return_value=fake_window)
            mock_tmux.capture_pane = AsyncMock(return_value="")

            from ccbot.bot import text_handler

            await text_handler(update, context)

        enqueue_status_update.assert_awaited_once()
        send_when_ready.assert_not_awaited()
        mock_sm.send_to_window.assert_awaited_once_with("@1", "hi")
        safe_reply.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_bound_topic_reports_when_direct_send_fails(self):
        update = _make_text_update("hi")
        context = _make_context()

        fake_window = MagicMock()
        fake_window.window_id = "@1"
        fake_window.window_name = "Projects"
        fake_window.cwd = "/tmp/project"

        with (
            patch("ccbot.bot.is_user_allowed", return_value=True),
            patch("ccbot.bot._get_thread_id", return_value=42),
            patch("ccbot.bot.session_manager") as mock_sm,
            patch("ccbot.bot.tmux_manager") as mock_tmux,
            patch("ccbot.bot.enqueue_status_update", new_callable=AsyncMock),
            patch("ccbot.bot.safe_reply", new_callable=AsyncMock) as safe_reply,
            patch(
                "ccbot.bot._send_to_window_when_codex_ready",
                new_callable=AsyncMock,
            ) as send_when_ready,
            patch("ccbot.bot._cancel_bash_capture"),
        ):
            mock_sm.get_window_for_thread.return_value = "@1"
            mock_sm.window_has_usage_limit_exceeded = AsyncMock(return_value=False)
            mock_sm.send_to_window = AsyncMock(return_value=(False, "send failed"))
            mock_tmux.find_window_by_id = AsyncMock(return_value=fake_window)
            mock_tmux.capture_pane = AsyncMock(return_value="")

            from ccbot.bot import text_handler

            await text_handler(update, context)

        send_when_ready.assert_not_awaited()
        mock_sm.send_to_window.assert_awaited_once_with("@1", "hi")
        safe_reply.assert_awaited_once_with(
            update.message,
            "❌ send failed",
        )

    @pytest.mark.asyncio
    async def test_bound_topic_queues_busy_codex_by_default(self):
        update = _make_text_update("wait for next turn")
        context = _make_context()

        fake_window = MagicMock()
        fake_window.window_id = "@1"
        fake_window.window_name = "Projects"
        fake_window.cwd = "/tmp/project"

        with (
            patch("ccbot.bot.is_user_allowed", return_value=True),
            patch("ccbot.bot._get_thread_id", return_value=42),
            patch("ccbot.bot.session_manager") as mock_sm,
            patch("ccbot.bot.tmux_manager") as mock_tmux,
            patch("ccbot.bot.enqueue_status_update", new_callable=AsyncMock),
            patch("ccbot.bot.safe_reply", new_callable=AsyncMock) as safe_reply,
            patch(
                "ccbot.bot._send_to_window_when_codex_ready",
                new_callable=AsyncMock,
                return_value=(True, "Sent"),
            ) as send_when_ready,
            patch("ccbot.bot._cancel_bash_capture"),
        ):
            mock_sm.get_window_for_thread.return_value = "@1"
            mock_sm.window_has_usage_limit_exceeded = AsyncMock(return_value=False)
            mock_sm.send_to_window = AsyncMock(return_value=(True, "Sent"))
            mock_tmux.find_window_by_id = AsyncMock(return_value=fake_window)
            mock_tmux.capture_pane = AsyncMock(
                return_value="• Working (12s • esc to interrupt)"
            )
            mock_tmux.send_keys = AsyncMock(return_value=True)

            from ccbot.bot import text_handler

            await text_handler(update, context)

        mock_tmux.send_keys.assert_not_awaited()
        send_when_ready.assert_not_awaited()
        mock_sm.send_to_window.assert_awaited_once_with("@1", "wait for next turn")
        safe_reply.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_interrupt_command_interrupts_busy_codex_before_forwarding_text(self):
        update = _make_text_update("/interrupt interrupt me")
        context = _make_context()

        fake_window = MagicMock()
        fake_window.window_id = "@1"
        fake_window.window_name = "Projects"
        fake_window.cwd = "/tmp/project"

        with (
            patch("ccbot.bot.is_user_allowed", return_value=True),
            patch("ccbot.bot._get_thread_id", return_value=42),
            patch("ccbot.bot.session_manager") as mock_sm,
            patch("ccbot.bot.tmux_manager") as mock_tmux,
            patch("ccbot.bot.enqueue_status_update", new_callable=AsyncMock),
            patch("ccbot.bot.safe_reply", new_callable=AsyncMock) as safe_reply,
            patch(
                "ccbot.bot._send_to_window_when_codex_ready",
                new_callable=AsyncMock,
                return_value=(True, "Sent"),
            ) as send_when_ready,
            patch(
                "ccbot.bot._refresh_session_map_after_first_prompt",
                new_callable=AsyncMock,
            ),
            patch("ccbot.bot.mark_window_working", new_callable=AsyncMock),
            patch("ccbot.bot._cancel_bash_capture"),
            patch("ccbot.bot.asyncio.sleep", new_callable=AsyncMock),
        ):
            mock_sm.get_window_for_thread.return_value = "@1"
            mock_sm.window_has_usage_limit_exceeded = AsyncMock(return_value=False)
            mock_sm.send_to_window = AsyncMock(return_value=(True, "Sent"))
            mock_tmux.find_window_by_id = AsyncMock(return_value=fake_window)
            mock_tmux.capture_pane = AsyncMock(
                return_value="• Working (12s • esc to interrupt)"
            )
            mock_tmux.send_keys = AsyncMock(return_value=True)

            from ccbot.bot import interrupt_command

            await interrupt_command(update, context)

        mock_tmux.send_keys.assert_awaited_once_with("@1", "\x1b", enter=False)
        send_when_ready.assert_awaited_once_with(
            "@1",
            "interrupt me",
            timeout=15.0,
            interval=0.25,
        )
        mock_sm.send_to_window.assert_not_awaited()
        safe_reply.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_bound_topic_recovers_missing_window_and_forwards_text(self):
        update = _make_text_update("continue")
        context = _make_context()
        old_state = WindowState(
            session_id="session-1",
            cwd="/tmp/project",
            window_name="Projects-2",
        )
        new_state = WindowState()

        with (
            patch("ccbot.bot.is_user_allowed", return_value=True),
            patch("ccbot.bot._get_thread_id", return_value=42),
            patch("ccbot.bot.session_manager") as mock_sm,
            patch("ccbot.bot.tmux_manager") as mock_tmux,
            patch("ccbot.bot.safe_reply", new_callable=AsyncMock) as safe_reply,
            patch(
                "ccbot.bot._send_to_window_when_codex_ready",
                new_callable=AsyncMock,
                return_value=(True, "Sent"),
            ) as send_when_ready,
            patch(
                "ccbot.bot._refresh_session_map_after_first_prompt",
                new_callable=AsyncMock,
            ) as refresh_session_map,
        ):
            mock_sm.get_window_for_thread.return_value = "@2"
            mock_sm.get_display_name.return_value = "Projects-2"
            mock_sm.window_states = {"@2": old_state}
            mock_sm.user_window_offsets = {12345: {"@2": 99}}
            mock_sm.wait_for_session_map_entry = AsyncMock(return_value=False)
            mock_sm.get_window_state.return_value = new_state
            mock_sm.remove_session_map_entry = AsyncMock()
            mock_tmux.find_window_by_id = AsyncMock(return_value=None)
            mock_tmux.create_window = AsyncMock(
                return_value=(
                    True,
                    "Created window 'Projects-2'",
                    "Projects-2",
                    "@3",
                )
            )

            from ccbot.bot import text_handler

            await text_handler(update, context)

        mock_tmux.create_window.assert_awaited_once_with(
            "/tmp/project",
            window_name="Projects-2",
            resume_session_id="session-1",
            account_name=None,
        )
        mock_sm.bind_thread.assert_called_once_with(
            12345, 42, "@3", window_name="Projects-2"
        )
        mock_sm.register_session_to_window.assert_called_once_with(
            "@3",
            "session-1",
            "/tmp/project",
            window_name="Projects-2",
            persist_session_map=True,
        )
        mock_sm.remove_session_map_entry.assert_awaited_once_with("@2")
        mock_sm.remove_window_state.assert_called_once_with("@2")
        assert mock_sm.user_window_offsets == {12345: {"@3": 99}}
        assert new_state.session_id == "session-1"
        assert new_state.cwd == "/tmp/project"
        send_when_ready.assert_awaited_once_with("@3", "continue")
        refresh_session_map.assert_awaited_once_with("@3")
        safe_reply.assert_awaited_once()
