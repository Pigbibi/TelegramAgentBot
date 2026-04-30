import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from unittest.mock import patch

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("ALLOWED_USERS", "1")
os.environ.setdefault("CCBOT_DIR", tempfile.mkdtemp(prefix="ccbot-test-config-"))
os.environ.setdefault(
    "CLAUDE_PROJECTS_PATH", tempfile.mkdtemp(prefix="ccbot-test-projects-")
)

import ccbot.session as session_module
from ccbot.session import SessionManager, WindowState


class SessionMapLoadingTests(unittest.IsolatedAsyncioTestCase):
    async def test_empty_session_map_does_not_drop_existing_window_states(self) -> None:
        manager = SessionManager()
        manager.window_states = {
            "@1": WindowState(
                session_id="rollout-2026-04-03T17-59-47-019d52c8-d90d-7f72-9062-45cf0f71f97e",
                cwd="/Users/lisiyi/Projects",
                window_name="Projects",
            )
        }

        with tempfile.TemporaryDirectory(prefix="ccbot-session-map-") as tmpdir:
            session_map_file = Path(tmpdir) / "session_map.json"
            session_map_file.write_text(json.dumps({}))

            with patch.object(
                session_module.config, "session_map_file", session_map_file
            ):
                await manager.load_session_map()

        self.assertIn("@1", manager.window_states)
        self.assertEqual(
            manager.window_states["@1"].session_id,
            "rollout-2026-04-03T17-59-47-019d52c8-d90d-7f72-9062-45cf0f71f97e",
        )

    async def test_stale_session_map_cwd_does_not_overwrite_live_window(self) -> None:
        manager = SessionManager()
        manager.window_states = {
            "@7": WindowState(
                session_id="stale-session",
                cwd="/tmp/memory",
                window_name="Projects-3",
            )
        }

        with tempfile.TemporaryDirectory(prefix="ccbot-session-map-") as tmpdir:
            session_map_file = Path(tmpdir) / "session_map.json"
            session_map_file.write_text(
                json.dumps(
                    {
                        "ccbot:@7": {
                            "session_id": "stale-session",
                            "cwd": "/tmp/memory",
                            "window_name": "Projects-3",
                        }
                    }
                )
            )

            with (
                patch.object(
                    session_module.config, "session_map_file", session_map_file
                ),
                patch.object(session_module.config, "tmux_session_name", "ccbot"),
                patch.object(
                    session_module.tmux_manager,
                    "list_windows",
                    AsyncMock(
                        return_value=[
                            SimpleNamespace(
                                window_id="@7",
                                cwd="/tmp/project",
                                window_name="Projects-3",
                            )
                        ]
                    ),
                ),
            ):
                await manager.load_session_map()

        self.assertIn("@7", manager.window_states)
        self.assertEqual(manager.window_states["@7"].session_id, "")
        self.assertEqual(manager.window_states["@7"].cwd, "")

    async def test_session_map_keeps_pending_launch_without_hook_entry(self) -> None:
        manager = SessionManager()
        manager.window_states = {
            "@8": WindowState(
                session_id="",
                cwd="/tmp/project",
                window_name="Projects-4",
                launch_started_at=1000.0,
            )
        }

        with tempfile.TemporaryDirectory(prefix="ccbot-session-map-") as tmpdir:
            session_map_file = Path(tmpdir) / "session_map.json"
            session_map_file.write_text(
                json.dumps(
                    {
                        "ccbot:@1": {
                            "session_id": "existing-session",
                            "cwd": "/tmp/other",
                            "window_name": "Projects",
                        }
                    }
                )
            )

            with (
                patch.object(
                    session_module.config, "session_map_file", session_map_file
                ),
                patch.object(session_module.config, "tmux_session_name", "ccbot"),
                patch.object(
                    session_module.tmux_manager,
                    "list_windows",
                    AsyncMock(
                        return_value=[
                            SimpleNamespace(
                                window_id="@8",
                                cwd="/tmp/project",
                                window_name="Projects-4",
                            )
                        ]
                    ),
                ),
            ):
                await manager.load_session_map()

        self.assertIn("@8", manager.window_states)
        self.assertEqual(manager.window_states["@8"].launch_started_at, 1000.0)

    async def test_load_session_map_clears_pending_launch_time(self) -> None:
        manager = SessionManager()
        manager.window_states = {
            "@1": WindowState(
                session_id="",
                cwd="/tmp/project",
                window_name="Projects",
                launch_started_at=1000.0,
            )
        }

        with tempfile.TemporaryDirectory(prefix="ccbot-session-map-") as tmpdir:
            session_map_file = Path(tmpdir) / "session_map.json"
            session_map_file.write_text(
                json.dumps(
                    {
                        "ccbot:@1": {
                            "session_id": "new-session",
                            "cwd": "/tmp/project",
                            "window_name": "Projects",
                        }
                    }
                )
            )

            with (
                patch.object(
                    session_module.config, "session_map_file", session_map_file
                ),
                patch.object(session_module.config, "tmux_session_name", "ccbot"),
                patch.object(
                    session_module.tmux_manager,
                    "list_windows",
                    AsyncMock(
                        return_value=[
                            SimpleNamespace(
                                window_id="@1",
                                cwd="/tmp/project",
                                window_name="Projects",
                            )
                        ]
                    ),
                ),
            ):
                await manager.load_session_map()

        self.assertEqual(manager.window_states["@1"].session_id, "new-session")
        self.assertEqual(manager.window_states["@1"].launch_started_at, 0.0)

    async def test_startup_keeps_missing_bound_window_for_recovery(self) -> None:
        manager = SessionManager()
        manager.window_states = {
            "@2": WindowState(
                session_id="session-quant",
                cwd="/home/ubuntu/Projects/QuantPlatformKit",
                window_name="QuantPlatformKit",
            )
        }
        manager.thread_bindings = {5992562050: {14847: "@2"}}
        manager.user_window_offsets = {5992562050: {"@2": 4096}}
        manager.window_display_names = {"@2": "QuantPlatformKit"}
        manager.group_chat_ids = {"5992562050:14847": -1003811990090}

        with tempfile.TemporaryDirectory(prefix="ccbot-session-map-") as tmpdir:
            session_map_file = Path(tmpdir) / "session_map.json"
            session_map_file.write_text(
                json.dumps(
                    {
                        "ccbot:@2": {
                            "session_id": "session-quant",
                            "cwd": "/home/ubuntu/Projects/QuantPlatformKit",
                            "window_name": "QuantPlatformKit",
                        }
                    }
                )
            )

            with (
                patch.object(
                    session_module.config, "session_map_file", session_map_file
                ),
                patch.object(session_module.config, "tmux_session_name", "ccbot"),
                patch.object(
                    session_module.tmux_manager,
                    "list_windows",
                    AsyncMock(
                        return_value=[
                            SimpleNamespace(
                                window_id="@0",
                                cwd="/home/ubuntu",
                                window_name="__main__",
                            )
                        ]
                    ),
                ),
            ):
                await manager.resolve_stale_ids()

            session_map = json.loads(session_map_file.read_text())

        self.assertIn("@2", manager.window_states)
        self.assertEqual(manager.thread_bindings[5992562050][14847], "@2")
        self.assertEqual(manager.user_window_offsets[5992562050]["@2"], 4096)
        self.assertEqual(manager.window_display_names["@2"], "QuantPlatformKit")
        self.assertEqual(manager.group_chat_ids["5992562050:14847"], -1003811990090)
        self.assertNotIn("ccbot:@2", session_map)


if __name__ == "__main__":
    unittest.main()
