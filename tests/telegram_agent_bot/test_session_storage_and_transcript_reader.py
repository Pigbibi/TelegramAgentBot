"""Tests for extracted session_storage and transcript_reader modules."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from telegram_agent_bot.backends.base import AgentTarget
from telegram_agent_bot.session_storage import (
    WindowState,
    load_state_file,
    mutate_session_map,
    read_session_map_async,
    save_state_file,
    serialize_state_payload,
)
from telegram_agent_bot.transcript_reader import (
    find_session_file,
    read_recent_messages,
    transcript_tail_contains_user_text,
)


class TestSessionStorage:
    def test_state_round_trip(self, tmp_path: Path) -> None:
        state_file = tmp_path / "state.json"
        payload = serialize_state_payload(
            window_states={
                "@1": WindowState(
                    session_id="sid-1",
                    cwd="/srv/app",
                    window_name="app",
                )
            },
            user_window_offsets={7: {"@1": 512}},
            user_window_offset_sessions={7: {"@1": "sid-1"}},
            thread_bindings={7: {42: "@1"}},
            thread_targets={
                7: {
                    42: AgentTarget(
                        backend_id="local",
                        node_id="local",
                        session_id="sid-1",
                        window_id="@1",
                    )
                }
            },
            window_display_names={"@1": "app"},
            group_chat_ids={"7:42": -100999},
            thread_output_modes={"7:42": "trace"},
            hidden_session_ids={"sid-old"},
            topic_managed_session_ids={"sid-1"},
        )
        save_state_file(state_file, payload)

        loaded = load_state_file(state_file)
        assert loaded is not None
        assert loaded.window_states["@1"].session_id == "sid-1"
        assert loaded.thread_bindings == {7: {42: "@1"}}
        assert loaded.group_chat_ids == {"7:42": -100999}
        assert loaded.thread_output_modes == {"7:42": "trace"}
        assert loaded.hidden_session_ids == {"sid-old"}
        assert loaded.needs_migration is False

    @pytest.mark.asyncio
    async def test_mutate_and_read_session_map_async(self, tmp_path: Path) -> None:
        map_file = tmp_path / "session_map.json"

        def add_entry(data: dict) -> bool:
            data["bot:@1"] = {"session_id": "sid-1", "cwd": "/tmp"}
            return True

        assert mutate_session_map(map_file, add_entry, create_if_missing=True) is True
        loaded = await read_session_map_async(map_file)
        assert loaded == {"bot:@1": {"session_id": "sid-1", "cwd": "/tmp"}}


class TestTranscriptReader:
    def test_find_session_file_and_tail_scan(self, tmp_path: Path) -> None:
        root = tmp_path / "projects"
        cwd = "/tmp/demo"
        session_id = "12345678-1234-1234-1234-1234567890ab"
        transcript = root / "-tmp-demo" / f"{session_id}.jsonl"
        transcript.parent.mkdir(parents=True)
        transcript.write_text(
            json.dumps(
                {
                    "type": "user",
                    "message": {"content": [{"type": "text", "text": "hello world"}]},
                }
            )
            + "\n",
            encoding="utf-8",
        )

        found = find_session_file(
            session_id,
            cwd,
            roots=[root],
            cursor_root=tmp_path / "cursor",
        )
        assert found == transcript
        assert transcript_tail_contains_user_text(transcript, "hello world") is True

    @pytest.mark.asyncio
    async def test_read_recent_messages_filters_thinking(self, tmp_path: Path) -> None:
        transcript = tmp_path / "session.jsonl"
        lines = [
            json.dumps(
                {
                    "type": "user",
                    "message": {"content": [{"type": "text", "text": "Question?"}]},
                }
            ),
            json.dumps(
                {
                    "type": "assistant",
                    "message": {
                        "content": [
                            {"type": "thinking", "thinking": "secret chain"},
                            {"type": "text", "text": "Answer."},
                        ]
                    },
                }
            ),
        ]
        transcript.write_text("\n".join(lines) + "\n", encoding="utf-8")

        messages, total = await read_recent_messages(transcript)
        assert total == 2
        assert [m["text"] for m in messages] == ["Question?", "Answer."]
