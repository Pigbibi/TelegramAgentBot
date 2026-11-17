"""Tests for modular handler packages and frozen launch specifications."""

from dataclasses import FrozenInstanceError
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from telegram import CallbackQuery, User

from telegram_agent_bot import bot as bot_module
from telegram_agent_bot.backends.base import AgentTarget, CreateSessionResult
from telegram_agent_bot.handlers import agent_login, native_input, window_lifecycle


def test_agent_and_window_launch_specs_are_frozen_and_serialize() -> None:
    spec = window_lifecycle.AgentLaunchSpec(
        cwd="/tmp/work",
        node_id="node-1",
        window_name="work",
        resume_session_id="sess-1",
        account_name="acct-a",
        agent_type="claude",
        model="sonnet",
        reasoning_effort="high",
        fast_mode=True,
        permission_mode="bypass",
    )
    assert spec.to_create_kwargs() == {
        "cwd": "/tmp/work",
        "window_name": "work",
        "resume_session_id": "sess-1",
        "account_name": "acct-a",
        "agent_type": "claude",
        "model": "sonnet",
        "reasoning_effort": "high",
        "fast_mode": True,
        "permission_mode": "bypass",
        "node_id": "node-1",
    }

    with pytest.raises(FrozenInstanceError):
        spec.cwd = "/tmp/other"  # type: ignore[misc]

    win_spec = window_lifecycle.WindowLaunchSpec(
        selected_path="/tmp/work",
        pending_thread_id=42,
        agent_type="claude",
        model="sonnet",
        fast_mode=True,
        node_id="node-1",
    )
    cfg = SimpleNamespace(
        agent_type="codex",
        claude_reasoning_effort="medium",
        codex_reasoning_effort="high",
    )
    profile = win_spec.resolve_profile(cfg=cfg)
    assert profile.agent_type == "claude"
    assert profile.reasoning_effort == "medium"
    assert profile.fast_mode is True

    agent_spec = win_spec.to_agent_launch_spec(profile, "acct-b")
    assert agent_spec == window_lifecycle.AgentLaunchSpec(
        cwd="/tmp/work",
        node_id="node-1",
        resume_session_id="",
        account_name="acct-b",
        agent_type="claude",
        model="sonnet",
        reasoning_effort="medium",
        fast_mode=True,
        permission_mode="ask",
    )

    with pytest.raises(FrozenInstanceError):
        win_spec.selected_path = "/tmp/other"  # type: ignore[misc]


def test_bot_module_reexports_extracted_types_and_state() -> None:
    assert bot_module.AgentLaunchSpec is window_lifecycle.AgentLaunchSpec
    assert bot_module.WindowLaunchSpec is window_lifecycle.WindowLaunchSpec
    assert bot_module._QueuedAgentInput is native_input.QueuedAgentInput
    assert bot_module._agent_input_queues is native_input._agent_input_queues
    assert bot_module._agent_login_tasks is agent_login._agent_login_tasks


@pytest.mark.asyncio
async def test_create_agent_target_and_local_window_from_spec() -> None:
    target = AgentTarget(backend_id="local", node_id="local", window_id="@12")
    create_session = AsyncMock(
        return_value=CreateSessionResult(
            ok=True,
            message="created",
            display_name="proj",
            target=target,
        )
    )
    runtime = SimpleNamespace(create_agent_session=create_session)
    spec = window_lifecycle.AgentLaunchSpec(cwd="/tmp/proj", agent_type="codex")

    ok, msg, display, wid, resolved_target = await window_lifecycle.create_agent_target(
        spec, runtime=runtime
    )
    assert (ok, msg, display, wid, resolved_target) == (
        True,
        "created",
        "proj",
        "@12",
        target,
    )
    create_session.assert_awaited_once_with(
        cwd="/tmp/proj",
        window_name="",
        resume_session_id="",
        account_name="",
        agent_type="codex",
        permission_mode="ask",
    )

    missing_wid_target = AgentTarget(backend_id="local", node_id="local", window_id="")
    runtime_missing_wid = SimpleNamespace(
        create_agent_session=AsyncMock(
            return_value=CreateSessionResult(
                ok=True,
                message="created",
                display_name="proj",
                target=missing_wid_target,
            )
        )
    )
    local_result = await window_lifecycle.create_agent_local_window(
        spec, runtime=runtime_missing_wid
    )
    assert local_result[0] is False
    assert "local window id" in local_result[1]
    assert local_result[3] == ""


@pytest.mark.asyncio
async def test_create_and_bind_window_with_window_launch_spec() -> None:
    target = AgentTarget(backend_id="local", node_id="local", window_id="@33")
    sm = MagicMock()
    sm.get_window_state.return_value = SimpleNamespace(account_name="")
    sm.wait_for_session_map_entry = AsyncMock(return_value=False)
    sm.resolve_chat_id.return_value = -100123

    cfg = SimpleNamespace(
        agent_type="codex",
        claude_reasoning_effort="medium",
        codex_reasoning_effort="high",
    )
    runtime = SimpleNamespace(
        config=cfg,
        session_manager=sm,
        get_current_account_name=lambda _agent: "work-acct",
        get_default_account_name=lambda _agent: "work-acct",
        remember_current_account=MagicMock(),
        create_agent_session=AsyncMock(
            return_value=CreateSessionResult(
                ok=True,
                message="ready",
                display_name="myproj",
                target=target,
            )
        ),
        safe_edit=AsyncMock(),
        safe_reply=AsyncMock(),
        safe_send=AsyncMock(),
        mark_window_working=AsyncMock(),
        enqueue_status_update=AsyncMock(),
        send_with_fallback=AsyncMock(),
        enable_fast_mode_if_requested=AsyncMock(),
    )

    query = MagicMock(spec=CallbackQuery)
    query.message = MagicMock()
    query.message.chat_id = -100123
    query.message.message_thread_id = 42
    query.answer = AsyncMock()
    context = MagicMock()
    context.bot = MagicMock()
    context.user_data = {
        "_pending_thread_id": 42,
        "_selected_path": "/tmp/myproj",
    }
    user = MagicMock(spec=User)
    user.id = 77
    spec = window_lifecycle.WindowLaunchSpec(
        selected_path="/tmp/myproj",
        pending_thread_id=42,
        agent_type="codex",
        model="gpt-5.4",
        reasoning_effort="high",
        answer_callback=False,
    )

    await window_lifecycle.create_and_bind_window(
        query, context, user, spec, runtime=runtime
    )

    sm.prepare_window_launch.assert_called_once_with(
        "@33",
        cwd="/tmp/myproj",
        window_name="myproj",
        account_name="work-acct",
        agent_type="codex",
        model="gpt-5.4",
        reasoning_effort="high",
        fast_mode=False,
    )
    sm.bind_thread_target.assert_called_once_with(77, 42, target, window_name="myproj")
    assert "_pending_thread_id" not in context.user_data


def test_agent_login_and_native_input_helpers_work_standalone() -> None:
    assert agent_login.agent_command_name("claude", "account") == "/claudeaccount"
    assert agent_login.agent_command_name("codex", "login") == "/codexlogin"
    assert "agentlogin" in agent_login.codex_auth_recovery_message()
    assert agent_login.extract_device_login_details(
        "Visit https://example.com/device and enter ABCD-EFGH"
    ) == ("https://example.com/device", "ABCD-EFGH")

    runtime = SimpleNamespace(
        session_manager=SimpleNamespace(conversation_owner_id=lambda u, _t: u),
    )
    key = native_input.agent_input_key(10, 42, "@5", runtime=runtime)
    assert key == (10, 42, "@5")
    assert native_input.same_agent_input_route((10, 42, "@5"), (10, 42, "@9")) is True
    assert native_input.same_agent_input_route((10, 42, "@5"), (10, 99, "@5")) is False
