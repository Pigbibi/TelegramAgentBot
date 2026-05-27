"""Session monitoring service — recursive Codex transcript mode.

Scans the Codex transcript root recursively, auto-binds newly discovered
sessions to matching tmux windows by cwd, and emits parsed messages to the bot.
"""

import asyncio
import contextlib
import json
import logging
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Awaitable, Callable

import aiofiles

from .account_manager import list_account_homes
from .config import config
from .monitor_state import MonitorState, TrackedSession
from .session import _is_shell_pane_command, _iter_transcript_roots, _session_ids_match
from .tmux_manager import tmux_manager
from .transcript_parser import PendingToolInfo, TranscriptParser
from .utils import is_subagent_transcript, read_cwd_from_jsonl

logger = logging.getLogger(__name__)
_ENTRY_END_OFFSET_KEY = "_telegram_codex_bot_end_offset"


@dataclass
class SessionInfo:
    """Information about a Codex session transcript file."""

    session_id: str
    file_path: Path
    cwd: str = ""


@dataclass
class NewMessage:
    """A new parsed message ready for Telegram delivery."""

    session_id: str
    text: str
    is_complete: bool
    content_type: str = "text"
    tool_use_id: str | None = None
    role: str = "assistant"
    tool_name: str | None = None
    image_data: list[tuple[str, bytes]] | None = None
    source_offset: int = 0


class SessionMonitor:
    """Monitor Codex transcripts and auto-bind them to tmux windows."""

    def __init__(
        self,
        projects_path: Path | None = None,
        poll_interval: float | None = None,
        state_file: Path | None = None,
    ) -> None:
        self.projects_path = (
            projects_path if projects_path is not None else config.codex_projects_path
        )
        self.poll_interval = (
            poll_interval if poll_interval is not None else config.monitor_poll_interval
        )
        self.state = MonitorState(state_file=state_file or config.monitor_state_file)
        self.state.load()
        self._running = False
        self._task: asyncio.Task | None = None
        self._message_callback: Callable[[NewMessage], Awaitable[None]] | None = None
        self._pending_tools: dict[str, dict[str, PendingToolInfo]] = {}
        self._file_mtimes: dict[str, float] = {}
        self._deferred_state_updates: dict[str, TrackedSession] = {}

    def set_message_callback(
        self, callback: Callable[[NewMessage], Awaitable[None]]
    ) -> None:
        self._message_callback = callback

    @staticmethod
    def _is_complete_json_line(line: str) -> bool:
        """Return True when a line is valid JSON, even if it is not a message."""
        try:
            json.loads(line)
        except json.JSONDecodeError:
            return False
        return True

    @staticmethod
    def _normalize_path(path_str: str) -> str:
        """Normalize cwd values for stable comparisons."""
        try:
            return str(Path(path_str).resolve())
        except OSError:
            return path_str

    @staticmethod
    def _window_id_sort_key(window_id: str) -> tuple[int, str]:
        """Sort tmux window ids numerically, newest last."""
        if window_id.startswith("@") and window_id[1:].isdigit():
            return (int(window_id[1:]), window_id)
        return (10**9, window_id)

    @staticmethod
    def _is_account_home_transcript(file_path: Path) -> bool:
        """Return whether a transcript lives under a telegram-codex-bot account home."""
        try:
            resolved = file_path.resolve()
        except OSError:
            resolved = file_path
        for account_home in list_account_homes():
            try:
                if resolved.is_relative_to(account_home.resolve()):
                    return True
            except OSError:
                continue
        return False

    @classmethod
    def _can_auto_bind_transcript(cls, file_path: Path) -> bool:
        """Avoid auto-binding unrelated local Codex history when accounts exist."""
        if is_subagent_transcript(file_path):
            return False
        account_homes = list_account_homes()
        if not account_homes:
            return True
        return cls._is_account_home_transcript(file_path)

    @staticmethod
    def _is_older_than_pending_launch(
        file_path: Path | None,
        state: Any,
        *,
        tolerance_seconds: float = 1.0,
    ) -> bool:
        """Return True when a transcript predates a pending fresh window launch."""
        launch_started_at = float(getattr(state, "launch_started_at", 0.0) or 0.0)
        if not file_path or not launch_started_at or getattr(state, "session_id", ""):
            return False
        try:
            return file_path.stat().st_mtime + tolerance_seconds < launch_started_at
        except OSError:
            return False

    async def _get_active_cwds(self) -> set[str]:
        """Get normalized cwds of all active tmux windows."""
        cwds = set()
        windows = await tmux_manager.list_windows()
        for window in windows:
            cwds.add(self._normalize_path(window.cwd))
        return cwds

    def _iter_session_files(self) -> list[Path]:
        """Recursively find candidate transcript files under the Codex root."""
        files: list[Path] = []
        seen: set[str] = set()

        for base_path in _iter_transcript_roots():
            base_path = base_path.expanduser()
            if not base_path.exists():
                continue

            for jsonl_file in base_path.rglob("*.jsonl"):
                if not jsonl_file.is_file():
                    continue
                lower_name = jsonl_file.name.lower()
                if "index" in lower_name or "history" in lower_name:
                    continue
                key = str(jsonl_file.resolve())
                if key in seen:
                    continue
                seen.add(key)
                files.append(jsonl_file)
        return files

    @staticmethod
    def _extract_usage_limit_message(line: str) -> str | None:
        """Extract a human-readable message from a usage_limit_exceeded event."""
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            return None

        if data.get("type") != "event_msg":
            return None

        payload = data.get("payload")
        if not isinstance(payload, dict):
            return None

        if (
            payload.get("type") != "error"
            or payload.get("codex_error_info") != "usage_limit_exceeded"
        ):
            return None

        message = payload.get("message")
        if isinstance(message, str) and message.strip():
            return message.strip()
        return "You've hit your usage limit."

    async def scan_projects(self) -> list[SessionInfo]:
        """Scan Codex transcripts whose cwd still matches a live tmux window."""
        active_cwds = await self._get_active_cwds()
        sessions: list[SessionInfo] = []

        for jsonl_file in self._iter_session_files():
            file_cwd = await asyncio.to_thread(read_cwd_from_jsonl, jsonl_file)
            if not file_cwd:
                continue
            if self._normalize_path(file_cwd) not in active_cwds:
                continue
            sessions.append(
                SessionInfo(
                    session_id=jsonl_file.stem, file_path=jsonl_file, cwd=file_cwd
                )
            )

        sessions.sort(
            key=lambda session: session.file_path.stat().st_mtime,
            reverse=True,
        )
        return sessions

    async def _read_new_lines(
        self, session: TrackedSession, file_path: Path
    ) -> tuple[list[dict], list[str]]:
        """Read incremental JSONL entries and keep offsets at valid boundaries."""
        new_entries: list[dict] = []
        usage_limit_messages: list[str] = []
        try:
            async with aiofiles.open(file_path, "r", encoding="utf-8") as file_obj:
                await file_obj.seek(0, 2)
                file_size = await file_obj.tell()

                if session.last_byte_offset > file_size:
                    logger.info(
                        "File truncated for session %s (offset %d > size %d). Resetting.",
                        session.session_id,
                        session.last_byte_offset,
                        file_size,
                    )
                    session.last_byte_offset = 0

                await file_obj.seek(session.last_byte_offset)

                if session.last_byte_offset > 0:
                    first_char = await file_obj.read(1)
                    if first_char and first_char != "{":
                        logger.warning(
                            "Corrupted offset %d in session %s, scanning to next line",
                            session.last_byte_offset,
                            session.session_id,
                        )
                        await file_obj.readline()
                        session.last_byte_offset = await file_obj.tell()
                        return [], []
                    await file_obj.seek(session.last_byte_offset)

                safe_offset = session.last_byte_offset
                async for line in file_obj:
                    line_end_offset = await file_obj.tell()
                    usage_limit_text = self._extract_usage_limit_message(line)
                    if usage_limit_text:
                        usage_limit_messages.append(usage_limit_text)

                    data = TranscriptParser.parse_line(line)
                    if data:
                        data[_ENTRY_END_OFFSET_KEY] = line_end_offset
                        new_entries.append(data)
                        safe_offset = line_end_offset
                    elif not line.strip():
                        safe_offset = line_end_offset
                    elif self._is_complete_json_line(line):
                        safe_offset = line_end_offset
                    elif line.strip():
                        logger.warning(
                            "Partial JSONL line in session %s, will retry next cycle",
                            session.session_id,
                        )
                        break

                session.last_byte_offset = safe_offset
        except OSError as exc:
            logger.error("Error reading session file %s: %s", file_path, exc)

        return new_entries, usage_limit_messages

    async def _auto_bind_session_to_window(
        self,
        session_id: str,
        project_path: str,
        session_manager: Any,
        *,
        session_file: Path | None = None,
    ) -> None:
        """Bind one discovered session to at most one matching tmux window."""
        if session_file and not self._can_auto_bind_transcript(session_file):
            return

        normalized_project_path = self._normalize_path(project_path)
        if not normalized_project_path:
            return

        for _user_id, _thread_id, window_id in session_manager.iter_thread_bindings():
            state = session_manager.get_window_state(window_id)
            if _session_ids_match(state.session_id, session_id):
                return

        windows = await tmux_manager.list_windows()
        matching_windows = [
            window
            for window in windows
            if self._normalize_path(window.cwd) == normalized_project_path
            and not _is_shell_pane_command(
                (getattr(window, "pane_current_command", "") or "").strip()
            )
            and not self._is_older_than_pending_launch(
                session_file,
                session_manager.get_window_state(window.window_id),
            )
        ]
        if not matching_windows:
            return

        bound_window_ids = {
            window_id
            for _user_id, _thread_id, window_id in session_manager.iter_thread_bindings()
        }

        candidate_windows = [
            window
            for window in matching_windows
            if window.window_id in bound_window_ids
            and not session_manager.get_window_state(window.window_id).session_id
        ]
        if not candidate_windows:
            candidate_windows = [
                window
                for window in matching_windows
                if not session_manager.get_window_state(window.window_id).session_id
            ]
        if not candidate_windows:
            stale_bound_windows = []
            for window in matching_windows:
                if window.window_id not in bound_window_ids:
                    continue
                state = session_manager.get_window_state(window.window_id)
                if state.session_id and (
                    self._normalize_path(state.cwd) != normalized_project_path
                ):
                    stale_bound_windows.append(window)
            candidate_windows = stale_bound_windows
        if not candidate_windows:
            return

        candidate = max(
            candidate_windows,
            key=lambda window: self._window_id_sort_key(window.window_id or ""),
        )
        logger.info(
            "Auto-binding session %s to window %s (%s)",
            session_id,
            candidate.window_id,
            candidate.window_name,
        )
        session_manager.register_session_to_window(
            candidate.window_id,
            session_id,
            project_path,
            window_name=candidate.window_name,
            persist_session_map=True,
        )

    def commit_deferred_state_updates(self) -> None:
        """Persist monitor offsets after queued Telegram delivery completes."""
        if not self._deferred_state_updates:
            return
        for tracked in self._deferred_state_updates.values():
            self.state.update_session(tracked)
        self._deferred_state_updates.clear()
        self.state.save_if_dirty()

    def discard_deferred_state_updates(self) -> None:
        """Drop uncommitted offsets so undelivered transcript lines can replay."""
        self._deferred_state_updates.clear()

    @staticmethod
    def _drop_backlog_before_latest_user(
        session_id: str,
        entries: list[Any],
    ) -> list[Any]:
        """Drop stale assistant output that predates a newer prompt.

        If the monitor falls behind, one unread slice may contain assistant output
        for an older prompt followed by a newer user prompt and its later output.
        Telegram cannot place the older output above the message the user already
        sent, and replaying it after the new prompt is confusing, so keep the
        newest user prompt and later output only.
        """
        latest_user_index: int | None = None
        for index, entry in enumerate(entries):
            if (
                getattr(entry, "role", None) == "user"
                and getattr(entry, "content_type", None) == "text"
            ):
                latest_user_index = index

        if latest_user_index is None or latest_user_index == 0:
            return entries

        filtered: list[Any] = []
        dropped_count = 0
        for index, entry in enumerate(entries):
            if index < latest_user_index and getattr(entry, "role", None) != "user":
                dropped_count += 1
                continue
            filtered.append(entry)

        if dropped_count:
            logger.warning(
                "Dropped %d stale transcript message(s) before latest user prompt "
                "for session %s",
                dropped_count,
                session_id,
            )
        return filtered

    @staticmethod
    def _initial_offset_from_user_window_offsets(
        session_id: str,
        file_path: Path,
        session_manager: Any,
    ) -> int:
        """Return a safe resume offset for an already-bound session.

        When monitor_state loses or falls behind a tracked session but Telegram
        has already advanced a bound window offset, reading older bytes replays
        old output.  Reuse the largest per-user window offset for that bound
        session instead.
        """
        try:
            file_size = file_path.stat().st_size
        except OSError:
            file_size = 0

        offsets: list[int] = []
        for _user_id, _thread_id, window_id in session_manager.iter_thread_bindings():
            state = session_manager.get_window_state(window_id)
            if not _session_ids_match(getattr(state, "session_id", ""), session_id):
                continue
            for user_offsets in getattr(
                session_manager, "user_window_offsets", {}
            ).values():
                offset = user_offsets.get(window_id)
                if isinstance(offset, int) and 0 < offset <= file_size:
                    offsets.append(offset)

        return max(offsets, default=0)

    async def check_for_updates(
        self,
        active_session_ids: set[str],
        *,
        save_state: bool = True,
    ) -> list[NewMessage]:
        """Check tracked Codex sessions for new parsed entries."""
        del active_session_ids

        new_messages: list[NewMessage] = []
        if not save_state:
            self._deferred_state_updates.clear()
        sessions = await self.scan_projects()

        from .session import session_manager

        for session_info in sessions:
            try:
                project_path = session_info.cwd
                tracked = self.state.get_session(session_info.session_id)
                if tracked is None:
                    initial_offset = self._initial_offset_from_user_window_offsets(
                        session_info.session_id,
                        session_info.file_path,
                        session_manager,
                    )
                    if initial_offset:
                        logger.warning(
                            "Initialized missing monitor offset for session %s from "
                            "user window offset: %d",
                            session_info.session_id,
                            initial_offset,
                        )
                    tracked = TrackedSession(
                        session_id=session_info.session_id,
                        file_path=str(session_info.file_path),
                        last_byte_offset=initial_offset,
                    )
                    if save_state:
                        self.state.update_session(tracked)

                    if not project_path:
                        project_path = await asyncio.to_thread(
                            read_cwd_from_jsonl,
                            session_info.file_path,
                        )
                elif not save_state:
                    tracked = replace(tracked)

                if tracked.file_path != str(session_info.file_path):
                    tracked.file_path = str(session_info.file_path)

                resume_offset = self._initial_offset_from_user_window_offsets(
                    session_info.session_id,
                    session_info.file_path,
                    session_manager,
                )
                if resume_offset > tracked.last_byte_offset:
                    logger.warning(
                        "Fast-forwarded stale monitor offset for session %s from "
                        "%d to user window offset %d",
                        session_info.session_id,
                        tracked.last_byte_offset,
                        resume_offset,
                    )
                    tracked.last_byte_offset = resume_offset
                    if save_state:
                        self.state.update_session(tracked)

                if project_path and not session_manager.has_bound_thread_for_session(
                    session_info.session_id
                ):
                    await self._auto_bind_session_to_window(
                        session_info.session_id,
                        project_path,
                        session_manager,
                        session_file=session_info.file_path,
                    )

                try:
                    stat_result = session_info.file_path.stat()
                except OSError:
                    continue

                current_mtime = stat_result.st_mtime
                current_size = stat_result.st_size
                last_mtime = self._file_mtimes.get(session_info.session_id, 0.0)
                if (
                    current_mtime <= last_mtime
                    and current_size <= tracked.last_byte_offset
                ):
                    continue

                new_entries, usage_limit_messages = await self._read_new_lines(
                    tracked, session_info.file_path
                )
                self._file_mtimes[session_info.session_id] = current_mtime

                for usage_limit_text in usage_limit_messages:
                    new_messages.append(
                        NewMessage(
                            session_id=session_info.session_id,
                            text=usage_limit_text,
                            is_complete=True,
                            content_type="usage_limit",
                            role="assistant",
                        )
                    )

                carry = self._pending_tools.get(session_info.session_id, {})
                parsed_entries, remaining = TranscriptParser.parse_entries(
                    new_entries,
                    pending_tools=carry,
                )
                if remaining:
                    self._pending_tools[session_info.session_id] = remaining
                else:
                    self._pending_tools.pop(session_info.session_id, None)

                parsed_entries = self._drop_backlog_before_latest_user(
                    session_info.session_id,
                    parsed_entries,
                )

                for entry in parsed_entries:
                    if not entry.text and not entry.image_data:
                        continue
                    if entry.role == "user" and not config.show_user_messages:
                        continue
                    new_messages.append(
                        NewMessage(
                            session_id=session_info.session_id,
                            text=entry.text,
                            is_complete=True,
                            content_type=entry.content_type,
                            tool_use_id=entry.tool_use_id,
                            role=entry.role,
                            tool_name=entry.tool_name,
                            image_data=entry.image_data,
                            source_offset=entry.source_offset,
                        )
                    )

                if save_state:
                    self.state.update_session(tracked)
                else:
                    self._deferred_state_updates[tracked.session_id] = replace(tracked)
            except OSError as exc:
                logger.debug(
                    "Error processing session %s: %s",
                    session_info.session_id,
                    exc,
                )

        if save_state:
            self.state.save_if_dirty()
        return new_messages

    async def _dispatch_new_messages(self, messages: list[NewMessage]) -> None:
        """Dispatch transcript messages without letting one session starve others.

        Message queue workers already preserve Telegram ordering per topic. The
        monitor should still preserve ordering within each Codex session, but it
        should not make an unrelated topic wait behind a large backlog from
        another session.
        """
        callback = self._message_callback
        if not messages or callback is None:
            return

        groups: dict[str, list[NewMessage]] = {}
        for message in messages:
            groups.setdefault(message.session_id, []).append(message)

        async def _dispatch_group(group: list[NewMessage]) -> None:
            for message in group:
                await callback(message)

        await asyncio.gather(*(_dispatch_group(group) for group in groups.values()))

    async def _monitor_loop(self) -> None:
        """Poll recursively discovered transcripts and forward new messages."""
        logger.info("Monitor started (Aggressive Auto-Binding Mode)")

        from .session import session_manager

        cleared = session_manager.cleanup_duplicate_window_sessions()
        if cleared:
            logger.info("Cleared duplicate window sessions on startup: %s", cleared)

        while self._running:
            try:
                await session_manager.load_session_map()
                new_messages = await self.check_for_updates(set(), save_state=False)
                await self._dispatch_new_messages(new_messages)
                self.commit_deferred_state_updates()
            except asyncio.CancelledError:
                self.discard_deferred_state_updates()
                raise
            except Exception as exc:
                self.discard_deferred_state_updates()
                logger.error("Loop error: %s", exc)
            await asyncio.sleep(self.poll_interval)

    def start(self) -> None:
        if self._running:
            logger.warning("Monitor already running")
            return
        self._running = True
        self._task = asyncio.create_task(self._monitor_loop())

    async def stop(self) -> None:
        self._running = False
        task = self._task
        if task:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            self._task = None
        self.discard_deferred_state_updates()
        self.state.save()
