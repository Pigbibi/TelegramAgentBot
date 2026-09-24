from unittest.mock import patch

import pytest

from telegram_agent_bot.agent_profile import (
    AGENT_CLAUDE,
    AGENT_CLAUDE_OFFICIAL,
    AGENT_CODEX,
    AGENT_CURSOR,
    AgentProfile,
    agent_capabilities,
)
from telegram_agent_bot.config import _permission_mode_env, config
from telegram_agent_bot.handlers.directory_browser import (
    build_agent_picker,
    build_profile_picker,
)
from telegram_agent_bot.tmux_manager import _agent_command_for_launch


def test_claude_code_alias_and_low_effort_are_normalized():
    profile = AgentProfile(
        agent_type="claudecode",
        model="deepseek-v4-pro",
        reasoning_effort="low",
    )

    assert profile.agent_type == "claude"
    assert profile.reasoning_effort == "low"
    assert profile.display_name == "Claude Code API"


def test_claude_official_profile_is_distinct_from_api_mode():
    profile = AgentProfile(agent_type="claude-official", model="sonnet")

    assert profile.agent_type == "claudeofficial"
    assert profile.display_name == "Claude Code Official Subscription"


@pytest.mark.parametrize(
    ("agent_type", "plugin", "native_queue", "claude_home"),
    [
        (AGENT_CODEX, "plugins", True, False),
        (AGENT_CLAUDE, "plugin", False, True),
        (AGENT_CLAUDE_OFFICIAL, "plugin", False, True),
        (AGENT_CURSOR, "plugins", True, False),
    ],
)
def test_agent_capabilities_keep_provider_semantics_together(
    agent_type, plugin, native_queue, claude_home
):
    capabilities = agent_capabilities(agent_type)

    assert capabilities.plugin_command == plugin
    assert capabilities.supports_native_queue is native_queue
    assert capabilities.uses_claude_home is claude_home


def test_cursor_agent_alias_uses_cursor_profile_without_reasoning_override():
    profile = AgentProfile(
        agent_type="cursor-agent",
        model="gpt-5",
        reasoning_effort="high",
    )

    assert profile.agent_type == "cursor"
    assert profile.display_name == "Cursor Agent"


def test_cursor_picker_omits_unsupported_reasoning_and_fast_controls():
    text, keyboard = build_profile_picker(AgentProfile(agent_type="cursor"), [])

    assert "Reasoning:" not in text
    assert "Fast mode:" not in text
    assert "Permissions: `Ask first`" in text
    assert [button.text for row in keyboard.inline_keyboard for button in row] == [
        "🔐 Permissions: Ask first",
        "✅ Create session",
        "Cancel",
    ]
    _picker_text, picker_keyboard = build_agent_picker()
    assert "🔵 Cursor Agent" in [
        button.text for row in picker_keyboard.inline_keyboard for button in row
    ]
    assert len(picker_keyboard.inline_keyboard[0]) == 2
    assert len(picker_keyboard.inline_keyboard[1]) == 2
    assert [button.text for button in picker_keyboard.inline_keyboard[1]] == [
        "🟣 Claude Code API",
        "🟣 Claude Code Official Subscription",
    ]


def test_fast_is_no_longer_a_reasoning_effort():
    profile = AgentProfile(agent_type="claude", reasoning_effort="fast")

    assert profile.reasoning_effort == "medium"


@pytest.mark.parametrize(
    ("effort", "label"),
    [("xhigh", "Extra High"), ("max", "Max"), ("ultra", "Ultra")],
)
def test_codex_extended_reasoning_efforts_are_preserved(effort, label):
    profile = AgentProfile(agent_type="codex", reasoning_effort=effort)

    assert profile.reasoning_effort == effort
    assert profile.effort_label == label


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
    assert [button.text for button in keyboard.inline_keyboard[-6]] == [
        "Low",
        "Standard",
    ]
    assert [button.text for button in keyboard.inline_keyboard[-5]] == [
        "✅ Deep",
        "Max",
    ]
    assert keyboard.inline_keyboard[-4][0].text == "⚡ Fast: On"


def test_codex_profile_exposes_fast_mode_toggle():
    profile = AgentProfile(
        agent_type="codex",
        model="gpt-5.4-mini",
        reasoning_effort="medium",
        fast_mode=False,
    )

    text, keyboard = build_profile_picker(profile, ["gpt-5.4-mini"])

    assert "Fast mode: `Off`" in text
    assert keyboard.inline_keyboard[-4][0].text == "⚡ Fast: Off"


@pytest.mark.parametrize(
    ("agent_type", "command", "expected"),
    [
        ("codex", "/usr/bin/codex", "--dangerously-bypass-approvals-and-sandbox"),
        ("claude", "/usr/bin/claude", "--dangerously-skip-permissions"),
        ("claudeofficial", "/usr/bin/claude", "--dangerously-skip-permissions"),
        ("cursor", "/usr/bin/agent", "--force"),
    ],
)
def test_full_permission_mode_uses_provider_flag(agent_type, command, expected):
    profile = AgentProfile(agent_type=agent_type, permission_mode="full")
    with (
        patch.object(config, "codex_cli_command", command),
        patch.object(config, "claude_command", command),
        patch.object(config, "cursor_command", command),
    ):
        launch = _agent_command_for_launch(profile)
    assert launch.endswith(expected)


def test_codex_ask_mode_overrides_vps_full_access_config():
    profile = AgentProfile(agent_type="codex", permission_mode="ask")
    with patch.object(
        config,
        "codex_cli_command",
        "/usr/bin/codex --dangerously-bypass-approvals-and-sandbox",
    ):
        launch = _agent_command_for_launch(profile)
    assert "--dangerously-bypass-approvals-and-sandbox" not in launch
    assert launch.endswith("--sandbox workspace-write --ask-for-approval on-request")


def test_ask_mode_removes_provider_bypass_flags_from_configured_commands():
    with (
        patch.object(
            config,
            "claude_command",
            "/usr/bin/claude --dangerously-skip-permissions",
        ),
        patch.object(config, "cursor_command", "/usr/bin/agent -f"),
    ):
        claude = _agent_command_for_launch(
            AgentProfile(agent_type="claude", permission_mode="ask")
        )
        cursor = _agent_command_for_launch(
            AgentProfile(agent_type="cursor", permission_mode="ask")
        )
    assert "--dangerously-skip-permissions" not in claude
    assert claude.endswith("--permission-mode default")
    assert cursor == "/usr/bin/agent"


def test_full_permissions_are_visible_and_explicit_in_profile_picker():
    profile = AgentProfile(agent_type="claude", permission_mode="full")
    text, keyboard = build_profile_picker(profile, [])
    assert "Permissions: `Full access`" in text
    assert "Full access skips agent approval prompts" in text
    assert "VPS account limits still apply" in text
    assert keyboard.inline_keyboard[-3][0].text == "🔐 Permissions: Full access"


@pytest.mark.parametrize("agent_type", [AGENT_CLAUDE, AGENT_CLAUDE_OFFICIAL])
def test_claude_permission_default_is_full_but_ask_remains_selectable(agent_type):
    from telegram_agent_bot.bot import _profile_from_context
    from telegram_agent_bot.handlers.directory_browser import PROFILE_AGENT_KEY

    with patch.object(config, "claude_default_permission_mode", "full"):
        profile = _profile_from_context({PROFILE_AGENT_KEY: agent_type})
        text, keyboard = build_profile_picker(profile, [])

    assert profile.permission_mode == "full"
    assert "Permissions: `Full access`" in text
    assert any(
        button.text == "🔐 Permissions: Full access"
        for row in keyboard.inline_keyboard
        for button in row
    )
    assert "--dangerously-skip-permissions" in _agent_command_for_launch(profile)


def test_cursor_server_full_mode_cannot_show_ask_first():
    from telegram_agent_bot.bot import _profile_from_context
    from telegram_agent_bot.handlers.directory_browser import (
        PROFILE_AGENT_KEY,
        PROFILE_PERMISSION_MODE_KEY,
    )

    with patch.object(config, "cursor_permission_mode", "full"):
        profile = _profile_from_context(
            {PROFILE_AGENT_KEY: AGENT_CURSOR, PROFILE_PERMISSION_MODE_KEY: "ask"}
        )
        text, keyboard = build_profile_picker(profile, [])

    assert profile.permission_mode == "full"
    assert "Permissions: `Full access`" in text
    assert all(
        "Permissions:" not in button.text
        for row in keyboard.inline_keyboard
        for button in row
    )
    assert _agent_command_for_launch(profile).endswith("--force")


def test_invalid_permission_env_value_keeps_ask_default():
    with patch.dict("os.environ", {"PERMISSION_MODE_TEST": "unexpected"}):
        assert _permission_mode_env("PERMISSION_MODE_TEST") == "ask"


def test_codex_profile_uses_model_supported_reasoning_efforts():
    profile = AgentProfile(
        agent_type="codex",
        model="gpt-5.6-sol",
        reasoning_effort="ultra",
    )

    text, keyboard = build_profile_picker(
        profile,
        ["gpt-5.6-sol"],
        effort_values=("low", "medium", "high", "xhigh", "max", "ultra"),
    )
    effort_labels = [
        button.text for row in keyboard.inline_keyboard[1:4] for button in row
    ]

    assert "Reasoning: `Ultra`" in text
    assert effort_labels == [
        "Low",
        "Standard",
        "Deep",
        "Extra High",
        "Max",
        "✅ Ultra",
    ]


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
        "/usr/bin/claude --model deepseek-v4-pro --effort low "
        "--permission-mode default"
    )


def test_claude_official_launch_does_not_source_deepseek_env(tmp_path):
    env_file = tmp_path / "claude.env"
    env_file.write_text("ANTHROPIC_BASE_URL=https://api.deepseek.com/anthropic\n")
    profile = AgentProfile(agent_type="claudeofficial", model="sonnet")

    with (
        patch.object(config, "claude_command", "/usr/bin/claude"),
        patch.object(config, "claude_env_file", env_file),
    ):
        command = _agent_command_for_launch(profile)

    assert command == (
        "/usr/bin/claude --model sonnet --effort medium --permission-mode default"
    )


def test_cursor_launch_uses_model_without_unsupported_reasoning_flag():
    profile = AgentProfile(
        agent_type="cursor",
        model="gpt-5",
        reasoning_effort="high",
    )

    with patch.object(config, "cursor_command", "/usr/bin/agent"):
        command = _agent_command_for_launch(profile)

    assert command == "/usr/bin/agent --model gpt-5"


def test_codex_uses_config_override_for_reasoning_effort():
    profile = AgentProfile(
        agent_type="codex",
        model="gpt-5.3-codex",
        reasoning_effort="low",
    )

    with patch.object(config, "codex_cli_command", "/usr/bin/codex"):
        command = _agent_command_for_launch(profile)

    assert command == (
        '/usr/bin/codex --model gpt-5.3-codex -c model_reasoning_effort="low" '
        "--sandbox workspace-write --ask-for-approval on-request"
    )


@pytest.mark.parametrize("effort", ["xhigh", "max", "ultra"])
def test_codex_launch_preserves_catalog_reasoning_effort(effort):
    profile = AgentProfile(
        agent_type="codex",
        model="gpt-5.6-sol",
        reasoning_effort=effort,
    )

    with patch.object(config, "codex_cli_command", "/usr/bin/codex"):
        command = _agent_command_for_launch(profile)

    assert f'-c model_reasoning_effort="{effort}"' in command
    assert command.endswith("--sandbox workspace-write --ask-for-approval on-request")


def test_model_effort_resolution_falls_back_when_selection_is_unsupported():
    from telegram_agent_bot.bot import _resolve_profile_effort

    with (
        patch.object(
            config,
            "codex_model_efforts",
            {
                "gpt-5.6-sol": ("low", "medium", "high", "xhigh", "max", "ultra"),
                "gpt-5.6-luna": ("low", "medium", "high", "xhigh", "max"),
            },
        ),
        patch.object(
            config,
            "codex_model_default_efforts",
            {"gpt-5.6-sol": "low", "gpt-5.6-luna": "medium"},
        ),
    ):
        assert _resolve_profile_effort("codex", "gpt-5.6-sol", "ultra") == "ultra"
        assert _resolve_profile_effort("codex", "gpt-5.6-luna", "ultra") == "medium"


def test_explicitly_empty_model_efforts_disable_reasoning_override():
    from telegram_agent_bot.bot import (
        _profile_effort_values,
        _resolve_profile_effort,
    )

    with patch.object(
        config,
        "codex_model_efforts",
        {"gpt-no-reasoning": ()},
    ):
        assert _profile_effort_values("codex", "gpt-no-reasoning") == ()
        assert _resolve_profile_effort("codex", "gpt-no-reasoning", "medium") == ""
