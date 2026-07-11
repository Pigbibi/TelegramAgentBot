"""Tests for terminal_parser — regex-based detection of Codex UI elements."""

import pytest

from telegram_agent_bot.terminal_parser import (
    codex_input_text,
    extract_auth_error_message,
    extract_bash_output,
    extract_interactive_content,
    is_codex_input_ready,
    is_interactive_ui,
    parse_public_progress_block,
    parse_status_line,
    parse_status_update,
    strip_pane_chrome,
)

# ── parse_status_line ────────────────────────────────────────────────────


class TestParseStatusLine:
    @pytest.mark.parametrize(
        ("spinner", "rest", "expected"),
        [
            ("·", "Working on task", "Working on task"),
            ("✻", "  Reading file  ", "Reading file"),
            ("✽", "Thinking deeply", "Thinking deeply"),
            ("✶", "Analyzing code", "Analyzing code"),
            ("✳", "Processing input", "Processing input"),
            ("✢", "Building project", "Building project"),
        ],
    )
    def test_spinner_chars(self, spinner: str, rest: str, expected: str, chrome: str):
        pane = f"some output\n{spinner}{rest}\n{chrome}"
        assert parse_status_line(pane) == expected

    @pytest.mark.parametrize(
        "pane",
        [
            pytest.param("just normal text\nno spinners here\n", id="no_spinner"),
            pytest.param("", id="empty"),
        ],
    )
    def test_returns_none(self, pane: str):
        assert parse_status_line(pane) is None

    def test_no_chrome_returns_none(self):
        """Without chrome separator, status can't be determined."""
        pane = "output\n✻ Doing work\nno chrome here\n"
        assert parse_status_line(pane) is None

    def test_blank_line_between_status_and_chrome(self, chrome: str):
        """Status line with blank lines before separator."""
        pane = f"output\n✻ Doing work\n\n{chrome}"
        assert parse_status_line(pane) == "Doing work"

    def test_idle_no_status(self, chrome: str):
        """Idle pane (no status line above chrome) returns None."""
        pane = f"some output\n● Tool result\n{chrome}"
        assert parse_status_line(pane) is None

    def test_false_positive_bullet(self, chrome: str):
        """· in regular output must NOT be detected as status."""
        pane = f"· bullet point one\n· bullet point two\nsome result\n{chrome}"
        assert parse_status_line(pane) is None

    def test_uses_fixture(self, sample_pane_status_line: str):
        assert parse_status_line(sample_pane_status_line) == "Reading file src/main.py"


class TestParseStatusUpdate:
    def test_prefers_recent_public_progress_block(self, chrome: str):
        pane = (
            '• Searched site:msci.com "MSCI USA Momentum Index"\n'
            "✻ Searching the web\n"
            f"{chrome}"
        )
        assert parse_status_update(pane) == (
            '• Searched site:msci.com "MSCI USA Momentum Index"\n\n⏳ Searching the web'
        )

    def test_extracts_multiline_progress_block(self, chrome: str):
        pane = f"• Explored\n  └ Read market_data.py\n✻ Reading file\n{chrome}"
        assert (
            parse_public_progress_block(pane) == "• Explored\n  └ Read market_data.py"
        )

    def test_falls_back_to_spinner_when_no_public_progress(self, chrome: str):
        pane = f"output\n✻ Reading file src/main.py\n{chrome}"
        assert parse_status_update(pane) == "Reading file src/main.py"

    @pytest.mark.parametrize(
        "completion",
        ["Brewed for 3s", "Cooked for 2.5s", "Worked for 4s"],
    )
    def test_hides_completion_footer(self, completion: str, chrome: str):
        pane = f"final answer\n✻ {completion}\n{chrome}"
        assert parse_status_update(pane) is None

    def test_keeps_codex_working_bullet_status(self, chrome: str):
        pane = (
            "• Working (3m 08s • esc to interrupt) · "
            "1 background terminal running · /ps to …\n"
            f"{chrome}"
        )
        assert parse_status_update(pane) == (
            "• Working (3m 08s • esc to interrupt) · "
            "1 background terminal running · /ps to …"
        )

    def test_ignores_stale_working_progress_when_idle(self, chrome: str):
        pane = (
            "• Working (3m 08s • esc to interrupt)\n\n"
            "Done with the requested change.\n"
            f"{chrome}"
        )
        assert parse_public_progress_block(pane) == (
            "• Working (3m 08s • esc to interrupt)"
        )
        assert parse_status_update(pane) is None
        assert is_codex_input_ready(pane)

    def test_ignores_final_answer_bullet_when_idle(self, chrome: str):
        """A completed final answer in the pane should not be sent as status."""
        pane = (
            "─ Worked for 2m 04s ─────────────────────────\n\n"
            "• 可以，那我已经按这个范围收住了：\n\n"
            "  - 后端接口演示环境：已部署到 Azure App Service\n"
            "    https://gdeiassistant.azurewebsites.net\n\n"
            "› Run /review on my current changes\n"
            f"{chrome}"
        )
        assert parse_public_progress_block(pane) is not None
        assert parse_status_update(pane) is None

    def test_codex_input_ready_when_idle_prompt_is_visible(self, chrome: str):
        pane = (
            "─ Worked for 2m 04s ─────────────────────────\n\n"
            "• Final answer already rendered\n\n"
            "› Run /review on my current changes\n"
            f"{chrome}"
        )
        assert is_codex_input_ready(pane)
        assert codex_input_text(pane) == ""

    def test_codex_input_text_returns_empty_for_empty_prompt(self):
        pane = "previous output\n\n›\n\n  gpt-5.5 · ~/repo"
        assert is_codex_input_ready(pane)
        assert codex_input_text(pane) == ""

    def test_codex_input_text_joins_wrapped_prompt(self):
        pane = (
            "previous output\n\n"
            "› Implement a more robust submit confirmation with a long\n"
            "  wrapped continuation line\n\n"
            "  gpt-5.5 · ~/repo"
        )
        assert codex_input_text(pane) == (
            "Implement a more robust submit confirmation with a long "
            "wrapped continuation line"
        )

    def test_codex_input_not_ready_while_working_even_with_prompt_row(
        self, chrome: str
    ):
        pane = (
            "• Working (1m 07s • esc to interrupt)\n"
            f"{chrome}"
            "❯ \n"
            f"{chrome}"
            "  [Opus 4.6] Context: 50%\n"
        )
        assert not is_codex_input_ready(pane)

    def test_active_progress_above_prompt_is_status(self):
        """Newer Codex panes can show active status directly above the prompt."""
        pane = (
            "• Waiting for background terminal (52s • esc to interrupt)\n\n"
            "› Find and fix a bug in @filename\n\n"
            "  gpt-5.4-mini medium · ~/Projects\n"
        )
        assert (
            parse_status_update(pane)
            == "• Waiting for background terminal (52s • esc to interrupt)"
        )
        assert not is_codex_input_ready(pane)
        assert codex_input_text(pane) is None

    def test_codex_input_not_ready_without_prompt(self):
        assert not is_codex_input_ready("output only\nno prompt")


class TestAuthErrorDetection:
    def test_detects_current_auth_error(self):
        pane = (
            "› hi\n\n"
            "■ Your access token could not be refreshed because you have since "
            "logged out or signed in to another account. Please sign in again.\n\n"
            "›\n\n"
            "  gpt-5.5 xhigh · ~/Projects\n"
        )

        message = extract_auth_error_message(pane)

        assert message is not None
        assert "access token could not be refreshed" in message

    def test_detects_codex_login_prompt(self):
        pane = (
            "Welcome to Codex, OpenAI's command-line coding agent\n\n"
            "Sign in with ChatGPT to use Codex as part of your paid plan\n"
            "or connect an API key for usage-based billing\n\n"
            "> 1. Sign in with ChatGPT\n"
            "  2. Sign in with Device Code\n"
            "  3. Provide your own API key\n\n"
            "Press enter to continue\n\n"
            "Login timed out\n"
        )

        message = extract_auth_error_message(pane)

        assert message is not None
        assert "Sign in with ChatGPT" in message

    def test_ignores_stale_auth_error_before_latest_prompt(self):
        pane = (
            "› hi\n\n"
            "■ Your access token could not be refreshed because your refresh token "
            "was revoked. Please log out and sign in again.\n\n"
            "› Explain this codebase\n\n"
            "• Working (1m 43s • esc to interrupt)\n\n"
            "  gpt-5.5 xhigh · ~/Projects\n"
        )

        assert extract_auth_error_message(pane) is None


# ── extract_interactive_content ──────────────────────────────────────────


class TestExtractInteractiveContent:
    def test_exit_plan_mode(self, sample_pane_exit_plan: str):
        result = extract_interactive_content(sample_pane_exit_plan)
        assert result is not None
        assert result.name == "ExitPlanMode"
        assert "Would you like to proceed?" in result.content
        assert "ctrl-g to edit in" in result.content

    def test_exit_plan_mode_variant(self):
        pane = (
            "  Codex has written up a plan\n  ─────\n  Details here\n  Esc to cancel\n"
        )
        result = extract_interactive_content(pane)
        assert result is not None
        assert result.name == "ExitPlanMode"
        assert "Codex has written up a plan" in result.content

    def test_ask_user_multi_tab(self, sample_pane_ask_user_multi_tab: str):
        result = extract_interactive_content(sample_pane_ask_user_multi_tab)
        assert result is not None
        assert result.name == "AskUserQuestion"
        assert "←" in result.content

    def test_ask_user_single_tab(self, sample_pane_ask_user_single_tab: str):
        result = extract_interactive_content(sample_pane_ask_user_single_tab)
        assert result is not None
        assert result.name == "AskUserQuestion"
        assert "Enter to select" in result.content

    def test_permission_prompt(self, sample_pane_permission: str):
        result = extract_interactive_content(sample_pane_permission)
        assert result is not None
        assert result.name == "PermissionPrompt"
        assert "Do you want to proceed?" in result.content

    def test_codex_command_approval_prompt(self):
        pane = (
            "  Would you like to run the following command?\n"
            "\n"
            "  $ rm -rf /tmp/coin_recovered && mkdir -p /tmp/coin_recovered &&\n"
            "  .venv/bin/uncompyle6 -o /tmp/coin_recovered script.pyc\n"
            "\n"
            "  › 1. Yes, proceed (y)\n"
            "    2. Yes, and don't ask again for commands that start with `rm -rf /tmp/coin_recovered` (p)\n"
            "    3. No, and tell Codex what to do differently (esc)\n"
            "\n"
            "  Press enter to confirm or esc to cancel\n"
        )
        result = extract_interactive_content(pane)
        assert result is not None
        assert result.name == "CommandApproval"
        assert "Would you like to run the following command?" in result.content
        assert "Press enter to confirm" in result.content

    def test_codex_edit_approval_prompt(self):
        pane = (
            "  Would you like to make the following edits?\n"
            "\n"
            "  Reason: command failed; retry without sandbox?\n"
            "\n"
            "  › 1. Yes, proceed (y)\n"
            "    2. Yes, and don't ask again for these files (a)\n"
            "    3. No, and tell Codex what to do differently (esc)\n"
            "\n"
            "  Press enter to confirm or esc to cancel\n"
        )
        result = extract_interactive_content(pane)
        assert result is not None
        assert result.name == "CommandApproval"
        assert "Would you like to make the following edits?" in result.content
        assert "Press enter to confirm" in result.content

    def test_codex_field_permission_prompt(self):
        pane = (
            "  Field 1/1\n"
            "  Allow GitHub to create a pull request?\n"
            "\n"
            "  Title: [codex] Require explicit strategy profile selection\n"
            "  Head branch: codex/live-profile-runtime-updates\n"
            "  Base branch: main\n"
            "\n"
            "  › 1. Allow\n"
            "    2. Allow for this session\n"
            "    3. Always allow\n"
            "    4. Cancel\n"
            "  enter to submit | esc to cancel\n"
        )
        result = extract_interactive_content(pane)
        assert result is not None
        assert result.name == "PermissionPrompt"
        assert "Allow GitHub to create a pull request?" in result.content
        assert "enter to submit" in result.content

    def test_restore_checkpoint(self):
        pane = (
            "  Restore the code to a previous state?\n"
            "  ─────\n"
            "  Some details\n"
            "  Enter to continue\n"
        )
        result = extract_interactive_content(pane)
        assert result is not None
        assert result.name == "RestoreCheckpoint"
        assert "Restore the code" in result.content

    def test_settings(self):
        pane = "  Settings: press tab to cycle\n  ─────\n  Option 1\n  Esc to cancel\n"
        result = extract_interactive_content(pane)
        assert result is not None
        assert result.name == "Settings"
        assert "Settings:" in result.content

    def test_settings_model_picker(self, sample_pane_settings: str):
        result = extract_interactive_content(sample_pane_settings)
        assert result is not None
        assert result.name == "Settings"
        assert "Select model" in result.content
        assert "Sonnet" in result.content
        assert "Enter to confirm" in result.content

    def test_settings_esc_to_cancel_bottom(self):
        pane = (
            "  Settings: press tab to cycle\n"
            "  ─────\n"
            "  Model\n"
            "  ─────\n"
            "  ● claude-sonnet-4-20250514\n"
            "  ○ claude-opus-4-20250514\n"
            "  Esc to cancel\n"
        )
        result = extract_interactive_content(pane)
        assert result is not None
        assert result.name == "Settings"
        assert "Esc to cancel" in result.content

    def test_settings_esc_to_exit_bottom(self):
        pane = (
            "  Settings: press tab to cycle\n"
            "  ─────\n"
            "  Model\n"
            "  ─────\n"
            "  ● Default (Opus 4.6)\n"
            "  ○ claude-sonnet-4-20250514\n"
            "\n"
            "  Enter to confirm · Esc to exit\n"
        )
        result = extract_interactive_content(pane)
        assert result is not None
        assert result.name == "Settings"
        assert "Enter to confirm" in result.content

    def test_hook_trust_prompt(self):
        pane = (
            "  Hooks\n"
            "  Lifecycle hooks from config and enabled plugins.\n"
            "\n"
            "  ⚠ 1 hook needs review before it can run.\n"
            "\n"
            "  Event                 Installed   Active      Review\n"
            "  SessionStart          1           0           1\n"
            "\n"
            "  Press t to trust all; enter to review hooks; esc to close\n"
        )
        result = extract_interactive_content(pane)
        assert result is not None
        assert result.name == "HookTrust"
        assert "needs review" in result.content
        assert "Press t to trust all" in result.content

    def test_directory_trust_prompt_is_not_input_ready(self):
        pane = (
            "  Do you trust the contents of this directory?\n"
            "\n"
            "› 1. Yes, continue\n"
            "  2. No, quit\n"
            "\n"
            "  Press enter to continue\n"
        )

        result = extract_interactive_content(pane)

        assert result is not None
        assert result.name == "DirectoryTrust"
        assert is_interactive_ui(pane) is True
        assert codex_input_text(pane) is None
        assert is_codex_input_ready(pane) is False

    @pytest.mark.parametrize(
        "pane",
        [
            pytest.param("$ echo hello\nhello\n$\n", id="no_ui"),
            pytest.param("", id="empty"),
        ],
    )
    def test_returns_none(self, pane: str):
        assert extract_interactive_content(pane) is None

    def test_min_gap_too_small_returns_none(self):
        pane = "  Do you want to proceed?\n  Esc to cancel\n"
        assert extract_interactive_content(pane) is None


# ── is_interactive_ui ────────────────────────────────────────────────────


class TestIsInteractiveUI:
    def test_true_when_ui_present(self, sample_pane_exit_plan: str):
        assert is_interactive_ui(sample_pane_exit_plan) is True

    def test_false_when_no_ui(self, sample_pane_no_ui: str):
        assert is_interactive_ui(sample_pane_no_ui) is False

    def test_settings_is_interactive(self, sample_pane_settings: str):
        assert is_interactive_ui(sample_pane_settings) is True

    def test_false_for_empty_string(self):
        assert is_interactive_ui("") is False


# ── strip_pane_chrome ───────────────────────────────────────────────────


class TestStripPaneChrome:
    def test_strips_from_separator(self):
        lines = [
            "some output",
            "more output",
            "─" * 30,
            "❯",
            "─" * 30,
            "  [Opus 4.6] Context: 34%",
        ]
        assert strip_pane_chrome(lines) == ["some output", "more output"]

    def test_no_separator_returns_all(self):
        lines = ["line 1", "line 2", "line 3"]
        assert strip_pane_chrome(lines) == lines

    def test_short_separator_not_triggered(self):
        lines = ["output", "─" * 10, "more output"]
        assert strip_pane_chrome(lines) == lines

    def test_only_searches_last_10_lines(self):
        # Separator at line 0 with 15 lines total — outside the last-10 window
        lines = ["─" * 30] + [f"line {i}" for i in range(14)]
        assert strip_pane_chrome(lines) == lines


# ── extract_bash_output ─────────────────────────────────────────────────


class TestExtractBashOutput:
    def test_extracts_command_output(self):
        pane = "some context\n! echo hello\n⎿ hello\n"
        result = extract_bash_output(pane, "echo hello")
        assert result is not None
        assert "! echo hello" in result
        assert "hello" in result

    def test_command_not_found_returns_none(self):
        pane = "some context\njust normal output\n"
        assert extract_bash_output(pane, "echo hello") is None

    def test_chrome_stripped(self):
        pane = (
            "some context\n"
            "! ls\n"
            "⎿ file.txt\n"
            + "─" * 30
            + "\n"
            + "❯\n"
            + "─" * 30
            + "\n"
            + "  [Opus 4.6] Context: 34%\n"
        )
        result = extract_bash_output(pane, "ls")
        assert result is not None
        assert "file.txt" in result
        assert "Opus" not in result

    def test_prefix_match_long_command(self):
        pane = "! long_comma…\n⎿ output\n"
        result = extract_bash_output(pane, "long_command_that_gets_truncated")
        assert result is not None
        assert "output" in result

    def test_trailing_blank_lines_stripped(self):
        pane = "! echo hi\n⎿ hi\n\n\n"
        result = extract_bash_output(pane, "echo hi")
        assert result is not None
        assert not result.endswith("\n")
