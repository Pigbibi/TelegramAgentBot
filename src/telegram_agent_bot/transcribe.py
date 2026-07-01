"""Voice-to-text transcription with multi-provider failover.

Supports OpenAI-compatible and Google Gemini providers, tried in the
order configured by AI_TRANSCRIPTION_PROVIDERS. If one provider fails
(HTTP error, empty response, network error), the next is attempted.

Key function: transcribe_voice(ogg_data) -> str
"""

from __future__ import annotations

import base64
import logging
from collections.abc import Awaitable, Callable

import httpx

from .config import config

logger = logging.getLogger(__name__)

_client: httpx.AsyncClient | None = None


class TranscriptionError(Exception):
    """All providers failed to transcribe the audio."""


def _get_client() -> httpx.AsyncClient:
    """Return a lazily-initialized httpx client singleton."""
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(timeout=60.0)
    return _client


async def _transcribe_openai(ogg_data: bytes) -> str:
    """Transcribe via an OpenAI-compatible /audio/transcriptions endpoint."""
    api_key = config.transcription_openai_api_key
    if not api_key:
        raise TranscriptionError("OpenAI provider: no API key configured")
    base_url = config.transcription_openai_base_url.rstrip("/")
    model = config.transcription_openai_model

    url = f"{base_url}/audio/transcriptions"
    client = _get_client()
    response = await client.post(
        url,
        headers={"Authorization": f"Bearer {api_key}"},
        files={"file": ("voice.ogg", ogg_data, "audio/ogg")},
        data={"model": model},
    )
    response.raise_for_status()
    text = response.json().get("text", "").strip()
    if not text:
        raise TranscriptionError("OpenAI provider: empty transcription")
    return text


async def _transcribe_google(ogg_data: bytes) -> str:
    """Transcribe via the Google Gemini API."""
    api_key = config.transcription_google_api_key
    if not api_key:
        raise TranscriptionError("Google provider: no API key configured")
    model = config.transcription_google_model

    url = (
        f"https://generativelanguage.googleapis.com/v1/models/{model}:generateContent"
        f"?key={api_key}"
    )
    b64_data = base64.b64encode(ogg_data).decode("ascii")
    payload = {
        "contents": [
            {
                "parts": [
                    {"inline_data": {"mime_type": "audio/ogg", "data": b64_data}},
                    {"text": "Transcribe the audio verbatim. Only output the text."},
                ]
            }
        ]
    }
    client = _get_client()
    response = await client.post(url, json=payload)
    response.raise_for_status()
    data = response.json()
    try:
        text = data["candidates"][0]["content"]["parts"][0]["text"].strip()
    except (KeyError, IndexError):
        raise TranscriptionError("Google provider: empty transcription")
    if not text:
        raise TranscriptionError("Google provider: empty transcription")
    return text


# Registered provider functions, tried in config.transcription_providers order
_PROVIDERS: dict[str, Callable[[bytes], Awaitable[str]]] = {
    "openai": _transcribe_openai,
    "google": _transcribe_google,
}


async def transcribe_voice(ogg_data: bytes) -> str:
    """Transcribe OGG voice data to text.

    Tries each configured provider in order. On failure, logs and
    attempts the next provider. Raises TranscriptionError when all
    providers are exhausted.
    """
    if not config.transcription_providers:
        raise TranscriptionError("No transcription providers configured")

    last_error: Exception | None = None
    for provider_id in config.transcription_providers:
        fn = _PROVIDERS.get(provider_id)
        if fn is None:
            logger.warning("Unknown transcription provider %r, skipping", provider_id)
            continue

        # Skip providers whose API key is not configured
        if provider_id == "openai" and not config.transcription_openai_api_key:
            logger.debug("Skipping openai provider: no API key")
            continue
        if provider_id == "google" and not config.transcription_google_api_key:
            logger.debug("Skipping google provider: no API key")
            continue

        try:
            text = await fn(ogg_data)
            if text:
                return text
        except TranscriptionError:
            raise
        except httpx.HTTPStatusError as exc:
            last_error = exc
            logger.warning(
                "Transcription provider %s failed (HTTP %s): %s",
                provider_id,
                exc.response.status_code,
                exc.response.text[:200],
            )
        except httpx.RequestError as exc:
            last_error = exc
            logger.warning(
                "Transcription provider %s unreachable: %s", provider_id, exc
            )
        except Exception as exc:
            last_error = exc
            logger.warning("Transcription provider %s error: %s", provider_id, exc)

    raise TranscriptionError(
        "All transcription providers failed" + (f": {last_error}" if last_error else "")
    ) from last_error


async def close_client() -> None:
    """Close the httpx client (call on shutdown)."""
    global _client
    if _client is not None and not _client.is_closed:
        await _client.aclose()
        _client = None
