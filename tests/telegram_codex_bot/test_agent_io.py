from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from telegram_codex_bot import agent_io
from telegram_codex_bot.agent_io import (
    capture_agent_output,
    create_agent_session,
    send_agent_control,
    send_agent_message,
)
from telegram_codex_bot.backends.base import AgentTarget, CreateSessionResult


class DummyBackend:
    def __init__(self, backend_id: str, text: str = "pane") -> None:
        self.backend_id = backend_id
        self.capture = AsyncMock(return_value=text)
        self.send_control = AsyncMock(
            return_value=SimpleNamespace(ok=True, message="sent")
        )
        self.send_message = AsyncMock(
            return_value=SimpleNamespace(ok=True, message="sent")
        )
        self.create_session = AsyncMock(
            return_value=CreateSessionResult(
                ok=True,
                message="created",
                target=AgentTarget(backend_id, "local", window_id="@2"),
                display_name="Project",
            )
        )


@pytest.mark.asyncio
async def test_create_agent_session_uses_configured_backend(monkeypatch):
    backend = DummyBackend("local")
    monkeypatch.setattr(agent_io, "get_configured_backend", lambda: backend)

    result = await create_agent_session(
        cwd="/tmp/project",
        window_name="Project",
        resume_session_id="sid-1",
        account_name="plus",
    )

    assert result.ok is True
    assert result.target == AgentTarget("local", "local", window_id="@2")
    backend.create_session.assert_awaited_once()
    request = backend.create_session.await_args.args[0]
    assert request.cwd == "/tmp/project"
    assert request.window_name == "Project"
    assert request.resume_session_id == "sid-1"
    assert request.account_name == "plus"


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


@pytest.mark.asyncio
async def test_send_control_local_target_verifies_window_and_uses_backend(
    monkeypatch,
):
    backend = DummyBackend("local")
    monkeypatch.setattr(agent_io, "get_configured_backend", lambda: backend)
    monkeypatch.setattr(
        agent_io.tmux_manager,
        "find_window_by_id",
        AsyncMock(return_value=SimpleNamespace(window_id="@2")),
    )
    monkeypatch.setattr(
        agent_io.session_manager,
        "resolve_target_for_thread",
        lambda user_id, thread_id: AgentTarget("local", "local", window_id="@2"),
    )

    result = await send_agent_control(100, 42, "@2", "Escape")

    assert result is not None
    assert result.ok is True
    assert result.missing is False
    backend.send_control.assert_awaited_once_with(
        AgentTarget("local", "local", window_id="@2"),
        "Escape",
    )


@pytest.mark.asyncio
async def test_send_control_local_target_reports_missing_window(monkeypatch):
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

    result = await send_agent_control(100, 42, "@2", "Escape")

    assert result is not None
    assert result.ok is False
    assert result.missing is True
    backend.send_control.assert_not_awaited()


@pytest.mark.asyncio
async def test_send_control_remote_target_uses_target_backend(monkeypatch):
    configured_backend = DummyBackend("local")
    remote_backend = DummyBackend("cluster")
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

    result = await send_agent_control(100, 42, "", "Enter")

    assert result is not None
    assert result.ok is True
    assert result.missing is False
    remote_backend.send_control.assert_awaited_once_with(remote_target, "Enter")
    configured_backend.send_control.assert_not_awaited()


@pytest.mark.asyncio
async def test_send_message_local_target_verifies_window_and_uses_backend(
    monkeypatch,
):
    backend = DummyBackend("local")
    monkeypatch.setattr(agent_io, "get_configured_backend", lambda: backend)
    monkeypatch.setattr(
        agent_io.tmux_manager,
        "find_window_by_id",
        AsyncMock(return_value=SimpleNamespace(window_id="@2")),
    )
    monkeypatch.setattr(
        agent_io.session_manager,
        "resolve_target_for_thread",
        lambda user_id, thread_id: AgentTarget("local", "local", window_id="@2"),
    )

    result = await send_agent_message(100, 42, "@2", "hello")

    assert result is not None
    assert result.ok is True
    assert result.missing is False
    backend.send_message.assert_awaited_once_with(
        AgentTarget("local", "local", window_id="@2"),
        "hello",
    )


@pytest.mark.asyncio
async def test_send_message_local_target_reports_missing_window(monkeypatch):
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

    result = await send_agent_message(100, 42, "@2", "hello")

    assert result is not None
    assert result.ok is False
    assert result.missing is True
    backend.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_send_message_remote_target_uses_target_backend(monkeypatch):
    configured_backend = DummyBackend("local")
    remote_backend = DummyBackend("cluster")
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

    result = await send_agent_message(100, 42, "", "hello")

    assert result is not None
    assert result.ok is True
    assert result.missing is False
    remote_backend.send_message.assert_awaited_once_with(remote_target, "hello")
    configured_backend.send_message.assert_not_awaited()
