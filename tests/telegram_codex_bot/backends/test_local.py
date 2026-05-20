from __future__ import annotations

from collections.abc import Awaitable, Callable
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from telegram_codex_bot.backends.local import LocalTmuxBackend
from telegram_codex_bot import session_monitor as session_monitor_module
from telegram_codex_bot import tmux_manager as tmux_manager_module


class DummyMonitor:
    def __init__(self) -> None:
        self.callback: Callable[[Any], Awaitable[None]] | None = None
        self.started = False
        self.stopped = False

    def set_message_callback(
        self,
        callback: Callable[[Any], Awaitable[None]],
    ) -> None:
        self.callback = callback

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.stopped = True


def test_local_backend_prepare_ensures_tmux_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    get_or_create_session = MagicMock(return_value=SimpleNamespace(session_name="test"))
    monkeypatch.setattr(
        tmux_manager_module.tmux_manager,
        "get_or_create_session",
        get_or_create_session,
    )

    LocalTmuxBackend().prepare()

    get_or_create_session.assert_called_once_with()


@pytest.mark.asyncio
async def test_local_backend_start_and_stop_monitor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dummy_monitor = DummyMonitor()
    monkeypatch.setattr(
        session_monitor_module,
        "SessionMonitor",
        lambda: dummy_monitor,
    )

    backend = LocalTmuxBackend()

    async def callback(_msg: Any) -> None:
        pass

    await backend.start(callback)

    assert backend.session_monitor is dummy_monitor
    assert dummy_monitor.callback is callback
    assert dummy_monitor.started is True

    await backend.stop()

    assert backend.session_monitor is None
    assert dummy_monitor.stopped is True
