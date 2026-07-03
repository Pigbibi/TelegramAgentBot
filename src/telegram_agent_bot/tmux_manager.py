"""Tmux session/window management via libtmux.

Wraps libtmux to provide async-friendly operations on a single tmux session:
  - list_windows / find_window_by_name: discover Codex windows.
  - capture_pane: read terminal content (plain or with ANSI colors).
  - send_keys: forward user input or control keys to a window.
  - create_window / kill_window: lifecycle management.

All blocking libtmux calls are wrapped in asyncio.to_thread().

Key class: TmuxManager (singleton instantiated as `tmux_manager`).
"""

from __future__ import annotations

import asyncio
import logging
import re
import shlex
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path

import libtmux

from .account_manager import disable_codex_update_prompt, ensure_account_home
from .config import SENSITIVE_ENV_VARS, config

logger = logging.getLogger(__name__)

_UUID_SUFFIX_RE = re.compile(
    r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$"
)
_FOLDED_PASTE_INPUT_RE = re.compile(r"^[›❯]\s*\[Pasted Content\s+\d+\s+chars\]")
_INSERT_OVERLAY_RE = re.compile(
    r"(?:press\s+)?enter\s+to\s+insert\s+or\s+esc\s+to\s+close",
    re.IGNORECASE,
)
_ACTIVE_WORKING_RE = re.compile(
    r"^[•◦]\s*(?:Working|Pursuing goal)\b"
    r"|^Pursuing goal\b"
    r"|esc\s+to\s+interrupt"
    r"|\bgpt-[\w.]+.*\bPursuing goal\b",
    re.IGNORECASE,
)
_WAITING_BACKGROUND_RE = re.compile(
    r"^[•◦]\s*Waiting for background terminal\b", re.IGNORECASE
)
_HOOK_TRUST_BYPASS_FLAG = "--dangerously-bypass-hook-trust"
_SUBMIT_SETTLE_CHECKS = 8
_SUBMIT_SETTLE_INTERVAL_SECONDS = 0.5


def _resume_target_id(session_id: str) -> str:
    """Return the identifier accepted by current Codex CLI resume.

    Newer Codex CLIs expect a bare UUID, while telegram-agent-bot often tracks session ids as
    rollout-prefixed JSONL stems. Strip the rollout prefix when present.
    """
    match = _UUID_SUFFIX_RE.search(session_id)
    if match:
        return match.group(1)
    return session_id


def _first_command_executable(parts: list[str]) -> str:
    """Return the first executable token, skipping leading env assignments."""
    for part in parts:
        name, sep, _value = part.partition("=")
        if sep and name.isidentifier() and not part.startswith("-"):
            continue
        return Path(part).name
    return ""


def _agent_command_for_launch() -> str:
    """Return the configured agent command with optional hook-trust bypass.

    Supports both Codex and Claude Code CLIs.
    """
    cmd = config.codex_command
    if not getattr(config, "codex_bypass_hook_trust", False):
        return cmd
    try:
        parts = shlex.split(cmd)
    except ValueError:
        logger.warning("Unable to parse command for hook-trust flag injection")
        return cmd
    if _HOOK_TRUST_BYPASS_FLAG in parts:
        return cmd
    first_exe = _first_command_executable(parts)
    # This flag is Codex-specific; Claude Code uses a different permission flag
    # with broader semantics, so do not inject it for Claude mode.
    if config.agent_type == "claude" or first_exe != "codex":
        return cmd
    return f"{cmd} {_HOOK_TRUST_BYPASS_FLAG}"


@dataclass
class TmuxWindow:
    """Information about a tmux window."""

    window_id: str
    window_name: str
    cwd: str  # Current working directory
    pane_current_command: str = ""  # Process running in active pane


class TmuxManager:
    """Manages tmux windows for Codex sessions."""

    def __init__(self, session_name: str | None = None):
        """Initialize tmux manager.

        Args:
            session_name: Name of the tmux session to use (default from config)
        """
        self.session_name = session_name or config.tmux_session_name
        self._server: libtmux.Server | None = None

    @property
    def server(self) -> libtmux.Server:
        """Get or create tmux server connection."""
        if self._server is None:
            self._server = libtmux.Server(socket_name=config.tmux_socket_name)
        return self._server

    @staticmethod
    def _tmux_cli_prefix() -> list[str]:
        """Build a tmux CLI prefix that targets the configured socket when set."""
        cmd = ["tmux"]
        if config.tmux_socket_name:
            cmd.extend(["-L", config.tmux_socket_name])
        return cmd

    @staticmethod
    def _literal_submit_delay(text: str) -> float:
        """Return a small settle delay before submitting literal pasted text.

        Codex's TUI can accept long or multiline Telegram messages more slowly
        than tmux reports the paste as delivered. If Enter arrives too early it
        may be interpreted as an input newline instead of prompt submission.
        """
        if len(text) <= 512 and "\n" not in text:
            return 0.5

        char_delay = min(len(text) / 1800.0, 2.0)
        line_delay = min(text.count("\n") * 0.035, 2.5)
        return min(max(0.5, 0.5 + char_delay + line_delay), 5.0)

    @staticmethod
    def _prompt_fragments(text: str) -> list[str]:
        """Return short fragments suitable for checking pending TUI input."""
        fragments: list[str] = []
        for line in text.splitlines():
            fragment = " ".join(line.strip().split())
            if not fragment:
                continue
            if len(fragment) <= 16:
                fragments.append(fragment)
            else:
                fragments.append(fragment[:24])
                fragments.append(fragment[:16])
            if len(fragments) >= 6:
                break

        if not fragments:
            fallback = " ".join(text.strip().split())
            if fallback:
                fragments.append(fallback[:24])

        seen: set[str] = set()
        deduped: list[str] = []
        for fragment in fragments:
            if len(fragment) < 4:
                continue
            if fragment not in seen:
                seen.add(fragment)
                deduped.append(fragment)
        return deduped

    @staticmethod
    def _input_candidate_lines(tail_lines: list[str]) -> list[str]:
        """Return the pane tail lines most likely to contain the active input row."""
        footer_index: int | None = None
        for index in range(len(tail_lines) - 1, -1, -1):
            stripped = tail_lines[index].strip()
            if "· ~" in stripped or re.search(r"\bgpt-[\w.]+", stripped):
                footer_index = index
                break

        if footer_index is not None:
            return tail_lines[max(0, footer_index - 10) : footer_index]
        return tail_lines[-12:]

    def get_session(self) -> libtmux.Session | None:
        """Get the tmux session if it exists."""
        try:
            return self.server.sessions.get(session_name=self.session_name)
        except Exception:
            return None

    def _paste_buffer_literal(self, window_id: str, text: str) -> bool:
        """Paste literal text via a temporary tmux buffer using bracketed paste."""
        buffer_name = f"telegram-agent-bot-paste-{uuid.uuid4().hex}"
        load_cmd = [*self._tmux_cli_prefix(), "load-buffer", "-b", buffer_name, "-"]
        paste_cmd = [
            *self._tmux_cli_prefix(),
            "paste-buffer",
            "-p",
            "-d",
            "-b",
            buffer_name,
            "-t",
            window_id,
        ]
        delete_cmd = [*self._tmux_cli_prefix(), "delete-buffer", "-b", buffer_name]

        loaded = False
        pasted = False
        try:
            load_result = subprocess.run(
                load_cmd,
                input=text,
                capture_output=True,
                text=True,
                check=False,
            )
            if load_result.returncode != 0:
                stderr = load_result.stderr.strip() or f"exit {load_result.returncode}"
                logger.error(
                    "Failed to load tmux paste buffer for window %s: %s",
                    window_id,
                    stderr,
                )
                return False
            loaded = True

            paste_result = subprocess.run(
                paste_cmd,
                capture_output=True,
                text=True,
                check=False,
            )
            if paste_result.returncode == 0:
                pasted = True
                return True

            stderr = paste_result.stderr.strip() or f"exit {paste_result.returncode}"
            logger.error(
                "Failed to bracket-paste text to window %s: %s",
                window_id,
                stderr,
            )
            return False
        except Exception as e:
            logger.error(
                "Failed to bracket-paste text to window %s: %s",
                window_id,
                e,
            )
            return False
        finally:
            if loaded and not pasted:
                try:
                    subprocess.run(
                        delete_cmd,
                        capture_output=True,
                        text=True,
                        check=False,
                    )
                except Exception:
                    pass

    def _capture_pane_tail(self, window_id: str, start: int = -30) -> str | None:
        cmd = [
            *self._tmux_cli_prefix(),
            "capture-pane",
            "-t",
            window_id,
            "-p",
            "-S",
            str(start),
        ]
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                check=False,
            )
        except Exception as e:
            logger.debug(
                "Unable to capture pane tail for window %s: %s",
                window_id,
                e,
            )
            return None

        if result.returncode != 0 or not isinstance(result.stdout, str):
            return None
        return result.stdout

    def _pane_has_insert_overlay(self, window_id: str) -> bool:
        """Return True when Codex's @-mention insert overlay is active."""
        pane_text = self._capture_pane_tail(window_id, start=-20)
        if not pane_text:
            return False
        return any(
            _INSERT_OVERLAY_RE.search(line) for line in pane_text.splitlines()[-20:]
        )

    def _pane_still_has_pending_literal_input(self, window_id: str, text: str) -> bool:
        """Return True if the pasted prompt still appears in Codex's input row."""
        fragments = self._prompt_fragments(text)
        may_render_as_folded_paste = len(text) > 1000 or "\n" in text
        if not fragments and not may_render_as_folded_paste:
            return False

        pane_text = self._capture_pane_tail(window_id, start=-30)
        if not pane_text:
            return False

        tail_lines = pane_text.splitlines()[-30:]

        input_seen = False
        waiting_background_index: int | None = None
        for index, line in enumerate(tail_lines):
            if _WAITING_BACKGROUND_RE.search(line.strip()):
                waiting_background_index = index

        candidate_lines = self._input_candidate_lines(tail_lines)
        if waiting_background_index is not None:
            lower_bound = max(0, len(tail_lines) - len(candidate_lines))
            input_seen_after_wait = False
            for offset, line in enumerate(candidate_lines, start=lower_bound):
                if offset <= waiting_background_index:
                    continue
                stripped = line.strip()
                is_input_start = stripped.startswith(("›", "❯"))
                is_input_continuation = input_seen_after_wait and line[:1].isspace()
                if is_input_start:
                    input_seen_after_wait = True
                    if may_render_as_folded_paste and _FOLDED_PASTE_INPUT_RE.match(
                        stripped
                    ):
                        return True
                if (is_input_start or is_input_continuation) and any(
                    fragment in stripped for fragment in fragments
                ):
                    return True

        if any(_ACTIVE_WORKING_RE.search(line.strip()) for line in tail_lines):
            return False

        for line in candidate_lines:
            stripped = line.strip()
            is_input_start = stripped.startswith(("›", "❯"))
            is_input_continuation = input_seen and line[:1].isspace()
            if is_input_start:
                input_seen = True
                if may_render_as_folded_paste and _FOLDED_PASTE_INPUT_RE.match(
                    stripped
                ):
                    return True
            if (is_input_start or is_input_continuation) and any(
                fragment in stripped for fragment in fragments
            ):
                return True
        return False

    def _send_control_key_sync(self, window_id: str, key: str) -> bool:
        """Send one tmux control key to a window."""
        cmd = [*self._tmux_cli_prefix(), "send-keys", "-t", window_id, key]
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                check=False,
            )
            if result.returncode == 0:
                return True
            stderr = result.stderr.strip() or f"exit {result.returncode}"
            logger.error(
                "Failed to send %s via tmux CLI to window %s: %s",
                key,
                window_id,
                stderr,
            )
        except Exception as e:
            logger.error(
                "Failed to send %s via tmux CLI to window %s: %s",
                key,
                window_id,
                e,
            )

        session = self.get_session()
        if not session:
            return False
        try:
            window = session.windows.get(window_id=window_id)
            if not window:
                return False
            pane = window.active_pane
            if not pane:
                return False
            if key == "Enter":
                pane.send_keys("", enter=True, literal=False)
            else:
                pane.send_keys(key, enter=False, literal=False)
            return True
        except Exception as e:
            logger.error(
                "Failed to send %s to window %s: %s",
                key,
                window_id,
                e,
            )
            return False

    async def prompt_still_pending(self, window_id: str, text: str) -> bool:
        """Return whether text still appears in the Codex input row."""
        return await asyncio.to_thread(
            self._pane_still_has_pending_literal_input,
            window_id,
            text,
        )

    async def send_control_key(self, window_id: str, key: str) -> bool:
        """Send a control key such as Enter or Escape to a tmux window."""
        return await asyncio.to_thread(self._send_control_key_sync, window_id, key)

    def get_or_create_session(self) -> libtmux.Session:
        """Get existing session or create a new one."""
        session = self.get_session()
        if session:
            self._scrub_session_env(session)
            return session

        # Create new session with main window named specifically
        session = self.server.new_session(
            session_name=self.session_name,
            start_directory=str(Path.home()),
        )
        # Rename the default window to the main window name
        if session.windows:
            session.windows[0].rename_window(config.tmux_main_window_name)
        self._scrub_session_env(session)
        return session

    @staticmethod
    def _scrub_session_env(session: libtmux.Session) -> None:
        """Remove sensitive env vars from the tmux session environment.

        Prevents new windows (and their child processes like Codex)
        from inheriting secrets such as TELEGRAM_BOT_TOKEN.
        """
        for var in SENSITIVE_ENV_VARS:
            try:
                session.unset_environment(var)
            except Exception:
                pass  # var not set in session env — nothing to remove

    async def list_windows(self) -> list[TmuxWindow]:
        """List all windows in the session with their working directories.

        Returns:
            List of TmuxWindow with window info and cwd
        """

        def _sync_list_windows() -> list[TmuxWindow]:
            windows = []
            session = self.get_session()

            if not session:
                return windows

            for window in session.windows:
                name = window.window_name or ""
                # Skip the main window (placeholder window)
                if name == config.tmux_main_window_name:
                    continue

                try:
                    # Get the active pane's current path and command
                    pane = window.active_pane
                    if pane:
                        cwd = pane.pane_current_path or ""
                        pane_cmd = pane.pane_current_command or ""
                    else:
                        cwd = ""
                        pane_cmd = ""

                    windows.append(
                        TmuxWindow(
                            window_id=window.window_id or "",
                            window_name=name,
                            cwd=cwd,
                            pane_current_command=pane_cmd,
                        )
                    )
                except Exception as e:
                    logger.debug(f"Error getting window info: {e}")

            return windows

        return await asyncio.to_thread(_sync_list_windows)

    async def find_window_by_name(self, window_name: str) -> TmuxWindow | None:
        """Find a window by its name.

        Args:
            window_name: The window name to match

        Returns:
            TmuxWindow if found, None otherwise
        """
        windows = await self.list_windows()
        for window in windows:
            if window.window_name == window_name:
                return window
        logger.debug("Window not found by name: %s", window_name)
        return None

    async def find_window_by_id(self, window_id: str) -> TmuxWindow | None:
        """Find a window by its tmux window ID (e.g. '@0', '@12').

        Args:
            window_id: The tmux window ID to match

        Returns:
            TmuxWindow if found, None otherwise
        """
        windows = await self.list_windows()
        for window in windows:
            if window.window_id == window_id:
                return window
        logger.debug("Window not found by id: %s", window_id)
        return None

    async def capture_pane(self, window_id: str, with_ansi: bool = False) -> str | None:
        """Capture the visible text content of a window's active pane.

        Args:
            window_id: The window ID to capture
            with_ansi: If True, capture with ANSI color codes

        Returns:
            The captured text, or None on failure.
        """
        if with_ansi:
            # Use async subprocess to call tmux capture-pane -e for ANSI colors
            try:
                proc = await asyncio.create_subprocess_exec(
                    *self._tmux_cli_prefix(),
                    "capture-pane",
                    "-e",
                    "-p",
                    "-t",
                    window_id,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout, stderr = await proc.communicate()
                if proc.returncode == 0:
                    return stdout.decode("utf-8")
                logger.error(
                    f"Failed to capture pane {window_id}: {stderr.decode('utf-8')}"
                )
                return None
            except Exception as e:
                logger.error(f"Unexpected error capturing pane {window_id}: {e}")
                return None

        # Original implementation for plain text - wrap in thread
        def _sync_capture() -> str | None:
            session = self.get_session()
            if not session:
                return None
            try:
                window = session.windows.get(window_id=window_id)
                if not window:
                    return None
                pane = window.active_pane
                if not pane:
                    return None
                lines = pane.capture_pane()
                return "\n".join(lines) if isinstance(lines, list) else str(lines)
            except Exception as e:
                logger.error(f"Failed to capture pane {window_id}: {e}")
                return None

        return await asyncio.to_thread(_sync_capture)

    async def send_keys(
        self, window_id: str, text: str, enter: bool = True, literal: bool = True
    ) -> bool:
        """Send keys to a specific window.

        Args:
            window_id: The window ID to send to
            text: Text to send
            enter: Whether to press enter after the text
            literal: If True, send text literally. If False, interpret special keys
                     like "Up", "Down", "Left", "Right", "Escape", "Enter".

        Returns:
            True if successful, False otherwise
        """
        if literal and enter:
            # Split into text + delay + Enter via libtmux.
            # Codex's TUI sometimes interprets a rapid-fire Enter
            # (arriving in the same input batch as the text) as a newline
            # rather than submit.  Long pasted tracebacks need a longer gap
            # so the TUI can process all text before receiving Enter.
            use_paste_buffer = len(text) > 512 or "\n" in text or "\r" in text

            def _send_literal(chars: str) -> bool:
                session = self.get_session()
                if not session:
                    logger.error("No tmux session found")
                    return False
                try:
                    window = session.windows.get(window_id=window_id)
                    if not window:
                        logger.error(f"Window {window_id} not found")
                        return False
                    pane = window.active_pane
                    if not pane:
                        logger.error(f"No active pane in window {window_id}")
                        return False
                    pane.send_keys(chars, enter=False, literal=True)
                    return True
                except Exception as e:
                    logger.error(f"Failed to send keys to window {window_id}: {e}")
                    return False

            def _send_control_key(key: str) -> bool:
                # libtmux's `pane.send_keys(..., enter=True)` can occasionally
                # fail to submit a prompt to the long-running Codex TUI even
                # though the text itself was delivered. The raw tmux CLI
                # `send-keys` has proven more reliable for control keys.
                return self._send_control_key_sync(window_id, key)

            # Codex's ! command mode: send "!" first so the TUI
            # switches to bash mode, wait 1s, then send the rest.
            if text.startswith("!"):
                if not await asyncio.to_thread(_send_literal, "!"):
                    return False
                rest = text[1:]
                if rest:
                    await asyncio.sleep(1.0)
                    use_paste_buffer = len(rest) > 512 or "\n" in rest or "\r" in rest
                    if use_paste_buffer:
                        if not await asyncio.to_thread(
                            self._paste_buffer_literal, window_id, rest
                        ):
                            return False
                    elif not await asyncio.to_thread(_send_literal, rest):
                        return False
            else:
                if use_paste_buffer:
                    if not await asyncio.to_thread(
                        self._paste_buffer_literal, window_id, text
                    ):
                        return False
                elif not await asyncio.to_thread(_send_literal, text):
                    return False
            await asyncio.sleep(self._literal_submit_delay(text))
            if await asyncio.to_thread(self._pane_has_insert_overlay, window_id):
                logger.info(
                    "Codex insert overlay detected in window %s; closing before submit",
                    window_id,
                )
                if not await asyncio.to_thread(_send_control_key, "Escape"):
                    return False
                await asyncio.sleep(0.2)
            if not await asyncio.to_thread(_send_control_key, "Enter"):
                return False
            await asyncio.sleep(0.5)
            if await asyncio.to_thread(
                self._pane_still_has_pending_literal_input,
                window_id,
                text,
            ):
                logger.warning(
                    "Codex prompt still appears pending in window %s after Enter; retrying submit",
                    window_id,
                )
                await asyncio.sleep(0.5)
                if "@" in text or await asyncio.to_thread(
                    self._pane_has_insert_overlay, window_id
                ):
                    if not await asyncio.to_thread(_send_control_key, "Escape"):
                        return False
                    await asyncio.sleep(0.2)
                if not await asyncio.to_thread(_send_control_key, "Enter"):
                    return False
                await asyncio.sleep(0.5)
                if await asyncio.to_thread(
                    self._pane_still_has_pending_literal_input,
                    window_id,
                    text,
                ):
                    submitted_after_settle = False
                    for _ in range(_SUBMIT_SETTLE_CHECKS):
                        # Codex can leave submitted /goal prompts rendered for a
                        # few seconds before the Pursuing goal status is painted.
                        # Poll briefly so a slow successful submit is not
                        # reported as a send failure.
                        await asyncio.sleep(_SUBMIT_SETTLE_INTERVAL_SECONDS)
                        if not await asyncio.to_thread(
                            self._pane_still_has_pending_literal_input,
                            window_id,
                            text,
                        ):
                            submitted_after_settle = True
                            break
                    if not submitted_after_settle:
                        logger.error(
                            "Codex prompt still appears pending in window %s after retry",
                            window_id,
                        )
                        return False
                    logger.info(
                        "Codex prompt cleared in window %s after submit settle grace",
                        window_id,
                    )
            return True

        # Other cases: special keys (literal=False) or no-enter
        def _sync_send_keys() -> bool:
            session = self.get_session()
            if not session:
                logger.error("No tmux session found")
                return False

            try:
                window = session.windows.get(window_id=window_id)
                if not window:
                    logger.error(f"Window {window_id} not found")
                    return False

                pane = window.active_pane
                if not pane:
                    logger.error(f"No active pane in window {window_id}")
                    return False

                pane.send_keys(text, enter=enter, literal=literal)
                return True

            except Exception as e:
                logger.error(f"Failed to send keys to window {window_id}: {e}")
                return False

        return await asyncio.to_thread(_sync_send_keys)

    async def rename_window(self, window_id: str, new_name: str) -> bool:
        """Rename a tmux window by its ID."""

        def _sync_rename() -> bool:
            session = self.get_session()
            if not session:
                return False
            try:
                window = session.windows.get(window_id=window_id)
                if not window:
                    return False
                window.rename_window(new_name)
                logger.info("Renamed window %s to '%s'", window_id, new_name)
                return True
            except Exception as e:
                logger.error(f"Failed to rename window {window_id}: {e}")
                return False

        return await asyncio.to_thread(_sync_rename)

    async def kill_window(self, window_id: str) -> bool:
        """Kill a tmux window by its ID."""

        def _sync_kill() -> bool:
            session = self.get_session()
            if not session:
                return False
            try:
                window = session.windows.get(window_id=window_id)
                if not window:
                    return False
                window.kill()
                logger.info("Killed window %s", window_id)
                return True
            except Exception as e:
                logger.error(f"Failed to kill window {window_id}: {e}")
                return False

        return await asyncio.to_thread(_sync_kill)

    async def create_window(
        self,
        work_dir: str,
        window_name: str | None = None,
        start_codex: bool = True,
        resume_session_id: str | None = None,
        account_name: str | None = None,
    ) -> tuple[bool, str, str, str]:
        """Create a new tmux window and optionally start Codex.

        Args:
            work_dir: Working directory for the new window
            window_name: Optional window name (defaults to directory name)
            start_codex: Whether to start the configured Codex command
            resume_session_id: If set, append `resume <id>` to the command

        Returns:
            Tuple of (success, message, window_name, window_id)
        """
        # Validate directory first
        path = Path(work_dir).expanduser().resolve()
        if not path.exists():
            return False, f"Directory does not exist: {work_dir}", "", ""
        if not path.is_dir():
            return False, f"Not a directory: {work_dir}", "", ""

        # Create window name, adding suffix if name already exists
        final_window_name = window_name if window_name else path.name

        # Check for existing window name
        base_name = final_window_name
        counter = 2
        while await self.find_window_by_name(final_window_name):
            final_window_name = f"{base_name}-{counter}"
            counter += 1

        # Create window in thread
        def _create_and_start() -> tuple[bool, str, str, str]:
            session = self.get_or_create_session()
            try:
                # Create new window
                window = session.new_window(
                    window_name=final_window_name,
                    start_directory=str(path),
                )

                wid = window.window_id or ""

                # Prevent Codex from overriding the window name
                window.set_window_option("allow-rename", "off")

                # Start the agent (Codex or Claude) if requested
                if start_codex:
                    if not account_name:
                        disable_codex_update_prompt()
                    pane = window.active_pane
                    if pane:
                        cmd = _agent_command_for_launch()
                        if resume_session_id:
                            resume_target = _resume_target_id(resume_session_id)
                            resume_arg = shlex.quote(resume_target)
                            if config.agent_type == "claude":
                                cmd = f"{cmd} --resume {resume_arg}"
                            else:
                                cmd = f"{cmd} resume {resume_arg}"
                        if account_name:
                            if config.agent_type == "claude":
                                account_home = ensure_account_home(account_name)
                                cmd = (
                                    f"export CLAUDE_HOME={shlex.quote(str(account_home))}; "
                                    f"{cmd}"
                                )
                            else:
                                account_home = ensure_account_home(account_name)
                                cmd = (
                                    f"export CODEX_HOME={shlex.quote(str(account_home))}; "
                                    f"{cmd}"
                                )
                        pane.send_keys(cmd, enter=True)

                logger.info(
                    "Created window '%s' (id=%s) at %s",
                    final_window_name,
                    wid,
                    path,
                )
                return (
                    True,
                    f"Created window '{final_window_name}' at {path}",
                    final_window_name,
                    wid,
                )

            except Exception as e:
                logger.error(f"Failed to create window: {e}")
                return False, f"Failed to create window: {e}", "", ""

        return await asyncio.to_thread(_create_and_start)


# Global instance with default session name
tmux_manager = TmuxManager()
