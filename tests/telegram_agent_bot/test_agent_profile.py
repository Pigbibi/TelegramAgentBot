from unittest.mock import patch

from telegram_agent_bot.agent_profile import AgentProfile
from telegram_agent_bot.config import config
from telegram_agent_bot.handlers.directory_browser import build_profile_picker
from telegram_agent_bot.tmux_manager import _agent_command_for_launch


def test_claude_code_alias_and_low_effort_are_normalized():
    profile = AgentProfile(
        agent_type="claudecode",
        model="deepseek-v4-pro",
        reasoning_effort="low",
    )

    assert profile.agent_type == "claude"
    assert profile.reasoning_effort == "low"
    assert profile.display_name == "Claude Code"


def test_fast_is_no_longer_a_reasoning_effort():
    profile = AgentProfile(agent_type="claude", reasoning_effort="fast")

    assert profile.reasoning_effort == "medium"


def test_fast_mode_is_separate_from_reasoning_and_buttons_fit_two_columns():
    profile = AgentProfile(
        agent_type="claude",
        model="deepseek-v4-pro",
        reasoning_effort="high",
        fast_mode=True,
    )

    text, keyboard = build_profile_picker(profile, ["deepseek-v4-pro"])

    assert "Reasoning: `Deep`" in text
    assert "Fast mode: `On`" in text
    assert keyboard.inline_keyboard[-2][0].text == "✅ Create session"
    assert [button.text for button in keyboard.inline_keyboard[-5]] == [
        "Low",
        "Standard",
    ]
    assert [button.text for button in keyboard.inline_keyboard[-4]] == [
        "✅ Deep",
        "Max",
    ]
    assert keyboard.inline_keyboard[-3][0].text == "⚡ Fast: On"


def test_codex_profile_exposes_fast_mode_toggle():
    profile = AgentProfile(
        agent_type="codex",
        model="gpt-5.4-mini",
        reasoning_effort="medium",
        fast_mode=False,
    )

    text, keyboard = build_profile_picker(profile, ["gpt-5.4-mini"])

    assert "Fast mode: `Off`" in text
    assert keyboard.inline_keyboard[-3][0].text == "⚡ Fast: Off"


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
