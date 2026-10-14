from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
import pytest
from telegram_agent_bot.agent_profile import AgentProfile
from telegram_agent_bot import tmux_manager as tmux_module
from telegram_agent_bot import model_catalog, bot
from telegram_agent_bot.updater import CodexUpdateSettings, check_codex_update
from telegram_agent_bot.claude_api_session import BudgetDenied, _command_options


@pytest.mark.parametrize("agent", ["codex", "cursor"])
@pytest.mark.parametrize("permission", ["ask", "full"])
def test_retired_profiles_never_build_a_command(agent, permission):
    with pytest.raises(ValueError, match="retired"):
        tmux_module._agent_command_for_launch(
            AgentProfile(agent_type=agent, permission_mode=permission)
        )


@pytest.mark.parametrize("agent", ["claude", "claudeofficial"])
def test_full_mode_is_refused_for_remaining_agents(agent):
    with pytest.raises(ValueError, match="retired"):
        tmux_module._agent_command_for_launch(
            AgentProfile(agent_type=agent, permission_mode="full")
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("agent", ["codex", "cursor"])
async def test_retired_create_refuses_before_tmux_or_account_access(tmp_path, agent):
    manager = tmux_module.TmuxManager()
    with (
        patch.object(manager, "get_or_create_session") as start,
        patch.object(tmux_module, "ensure_account_home") as account,
    ):
        result = await manager.create_window(
            str(tmp_path),
            agent_type=agent,
            resume_session_id="synthetic",
            account_name="synthetic",
        )
    assert result[0] is False
    start.assert_not_called()
    account.assert_not_called()


@pytest.mark.asyncio
async def test_codex_discovery_never_starts_a_process():
    with patch.object(
        model_catalog.asyncio, "create_subprocess_exec", new_callable=AsyncMock
    ) as start:
        assert await model_catalog._discover_codex_app_server_models() == []
        assert await model_catalog._discover_codex_models({}) == []
    start.assert_not_awaited()


@pytest.mark.parametrize("agent", ["codex", "cursor"])
def test_retired_login_and_updater_never_execute(agent, tmp_path):
    with pytest.raises(ValueError, match="retired"):
        bot._agent_login_args(agent)
    runner = Mock()
    result = check_codex_update(
        CodexUpdateSettings(agent_type=agent, codex_executable=agent),
        runner=runner,
        cwd=tmp_path,
    )
    assert result.skipped_reason == "retired_cli"
    runner.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("agent", ["codex", "cursor", ""])
async def test_retired_or_unidentified_windows_receive_no_input(agent):
    from telegram_agent_bot.session import session_manager

    manager = tmux_module.TmuxManager()
    with (
        patch.dict(
            session_manager.window_states,
            {"@retired": SimpleNamespace(agent_type=agent)},
        ),
        patch.object(manager, "_paste_buffer_literal") as paste,
        patch.object(tmux_module.subprocess, "run") as run,
    ):
        assert not await manager.send_keys("@retired", "run command")
        assert not manager._send_control_key_sync("@retired", "Enter")
    paste.assert_not_called()
    run.assert_not_called()


def test_api_wrapper_refuses_permission_bypass():
    with pytest.raises(BudgetDenied):
        _command_options(["claude", "--dangerously-skip-permissions"])
