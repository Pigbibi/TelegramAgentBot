"""Unit tests for SessionMonitor JSONL reading and offset handling."""

import asyncio
import json
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from unittest.mock import patch

import pytest

from telegram_agent_bot.monitor_state import TrackedSession
from telegram_agent_bot.session_monitor import NewMessage, SessionInfo, SessionMonitor


class TestSessionMonitorDispatch:
    """Tests for monitor dispatch fairness across sessions."""

    @pytest.mark.asyncio
    async def test_dispatches_different_sessions_concurrently(self, tmp_path):
        monitor = SessionMonitor(
            projects_path=tmp_path / "projects",
            state_file=tmp_path / "monitor_state.json",
        )
        first_session_can_finish = asyncio.Event()
        first_session_started = asyncio.Event()
        started: list[str] = []
        completed: list[str] = []

        async def callback(message: NewMessage) -> None:
            started.append(message.text)
            if message.text == "a1":
                first_session_started.set()
                await first_session_can_finish.wait()
            completed.append(message.text)

        monitor.set_message_callback(callback)
        dispatch_task = asyncio.create_task(
            monitor._dispatch_new_messages(
                [
                    NewMessage("session-a", "a1", True),
                    NewMessage("session-a", "a2", True),
                    NewMessage("session-b", "b1", True),
                ]
            )
        )

        await asyncio.wait_for(first_session_started.wait(), timeout=1)
        await asyncio.sleep(0.05)

        assert "b1" in started
        assert "a2" not in started

        first_session_can_finish.set()
        await dispatch_task

        assert completed.index("a1") < completed.index("a2")
        assert "b1" in completed

    @pytest.mark.asyncio
    async def test_dispatch_timeout_does_not_block_other_sessions(
        self, tmp_path, monkeypatch
    ):
        monitor = SessionMonitor(
            projects_path=tmp_path / "projects",
            state_file=tmp_path / "monitor_state.json",
        )
        first_session_started = asyncio.Event()
        completed: list[str] = []

        monkeypatch.setattr(
            "telegram_agent_bot.session_monitor._DISPATCH_GROUP_TIMEOUT_SECONDS",
            0.05,
        )

        async def callback(message: NewMessage) -> None:
            if message.session_id == "session-a":
                first_session_started.set()
                await asyncio.sleep(60)
            completed.append(message.text)

        monitor.set_message_callback(callback)

        dispatched_session_ids = await monitor._dispatch_new_messages(
            [
                NewMessage("session-a", "a1", True),
                NewMessage("session-b", "b1", True),
            ]
        )

        assert first_session_started.is_set()
        assert completed == ["b1"]
        assert dispatched_session_ids == {"session-b"}

    def test_commit_deferred_state_updates_can_commit_subset(self, tmp_path):
        monitor = SessionMonitor(
            projects_path=tmp_path / "projects",
            state_file=tmp_path / "monitor_state.json",
        )
        monitor._deferred_state_updates = {
            "session-a": TrackedSession(
                session_id="session-a",
                file_path="a.jsonl",
                last_byte_offset=10,
            ),
            "session-b": TrackedSession(
                session_id="session-b",
                file_path="b.jsonl",
                last_byte_offset=20,
            ),
        }

        monitor.commit_deferred_state_updates({"session-b"})

        assert monitor.state.get_session("session-a") is None
        assert monitor.state.get_session("session-b").last_byte_offset == 20
        assert set(monitor._deferred_state_updates) == {"session-a"}

        monitor.discard_deferred_state_updates({"session-a"})

        assert monitor._deferred_state_updates == {}

    @pytest.mark.asyncio
    async def test_dispatch_timeout_commits_delivered_message_offset(
        self, tmp_path, monkeypatch
    ):
        monitor = SessionMonitor(
            projects_path=tmp_path / "projects",
            state_file=tmp_path / "monitor_state.json",
        )
        monkeypatch.setattr(
            "telegram_agent_bot.session_monitor._DISPATCH_GROUP_TIMEOUT_SECONDS",
            0.05,
        )
        monitor.state.update_session(
            TrackedSession(
                session_id="session-a",
                file_path="a.jsonl",
                last_byte_offset=0,
            )
        )
        monitor._deferred_state_updates = {
            "session-a": TrackedSession(
                session_id="session-a",
                file_path="a.jsonl",
                last_byte_offset=30,
            )
        }

        async def callback(message: NewMessage) -> None:
            if message.text == "second":
                await asyncio.sleep(60)

        monitor.set_message_callback(callback)

        dispatched_session_ids = await monitor._dispatch_new_messages(
            [
                NewMessage("session-a", "first", True, source_offset=10),
                NewMessage("session-a", "second", True, source_offset=20),
            ]
        )

        assert dispatched_session_ids == set()
        assert monitor.state.get_session("session-a").last_byte_offset == 10
        assert monitor._deferred_state_updates["session-a"].last_byte_offset == 30

    @pytest.mark.asyncio
    async def test_dispatch_batches_large_session_without_full_commit(
        self, tmp_path, monkeypatch
    ):
        monitor = SessionMonitor(
            projects_path=tmp_path / "projects",
            state_file=tmp_path / "monitor_state.json",
        )
        monkeypatch.setattr(
            "telegram_agent_bot.session_monitor._DISPATCH_GROUP_MAX_MESSAGES",
            1,
        )
        monitor.state.update_session(
            TrackedSession(
                session_id="session-a",
                file_path="a.jsonl",
                last_byte_offset=0,
            )
        )
        monitor._deferred_state_updates = {
            "session-a": TrackedSession(
                session_id="session-a",
                file_path="a.jsonl",
                last_byte_offset=30,
            )
        }
        delivered: list[str] = []

        async def callback(message: NewMessage) -> None:
            delivered.append(message.text)

        monitor.set_message_callback(callback)

        dispatched_session_ids = await monitor._dispatch_new_messages(
            [
                NewMessage("session-a", "first", True, source_offset=10),
                NewMessage("session-a", "second", True, source_offset=20),
            ]
        )

        assert delivered == ["first"]
        assert dispatched_session_ids == set()
        assert monitor.state.get_session("session-a").last_byte_offset == 10
        assert monitor._deferred_state_updates["session-a"].last_byte_offset == 30


class TestReadNewLinesOffsetRecovery:
    """Tests for _read_new_lines offset corruption recovery."""

    @pytest.fixture
    def monitor(self, tmp_path):
        """Create a SessionMonitor with temp state file."""
        return SessionMonitor(
            projects_path=tmp_path / "projects",
            state_file=tmp_path / "monitor_state.json",
        )

    @pytest.mark.asyncio
    async def test_mid_line_offset_recovery(self, monitor, tmp_path, make_jsonl_entry):
        """Recover from corrupted offset pointing mid-line."""
        # Create JSONL file with two valid lines
        jsonl_file = tmp_path / "session.jsonl"
        entry1 = make_jsonl_entry(msg_type="assistant", content="first message")
        entry2 = make_jsonl_entry(msg_type="assistant", content="second message")
        jsonl_file.write_text(
            json.dumps(entry1) + "\n" + json.dumps(entry2) + "\n",
            encoding="utf-8",
        )

        # Calculate offset pointing into the middle of line 1
        line1_bytes = len(json.dumps(entry1).encode("utf-8")) // 2
        session = TrackedSession(
            session_id="test-session",
            file_path=str(jsonl_file),
            last_byte_offset=line1_bytes,  # Mid-line (corrupted)
        )

        # Read should recover and return empty (offset moved to next line)
        entries, usage_limit_messages = await monitor._read_new_lines(
            session, jsonl_file
        )

        # Should return empty list (recovery skips to next line, no new content yet)
        assert entries == []
        assert usage_limit_messages == []

        # Offset should now point to start of line 2
        line1_full = len(json.dumps(entry1).encode("utf-8")) + 1  # +1 for newline
        assert session.last_byte_offset == line1_full

    @pytest.mark.asyncio
    async def test_valid_offset_reads_normally(
        self, monitor, tmp_path, make_jsonl_entry
    ):
        """Normal reading when offset points to line start."""
        jsonl_file = tmp_path / "session.jsonl"
        entry1 = make_jsonl_entry(msg_type="assistant", content="first")
        entry2 = make_jsonl_entry(msg_type="assistant", content="second")
        jsonl_file.write_text(
            json.dumps(entry1) + "\n" + json.dumps(entry2) + "\n",
            encoding="utf-8",
        )

        # Offset at 0 should read both lines
        session = TrackedSession(
            session_id="test-session",
            file_path=str(jsonl_file),
            last_byte_offset=0,
        )

        entries, usage_limit_messages = await monitor._read_new_lines(
            session, jsonl_file
        )

        assert len(entries) == 2
        assert usage_limit_messages == []
        assert session.last_byte_offset == jsonl_file.stat().st_size

    @pytest.mark.asyncio
    async def test_truncation_detection(self, monitor, tmp_path, make_jsonl_entry):
        """Detect file truncation and reset offset."""
        jsonl_file = tmp_path / "session.jsonl"
        entry = make_jsonl_entry(msg_type="assistant", content="content")
        jsonl_file.write_text(json.dumps(entry) + "\n", encoding="utf-8")

        # Set offset beyond file size (simulates truncation)
        session = TrackedSession(
            session_id="test-session",
            file_path=str(jsonl_file),
            last_byte_offset=9999,  # Beyond file size
        )

        entries, usage_limit_messages = await monitor._read_new_lines(
            session, jsonl_file
        )

        # Should reset offset to 0 and read the line
        assert session.last_byte_offset == jsonl_file.stat().st_size
        assert len(entries) == 1
        assert usage_limit_messages == []

    @pytest.mark.asyncio
    async def test_usage_limit_event_emits_notification(self, monitor, tmp_path):
        """A usage_limit_exceeded event should be surfaced as a monitor message."""
        jsonl_file = tmp_path / "session.jsonl"
        event = {
            "type": "event_msg",
            "payload": {
                "type": "error",
                "message": "You've hit your usage limit.",
                "codex_error_info": "usage_limit_exceeded",
            },
        }
        jsonl_file.write_text(json.dumps(event) + "\n", encoding="utf-8")

        monitor.scan_projects = AsyncMock(
            return_value=[
                SessionInfo(
                    session_id="session-4",
                    file_path=jsonl_file,
                )
            ]
        )
        tracked = TrackedSession(
            session_id="session-4",
            file_path=str(jsonl_file),
            last_byte_offset=0,
        )
        monitor.state.update_session(tracked)

        messages = await monitor.check_for_updates(set())

        assert len(messages) == 1
        assert messages[0].content_type == "usage_limit"
        assert "usage limit" in messages[0].text.lower()

    @pytest.mark.asyncio
    async def test_generic_error_event_emits_assistant_message(self, monitor, tmp_path):
        """A non-usage Codex error event should be visible in Telegram output."""
        jsonl_file = tmp_path / "session.jsonl"
        event = {
            "type": "event_msg",
            "payload": {
                "type": "error",
                "message": "Your access token could not be refreshed.",
            },
        }
        jsonl_file.write_text(json.dumps(event) + "\n", encoding="utf-8")

        monitor.scan_projects = AsyncMock(
            return_value=[
                SessionInfo(
                    session_id="session-auth-error",
                    file_path=jsonl_file,
                )
            ]
        )
        monitor.state.update_session(
            TrackedSession(
                session_id="session-auth-error",
                file_path=str(jsonl_file),
                last_byte_offset=0,
            )
        )

        messages = await monitor.check_for_updates(set())

        assert len(messages) == 1
        assert messages[0].content_type == "text"
        assert messages[0].role == "assistant"
        assert messages[0].text == (
            "⚠️ Codex error: Your access token could not be refreshed.\n\nUse /codexlogin to start a Codex device login from Telegram."
        )

    @pytest.mark.asyncio
    async def test_stale_backlog_before_latest_user_is_dropped(
        self, monitor, tmp_path, make_jsonl_entry
    ):
        """Lagged monitor backlog should not replay older replies after a new prompt."""
        jsonl_file = tmp_path / "session.jsonl"
        old_answer = make_jsonl_entry(msg_type="assistant", content="old answer")
        latest_user = make_jsonl_entry(msg_type="user", content="new question")
        latest_answer = make_jsonl_entry(msg_type="assistant", content="new answer")
        jsonl_file.write_text(
            "\n".join(
                json.dumps(entry) for entry in (old_answer, latest_user, latest_answer)
            )
            + "\n",
            encoding="utf-8",
        )

        monitor.scan_projects = AsyncMock(
            return_value=[
                SessionInfo(
                    session_id="session-stale-backlog",
                    file_path=jsonl_file,
                )
            ]
        )
        monitor.state.update_session(
            TrackedSession(
                session_id="session-stale-backlog",
                file_path=str(jsonl_file),
                last_byte_offset=0,
            )
        )

        with patch("telegram_agent_bot.session_monitor.config") as mock_config:
            mock_config.show_user_messages = False
            messages = await monitor.check_for_updates(set())

        assert [message.text for message in messages] == ["new answer"]

    @pytest.mark.asyncio
    async def test_missing_monitor_state_resumes_from_user_window_offset(
        self, monitor, tmp_path, make_jsonl_entry
    ):
        """A missing tracked session should not replay bytes already sent to a bound topic."""
        jsonl_file = tmp_path / "session.jsonl"
        old_answer = make_jsonl_entry(msg_type="assistant", content="old answer")
        jsonl_file.write_text(json.dumps(old_answer) + "\n", encoding="utf-8")
        old_size = jsonl_file.stat().st_size
        new_answer = make_jsonl_entry(msg_type="assistant", content="new answer")
        with jsonl_file.open("a", encoding="utf-8") as f:
            f.write(json.dumps(new_answer) + "\n")

        monitor.scan_projects = AsyncMock(
            return_value=[
                SessionInfo(
                    session_id="session-missing-state",
                    file_path=jsonl_file,
                )
            ]
        )

        state = SimpleNamespace(session_id="session-missing-state")
        with patch("telegram_agent_bot.session.session_manager") as mock_sm:
            mock_sm.has_bound_thread_for_session.return_value = True
            mock_sm.iter_thread_bindings.return_value = [(12345, 42, "@1")]
            mock_sm.get_window_state.return_value = state
            mock_sm.user_window_offsets = {12345: {"@1": old_size}}
            messages = await monitor.check_for_updates(set())

        assert [message.text for message in messages] == ["new answer"]
        tracked = monitor.state.get_session("session-missing-state")
        assert tracked is not None
        assert tracked.last_byte_offset == jsonl_file.stat().st_size

    @pytest.mark.asyncio
    async def test_stale_monitor_state_fast_forwards_from_user_window_offset(
        self, monitor, tmp_path, make_jsonl_entry
    ):
        """A tracked session behind the delivered window offset should not replay."""
        jsonl_file = tmp_path / "session.jsonl"
        old_answer = make_jsonl_entry(msg_type="assistant", content="old answer")
        jsonl_file.write_text(json.dumps(old_answer) + "\n", encoding="utf-8")
        old_size = jsonl_file.stat().st_size
        new_answer = make_jsonl_entry(msg_type="assistant", content="new answer")
        with jsonl_file.open("a", encoding="utf-8") as f:
            f.write(json.dumps(new_answer) + "\n")

        monitor.scan_projects = AsyncMock(
            return_value=[
                SessionInfo(
                    session_id="session-stale-state",
                    file_path=jsonl_file,
                )
            ]
        )
        monitor.state.update_session(
            TrackedSession(
                session_id="session-stale-state",
                file_path=str(jsonl_file),
                last_byte_offset=0,
            )
        )

        state = SimpleNamespace(session_id="session-stale-state")
        with patch("telegram_agent_bot.session.session_manager") as mock_sm:
            mock_sm.has_bound_thread_for_session.return_value = True
            mock_sm.iter_thread_bindings.return_value = [(12345, 42, "@1")]
            mock_sm.get_window_state.return_value = state
            mock_sm.user_window_offsets = {12345: {"@1": old_size}}
            messages = await monitor.check_for_updates(set())

        assert [message.text for message in messages] == ["new answer"]
        tracked = monitor.state.get_session("session-stale-state")
        assert tracked is not None
        assert tracked.last_byte_offset == jsonl_file.stat().st_size

    @pytest.mark.asyncio
    async def test_stale_monitor_state_uses_earliest_bound_user_offset(
        self, monitor, tmp_path, make_jsonl_entry
    ):
        """A shared session should resume from the earliest bound recipient offset."""
        jsonl_file = tmp_path / "session.jsonl"
        first = make_jsonl_entry(msg_type="assistant", content="first answer")
        second = make_jsonl_entry(msg_type="assistant", content="second answer")
        third = make_jsonl_entry(msg_type="assistant", content="third answer")
        first_line = json.dumps(first) + "\n"
        second_line = json.dumps(second) + "\n"
        third_line = json.dumps(third) + "\n"
        jsonl_file.write_text(
            first_line + second_line + third_line,
            encoding="utf-8",
        )
        first_offset = len(first_line.encode("utf-8"))
        second_offset = first_offset + len(second_line.encode("utf-8"))

        monitor.scan_projects = AsyncMock(
            return_value=[
                SessionInfo(
                    session_id="session-shared-state",
                    file_path=jsonl_file,
                )
            ]
        )
        monitor.state.update_session(
            TrackedSession(
                session_id="session-shared-state",
                file_path=str(jsonl_file),
                last_byte_offset=0,
            )
        )

        state = SimpleNamespace(session_id="session-shared-state")
        with patch("telegram_agent_bot.session.session_manager") as mock_sm:
            mock_sm.has_bound_thread_for_session.return_value = True
            mock_sm.iter_thread_bindings.return_value = [
                (11111, 41, "@1"),
                (22222, 42, "@1"),
            ]
            mock_sm.get_window_state.return_value = state
            mock_sm.user_window_offsets = {
                11111: {"@1": second_offset},
                22222: {"@1": first_offset},
            }
            messages = await monitor.check_for_updates(set())

        assert [message.text for message in messages] == [
            "second answer",
            "third answer",
        ]
        tracked = monitor.state.get_session("session-shared-state")
        assert tracked is not None
        assert tracked.last_byte_offset == jsonl_file.stat().st_size

    @pytest.mark.asyncio
    async def test_messages_carry_source_line_offsets(
        self, monitor, tmp_path, make_jsonl_entry
    ):
        """Delivered messages should expose the JSONL offset they came from."""
        jsonl_file = tmp_path / "session.jsonl"
        first = make_jsonl_entry(msg_type="assistant", content="first")
        second = make_jsonl_entry(msg_type="assistant", content="second")
        first_line = json.dumps(first) + "\n"
        second_line = json.dumps(second) + "\n"
        jsonl_file.write_text(first_line + second_line, encoding="utf-8")
        first_offset = len(first_line.encode("utf-8"))
        second_offset = first_offset + len(second_line.encode("utf-8"))

        monitor.scan_projects = AsyncMock(
            return_value=[
                SessionInfo(
                    session_id="session-offsets",
                    file_path=jsonl_file,
                )
            ]
        )
        monitor.state.update_session(
            TrackedSession(
                session_id="session-offsets",
                file_path=str(jsonl_file),
                last_byte_offset=0,
            )
        )

        messages = await monitor.check_for_updates(set())

        assert [(message.text, message.source_offset) for message in messages] == [
            ("first", first_offset),
            ("second", second_offset),
        ]

    @pytest.mark.asyncio
    async def test_deferred_state_waits_for_delivery_ack(self, monitor, tmp_path):
        """Monitor offsets should not persist until Telegram delivery is acked."""
        jsonl_file = tmp_path / "session.jsonl"
        entry = {
            "timestamp": "2026-03-25T22:21:29.901Z",
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "Delivered later"}],
            },
        }
        jsonl_file.write_text(json.dumps(entry) + "\n", encoding="utf-8")

        monitor.scan_projects = AsyncMock(
            return_value=[
                SessionInfo(
                    session_id="session-deferred",
                    file_path=jsonl_file,
                )
            ]
        )
        monitor.state.update_session(
            TrackedSession(
                session_id="session-deferred",
                file_path=str(jsonl_file),
                last_byte_offset=0,
            )
        )

        messages = await monitor.check_for_updates(set(), save_state=False)

        assert [message.text for message in messages] == ["Delivered later"]
        assert monitor.state.get_session("session-deferred").last_byte_offset == 0

        monitor.commit_deferred_state_updates()

        assert (
            monitor.state.get_session("session-deferred").last_byte_offset
            == jsonl_file.stat().st_size
        )

    @pytest.mark.asyncio
    async def test_rebinds_bound_window_with_stale_cwd(self, monitor, tmp_path):
        """Rebind a topic window when session_map points at an old cwd."""
        jsonl_file = tmp_path / "session.jsonl"
        entry = {
            "timestamp": "2026-03-25T22:21:29.901Z",
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "Hi from Codex"}],
            },
        }
        jsonl_file.write_text(json.dumps(entry) + "\n", encoding="utf-8")

        monitor.scan_projects = AsyncMock(
            return_value=[
                SessionInfo(
                    session_id="session-5",
                    file_path=jsonl_file,
                    cwd="/tmp/project",
                )
            ]
        )
        monitor.state.update_session(
            TrackedSession(
                session_id="session-5",
                file_path=str(jsonl_file),
                last_byte_offset=0,
            )
        )

        states = {
            "@1": SimpleNamespace(
                session_id="old-session",
                cwd="/tmp/other",
                window_name="project-1",
            ),
            "@2": SimpleNamespace(
                session_id="other-session",
                cwd="/tmp/project",
                window_name="project-2",
            ),
        }

        with (
            patch(
                "telegram_agent_bot.session_monitor.list_account_homes", return_value=[]
            ),
            patch("telegram_agent_bot.session_monitor.tmux_manager") as mock_tmux,
            patch("telegram_agent_bot.session.session_manager") as mock_sm,
        ):
            mock_tmux.list_windows = AsyncMock(
                return_value=[
                    SimpleNamespace(
                        window_id="@1",
                        cwd="/tmp/project",
                        window_name="project-1",
                    ),
                    SimpleNamespace(
                        window_id="@2",
                        cwd="/tmp/project",
                        window_name="project-2",
                    ),
                ]
            )
            mock_sm.iter_thread_bindings.return_value = [
                (100, 1, "@1"),
                (100, 2, "@2"),
            ]
            mock_sm.get_window_state.side_effect = lambda wid: states[wid]
            mock_sm.has_bound_thread_for_session.return_value = False

            messages = await monitor.check_for_updates(set())

        mock_sm.register_session_to_window.assert_called_once_with(
            "@1",
            "session-5",
            "/tmp/project",
            window_name="project-1",
            persist_session_map=True,
        )
        assert [message.text for message in messages] == ["Hi from Codex"]

    @pytest.mark.asyncio
    async def test_skips_external_transcript_auto_bind_when_account_homes_exist(
        self, monitor, tmp_path
    ):
        """Do not bind unrelated ~/.codex history when telegram-agent-bot account homes exist."""
        jsonl_file = tmp_path / "default-codex" / "session-6.jsonl"
        jsonl_file.parent.mkdir(parents=True)
        entry = {
            "timestamp": "2026-03-25T22:21:29.901Z",
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "External hello"}],
            },
        }
        jsonl_file.write_text(json.dumps(entry) + "\n", encoding="utf-8")
        account_home = tmp_path / "homes" / "plus1"
        account_home.mkdir(parents=True)

        monitor.scan_projects = AsyncMock(
            return_value=[
                SessionInfo(
                    session_id="session-6",
                    file_path=jsonl_file,
                    cwd="/tmp/project",
                )
            ]
        )
        monitor.state.update_session(
            TrackedSession(
                session_id="session-6",
                file_path=str(jsonl_file),
                last_byte_offset=0,
            )
        )

        with (
            patch(
                "telegram_agent_bot.session_monitor.list_account_homes",
                return_value=[account_home],
            ),
            patch("telegram_agent_bot.session.session_manager") as mock_sm,
        ):
            mock_sm.has_bound_thread_for_session.return_value = False

            messages = await monitor.check_for_updates(set())

        mock_sm.register_session_to_window.assert_not_called()
        assert [message.text for message in messages] == ["External hello"]

    @pytest.mark.asyncio
    async def test_skips_subagent_transcript_auto_bind(self, monitor, tmp_path):
        """Do not let spawned-agent transcripts claim a Telegram topic window."""
        jsonl_file = tmp_path / "rollout-subagent.jsonl"
        jsonl_file.write_text(
            json.dumps(
                {
                    "type": "session_meta",
                    "payload": {
                        "cwd": "/tmp/project",
                        "source": {"subagent": {"thread_spawn": {}}},
                    },
                }
            )
            + "\n",
            encoding="utf-8",
        )

        state = SimpleNamespace(
            session_id="",
            cwd="/tmp/project",
            window_name="project-1",
        )
        mock_sm = SimpleNamespace(
            iter_thread_bindings=lambda: [(100, 1, "@1")],
            get_window_state=lambda _wid: state,
            register_session_to_window=MagicMock(),
        )

        with (
            patch(
                "telegram_agent_bot.session_monitor.list_account_homes", return_value=[]
            ),
            patch("telegram_agent_bot.session_monitor.tmux_manager") as mock_tmux,
        ):
            mock_tmux.list_windows = AsyncMock(
                return_value=[
                    SimpleNamespace(
                        window_id="@1",
                        cwd="/tmp/project",
                        window_name="project-1",
                        pane_current_command="node",
                    )
                ]
            )

            await monitor._auto_bind_session_to_window(
                "rollout-subagent",
                "/tmp/project",
                mock_sm,
                session_file=jsonl_file,
            )

        mock_sm.register_session_to_window.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_shell_windows_during_auto_bind(self, monitor, tmp_path):
        """Do not auto-bind a transcript to a tmux window that fell back to zsh."""
        jsonl_file = tmp_path / "homes" / "plus1" / "session-7.jsonl"
        jsonl_file.parent.mkdir(parents=True)
        jsonl_file.write_text("{}\n", encoding="utf-8")

        state = SimpleNamespace(
            session_id="",
            cwd="/tmp/project",
            window_name="project-1",
        )

        with (
            patch(
                "telegram_agent_bot.session_monitor.list_account_homes", return_value=[]
            ),
            patch("telegram_agent_bot.session_monitor.tmux_manager") as mock_tmux,
        ):
            mock_tmux.list_windows = AsyncMock(
                return_value=[
                    SimpleNamespace(
                        window_id="@1",
                        cwd="/tmp/project",
                        window_name="project-1",
                        pane_current_command="zsh",
                    )
                ]
            )
            mock_sm = SimpleNamespace(
                iter_thread_bindings=lambda: [(100, 1, "@1")],
                get_window_state=lambda _wid: state,
                register_session_to_window=AsyncMock(),
            )

            await monitor._auto_bind_session_to_window(
                "session-7",
                "/tmp/project",
                mock_sm,
                session_file=jsonl_file,
            )

        mock_sm.register_session_to_window.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_old_transcript_for_pending_fresh_window(
        self, monitor, tmp_path
    ):
        """A fresh bound window must not be claimed by an older same-cwd transcript."""
        jsonl_file = tmp_path / "old-session.jsonl"
        jsonl_file.write_text("{}\n", encoding="utf-8")
        os.utime(jsonl_file, (1000, 1000))

        state = SimpleNamespace(
            session_id="",
            cwd="/tmp/project",
            window_name="project-1",
            launch_started_at=2000.0,
        )
        mock_sm = SimpleNamespace(
            iter_thread_bindings=lambda: [(100, 1, "@1")],
            get_window_state=lambda _wid: state,
            register_session_to_window=MagicMock(),
        )

        with (
            patch(
                "telegram_agent_bot.session_monitor.list_account_homes", return_value=[]
            ),
            patch("telegram_agent_bot.session_monitor.tmux_manager") as mock_tmux,
        ):
            mock_tmux.list_windows = AsyncMock(
                return_value=[
                    SimpleNamespace(
                        window_id="@1",
                        cwd="/tmp/project",
                        window_name="project-1",
                        pane_current_command="node",
                    )
                ]
            )

            await monitor._auto_bind_session_to_window(
                "old-session",
                "/tmp/project",
                mock_sm,
                session_file=jsonl_file,
            )

        mock_sm.register_session_to_window.assert_not_called()
