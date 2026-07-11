"""Tests for Telegram account/login management commands."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _make_update(user_id: int = 12345, thread_id: int = 42) -> MagicMock:
    update = MagicMock()
    update.effective_user = MagicMock(id=user_id)
    update.effective_chat = MagicMock(id=-1001234567890, type="supergroup")
    update.message = MagicMock()
    update.message.message_thread_id = thread_id
    return update


def _make_context(args: list[str]) -> MagicMock:
    context = MagicMock()
    context.args = args
    context.bot = AsyncMock()
    context.user_data = {}
    return context


def test_extract_device_login_details_strips_ansi() -> None:
    from telegram_agent_bot.bot import _extract_device_login_details

    output = (
        "\x1b[32mhttps://auth.openai.com/activate?user_code=ABCD-EFGH\x1b[0m\n"
        "Enter this one-time code\n"
        "ABCD-EFGH\n"
    )

    assert _extract_device_login_details(output) == (
        "https://auth.openai.com/activate?user_code=ABCD-EFGH",
        "ABCD-EFGH",
    )


def test_agent_login_args_use_codex_device_auth() -> None:
    from telegram_agent_bot import bot as bot_module

    with (
        patch.object(bot_module.config, "agent_type", "codex"),
        patch.object(bot_module.config, "codex_command", "codex"),
        patch("telegram_agent_bot.bot.shutil.which", return_value="/usr/bin/codex"),
    ):
        assert bot_module._agent_login_args() == [
            "/usr/bin/codex",
            "login",
            "--device-auth",
        ]


def test_agent_login_args_use_claude_auth_login() -> None:
    from telegram_agent_bot import bot as bot_module

    with (
        patch.object(bot_module.config, "agent_type", "claude"),
        patch.object(bot_module.config, "codex_command", "claude"),
        patch("telegram_agent_bot.bot.shutil.which", return_value="/usr/bin/claude"),
    ):
        assert bot_module._agent_login_args() == [
            "/usr/bin/claude",
            "auth",
            "login",
        ]


def test_explicit_agent_login_args_ignore_global_agent_type() -> None:
    from telegram_agent_bot import bot as bot_module

    with (
        patch.object(bot_module.config, "agent_type", "codex"),
        patch.object(bot_module.config, "claude_command", "claude"),
        patch.object(bot_module.config, "codex_cli_command", "codex"),
        patch(
            "telegram_agent_bot.bot.shutil.which",
            side_effect=lambda value: f"/usr/bin/{value}",
        ),
    ):
        assert bot_module._agent_login_args("claude") == [
            "/usr/bin/claude",
            "auth",
            "login",
        ]
        assert bot_module._agent_login_args("codex") == [
            "/usr/bin/codex",
            "login",
            "--device-auth",
        ]


@pytest.mark.asyncio
async def test_agent_account_list_reports_status() -> None:
    update = _make_update()
    context = _make_context([])

    with (
        patch("telegram_agent_bot.bot.is_user_allowed", return_value=True),
        patch("telegram_agent_bot.bot.list_account_names", return_value=["backup"]),
        patch("telegram_agent_bot.bot.get_current_account_name", return_value=None),
        patch(
            "telegram_agent_bot.bot.safe_reply", new_callable=AsyncMock
        ) as safe_reply,
    ):
        from telegram_agent_bot.bot import agent_account_command

        await agent_account_command(update, context)

    safe_reply.assert_awaited_once()
    text = safe_reply.await_args.args[1]
    assert "Automatic quota rotation: disabled" in text
    assert "service user's default CODEX_HOME" in text
    assert "`backup`" in text


@pytest.mark.asyncio
async def test_agent_account_use_selects_saved_account() -> None:
    update = _make_update()
    context = _make_context(["use", "backup"])

    with (
        patch("telegram_agent_bot.bot.is_user_allowed", return_value=True),
        patch("telegram_agent_bot.bot.list_account_names", return_value=["backup"]),
        patch("telegram_agent_bot.bot.remember_current_account") as remember,
        patch(
            "telegram_agent_bot.bot.safe_reply", new_callable=AsyncMock
        ) as safe_reply,
    ):
        from telegram_agent_bot.bot import agent_account_command

        await agent_account_command(update, context)

    remember.assert_called_once_with("backup", "codex")
    safe_reply.assert_awaited_once()
    assert (
        "New sessions will use saved account `backup`" in safe_reply.await_args.args[1]
    )


@pytest.mark.asyncio
async def test_agent_login_command_rejects_invalid_account_name() -> None:
    update = _make_update()
    context = _make_context(["../bad"])

    with (
        patch("telegram_agent_bot.bot.is_user_allowed", return_value=True),
        patch(
            "telegram_agent_bot.bot.safe_reply", new_callable=AsyncMock
        ) as safe_reply,
    ):
        from telegram_agent_bot.bot import agent_login_command

        await agent_login_command(update, context)

    safe_reply.assert_awaited_once()
    assert "Invalid account name" in safe_reply.await_args.args[1]
