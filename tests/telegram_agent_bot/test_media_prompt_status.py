from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest
from telegram.error import TimedOut

from telegram_agent_bot.backends.base import AgentTarget
from telegram_agent_bot.backends.files import FileUploadResult


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
async def test_download_telegram_media_retries_transient_timeout(tmp_path):
    from telegram_agent_bot import bot as bot_module

    media = MagicMock()
    file_path = tmp_path / "photo.jpg"
    tg_file = MagicMock()
    tg_file.download_to_drive = AsyncMock()
    media.get_file = AsyncMock(side_effect=[TimedOut("Timed out"), tg_file])

    expected_timeout_kwargs = {
        "connect_timeout": bot_module.MEDIA_DOWNLOAD_CONNECT_TIMEOUT_SECONDS,
        "read_timeout": bot_module.MEDIA_DOWNLOAD_READ_TIMEOUT_SECONDS,
        "write_timeout": bot_module.MEDIA_DOWNLOAD_WRITE_TIMEOUT_SECONDS,
        "pool_timeout": bot_module.MEDIA_DOWNLOAD_POOL_TIMEOUT_SECONDS,
    }

    with patch("telegram_agent_bot.bot.asyncio.sleep", new_callable=AsyncMock) as sleep:
        await bot_module._download_telegram_media(media, file_path, label="photo")

    assert media.get_file.await_args_list == [
        call(**expected_timeout_kwargs),
        call(**expected_timeout_kwargs),
    ]
    tg_file.download_to_drive.assert_awaited_once_with(
        file_path, **expected_timeout_kwargs
    )
    sleep.assert_awaited_once_with(bot_module.MEDIA_DOWNLOAD_RETRY_DELAY_SECONDS)


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
        patch("telegram_agent_bot.bot.is_user_allowed", return_value=True),
        patch("telegram_agent_bot.bot._get_thread_id", return_value=42),
        patch("telegram_agent_bot.bot._IMAGES_DIR", tmp_path),
        patch("telegram_agent_bot.bot.session_manager") as session_manager,
        patch("telegram_agent_bot.bot.tmux_manager") as tmux_manager,
        patch(
            "telegram_agent_bot.bot._safe_send_typing_action", new_callable=AsyncMock
        ),
        patch(
            "telegram_agent_bot.bot.enqueue_status_update",
            new_callable=AsyncMock,
            side_effect=record_clear,
        ) as enqueue_status_update,
        patch(
            "telegram_agent_bot.bot._send_or_queue_agent_input",
            new_callable=AsyncMock,
            return_value=(True, "Sent", False),
        ),
        patch(
            "telegram_agent_bot.bot.safe_reply",
            new_callable=AsyncMock,
            side_effect=record_reply,
        ),
        patch(
            "telegram_agent_bot.bot.mark_window_working",
            new_callable=AsyncMock,
            side_effect=record_working,
        ) as mark_window_working,
    ):
        session_manager.get_window_for_thread.return_value = "@1"
        session_manager.window_has_usage_limit_exceeded = AsyncMock(return_value=False)
        tmux_manager.find_window_by_id = AsyncMock(return_value=window)

        from telegram_agent_bot.bot import photo_handler

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
async def test_photo_prompt_uploads_file_for_remote_target(tmp_path):
    update = _make_base_update()
    context = _make_context()
    update.message.caption = "check remote"

    photo = MagicMock()
    photo.file_unique_id = "photo1"
    tg_file = MagicMock()
    tg_file.download_to_drive = AsyncMock()
    photo.get_file = AsyncMock(return_value=tg_file)
    update.message.photo = [photo]

    remote_target = AgentTarget("socket-cluster", "macbook", session_id="remote-1")

    with (
        patch("telegram_agent_bot.bot.is_user_allowed", return_value=True),
        patch("telegram_agent_bot.bot._get_thread_id", return_value=42),
        patch("telegram_agent_bot.bot._IMAGES_DIR", tmp_path),
        patch("telegram_agent_bot.bot.session_manager") as session_manager,
        patch(
            "telegram_agent_bot.bot._safe_send_typing_action", new_callable=AsyncMock
        ),
        patch(
            "telegram_agent_bot.bot.upload_agent_file",
            new_callable=AsyncMock,
            return_value=FileUploadResult(ok=True, path="/remote/uploads/photo1.jpg"),
        ) as upload_agent_file,
        patch(
            "telegram_agent_bot.bot._send_message_to_agent",
            new_callable=AsyncMock,
            return_value=(True, "Sent"),
        ) as send_message,
        patch(
            "telegram_agent_bot.bot.safe_reply", new_callable=AsyncMock
        ) as safe_reply,
        patch(
            "telegram_agent_bot.bot.mark_window_working", new_callable=AsyncMock
        ) as mark_window_working,
        patch(
            "telegram_agent_bot.bot.enqueue_status_update", new_callable=AsyncMock
        ) as enqueue_status_update,
    ):
        session_manager.get_window_for_thread.return_value = None
        session_manager.resolve_target_for_thread.return_value = remote_target

        from telegram_agent_bot.bot import photo_handler

        await photo_handler(update, context)

    upload_agent_file.assert_awaited_once()
    upload_args = upload_agent_file.await_args.args
    upload_kwargs = upload_agent_file.await_args.kwargs
    assert upload_args[:3] == (12345, 42, "")
    assert upload_kwargs["filename"].endswith("_photo1.jpg")
    send_message.assert_awaited_once_with(
        12345,
        42,
        "",
        "check remote\n\n(image attached: /remote/uploads/photo1.jpg)",
    )
    safe_reply.assert_awaited_once()
    mark_window_working.assert_not_awaited()
    enqueue_status_update.assert_not_awaited()


@pytest.mark.asyncio
async def test_document_prompt_uploads_file_for_remote_target(tmp_path):
    update = _make_base_update()
    context = _make_context()
    update.message.caption = "check this file"

    document = MagicMock()
    document.file_unique_id = "doc1"
    document.file_name = "report.pdf"
    tg_file = MagicMock()
    tg_file.download_to_drive = AsyncMock()
    document.get_file = AsyncMock(return_value=tg_file)
    update.message.document = document

    remote_target = AgentTarget("socket-cluster", "macbook", session_id="remote-1")

    with (
        patch("telegram_agent_bot.bot.is_user_allowed", return_value=True),
        patch("telegram_agent_bot.bot._get_thread_id", return_value=42),
        patch("telegram_agent_bot.bot._FILES_DIR", tmp_path),
        patch("telegram_agent_bot.bot.session_manager") as session_manager,
        patch(
            "telegram_agent_bot.bot._safe_send_typing_action", new_callable=AsyncMock
        ),
        patch(
            "telegram_agent_bot.bot.upload_agent_file",
            new_callable=AsyncMock,
            return_value=FileUploadResult(ok=True, path="/remote/uploads/report.pdf"),
        ) as upload_agent_file,
        patch(
            "telegram_agent_bot.bot._send_message_to_agent",
            new_callable=AsyncMock,
            return_value=(True, "Sent"),
        ) as send_message,
        patch(
            "telegram_agent_bot.bot.safe_reply", new_callable=AsyncMock
        ) as safe_reply,
        patch(
            "telegram_agent_bot.bot.mark_window_working", new_callable=AsyncMock
        ) as mark_window_working,
        patch(
            "telegram_agent_bot.bot.enqueue_status_update", new_callable=AsyncMock
        ) as enqueue_status_update,
    ):
        session_manager.get_window_for_thread.return_value = None
        session_manager.resolve_target_for_thread.return_value = remote_target

        from telegram_agent_bot.bot import document_handler

        await document_handler(update, context)

    upload_agent_file.assert_awaited_once()
    upload_kwargs = upload_agent_file.await_args.kwargs
    assert upload_kwargs["filename"] == "report.pdf"
    send_message.assert_awaited_once_with(
        12345,
        42,
        "",
        "check this file\n\n(file attached: /remote/uploads/report.pdf)",
    )
    safe_reply.assert_awaited_once()
    mark_window_working.assert_not_awaited()
    enqueue_status_update.assert_not_awaited()


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
        patch("telegram_agent_bot.bot.is_user_allowed", return_value=True),
        patch("telegram_agent_bot.bot._get_thread_id", return_value=42),
        patch("telegram_agent_bot.bot.config.openai_api_key", "test-key"),
        patch("telegram_agent_bot.bot.session_manager") as session_manager,
        patch("telegram_agent_bot.bot.tmux_manager") as tmux_manager,
        patch(
            "telegram_agent_bot.bot.transcribe_voice",
            new_callable=AsyncMock,
            return_value="transcribed text",
        ),
        patch(
            "telegram_agent_bot.bot._safe_send_typing_action", new_callable=AsyncMock
        ),
        patch(
            "telegram_agent_bot.bot.enqueue_status_update",
            new_callable=AsyncMock,
            side_effect=record_clear,
        ) as enqueue_status_update,
        patch(
            "telegram_agent_bot.bot._send_or_queue_agent_input",
            new_callable=AsyncMock,
            return_value=(True, "Sent", False),
        ),
        patch(
            "telegram_agent_bot.bot.safe_reply",
            new_callable=AsyncMock,
            side_effect=record_reply,
        ) as safe_reply,
        patch(
            "telegram_agent_bot.bot.mark_window_working",
            new_callable=AsyncMock,
            side_effect=record_working,
        ) as mark_window_working,
    ):
        session_manager.get_window_for_thread.return_value = "@1"
        session_manager.resolve_target_for_thread.return_value = None
        session_manager.window_has_usage_limit_exceeded = AsyncMock(return_value=False)
        tmux_manager.find_window_by_id = AsyncMock(return_value=window)

        from telegram_agent_bot.bot import voice_handler

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
