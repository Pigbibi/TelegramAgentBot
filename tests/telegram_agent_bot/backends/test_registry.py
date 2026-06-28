from __future__ import annotations

import sys
import types
from collections.abc import Awaitable, Callable
from typing import Any

import pytest

from telegram_agent_bot.backends.base import (
    AgentTarget,
    BackendInfo,
    CreateSessionRequest,
    CreateSessionResult,
    SendResult,
)
from telegram_agent_bot.backends.registry import (
    available_backends,
    load_backend,
    load_backend_plugins,
    register_backend,
)


class DummyBackend:
    backend_id = "dummy"

    def info(self) -> BackendInfo:
        return BackendInfo(
            backend_id=self.backend_id,
            display_name="Dummy",
            mode="test",
        )

    def prepare(self) -> None:
        pass

    async def start(self, message_callback: Callable[[Any], Awaitable[None]]) -> None:
        pass

    async def stop(self) -> None:
        pass

    async def create_session(
        self,
        request: CreateSessionRequest,
    ) -> CreateSessionResult:
        return CreateSessionResult(
            ok=True,
            message="created",
            target=AgentTarget("dummy", "local"),
        )

    async def send_message(self, target: AgentTarget, text: str) -> SendResult:
        return SendResult(True, "sent")

    async def send_control(self, target: AgentTarget, key: str) -> SendResult:
        return SendResult(True, "sent")

    async def capture(self, target: AgentTarget, *, with_ansi: bool = False) -> str:
        return "capture"


def test_builtin_local_backend_is_available() -> None:
    assert "local" in available_backends()


def test_register_and_load_backend_without_cache() -> None:
    register_backend("dummy-test", DummyBackend)

    backend = load_backend("dummy-test", use_cache=False)

    assert isinstance(backend, DummyBackend)
    assert backend.info().backend_id == "dummy"


def test_module_plugin_can_register_backends(monkeypatch: pytest.MonkeyPatch) -> None:
    module = types.ModuleType("telegram_agent_bot_test_plugin")

    def register_backends(register):
        register("module-dummy", DummyBackend)

    setattr(module, "register_backends", register_backends)
    monkeypatch.setitem(sys.modules, module.__name__, module)

    load_backend_plugins([module.__name__])
    backend = load_backend("module-dummy", use_cache=False)

    assert isinstance(backend, DummyBackend)


def test_unknown_backend_reports_available_names() -> None:
    with pytest.raises(ValueError, match="Unknown agent backend"):
        load_backend("does-not-exist", use_cache=False)
