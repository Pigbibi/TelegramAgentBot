"""Regression tests for keeping Telegram topics isolated by Codex session."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from telegram_agent_bot.backends.base import AgentTarget, CreateSessionResult
from telegram_agent_bot.handlers.callback_data import (
    CB_ASK_TRUST,
    CB_DIR_CONFIRM,
    CB_PROFILE_AGENT,
    CB_PROFILE_CONFIRM,
    CB_PROFILE_EFFORT,
    CB_SESSION_SELECT,
)
from telegram_agent_bot.handlers.directory_browser import (
    BROWSE_PATH_KEY,
    PROFILE_AGENT_KEY,
    PROFILE_EFFORT_KEY,
    PROFILE_FAST_MODE_KEY,
    PROFILE_MODEL_KEY,
    PROFILE_MODELS_KEY,
    SESSIONS_KEY,
)
from telegram_agent_bot.session import CodexSession


def _make_callback_update(data: str, thread_id: int = 42, user_id: int = 12345):
    """Build a minimal callback-query update in a forum topic."""
    update = MagicMock()
    update.effective_user = MagicMock()
    update.effective_user.id = user_id
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
    """Build a minimal callback context."""
    context = MagicMock()
    context.bot = AsyncMock()
    context.user_data = {}
    return context


@pytest.mark.asyncio
async def test_hook_trust_callback_sends_t_key():
    update, query = _make_callback_update(f"{CB_ASK_TRUST}@4")
    context = _make_context()

    with (
        patch("telegram_agent_bot.bot.is_user_allowed", return_value=True),
        patch("telegram_agent_bot.bot._get_thread_id", return_value=42),
        patch("telegram_agent_bot.bot.session_manager"),
        patch(
            "telegram_agent_bot.bot._send_control_to_agent",
            new_callable=AsyncMock,
            return_value=(True, "Sent"),
        ) as send_control,
        patch(
            "telegram_agent_bot.bot.handle_interactive_ui",
            new_callable=AsyncMock,
        ) as refresh_ui,
        patch("telegram_agent_bot.bot.asyncio.sleep", new_callable=AsyncMock),
    ):
        from telegram_agent_bot.bot import callback_handler

        await callback_handler(update, context)

    send_control.assert_awaited_once_with(12345, 42, "@4", "t")
    refresh_ui.assert_awaited_once_with(context.bot, 12345, "@4", 42)
    query.answer.assert_awaited_once_with("Trusted hooks")


@pytest.mark.asyncio
async def test_codex_profile_selection_refreshes_model_catalog():
    update, query = _make_callback_update(f"{CB_PROFILE_AGENT}codex")
    context = _make_context()

    with (
        patch("telegram_agent_bot.bot.is_user_allowed", return_value=True),
        patch("telegram_agent_bot.bot._get_thread_id", return_value=42),
        patch("telegram_agent_bot.bot.session_manager"),
        patch(
            "telegram_agent_bot.bot.refresh_model_catalog", new_callable=AsyncMock
        ) as refresh,
        patch(
            "telegram_agent_bot.bot._profile_models",
            return_value=("gpt-5.6-sol", "gpt-5.6-luna"),
        ),
        patch(
            "telegram_agent_bot.bot._show_agent_profile_settings",
            new_callable=AsyncMock,
        ) as show_settings,
    ):
        from telegram_agent_bot.bot import callback_handler

        await callback_handler(update, context)

    refresh.assert_awaited_once_with("codex")
    assert context.user_data[PROFILE_MODEL_KEY] == "gpt-5.6-sol"
    show_settings.assert_awaited_once_with(query, context)


@pytest.mark.asyncio
async def test_profile_rejects_effort_not_supported_by_selected_model():
    update, query = _make_callback_update(f"{CB_PROFILE_EFFORT}ultra")
    context = _make_context()
    context.user_data = {
        PROFILE_AGENT_KEY: "codex",
        PROFILE_MODEL_KEY: "gpt-5.6-luna",
    }

    with (
        patch("telegram_agent_bot.bot.is_user_allowed", return_value=True),
        patch("telegram_agent_bot.bot._get_thread_id", return_value=42),
        patch("telegram_agent_bot.bot.session_manager"),
        patch(
            "telegram_agent_bot.bot.config.codex_model_efforts",
            {"gpt-5.6-luna": ("low", "medium", "high", "xhigh", "max")},
        ),
        patch(
            "telegram_agent_bot.bot._show_agent_profile_settings",
            new_callable=AsyncMock,
        ) as show_settings,
    ):
        from telegram_agent_bot.bot import callback_handler

        await callback_handler(update, context)

    query.answer.assert_awaited_once_with(
        "Reasoning level unavailable", show_alert=True
    )
    show_settings.assert_not_awaited()


class TestSessionPickerIsolation:
    @pytest.mark.asyncio
    async def test_dir_confirm_shows_agent_picker_before_session_lookup(self):
        """Directory confirmation now selects an agent before scanning sessions."""
        update, query = _make_callback_update(CB_DIR_CONFIRM)
        context = _make_context()
        context.user_data = {
            BROWSE_PATH_KEY: "/tmp/project",
            "_pending_thread_id": 42,
            "_pending_thread_text": "hello",
        }
        with (
            patch("telegram_agent_bot.bot.is_user_allowed", return_value=True),
            patch("telegram_agent_bot.bot._get_thread_id", return_value=42),
            patch("telegram_agent_bot.bot.session_manager"),
            patch("telegram_agent_bot.bot.safe_edit", new_callable=AsyncMock),
        ):
            from telegram_agent_bot.bot import callback_handler

            await callback_handler(update, context)

        assert query.answer.await_args_list[0].args == ("Choose agent",)
        assert context.user_data["_selected_path"] == "/tmp/project"

    @pytest.mark.asyncio
    async def test_profile_confirmation_creates_claude_session_with_selected_settings(
        self,
    ):
        update, query = _make_callback_update(CB_PROFILE_CONFIRM)
        context = _make_context()
        context.user_data = {
            "_pending_thread_id": 42,
            "_selected_path": "/tmp/project",
            PROFILE_AGENT_KEY: "claude",
            PROFILE_MODEL_KEY: "deepseek-v4-pro",
            PROFILE_EFFORT_KEY: "low",
            PROFILE_FAST_MODE_KEY: False,
            PROFILE_MODELS_KEY: ["deepseek-v4-pro"],
        }

        with (
            patch("telegram_agent_bot.bot.is_user_allowed", return_value=True),
            patch("telegram_agent_bot.bot._get_thread_id", return_value=42),
            patch("telegram_agent_bot.bot.session_manager") as mock_sm,
            patch(
                "telegram_agent_bot.bot._create_and_bind_window", new_callable=AsyncMock
            ) as create,
            patch("telegram_agent_bot.bot.safe_edit", new_callable=AsyncMock),
        ):
            mock_sm.list_sessions_for_directory = AsyncMock(return_value=[])

            from telegram_agent_bot.bot import callback_handler

            await callback_handler(update, context)

        create.assert_awaited_once_with(
            query,
            context,
            update.effective_user,
            "/tmp/project",
            42,
            answer_callback=False,
            agent_type="claude",
            model="deepseek-v4-pro",
            reasoning_effort="low",
            fast_mode=False,
        )

    @pytest.mark.asyncio
    async def test_dir_confirm_acknowledges_callback_before_long_session_scan(self):
        """Directory confirm should acknowledge Telegram before scanning sessions."""
        update, query = _make_callback_update(CB_DIR_CONFIRM)
        context = _make_context()
        context.user_data = {
            BROWSE_PATH_KEY: "/tmp/project",
            "_pending_thread_id": 42,
        }

        with (
            patch("telegram_agent_bot.bot.is_user_allowed", return_value=True),
            patch("telegram_agent_bot.bot._get_thread_id", return_value=42),
            patch("telegram_agent_bot.bot.session_manager"),
            patch("telegram_agent_bot.bot.safe_edit", new_callable=AsyncMock),
        ):
            from telegram_agent_bot.bot import callback_handler

            await callback_handler(update, context)

        assert query.answer.await_count == 1
        assert query.answer.await_args_list[0].args == ("Choose agent",)
        assert context.user_data["_selected_path"] == "/tmp/project"

    @pytest.mark.asyncio
    async def test_session_select_rejects_session_already_active_elsewhere(self):
        """Session picker must reject selecting a session already bound to a topic."""
        update, query = _make_callback_update(f"{CB_SESSION_SELECT}0")
        context = _make_context()
        context.user_data = {
            SESSIONS_KEY: [
                CodexSession(
                    session_id="session-a",
                    summary="Existing chat",
                    message_count=12,
                    file_path="/tmp/project/session-a.jsonl",
                )
            ],
            "_selected_path": "/tmp/project",
            "_pending_thread_id": 42,
        }

        with (
            patch("telegram_agent_bot.bot.is_user_allowed", return_value=True),
            patch("telegram_agent_bot.bot._get_thread_id", return_value=42),
            patch("telegram_agent_bot.bot.session_manager") as mock_sm,
            patch(
                "telegram_agent_bot.bot._create_and_bind_window", new_callable=AsyncMock
            ) as create,
            patch(
                "telegram_agent_bot.bot.safe_edit", new_callable=AsyncMock
            ) as safe_edit,
        ):
            mock_sm.has_bound_thread_for_session.return_value = True

            from telegram_agent_bot.bot import callback_handler

            await callback_handler(update, context)

        create.assert_not_called()
        safe_edit.assert_called_once()
        assert "already active" in safe_edit.await_args.args[1]

    @pytest.mark.asyncio
    async def test_create_and_bind_window_does_not_rename_topic(self):
        """Creating a session should preserve the Telegram topic name."""

        class DummyCallbackQuery:
            def __init__(self) -> None:
                self.answer = AsyncMock()
                self.from_user = MagicMock(id=12345)

        class DummyUser:
            id = 12345

        query = DummyCallbackQuery()
        context = _make_context()
        user = DummyUser()

        with (
            patch("telegram.CallbackQuery", DummyCallbackQuery),
            patch("telegram.User", DummyUser),
            patch("telegram_agent_bot.bot.session_manager") as mock_sm,
            patch(
                "telegram_agent_bot.bot.safe_edit", new_callable=AsyncMock
            ) as safe_edit,
            patch("telegram_agent_bot.bot.get_default_account_name", return_value=""),
            patch(
                "telegram_agent_bot.bot.create_agent_session",
                new_callable=AsyncMock,
            ) as create_agent_session,
        ):
            create_agent_session.return_value = CreateSessionResult(
                ok=True,
                message="Created window 'project'",
                target=AgentTarget("local", "local", window_id="@1"),
                display_name="project",
            )
            mock_sm.resolve_chat_id.return_value = -1001234567890

            from telegram_agent_bot.bot import _create_and_bind_window

            await _create_and_bind_window(
                query,
                context,
                user,
                "/tmp/project",
                42,
            )

        context.bot.edit_forum_topic.assert_not_called()
        mock_sm.bind_thread_target.assert_called_once_with(
            12345,
            42,
            AgentTarget("local", "local", window_id="@1"),
            window_name="project",
        )
        safe_edit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_create_and_bind_window_accepts_remote_backend_target(self):
        """A backend plugin can create a session without a local tmux window."""

        class DummyCallbackQuery:
            def __init__(self) -> None:
                self.answer = AsyncMock()
                self.from_user = MagicMock(id=12345)

        class DummyUser:
            id = 12345

        query = DummyCallbackQuery()
        context = _make_context()
        context.user_data = {
            "_pending_thread_id": 42,
            "_pending_thread_text": "hello remote",
        }
        user = DummyUser()
        remote_target = AgentTarget("cluster", "macbook", session_id="remote-1")

        with (
            patch("telegram.CallbackQuery", DummyCallbackQuery),
            patch("telegram.User", DummyUser),
            patch("telegram_agent_bot.bot.session_manager") as mock_sm,
            patch(
                "telegram_agent_bot.bot.safe_edit", new_callable=AsyncMock
            ) as safe_edit,
            patch("telegram_agent_bot.bot.get_default_account_name", return_value=""),
            patch(
                "telegram_agent_bot.bot.create_agent_session",
                new_callable=AsyncMock,
            ) as create_agent_session,
            patch(
                "telegram_agent_bot.bot._send_message_to_agent",
                new_callable=AsyncMock,
                return_value=(True, "sent"),
            ) as send_message,
        ):
            create_agent_session.return_value = CreateSessionResult(
                ok=True,
                message="Created remote session on macbook",
                target=remote_target,
                display_name="macbook",
            )
            mock_sm.resolve_chat_id.return_value = -1001234567890

            from telegram_agent_bot.bot import _create_and_bind_window

            await _create_and_bind_window(
                query,
                context,
                user,
                "/tmp/project",
                42,
            )

        mock_sm.prepare_window_launch.assert_not_called()
        mock_sm.bind_thread_target.assert_called_once_with(
            12345,
            42,
            remote_target,
            window_name="macbook",
        )
        send_message.assert_awaited_once_with(12345, 42, "", "hello remote")
        safe_edit.assert_awaited_once()
