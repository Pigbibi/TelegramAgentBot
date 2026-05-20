from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from telegram_codex_bot import agent_io
from telegram_codex_bot.agent_io import capture_agent_output
from telegram_codex_bot.backends.base import AgentTarget


class DummyBackend:
    def __init__(self, backend_id: str, text: str = "pane") -> None:
        self.backend_id = backend_id
        self.capture = AsyncMock(return_value=text)


@pytest.mark.asyncio
async def test_capture_local_target_verifies_window_and_uses_backend(monkeypatch):
    backend = DummyBackend("local", text="local pane")
    monkeypatch.setattr(agent_io, "get_configured_backend", lambda: backend)
    monkeypatch.setattr(
        agent_io.tmux_manager,
        "find_window_by_id",
        AsyncMock(return_value=SimpleNamespace(window_id="@2")),
    )
    monkeypatch.setattr(
        agent_io.session_manager,
        "resolve_target_for_thread",
        lambda user_id, thread_id: AgentTarget(
            "local",
            "local",
            session_id="sid-2",
            window_id="@2",
        ),
    )

    result = await capture_agent_output(100, 42, "@2", with_ansi=True)

    assert result is not None
    assert result.text == "local pane"
    assert result.missing is False
    backend.capture.assert_awaited_once_with(
        AgentTarget("local", "local", session_id="sid-2", window_id="@2"),
        with_ansi=True,
    )


@pytest.mark.asyncio
async def test_capture_local_target_reports_missing_window(monkeypatch):
    backend = DummyBackend("local")
    monkeypatch.setattr(agent_io, "get_configured_backend", lambda: backend)
    monkeypatch.setattr(
        agent_io.tmux_manager,
        "find_window_by_id",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        agent_io.session_manager,
        "resolve_target_for_thread",
        lambda user_id, thread_id: AgentTarget("local", "local", window_id="@2"),
    )

    result = await capture_agent_output(100, 42, "@2")

    assert result is not None
    assert result.missing is True
    backend.capture.assert_not_awaited()


@pytest.mark.asyncio
async def test_capture_remote_target_uses_target_backend(monkeypatch):
    configured_backend = DummyBackend("local")
    remote_backend = DummyBackend("cluster", text="remote pane")
    remote_target = AgentTarget("cluster", "macbook", session_id="remote-1")

    monkeypatch.setattr(agent_io, "get_configured_backend", lambda: configured_backend)
    monkeypatch.setattr(
        agent_io, "load_backend", lambda *args, **kwargs: remote_backend
    )
    monkeypatch.setattr(
        agent_io.session_manager,
        "resolve_target_for_thread",
        lambda user_id, thread_id: remote_target,
    )

    result = await capture_agent_output(100, 42, "")

    assert result is not None
    assert result.text == "remote pane"
    assert result.missing is False
    remote_backend.capture.assert_awaited_once_with(remote_target, with_ansi=False)
    configured_backend.capture.assert_not_awaited()
