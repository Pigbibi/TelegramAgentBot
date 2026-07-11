from unittest.mock import patch

from telegram_agent_bot.agent_profile import AgentProfile
from telegram_agent_bot.config import config
from telegram_agent_bot.tmux_manager import _agent_command_for_launch


def test_claude_code_alias_and_fast_effort_are_normalized():
    profile = AgentProfile(
        agent_type="claudecode",
        model="deepseek-v4-pro",
        reasoning_effort="fast",
    )

    assert profile.agent_type == "claude"
    assert profile.reasoning_effort == "low"
    assert profile.display_name == "Claude Code"


def test_claude_launch_uses_effort_flag_and_env_file(tmp_path):
    env_file = tmp_path / "claude.env"
    env_file.write_text("ANTHROPIC_BASE_URL=https://api.deepseek.com/anthropic\n")
    profile = AgentProfile(
        agent_type="claude",
        model="deepseek-v4-pro",
        reasoning_effort="low",
    )

    with (
        patch.object(config, "claude_command", "/usr/bin/claude"),
        patch.object(config, "claude_env_file", env_file),
    ):
        command = _agent_command_for_launch(profile)

    assert command == (
        f"set -a; . {env_file}; set +a; "
        "/usr/bin/claude --model deepseek-v4-pro --effort low"
    )


def test_codex_uses_config_override_for_reasoning_effort():
    profile = AgentProfile(
        agent_type="codex",
        model="gpt-5.3-codex",
        reasoning_effort="low",
    )

    with patch.object(config, "codex_cli_command", "/usr/bin/codex"):
        command = _agent_command_for_launch(profile)

    assert command == (
        '/usr/bin/codex --model gpt-5.3-codex -c model_reasoning_effort="low"'
    )
