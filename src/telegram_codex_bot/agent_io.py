"""Agent I/O helpers shared by Telegram handlers."""

from __future__ import annotations

from dataclasses import dataclass

from .backends.base import (
    AgentBackend,
    AgentTarget,
    CreateSessionRequest,
    CreateSessionResult,
)
from .backends.local import LocalTmuxBackend
from .backends.registry import get_configured_backend, load_backend
from .config import config
from .session import session_manager
from .tmux_manager import tmux_manager


@dataclass(frozen=True)
class CaptureResult:
    """Result of capturing an agent terminal."""

    target: AgentTarget
    text: str | None
    missing: bool = False


@dataclass(frozen=True)
class ControlResult:
    """Result of sending a control key to an agent terminal."""

    target: AgentTarget
    ok: bool
    message: str = ""
    missing: bool = False


@dataclass(frozen=True)
class MessageResult:
    """Result of sending text to an agent terminal."""

    target: AgentTarget
    ok: bool
    message: str = ""
    missing: bool = False


def local_target_from_window(window_id: str) -> AgentTarget:
    """Build a local target from a window ID and known session state."""
    state = session_manager.window_states.get(window_id)
    return LocalTmuxBackend.target_from_window(
        window_id,
        session_id=state.session_id if state else "",
    )


def target_for_context(
    user_id: int,
    thread_id: int | None,
    window_id: str,
) -> AgentTarget | None:
    """Resolve the backend target for a topic, falling back to local window IDs."""
    target = session_manager.resolve_target_for_thread(user_id, thread_id)
    if target:
        return target
    if window_id:
        return local_target_from_window(window_id)
    return None


def backend_for_target(target: AgentTarget) -> AgentBackend:
    """Return the backend responsible for a target."""
    backend = get_configured_backend()
    if backend.backend_id == target.backend_id:
        return backend
    return load_backend(
        target.backend_id,
        plugin_modules=config.backend_plugins,
    )


async def create_agent_session(
    *,
    cwd: str,
    window_name: str = "",
    resume_session_id: str = "",
    account_name: str = "",
) -> CreateSessionResult:
    """Create or resume an agent session through the configured backend."""
    backend = get_configured_backend()
    return await backend.create_session(
        CreateSessionRequest(
            cwd=cwd,
            window_name=window_name,
            resume_session_id=resume_session_id,
            account_name=account_name,
        )
    )


async def capture_agent_output(
    user_id: int,
    thread_id: int | None,
    window_id: str,
    *,
    with_ansi: bool = False,
) -> CaptureResult | None:
    """Capture visible output from the target bound to a topic/window.

    Local targets still verify the tmux window exists so existing "window gone"
    user feedback remains accurate. Non-local backends decide whether a missing
    capture means the target is unavailable or just has no visible output.
    """
    target = target_for_context(user_id, thread_id, window_id)
    if target is None:
        return None

    if target.backend_id == "local":
        local_window_id = target.window_id or window_id
        if not local_window_id:
            return CaptureResult(target=target, text=None, missing=True)
        window = await tmux_manager.find_window_by_id(local_window_id)
        if not window:
            return CaptureResult(target=target, text=None, missing=True)
        target = LocalTmuxBackend.target_from_window(
            window.window_id,
            session_id=target.session_id,
        )

    backend = backend_for_target(target)
    text = await backend.capture(target, with_ansi=with_ansi)
    return CaptureResult(target=target, text=text)


async def send_agent_control(
    user_id: int,
    thread_id: int | None,
    window_id: str,
    key: str,
) -> ControlResult | None:
    """Send one control key to the target bound to a topic/window."""
    target = target_for_context(user_id, thread_id, window_id)
    if target is None:
        return None

    if target.backend_id == "local":
        local_window_id = target.window_id or window_id
        if not local_window_id:
            return ControlResult(target=target, ok=False, missing=True)
        window = await tmux_manager.find_window_by_id(local_window_id)
        if not window:
            return ControlResult(target=target, ok=False, missing=True)
        target = LocalTmuxBackend.target_from_window(
            window.window_id,
            session_id=target.session_id,
        )

    backend = backend_for_target(target)
    result = await backend.send_control(target, key)
    return ControlResult(
        target=target,
        ok=result.ok,
        message=result.message,
    )


async def send_agent_message(
    user_id: int,
    thread_id: int | None,
    window_id: str,
    text: str,
) -> MessageResult | None:
    """Send user text to the target bound to a topic/window."""
    target = target_for_context(user_id, thread_id, window_id)
    if target is None:
        return None

    if target.backend_id == "local":
        local_window_id = target.window_id or window_id
        if not local_window_id:
            return MessageResult(target=target, ok=False, missing=True)
        window = await tmux_manager.find_window_by_id(local_window_id)
        if not window:
            return MessageResult(target=target, ok=False, missing=True)
        target = LocalTmuxBackend.target_from_window(
            window.window_id,
            session_id=target.session_id,
        )

    backend = backend_for_target(target)
    result = await backend.send_message(target, text)
    return MessageResult(
        target=target,
        ok=result.ok,
        message=result.message,
    )
