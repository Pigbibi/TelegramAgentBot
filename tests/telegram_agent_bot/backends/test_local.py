from __future__ import annotations

from collections.abc import Awaitable, Callable
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock
from unittest.mock import AsyncMock

import pytest

from telegram_agent_bot.backends.local import LocalTmuxBackend
from telegram_agent_bot.backends.base import CreateSessionRequest
from telegram_agent_bot import session_monitor as session_monitor_module
from telegram_agent_bot import session as session_module
from telegram_agent_bot import tmux_manager as tmux_manager_module


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

    async def stop(self) -> None:
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


@pytest.mark.asyncio
async def test_local_backend_create_session_returns_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    create_window = AsyncMock(
        return_value=(True, "Created", "Project", "@7"),
    )
    monkeypatch.setattr(
        tmux_manager_module.tmux_manager, "create_window", create_window
    )

    result = await LocalTmuxBackend().create_session(
        CreateSessionRequest(
            cwd="/tmp/project",
            window_name="Project",
            resume_session_id="sid-1",
            account_name="plus1",
        )
    )

    assert result.ok is True
    assert result.target is not None
    assert result.target.backend_id == "local"
    assert result.target.node_id == "local"
    assert result.target.window_id == "@7"
    assert result.display_name == "Project"
    create_window.assert_awaited_once_with(
        "/tmp/project",
        window_name="Project",
        resume_session_id="sid-1",
        account_name="plus1",
    )


@pytest.mark.asyncio
async def test_local_backend_send_and_capture(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    send_to_window = AsyncMock(return_value=(True, "Sent"))
    send_control_key = AsyncMock(return_value=True)
    capture_pane = AsyncMock(return_value="pane")
    monkeypatch.setattr(
        session_module.session_manager, "send_to_window", send_to_window
    )
    monkeypatch.setattr(
        tmux_manager_module.tmux_manager, "send_control_key", send_control_key
    )
    monkeypatch.setattr(tmux_manager_module.tmux_manager, "capture_pane", capture_pane)

    backend = LocalTmuxBackend()
    target = backend.target_from_window("@9")

    send_result = await backend.send_message(target, "hello")
    control_result = await backend.send_control(target, "Enter")
    pane = await backend.capture(target, with_ansi=True)

    assert send_result.ok is True
    assert send_result.message == "Sent"
    assert control_result.ok is True
    assert pane == "pane"
    send_to_window.assert_awaited_once_with("@9", "hello", reject_busy=False)
    send_control_key.assert_awaited_once_with("@9", "Enter")
    capture_pane.assert_awaited_once_with("@9", with_ansi=True)
