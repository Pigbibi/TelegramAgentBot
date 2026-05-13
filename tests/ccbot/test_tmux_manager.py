import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("ALLOWED_USERS", "1")
os.environ.setdefault("CCBOT_DIR", tempfile.mkdtemp(prefix="ccbot-test-config-"))
os.environ.setdefault(
    "CLAUDE_PROJECTS_PATH", tempfile.mkdtemp(prefix="ccbot-test-projects-")
)

import ccbot.tmux_manager as tmux_manager_module


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
        manager = tmux_manager_module.TmuxManager(session_name="ccbot-test")

        with tempfile.TemporaryDirectory(prefix="ccbot-workdir-") as tmpdir:
            with (
                patch.object(
                    manager, "find_window_by_name", AsyncMock(return_value=None)
                ),
                patch.object(manager, "get_or_create_session", return_value=session),
                patch("ccbot.tmux_manager.disable_codex_update_prompt"),
                patch.object(
                    tmux_manager_module.config,
                    "codex_command",
                    "/usr/local/bin/codex --search -s danger-full-access",
                ),
                patch(
                    "ccbot.tmux_manager.ensure_account_home",
                    return_value=Path("/tmp/ccbot-account-home"),
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
                    "export CODEX_HOME=/tmp/ccbot-account-home; "
                    "/usr/local/bin/codex --search -s danger-full-access resume sid-123",
                    True,
                )
            ],
        )

    async def test_create_window_strips_rollout_prefix_for_resume(self) -> None:
        pane = _DummyPane()
        window = _DummyWindow(pane)
        session = _DummySession(window)
        manager = tmux_manager_module.TmuxManager(session_name="ccbot-test")

        with tempfile.TemporaryDirectory(prefix="ccbot-workdir-") as tmpdir:
            with (
                patch.object(
                    manager, "find_window_by_name", AsyncMock(return_value=None)
                ),
                patch.object(manager, "get_or_create_session", return_value=session),
                patch("ccbot.tmux_manager.disable_codex_update_prompt") as disable_mock,
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
        manager = tmux_manager_module.TmuxManager(session_name="ccbot-test")
        capture = (
            "previous transcript\n"
            "› CryptoSnapshotPipelines/pull/48\n"
            "  - Status: ready for review\n"
            "  gpt-5.5 xhigh · ~/Projects\n"
        )

        with patch(
            "ccbot.tmux_manager.subprocess.run",
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

    def test_pane_pending_literal_input_ignores_submitted_transcript(self) -> None:
        manager = tmux_manager_module.TmuxManager(session_name="ccbot-test")
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
            "ccbot.tmux_manager.subprocess.run",
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
        manager = tmux_manager_module.TmuxManager(session_name="ccbot-test")
        capture = (
            "─ Worked for 1m 53s ─────────────────────────\n"
            "• Done\n"
            "\n"
            "› [Pasted Content 2048 chars] #3\n"
            "\n"
            "  gpt-5.5 xhigh · ~/Projects\n"
        )

        with patch(
            "ccbot.tmux_manager.subprocess.run",
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
        manager = tmux_manager_module.TmuxManager(session_name="ccbot-test")
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
            "ccbot.tmux_manager.subprocess.run",
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

    def test_pane_has_insert_overlay_detects_codex_at_mention_popup(self) -> None:
        manager = tmux_manager_module.TmuxManager(session_name="ccbot-test")
        capture = (
            "› 🧪 模拟市价卖出 BOXX.US: 1.3823股 @ $116.67\n"
            "  no matches\n"
            "  Press enter to insert or esc to close\n"
        )

        with patch(
            "ccbot.tmux_manager.subprocess.run",
            return_value=subprocess.CompletedProcess(
                args=["tmux", "capture-pane"],
                returncode=0,
                stdout=capture,
            ),
        ):
            self.assertTrue(manager._pane_has_insert_overlay("@9"))

    def test_pane_has_insert_overlay_ignores_regular_permission_prompt(self) -> None:
        manager = tmux_manager_module.TmuxManager(session_name="ccbot-test")
        capture = "  Allow command?\n  Press enter to confirm or esc to cancel\n"

        with patch(
            "ccbot.tmux_manager.subprocess.run",
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
        manager = tmux_manager_module.TmuxManager(session_name="ccbot-test")

        with (
            patch.object(manager, "get_session", return_value=session),
            patch(
                "ccbot.tmux_manager.subprocess.run",
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
        manager = tmux_manager_module.TmuxManager(session_name="ccbot-test")

        with (
            patch.object(manager, "get_session", return_value=session),
            patch(
                "ccbot.tmux_manager.subprocess.run",
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
        manager = tmux_manager_module.TmuxManager(session_name="ccbot-test")
        text = "line 1\nline 2\nline 3"

        with (
            patch.object(manager, "get_session", return_value=session),
            patch.object(
                manager, "_paste_buffer_literal", return_value=True
            ) as paste_mock,
            patch(
                "ccbot.tmux_manager.subprocess.run",
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
        manager = tmux_manager_module.TmuxManager(session_name="ccbot-test")

        with (
            patch.object(manager, "get_session", return_value=session),
            patch(
                "ccbot.tmux_manager.subprocess.run",
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
        manager = tmux_manager_module.TmuxManager(session_name="ccbot-test")

        with (
            patch.object(manager, "get_session", return_value=session),
            patch(
                "ccbot.tmux_manager.subprocess.run",
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
        manager = tmux_manager_module.TmuxManager(session_name="ccbot-test")

        with (
            patch.object(manager, "get_session", return_value=session),
            patch(
                "ccbot.tmux_manager.subprocess.run",
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

    async def test_send_keys_returns_false_when_retry_leaves_prompt_pending(
        self,
    ) -> None:
        pane = _SendKeysDummyPane()
        window = _SendKeysDummyWindow(pane)
        session = _SendKeysDummySession(window)
        manager = tmux_manager_module.TmuxManager(session_name="ccbot-test")

        with (
            patch.object(manager, "get_session", return_value=session),
            patch(
                "ccbot.tmux_manager.subprocess.run",
                return_value=subprocess.CompletedProcess(
                    args=["tmux", "send-keys"], returncode=0
                ),
            ) as run_mock,
            patch.object(
                manager,
                "_pane_still_has_pending_literal_input",
                side_effect=[True, True],
            ),
            patch.object(manager, "_pane_has_insert_overlay", return_value=False),
        ):
            ok = await manager.send_keys("@9", "hello")

        self.assertFalse(ok)
        self.assertEqual(run_mock.call_count, 2)

    def test_paste_buffer_literal_uses_tmux_bracketed_paste(self) -> None:
        manager = tmux_manager_module.TmuxManager(session_name="ccbot-test")
        text = "line 1\nline 2\nline 3"
        run_results = [
            subprocess.CompletedProcess(args=["tmux", "load-buffer"], returncode=0),
            subprocess.CompletedProcess(args=["tmux", "paste-buffer"], returncode=0),
        ]

        with patch(
            "ccbot.tmux_manager.subprocess.run",
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
