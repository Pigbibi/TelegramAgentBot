from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _make_context():
    context = MagicMock()
    context.bot = AsyncMock()
    return context


def _make_base_update(user_id: int = 12345, thread_id: int = 42):
    update = MagicMock()
    update.effective_user = MagicMock(id=user_id)
    update.message = MagicMock()
    update.message.message_thread_id = thread_id
    update.message.chat = MagicMock()
    update.message.chat.type = "supergroup"
    update.message.chat.send_action = AsyncMock()
    update.effective_chat = MagicMock()
    update.effective_chat.type = "supergroup"
    update.effective_chat.id = -1001234567890
    return update


@pytest.mark.asyncio
async def test_photo_prompt_recreates_working_status_after_confirmation(tmp_path):
    update = _make_base_update()
    context = _make_context()
    update.message.caption = "check this"

    photo = MagicMock()
    photo.file_unique_id = "photo1"
    tg_file = MagicMock()
    tg_file.download_to_drive = AsyncMock()
    photo.get_file = AsyncMock(return_value=tg_file)
    update.message.photo = [photo]

    window = SimpleNamespace(window_id="@1", cwd="/tmp/project")
    events: list[str] = []

    async def record_clear(*args, **kwargs):
        events.append("clear")

    async def record_reply(*args, **kwargs):
        events.append("reply")

    async def record_working(*args, **kwargs):
        events.append("working")

    with (
        patch("telegram_codex_bot.bot.is_user_allowed", return_value=True),
        patch("telegram_codex_bot.bot._get_thread_id", return_value=42),
        patch("telegram_codex_bot.bot._IMAGES_DIR", tmp_path),
        patch("telegram_codex_bot.bot.session_manager") as session_manager,
        patch("telegram_codex_bot.bot.tmux_manager") as tmux_manager,
        patch(
            "telegram_codex_bot.bot._safe_send_typing_action", new_callable=AsyncMock
        ),
        patch(
            "telegram_codex_bot.bot.enqueue_status_update",
            new_callable=AsyncMock,
            side_effect=record_clear,
        ) as enqueue_status_update,
        patch(
            "telegram_codex_bot.bot._send_message_to_agent",
            new_callable=AsyncMock,
            return_value=(True, "Sent"),
        ),
        patch(
            "telegram_codex_bot.bot.safe_reply",
            new_callable=AsyncMock,
            side_effect=record_reply,
        ),
        patch(
            "telegram_codex_bot.bot.mark_window_working",
            new_callable=AsyncMock,
            side_effect=record_working,
        ) as mark_window_working,
    ):
        session_manager.get_window_for_thread.return_value = "@1"
        session_manager.window_has_usage_limit_exceeded = AsyncMock(return_value=False)
        tmux_manager.find_window_by_id = AsyncMock(return_value=window)

        from telegram_codex_bot.bot import photo_handler

        await photo_handler(update, context)

    enqueue_status_update.assert_awaited_once_with(
        context.bot,
        12345,
        "@1",
        None,
        thread_id=42,
    )
    mark_window_working.assert_awaited_once_with(context.bot, 12345, "@1", 42)
    assert events == ["clear", "reply", "working"]


@pytest.mark.asyncio
async def test_voice_prompt_recreates_working_status_after_transcript_reply():
    update = _make_base_update()
    context = _make_context()

    voice = MagicMock()
    tg_file = MagicMock()
    tg_file.download_as_bytearray = AsyncMock(return_value=bytearray(b"voice"))
    voice.get_file = AsyncMock(return_value=tg_file)
    update.message.voice = voice

    window = SimpleNamespace(window_id="@1", cwd="/tmp/project")
    events: list[str] = []

    async def record_clear(*args, **kwargs):
        events.append("clear")

    async def record_reply(*args, **kwargs):
        events.append("reply")

    async def record_working(*args, **kwargs):
        events.append("working")

    with (
        patch("telegram_codex_bot.bot.is_user_allowed", return_value=True),
        patch("telegram_codex_bot.bot._get_thread_id", return_value=42),
        patch("telegram_codex_bot.bot.config.openai_api_key", "test-key"),
        patch("telegram_codex_bot.bot.session_manager") as session_manager,
        patch("telegram_codex_bot.bot.tmux_manager") as tmux_manager,
        patch(
            "telegram_codex_bot.bot.transcribe_voice",
            new_callable=AsyncMock,
            return_value="transcribed text",
        ),
        patch(
            "telegram_codex_bot.bot._safe_send_typing_action", new_callable=AsyncMock
        ),
        patch(
            "telegram_codex_bot.bot.enqueue_status_update",
            new_callable=AsyncMock,
            side_effect=record_clear,
        ) as enqueue_status_update,
        patch(
            "telegram_codex_bot.bot._send_message_to_agent",
            new_callable=AsyncMock,
            return_value=(True, "Sent"),
        ),
        patch(
            "telegram_codex_bot.bot.safe_reply",
            new_callable=AsyncMock,
            side_effect=record_reply,
        ) as safe_reply,
        patch(
            "telegram_codex_bot.bot.mark_window_working",
            new_callable=AsyncMock,
            side_effect=record_working,
        ) as mark_window_working,
    ):
        session_manager.get_window_for_thread.return_value = "@1"
        session_manager.resolve_target_for_thread.return_value = None
        session_manager.window_has_usage_limit_exceeded = AsyncMock(return_value=False)
        tmux_manager.find_window_by_id = AsyncMock(return_value=window)

        from telegram_codex_bot.bot import voice_handler

        await voice_handler(update, context)

    enqueue_status_update.assert_awaited_once_with(
        context.bot,
        12345,
        "@1",
        None,
        thread_id=42,
    )
    safe_reply.assert_awaited_once()
    assert safe_reply.await_args.args[1].endswith('"transcribed text"')
    mark_window_working.assert_awaited_once_with(context.bot, 12345, "@1", 42)
    assert events == ["clear", "reply", "working"]
