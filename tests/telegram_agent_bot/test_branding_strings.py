import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("ALLOWED_USERS", "1")
os.environ.setdefault(
    "TELEGRAM_AGENT_BOT_DIR", tempfile.mkdtemp(prefix="telegram-agent-bot-test-config-")
)
os.environ.setdefault(
    "TELEGRAM_AGENT_BOT_CODEX_PROJECTS_PATH",
    tempfile.mkdtemp(prefix="telegram-agent-bot-test-projects-"),
)

from telegram_agent_bot import bot


class BrandingStringTests(unittest.TestCase):
    def test_user_visible_branding_uses_generic_agent_name(self) -> None:
        self.assertEqual(bot.PRODUCT_NAME, "Agent")
        self.assertIn("Agent Monitor", bot.WELCOME_MESSAGE)
        self.assertEqual(
            bot.UNSUPPORTED_CONTENT_MESSAGE,
            "⚠ Only text, photo, file, and voice messages are supported. Stickers, video, and other media cannot be forwarded to Agent.",
        )
        self.assertEqual(bot.PHOTO_CONFIRMATION_MESSAGE, "📷 Image sent to Agent.")
        self.assertEqual(bot.FILE_CONFIRMATION_MESSAGE, "📎 File sent to Agent.")
        self.assertEqual(
            bot.SESSION_STILL_RUNNING_MESSAGE,
            "The Agent session is still running in tmux.",
        )
        self.assertEqual(bot.HELP_COMMAND_DESCRIPTION, "↗ Show Agent help")
        self.assertEqual(bot.CC_COMMANDS["goal"], "↗ Set or update session goal")
        self.assertEqual(bot.CC_COMMANDS["memory"], "↗ Edit AGENTS.md")
        self.assertEqual(bot.ESC_COMMAND_DESCRIPTION, "Interrupt current Agent run")
        self.assertEqual(
            bot.INTERRUPT_COMMAND_DESCRIPTION,
            "Interrupt; optional text sends next",
        )
        self.assertEqual(bot.USAGE_COMMAND_DESCRIPTION, "Show Agent usage remaining")

    def test_default_directory_browser_path_is_not_machine_specific(self) -> None:
        projects_dir = bot.config.default_projects_path
        expected = projects_dir if projects_dir.is_dir() else Path.home()
        self.assertEqual(bot._default_directory_browser_path(), str(expected))

    def test_default_directory_browser_path_uses_configured_projects_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            original = bot.config.default_projects_path
            try:
                bot.config.default_projects_path = Path(tmpdir)
                self.assertEqual(bot._default_directory_browser_path(), tmpdir)
            finally:
                bot.config.default_projects_path = original


if __name__ == "__main__":
    unittest.main()
