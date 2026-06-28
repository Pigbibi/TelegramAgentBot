import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("ALLOWED_USERS", "1")
os.environ.setdefault(
    "TELEGRAM_AGENT_BOT_DIR", tempfile.mkdtemp(prefix="telegram-agent-bot-test-config-")
)
os.environ.setdefault(
    "TELEGRAM_AGENT_BOT_CODEX_PROJECTS_PATH",
    tempfile.mkdtemp(prefix="telegram-agent-bot-test-projects-"),
)

from telegram_agent_bot.main import AlreadyRunningError, acquire_instance_lock


class InstanceLockTests(unittest.TestCase):
    def test_acquire_instance_lock_writes_current_pid(self) -> None:
        lock_path = (
            Path(tempfile.mkdtemp(prefix="telegram-agent-bot-lock-"))
            / "telegram_agent_bot.lock"
        )

        handle = acquire_instance_lock(lock_path)

        self.assertTrue(lock_path.exists())
        self.assertEqual(
            lock_path.read_text(encoding="utf-8").strip(), str(os.getpid())
        )
        handle.close()

    def test_acquire_instance_lock_raises_when_lock_is_unavailable(self) -> None:
        lock_path = (
            Path(tempfile.mkdtemp(prefix="telegram-agent-bot-lock-"))
            / "telegram_agent_bot.lock"
        )

        with patch("telegram_agent_bot.main.fcntl.flock", side_effect=BlockingIOError):
            with self.assertRaises(AlreadyRunningError):
                acquire_instance_lock(lock_path)


if __name__ == "__main__":
    unittest.main()
