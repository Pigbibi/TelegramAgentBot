"""Tests for provider model discovery and fallback behavior."""

from unittest.mock import AsyncMock, patch

import pytest


def test_models_url_handles_deepseek_anthropic_base_url() -> None:
    from telegram_agent_bot.model_catalog import _models_url

    assert (
        _models_url("https://api.deepseek.com/anthropic", provider="deepseek")
        == "https://api.deepseek.com/models"
    )


def test_models_url_handles_anthropic_v1_base_url() -> None:
    from telegram_agent_bot.model_catalog import _models_url

    assert (
        _models_url("https://api.anthropic.com/v1", provider="anthropic")
        == "https://api.anthropic.com/v1/models"
    )


def test_codex_model_filter_excludes_non_agent_models() -> None:
    from telegram_agent_bot.model_catalog import _codex_model_ids

    assert _codex_model_ids(
        ["gpt-5.6-luna", "gpt-4o-transcribe", "text-embedding-3-large", "o3"]
    ) == ["gpt-5.6-luna", "o3"]


def test_extract_codex_model_ids_uses_app_server_model_field() -> None:
    from telegram_agent_bot.model_catalog import _extract_codex_model_ids

    assert _extract_codex_model_ids(
        {"data": [{"model": "gpt-5.6-luna"}, {"model": "o3"}]}
    ) == ["gpt-5.6-luna", "o3"]


@pytest.mark.asyncio
async def test_refresh_model_catalog_updates_only_auto_lists(monkeypatch) -> None:
    from telegram_agent_bot import model_catalog

    monkeypatch.setattr(model_catalog.config, "model_discovery_enabled", True)
    monkeypatch.setattr(model_catalog.config, "codex_models_raw", "auto")
    monkeypatch.setattr(model_catalog.config, "claude_models_raw", "auto")
    monkeypatch.setattr(model_catalog.config, "codex_model", "gpt-5.6-luna")
    monkeypatch.setattr(model_catalog.config, "claude_model", "deepseek-v4-flash")
    monkeypatch.setattr(model_catalog.config, "codex_models", ("gpt-5.6-luna",))
    monkeypatch.setattr(model_catalog.config, "claude_models", ("deepseek-v4-flash",))
    monkeypatch.setattr(model_catalog, "_load_provider_env", lambda: {})
    codex_discovery = AsyncMock(return_value=["gpt-5.6-sol", "gpt-5.6-luna"])
    claude_discovery = AsyncMock(return_value=["deepseek-v4-pro"])
    monkeypatch.setattr(model_catalog, "_discover_codex_models", codex_discovery)
    monkeypatch.setattr(model_catalog, "_discover_claude_models", claude_discovery)

    await model_catalog.refresh_model_catalog()

    assert model_catalog.config.codex_models == ("gpt-5.6-luna", "gpt-5.6-sol")
    assert model_catalog.config.claude_models == (
        "deepseek-v4-flash",
        "deepseek-v4-pro",
        "sonnet",
        "opus",
    )
    codex_discovery.assert_awaited_once()
    claude_discovery.assert_awaited_once()


@pytest.mark.asyncio
async def test_refresh_model_catalog_respects_manual_lists(monkeypatch) -> None:
    from telegram_agent_bot import model_catalog

    monkeypatch.setattr(model_catalog.config, "model_discovery_enabled", True)
    monkeypatch.setattr(model_catalog.config, "codex_models_raw", "gpt-5.6-luna")
    monkeypatch.setattr(model_catalog.config, "claude_models_raw", "sonnet,opus")
    discovery = AsyncMock(return_value=["unexpected-model"])
    monkeypatch.setattr(model_catalog, "_discover_codex_models", discovery)
    monkeypatch.setattr(model_catalog, "_discover_claude_models", discovery)

    with patch.object(model_catalog.config, "codex_models", ("gpt-5.6-luna",)):
        with patch.object(model_catalog.config, "claude_models", ("sonnet", "opus")):
            await model_catalog.refresh_model_catalog()

    discovery.assert_not_awaited()
