"""Tests for the domain-organized callback query registry and router."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from telegram_agent_bot import bot as bot_module
from telegram_agent_bot.handlers import callback_router
from telegram_agent_bot.handlers.callback_data import CB_KEYS_PREFIX
from telegram_agent_bot.handlers.callback_router import CallbackContext


def _make_ctx(data: str, *, deps: object | None = None) -> CallbackContext:
    query = MagicMock()
    query.data = data
    query.answer = AsyncMock()
    update = MagicMock()
    context = MagicMock()
    context.user_data = {}
    user = MagicMock()
    user.id = 12345
    return CallbackContext(
        update=update,
        context=context,
        query=query,
        data=data,
        user=user,
        thread_id=42,
        deps=deps or bot_module,
    )


class TestCallbackRegistry:
    @pytest.mark.asyncio
    async def test_exact_takes_priority_over_prefix_and_unknown_returns_false(
        self,
    ) -> None:
        registry = bot_module.CallbackRegistry()
        called: list[str] = []

        async def on_exact(ctx: CallbackContext) -> None:
            called.append(f"exact:{ctx.data}")

        async def on_prefix(ctx: CallbackContext) -> None:
            called.append(f"prefix:{ctx.data}")

        registry.register_prefix("demo:", on_prefix)
        registry.register_exact("demo:exact", on_exact)

        assert await registry.dispatch(_make_ctx("demo:exact")) is True
        assert await registry.dispatch(_make_ctx("demo:other")) is True
        assert await registry.dispatch(_make_ctx("unknown:key")) is False
        assert called == ["exact:demo:exact", "prefix:demo:other"]

    @pytest.mark.asyncio
    async def test_noop_and_invalid_quick_keys_are_handled(self) -> None:
        noop_ctx = _make_ctx("noop")
        assert await bot_module.DEFAULT_CALLBACK_REGISTRY.dispatch(noop_ctx) is True
        noop_ctx.query.answer.assert_awaited_once_with()

        bad_key_ctx = _make_ctx(
            f"{CB_KEYS_PREFIX}missing_colon",
            deps=SimpleNamespace(_KEYS_SEND_MAP={}),
        )
        assert await bot_module.DEFAULT_CALLBACK_REGISTRY.dispatch(bad_key_ctx) is True
        bad_key_ctx.query.answer.assert_awaited_once_with("Invalid data")

    def test_callback_router_keeps_registry_wiring_in_bot_module(self) -> None:
        assert not hasattr(callback_router, "CallbackRegistry")
        assert not hasattr(callback_router, "build_default_callback_registry")
        assert not hasattr(callback_router, "DEFAULT_CALLBACK_REGISTRY")
