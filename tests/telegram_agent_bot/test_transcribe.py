"""Unit tests for transcribe — multi-provider voice-to-text with failover."""

from unittest.mock import AsyncMock, patch

import httpx
import pytest

from telegram_agent_bot import transcribe


@pytest.fixture(autouse=True)
def _reset_client():
    """Ensure each test starts with a fresh client."""
    transcribe._client = None
    yield
    transcribe._client = None


@pytest.fixture
def mock_config():
    """Patch config with test values (openai provider by default)."""
    with patch.object(transcribe, "config") as cfg:
        cfg.transcription_providers = ("openai",)
        cfg.transcription_openai_api_key = "sk-test-key"
        cfg.transcription_openai_base_url = "https://api.openai.com/v1"
        cfg.transcription_openai_model = "gpt-4o-transcribe"
        cfg.transcription_google_api_key = ""
        cfg.transcription_google_model = "gemini-2.0-flash-lite"
        yield cfg


def _mock_response(*, json_data: dict, status_code: int = 200) -> httpx.Response:
    """Build a fake httpx.Response."""
    request = httpx.Request("POST", "https://api.openai.com/v1/audio/transcriptions")
    resp = httpx.Response(status_code=status_code, json=json_data, request=request)
    return resp


class TestTranscribeOpenAI:
    @pytest.mark.asyncio
    async def test_success(self, mock_config):
        resp = _mock_response(json_data={"text": "Hello world"})
        with patch.object(
            httpx.AsyncClient, "post", new_callable=AsyncMock, return_value=resp
        ) as mock_post:
            result = await transcribe.transcribe_voice(b"fake-ogg-data")

        assert result == "Hello world"
        mock_post.assert_called_once()
        call_kwargs = mock_post.call_args
        assert "Bearer sk-test-key" in str(call_kwargs)

    @pytest.mark.asyncio
    async def test_empty_transcription_raises(self, mock_config):
        resp = _mock_response(json_data={"text": ""})
        with patch.object(
            httpx.AsyncClient, "post", new_callable=AsyncMock, return_value=resp
        ):
            with pytest.raises(transcribe.TranscriptionError, match="empty transcription"):
                await transcribe.transcribe_voice(b"fake-ogg-data")

    @pytest.mark.asyncio
    async def test_whitespace_only_raises(self, mock_config):
        resp = _mock_response(json_data={"text": "   "})
        with patch.object(
            httpx.AsyncClient, "post", new_callable=AsyncMock, return_value=resp
        ):
            with pytest.raises(transcribe.TranscriptionError, match="empty transcription"):
                await transcribe.transcribe_voice(b"fake-ogg-data")

    @pytest.mark.asyncio
    async def test_missing_text_field_raises(self, mock_config):
        resp = _mock_response(json_data={"result": "something"})
        with patch.object(
            httpx.AsyncClient, "post", new_callable=AsyncMock, return_value=resp
        ):
            with pytest.raises(transcribe.TranscriptionError, match="empty transcription"):
                await transcribe.transcribe_voice(b"fake-ogg-data")

    @pytest.mark.asyncio
    async def test_api_error_raises(self, mock_config):
        resp = _mock_response(json_data={"error": "Unauthorized"}, status_code=401)
        with patch.object(
            httpx.AsyncClient, "post", new_callable=AsyncMock, return_value=resp
        ):
            with pytest.raises(transcribe.TranscriptionError):
                await transcribe.transcribe_voice(b"fake-ogg-data")

    @pytest.mark.asyncio
    async def test_custom_base_url(self, mock_config):
        mock_config.transcription_openai_base_url = "https://proxy.example.com/v1"
        resp = _mock_response(json_data={"text": "Transcribed"})
        with patch.object(
            httpx.AsyncClient, "post", new_callable=AsyncMock, return_value=resp
        ) as mock_post:
            result = await transcribe.transcribe_voice(b"fake-ogg-data")

        assert result == "Transcribed"
        url_arg = mock_post.call_args[0][0]
        assert url_arg == "https://proxy.example.com/v1/audio/transcriptions"

    @pytest.mark.asyncio
    async def test_base_url_trailing_slash_stripped(self, mock_config):
        mock_config.transcription_openai_base_url = "https://proxy.example.com/v1/"
        resp = _mock_response(json_data={"text": "OK"})
        with patch.object(
            httpx.AsyncClient, "post", new_callable=AsyncMock, return_value=resp
        ) as mock_post:
            await transcribe.transcribe_voice(b"fake-ogg-data")

        url_arg = mock_post.call_args[0][0]
        assert url_arg == "https://proxy.example.com/v1/audio/transcriptions"


class TestTranscribeMultiProvider:
    @pytest.mark.asyncio
    async def test_skips_provider_without_api_key(self, mock_config):
        """When openai has no api key and no other providers, raises error."""
        mock_config.transcription_openai_api_key = ""
        with pytest.raises(transcribe.TranscriptionError, match="All transcription providers failed"):
            await transcribe.transcribe_voice(b"fake-ogg-data")

    @pytest.mark.asyncio
    async def test_fallback_to_second_provider(self, mock_config):
        """When the first provider fails, falls through to the second."""
        mock_config.transcription_providers = ("openai", "openai")
        mock_config.transcription_openai_api_key = "sk-valid"

        fail_resp = _mock_response(json_data={"error": "Over quota"}, status_code=429)
        ok_resp = _mock_response(json_data={"text": "Fallback worked"})

        with patch.object(
            httpx.AsyncClient, "post", new_callable=AsyncMock
        ) as mock_post_method:
            mock_post_method.side_effect = [fail_resp, ok_resp]
            result = await transcribe.transcribe_voice(b"fake-ogg-data")

        assert result == "Fallback worked"
        assert mock_post_method.call_count == 2

    @pytest.mark.asyncio
    async def test_all_providers_fail_raises(self, mock_config):
        """When every provider fails, a TranscriptionError is raised."""
        mock_config.transcription_providers = ("openai",)
        fail_resp = _mock_response(json_data={"error": "Server error"}, status_code=500)
        with patch.object(
            httpx.AsyncClient, "post", new_callable=AsyncMock, return_value=fail_resp
        ):
            with pytest.raises(transcribe.TranscriptionError):
                await transcribe.transcribe_voice(b"fake-ogg-data")

    @pytest.mark.asyncio
    async def test_unknown_provider_skipped(self, mock_config):
        """Unknown provider IDs are logged and skipped."""
        mock_config.transcription_providers = ("nonexistent",)
        with pytest.raises(transcribe.TranscriptionError, match="All transcription providers failed"):
            await transcribe.transcribe_voice(b"fake-ogg-data")


class TestCloseClient:
    @pytest.mark.asyncio
    async def test_close_client_when_open(self):
        transcribe._client = httpx.AsyncClient()
        assert transcribe._client is not None
        await transcribe.close_client()
        assert transcribe._client is None

    @pytest.mark.asyncio
    async def test_close_client_when_none(self):
        assert transcribe._client is None
        await transcribe.close_client()
        assert transcribe._client is None
