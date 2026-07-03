import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("ALLOWED_USERS", "1")
os.environ.setdefault(
    "TELEGRAM_AGENT_BOT_DIR", tempfile.mkdtemp(prefix="telegram-agent-bot-test-config-")
)
os.environ.setdefault(
    "TELEGRAM_AGENT_BOT_CODEX_PROJECTS_PATH",
    tempfile.mkdtemp(prefix="telegram-agent-bot-test-projects-"),
)

import telegram_agent_bot.tmux_manager as tmux_manager_module


class _DummyPane:
    def __init__(self) -> None:
        self.commands: list[tuple[str, bool]] = []

    def send_keys(self, cmd: str, enter: bool = False) -> None:
        self.commands.append((cmd, enter))


class _DummyWindow:
    def __init__(self, pane: _DummyPane) -> None:
        self.window_id = "@9"
        self.active_pane = pane
        self.window_options: list[tuple[str, str]] = []

    def set_window_option(self, name: str, value: str) -> None:
        self.window_options.append((name, value))


class _DummySession:
    def __init__(self, window: _DummyWindow) -> None:
        self.window = window
        self.created: tuple[str, str] | None = None

    def new_window(self, window_name: str, start_directory: str) -> _DummyWindow:
        self.created = (window_name, start_directory)
        return self.window


class CreateWindowTests(unittest.IsolatedAsyncioTestCase):
    async def test_create_window_uses_resume_subcommand(self) -> None:
        pane = _DummyPane()
        window = _DummyWindow(pane)
        session = _DummySession(window)
        manager = tmux_manager_module.TmuxManager(
            session_name="telegram-agent-bot-test"
        )

        with tempfile.TemporaryDirectory(
            prefix="telegram-agent-bot-workdir-"
        ) as tmpdir:
            with (
                patch.object(
                    manager, "find_window_by_name", AsyncMock(return_value=None)
                ),
                patch.object(manager, "get_or_create_session", return_value=session),
                patch("telegram_agent_bot.tmux_manager.disable_codex_update_prompt"),
                patch.object(
                    tmux_manager_module.config,
                    "codex_command",
                    "/usr/local/bin/codex --search -s danger-full-access",
                ),
                patch(
                    "telegram_agent_bot.tmux_manager.ensure_account_home",
                    return_value=Path("/tmp/telegram-agent-bot-account-home"),
                ),
            ):
                ok, _msg, _window_name, window_id = await manager.create_window(
                    tmpdir,
                    window_name="Projects",
                    resume_session_id="sid-123",
                    account_name="plus1",
                )

        self.assertTrue(ok)
        self.assertEqual(window_id, "@9")
        self.assertEqual(session.created, ("Projects", str(Path(tmpdir).resolve())))
        self.assertEqual(window.window_options, [("allow-rename", "off")])
        self.assertEqual(
            pane.commands,
            [
                (
                    "export CODEX_HOME=/tmp/telegram-agent-bot-account-home; "
                    "/usr/local/bin/codex --search -s danger-full-access resume sid-123",
                    True,
                )
            ],
        )

    async def test_create_window_strips_rollout_prefix_for_resume(self) -> None:
        pane = _DummyPane()
        window = _DummyWindow(pane)
        session = _DummySession(window)
        manager = tmux_manager_module.TmuxManager(
            session_name="telegram-agent-bot-test"
        )

        with tempfile.TemporaryDirectory(
            prefix="telegram-agent-bot-workdir-"
        ) as tmpdir:
            with (
                patch.object(
                    manager, "find_window_by_name", AsyncMock(return_value=None)
                ),
                patch.object(manager, "get_or_create_session", return_value=session),
                patch(
                    "telegram_agent_bot.tmux_manager.disable_codex_update_prompt"
                ) as disable_mock,
                patch.object(
                    tmux_manager_module.config,
                    "codex_command",
                    "/usr/local/bin/codex --search -s danger-full-access",
                ),
            ):
                ok, _msg, _window_name, _window_id = await manager.create_window(
                    tmpdir,
                    window_name="Projects",
                    resume_session_id=(
                        "rollout-2026-04-03T17-59-47-"
                        "019d52c8-d90d-7f72-9062-45cf0f71f97e"
                    ),
                )

        self.assertTrue(ok)
        disable_mock.assert_called_once_with()
        self.assertEqual(
            pane.commands,
            [
                (
                    "/usr/local/bin/codex --search -s danger-full-access resume "
                    "019d52c8-d90d-7f72-9062-45cf0f71f97e",
                    True,
                )
            ],
        )

    async def test_create_window_can_bypass_codex_hook_trust(self) -> None:
        pane = _DummyPane()
        window = _DummyWindow(pane)
        session = _DummySession(window)
        manager = tmux_manager_module.TmuxManager(
            session_name="telegram-agent-bot-test"
        )

        with tempfile.TemporaryDirectory(
            prefix="telegram-agent-bot-workdir-"
        ) as tmpdir:
            with (
                patch.object(
                    manager, "find_window_by_name", AsyncMock(return_value=None)
                ),
                patch.object(manager, "get_or_create_session", return_value=session),
                patch("telegram_agent_bot.tmux_manager.disable_codex_update_prompt"),
                patch.object(
                    tmux_manager_module.config,
                    "codex_command",
                    "IS_SANDBOX=1 /usr/local/bin/codex --search",
                ),
                patch.object(
                    tmux_manager_module.config,
                    "codex_bypass_hook_trust",
                    True,
                ),
            ):
                ok, _msg, _window_name, _window_id = await manager.create_window(
                    tmpdir,
                    window_name="Projects",
                )

        self.assertTrue(ok)
        self.assertEqual(
            pane.commands,
            [
                (
                    "IS_SANDBOX=1 /usr/local/bin/codex --search "
                    "--dangerously-bypass-hook-trust",
                    True,
                )
            ],
        )

    async def test_create_window_uses_claude_resume_flag_and_home(self) -> None:
        pane = _DummyPane()
        window = _DummyWindow(pane)
        session = _DummySession(window)
        manager = tmux_manager_module.TmuxManager(
            session_name="telegram-agent-bot-test"
        )

        with tempfile.TemporaryDirectory(
            prefix="telegram-agent-bot-workdir-"
        ) as tmpdir:
            with (
                patch.object(
                    manager, "find_window_by_name", AsyncMock(return_value=None)
                ),
                patch.object(manager, "get_or_create_session", return_value=session),
                patch.object(tmux_manager_module.config, "agent_type", "claude"),
                patch.object(
                    tmux_manager_module.config,
                    "codex_command",
                    "/usr/bin/claude --model deepseek-v4-pro",
                ),
                patch.object(
                    tmux_manager_module.config,
                    "codex_bypass_hook_trust",
                    True,
                ),
                patch(
                    "telegram_agent_bot.tmux_manager.ensure_account_home",
                    return_value=Path("/tmp/telegram-agent-bot-claude-home"),
                ),
            ):
                ok, _msg, _window_name, window_id = await manager.create_window(
                    tmpdir,
                    window_name="Projects",
                    resume_session_id="550e8400-e29b-41d4-a716-446655440000",
                    account_name="main",
                )

        self.assertTrue(ok)
        self.assertEqual(window_id, "@9")
        self.assertEqual(
            pane.commands,
            [
                (
                    "export HOME=/tmp/telegram-agent-bot-claude-home; "
                    "/usr/bin/claude --model deepseek-v4-pro --resume "
                    "550e8400-e29b-41d4-a716-446655440000",
                    True,
                )
            ],
        )


class _SendKeysDummyPane:
    def __init__(self) -> None:
        self.commands: list[tuple[str, bool, bool]] = []

    def send_keys(self, cmd: str, enter: bool = False, literal: bool = False) -> None:
        self.commands.append((cmd, enter, literal))


class _SendKeysDummyWindow:
    def __init__(self, pane: _SendKeysDummyPane) -> None:
        self.window_id = "@9"
        self.active_pane = pane


class _SendKeysWindows:
    def __init__(self, window: _SendKeysDummyWindow) -> None:
        self._window = window

    def get(self, *, window_id: str) -> _SendKeysDummyWindow | None:
        if window_id == self._window.window_id:
            return self._window
        return None


class _SendKeysDummySession:
    def __init__(self, window: _SendKeysDummyWindow) -> None:
        self.windows = _SendKeysWindows(window)


class SendKeysTests(unittest.IsolatedAsyncioTestCase):
    def test_literal_submit_delay_increases_for_long_multiline_text(self) -> None:
        short_delay = tmux_manager_module.TmuxManager._literal_submit_delay("hello")
        long_delay = tmux_manager_module.TmuxManager._literal_submit_delay(
            "Traceback\n" + ("line\n" * 80) + ("x" * 4000)
        )

        self.assertEqual(short_delay, 0.5)
        self.assertGreater(long_delay, short_delay)
        self.assertLessEqual(long_delay, 5.0)

    def test_pane_pending_literal_input_detects_codex_input_row(self) -> None:
        manager = tmux_manager_module.TmuxManager(
            session_name="telegram-agent-bot-test"
        )
        capture = (
            "previous transcript\n"
            "› CryptoSnapshotPipelines/pull/48\n"
            "  - Status: ready for review\n"
            "  gpt-5.5 xhigh · ~/Projects\n"
        )

        with patch(
            "telegram_agent_bot.tmux_manager.subprocess.run",
            return_value=subprocess.CompletedProcess(
                args=["tmux", "capture-pane"],
                returncode=0,
                stdout=capture,
            ),
        ):
            pending = manager._pane_still_has_pending_literal_input(
                "@9",
                "CryptoSnapshotPipelines/pull/48\n- Status: ready for review",
            )

        self.assertTrue(pending)

    def test_pane_pending_literal_input_detects_wrapped_chinese_input(self) -> None:
        manager = tmux_manager_module.TmuxManager(
            session_name="telegram-agent-bot-test"
        )
        prompt = "本机有个first trade的对话窗口也有不回复的问题，都一起处理掉"
        capture = (
            "previous transcript\n"
            "› 本机有个first trade的对话窗口也有不回复的问题，都一起处理\n"
            "  掉\n"
            "\n"
            "  gpt-5.5 xhigh · ~/Projects\n"
        )

        with patch(
            "telegram_agent_bot.tmux_manager.subprocess.run",
            return_value=subprocess.CompletedProcess(
                args=["tmux", "capture-pane"],
                returncode=0,
                stdout=capture,
            ),
        ):
            pending = manager._pane_still_has_pending_literal_input("@9", prompt)

        self.assertTrue(pending)

    def test_pane_pending_literal_input_ignores_submitted_transcript(self) -> None:
        manager = tmux_manager_module.TmuxManager(
            session_name="telegram-agent-bot-test"
        )
        capture = (
            "  CryptoSnapshotPipelines/pull/48\n"
            "  - Status: ready for review\n"
            "◦ Working (13s • esc to interrupt)\n"
            "\n"
            "› Write tests for @filename\n"
            "\n"
            "  gpt-5.5 xhigh · ~/Projects\n"
        )

        with patch(
            "telegram_agent_bot.tmux_manager.subprocess.run",
            return_value=subprocess.CompletedProcess(
                args=["tmux", "capture-pane"],
                returncode=0,
                stdout=capture,
            ),
        ):
            pending = manager._pane_still_has_pending_literal_input(
                "@9",
                "CryptoSnapshotPipelines/pull/48\n- Status: ready for review",
            )

        self.assertFalse(pending)

    def test_pane_pending_literal_input_detects_folded_paste_row(self) -> None:
        manager = tmux_manager_module.TmuxManager(
            session_name="telegram-agent-bot-test"
        )
        capture = (
            "─ Worked for 1m 53s ─────────────────────────\n"
            "• Done\n"
            "\n"
            "› [Pasted Content 2048 chars] #3\n"
            "\n"
            "  gpt-5.5 xhigh · ~/Projects\n"
        )

        with patch(
            "telegram_agent_bot.tmux_manager.subprocess.run",
            return_value=subprocess.CompletedProcess(
                args=["tmux", "capture-pane"],
                returncode=0,
                stdout=capture,
            ),
        ):
            pending = manager._pane_still_has_pending_literal_input(
                "@9",
                "line 1\n" + ("long content\n" * 200),
            )

        self.assertTrue(pending)

    def test_pane_pending_literal_input_ignores_active_working_status(self) -> None:
        manager = tmux_manager_module.TmuxManager(
            session_name="telegram-agent-bot-test"
        )
        capture = (
            "› Improve documentation in @filename\n"
            "\n"
            "◦ Working (51s • esc to interrupt)\n"
            "\n"
            "› Improve documentation in @filename\n"
            "\n"
            "  gpt-5.5 xhigh · ~/Projects\n"
        )

        with patch(
            "telegram_agent_bot.tmux_manager.subprocess.run",
            return_value=subprocess.CompletedProcess(
                args=["tmux", "capture-pane"],
                returncode=0,
                stdout=capture,
            ),
        ):
            pending = manager._pane_still_has_pending_literal_input(
                "@9",
                "Improve documentation in @filename",
            )

        self.assertFalse(pending)

    def test_pane_pending_literal_input_ignores_pursuing_goal_footer(self) -> None:
        manager = tmux_manager_module.TmuxManager(
            session_name="telegram-agent-bot-test"
        )
        capture = (
            "› /goal 按照你的建议进行实现，完成后推送提交，合并 pr\n"
            "\n"
            "  gpt-5.5 xhigh · ~/Projects · Main [default]               "
            "Pursuing goal (3m)\n"
        )

        with patch(
            "telegram_agent_bot.tmux_manager.subprocess.run",
            return_value=subprocess.CompletedProcess(
                args=["tmux", "capture-pane"],
                returncode=0,
                stdout=capture,
            ),
        ):
            pending = manager._pane_still_has_pending_literal_input(
                "@9",
                "/goal 按照你的建议进行实现，完成后推送提交，合并 pr",
            )

        self.assertFalse(pending)

    def test_pane_pending_literal_input_detects_queued_prompt_after_waiting_background(
        self,
    ) -> None:
        manager = tmux_manager_module.TmuxManager(
            session_name="telegram-agent-bot-test"
        )
        capture = (
            "• Waiting for background terminal (1m 13s • esc to interrupt)\n"
            "\n"
            "› Improve documentation in @filename\n"
            "\n"
            "  gpt-5.5 xhigh · ~/Projects\n"
        )

        with patch(
            "telegram_agent_bot.tmux_manager.subprocess.run",
            return_value=subprocess.CompletedProcess(
                args=["tmux", "capture-pane"],
                returncode=0,
                stdout=capture,
            ),
        ):
            pending = manager._pane_still_has_pending_literal_input(
                "@9",
                "Improve documentation in @filename",
            )

        self.assertTrue(pending)

    def test_pane_has_insert_overlay_detects_codex_at_mention_popup(self) -> None:
        manager = tmux_manager_module.TmuxManager(
            session_name="telegram-agent-bot-test"
        )
        capture = (
            "› 🧪 模拟市价卖出 BOXX.US: 1.3823股 @ $116.67\n"
            "  no matches\n"
            "  Press enter to insert or esc to close\n"
        )

        with patch(
            "telegram_agent_bot.tmux_manager.subprocess.run",
            return_value=subprocess.CompletedProcess(
                args=["tmux", "capture-pane"],
                returncode=0,
                stdout=capture,
            ),
        ):
            self.assertTrue(manager._pane_has_insert_overlay("@9"))

    def test_pane_has_insert_overlay_ignores_regular_permission_prompt(self) -> None:
        manager = tmux_manager_module.TmuxManager(
            session_name="telegram-agent-bot-test"
        )
        capture = "  Allow command?\n  Press enter to confirm or esc to cancel\n"

        with patch(
            "telegram_agent_bot.tmux_manager.subprocess.run",
            return_value=subprocess.CompletedProcess(
                args=["tmux", "capture-pane"],
                returncode=0,
                stdout=capture,
            ),
        ):
            self.assertFalse(manager._pane_has_insert_overlay("@9"))

    async def test_send_keys_uses_tmux_cli_for_enter_after_literal_text(self) -> None:
        pane = _SendKeysDummyPane()
        window = _SendKeysDummyWindow(pane)
        session = _SendKeysDummySession(window)
        manager = tmux_manager_module.TmuxManager(
            session_name="telegram-agent-bot-test"
        )

        with (
            patch.object(manager, "get_session", return_value=session),
            patch(
                "telegram_agent_bot.tmux_manager.subprocess.run",
                return_value=subprocess.CompletedProcess(
                    args=["tmux", "send-keys"], returncode=0
                ),
            ) as run_mock,
            patch.object(
                manager, "_pane_still_has_pending_literal_input", return_value=False
            ),
            patch.object(manager, "_pane_has_insert_overlay", return_value=False),
        ):
            ok = await manager.send_keys("@9", "hello")

        self.assertTrue(ok)
        self.assertEqual(pane.commands, [("hello", False, True)])
        run_mock.assert_called_once_with(
            [*manager._tmux_cli_prefix(), "send-keys", "-t", "@9", "Enter"],
            capture_output=True,
            text=True,
            check=False,
        )

    async def test_send_keys_falls_back_to_libtmux_when_cli_enter_fails(self) -> None:
        pane = _SendKeysDummyPane()
        window = _SendKeysDummyWindow(pane)
        session = _SendKeysDummySession(window)
        manager = tmux_manager_module.TmuxManager(
            session_name="telegram-agent-bot-test"
        )

        with (
            patch.object(manager, "get_session", return_value=session),
            patch(
                "telegram_agent_bot.tmux_manager.subprocess.run",
                return_value=subprocess.CompletedProcess(
                    args=["tmux", "send-keys"],
                    returncode=1,
                    stderr="send failed",
                ),
            ),
            patch.object(
                manager, "_pane_still_has_pending_literal_input", return_value=False
            ),
            patch.object(manager, "_pane_has_insert_overlay", return_value=False),
        ):
            ok = await manager.send_keys("@9", "hello")

        self.assertTrue(ok)
        self.assertEqual(
            pane.commands,
            [
                ("hello", False, True),
                ("", True, False),
            ],
        )

    async def test_send_keys_uses_bracketed_paste_for_multiline_text(self) -> None:
        pane = _SendKeysDummyPane()
        window = _SendKeysDummyWindow(pane)
        session = _SendKeysDummySession(window)
        manager = tmux_manager_module.TmuxManager(
            session_name="telegram-agent-bot-test"
        )
        text = "line 1\nline 2\nline 3"

        with (
            patch.object(manager, "get_session", return_value=session),
            patch.object(
                manager, "_paste_buffer_literal", return_value=True
            ) as paste_mock,
            patch(
                "telegram_agent_bot.tmux_manager.subprocess.run",
                return_value=subprocess.CompletedProcess(
                    args=["tmux", "send-keys"], returncode=0
                ),
            ) as run_mock,
            patch.object(
                manager, "_pane_still_has_pending_literal_input", return_value=False
            ),
            patch.object(manager, "_pane_has_insert_overlay", return_value=False),
        ):
            ok = await manager.send_keys("@9", text)

        self.assertTrue(ok)
        self.assertEqual(pane.commands, [])
        paste_mock.assert_called_once_with("@9", text)
        self.assertEqual(
            run_mock.call_args_list[0].args[0],
            [*manager._tmux_cli_prefix(), "send-keys", "-t", "@9", "Enter"],
        )

    async def test_send_keys_retries_enter_when_prompt_is_still_pending(self) -> None:
        pane = _SendKeysDummyPane()
        window = _SendKeysDummyWindow(pane)
        session = _SendKeysDummySession(window)
        manager = tmux_manager_module.TmuxManager(
            session_name="telegram-agent-bot-test"
        )

        with (
            patch.object(manager, "get_session", return_value=session),
            patch(
                "telegram_agent_bot.tmux_manager.subprocess.run",
                return_value=subprocess.CompletedProcess(
                    args=["tmux", "send-keys"], returncode=0
                ),
            ) as run_mock,
            patch.object(
                manager,
                "_pane_still_has_pending_literal_input",
                side_effect=[True, False],
            ),
            patch.object(manager, "_pane_has_insert_overlay", return_value=False),
        ):
            ok = await manager.send_keys("@9", "hello")

        self.assertTrue(ok)
        self.assertEqual(pane.commands, [("hello", False, True)])
        self.assertEqual(run_mock.call_count, 2)
        for call in run_mock.call_args_list:
            self.assertEqual(
                call.args[0],
                [*manager._tmux_cli_prefix(), "send-keys", "-t", "@9", "Enter"],
            )

    async def test_send_keys_closes_at_mention_completion_before_retry(self) -> None:
        pane = _SendKeysDummyPane()
        window = _SendKeysDummyWindow(pane)
        session = _SendKeysDummySession(window)
        manager = tmux_manager_module.TmuxManager(
            session_name="telegram-agent-bot-test"
        )

        with (
            patch.object(manager, "get_session", return_value=session),
            patch(
                "telegram_agent_bot.tmux_manager.subprocess.run",
                return_value=subprocess.CompletedProcess(
                    args=["tmux", "send-keys"], returncode=0
                ),
            ) as run_mock,
            patch.object(
                manager,
                "_pane_still_has_pending_literal_input",
                side_effect=[True, False],
            ),
            patch.object(manager, "_pane_has_insert_overlay", return_value=False),
        ):
            ok = await manager.send_keys("@9", "Improve documentation in @filename")

        self.assertTrue(ok)
        self.assertEqual(
            [call.args[0][-1] for call in run_mock.call_args_list],
            ["Enter", "Escape", "Enter"],
        )

    async def test_send_keys_closes_insert_overlay_before_submit(self) -> None:
        pane = _SendKeysDummyPane()
        window = _SendKeysDummyWindow(pane)
        session = _SendKeysDummySession(window)
        manager = tmux_manager_module.TmuxManager(
            session_name="telegram-agent-bot-test"
        )

        with (
            patch.object(manager, "get_session", return_value=session),
            patch(
                "telegram_agent_bot.tmux_manager.subprocess.run",
                return_value=subprocess.CompletedProcess(
                    args=["tmux", "send-keys"], returncode=0
                ),
            ) as run_mock,
            patch.object(
                manager, "_pane_still_has_pending_literal_input", return_value=False
            ),
            patch.object(manager, "_pane_has_insert_overlay", return_value=True),
        ):
            ok = await manager.send_keys(
                "@9",
                "🧪 模拟市价卖出 BOXX.US: 1.3823股 @ $116.67",
            )

        self.assertTrue(ok)
        self.assertEqual(
            [call.args[0][-1] for call in run_mock.call_args_list],
            ["Escape", "Enter"],
        )

    async def test_send_keys_allows_slow_submit_status_after_retry(self) -> None:
        pane = _SendKeysDummyPane()
        window = _SendKeysDummyWindow(pane)
        session = _SendKeysDummySession(window)
        manager = tmux_manager_module.TmuxManager(
            session_name="telegram-agent-bot-test"
        )

        with (
            patch.object(manager, "get_session", return_value=session),
            patch(
                "telegram_agent_bot.tmux_manager.subprocess.run",
                return_value=subprocess.CompletedProcess(
                    args=["tmux", "send-keys"], returncode=0
                ),
            ) as run_mock,
            patch.object(
                manager,
                "_pane_still_has_pending_literal_input",
                side_effect=[True, True, False],
            ),
            patch.object(manager, "_pane_has_insert_overlay", return_value=False),
        ):
            ok = await manager.send_keys("@9", "hello")

        self.assertTrue(ok)
        self.assertEqual(run_mock.call_count, 2)

    async def test_send_keys_waits_for_goal_status_after_retry_settle(self) -> None:
        pane = _SendKeysDummyPane()
        window = _SendKeysDummyWindow(pane)
        session = _SendKeysDummySession(window)
        manager = tmux_manager_module.TmuxManager(
            session_name="telegram-agent-bot-test"
        )

        with (
            patch.object(manager, "get_session", return_value=session),
            patch(
                "telegram_agent_bot.tmux_manager.subprocess.run",
                return_value=subprocess.CompletedProcess(
                    args=["tmux", "send-keys"], returncode=0
                ),
            ) as run_mock,
            patch.object(
                manager,
                "_pane_still_has_pending_literal_input",
                side_effect=[True, True, True, True, False],
            ) as pending_mock,
            patch.object(manager, "_pane_has_insert_overlay", return_value=False),
            patch(
                "telegram_agent_bot.tmux_manager.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            ok = await manager.send_keys("@9", "/goal 按照你的建议全部完善优化")

        self.assertTrue(ok)
        self.assertEqual(run_mock.call_count, 2)
        self.assertEqual(pending_mock.call_count, 5)

    async def test_send_keys_returns_false_when_retry_leaves_prompt_pending(
        self,
    ) -> None:
        pane = _SendKeysDummyPane()
        window = _SendKeysDummyWindow(pane)
        session = _SendKeysDummySession(window)
        manager = tmux_manager_module.TmuxManager(
            session_name="telegram-agent-bot-test"
        )

        with (
            patch.object(manager, "get_session", return_value=session),
            patch(
                "telegram_agent_bot.tmux_manager.subprocess.run",
                return_value=subprocess.CompletedProcess(
                    args=["tmux", "send-keys"], returncode=0
                ),
            ) as run_mock,
            patch.object(
                manager,
                "_pane_still_has_pending_literal_input",
                side_effect=[True, True]
                + [True] * tmux_manager_module._SUBMIT_SETTLE_CHECKS,
            ),
            patch.object(manager, "_pane_has_insert_overlay", return_value=False),
            patch(
                "telegram_agent_bot.tmux_manager.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            ok = await manager.send_keys("@9", "hello")

        self.assertFalse(ok)
        self.assertEqual(run_mock.call_count, 2)

    def test_paste_buffer_literal_uses_tmux_bracketed_paste(self) -> None:
        manager = tmux_manager_module.TmuxManager(
            session_name="telegram-agent-bot-test"
        )
        text = "line 1\nline 2\nline 3"
        run_results = [
            subprocess.CompletedProcess(args=["tmux", "load-buffer"], returncode=0),
            subprocess.CompletedProcess(args=["tmux", "paste-buffer"], returncode=0),
        ]

        with patch(
            "telegram_agent_bot.tmux_manager.subprocess.run",
            side_effect=run_results,
        ) as run_mock:
            ok = manager._paste_buffer_literal("@9", text)

        self.assertTrue(ok)
        self.assertEqual(run_mock.call_count, 2)
        load_cmd = run_mock.call_args_list[0].args[0]
        self.assertEqual(
            load_cmd[: len(manager._tmux_cli_prefix())], manager._tmux_cli_prefix()
        )
        self.assertEqual(
            load_cmd[
                len(manager._tmux_cli_prefix()) : len(manager._tmux_cli_prefix()) + 2
            ],
            ["load-buffer", "-b"],
        )
        self.assertEqual(load_cmd[-1], "-")
        self.assertEqual(run_mock.call_args_list[0].kwargs["input"], text)
        paste_cmd = run_mock.call_args_list[1].args[0]
        self.assertEqual(
            paste_cmd[: len(manager._tmux_cli_prefix())], manager._tmux_cli_prefix()
        )
        self.assertEqual(
            paste_cmd[
                len(manager._tmux_cli_prefix()) : len(manager._tmux_cli_prefix()) + 3
            ],
            ["paste-buffer", "-p", "-d"],
        )
        self.assertEqual(paste_cmd[-2:], ["-t", "@9"])


if __name__ == "__main__":
    unittest.main()
