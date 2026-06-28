"""Tests for telegram-agent-bot.transcript_parser — pure logic, no I/O."""

import json

import pytest

from telegram_agent_bot.config import config
from telegram_agent_bot.transcript_parser import (
    ParsedMessage,
    TranscriptParser,
)

EXPQUOTE_START = TranscriptParser.EXPANDABLE_QUOTE_START
EXPQUOTE_END = TranscriptParser.EXPANDABLE_QUOTE_END


# ── parse_line ───────────────────────────────────────────────────────────


class TestParseLine:
    @pytest.mark.parametrize(
        "line, expected",
        [
            ('{"type": "user"}', {"type": "user"}),
            ("not-json", None),
            ("", None),
            ("   \t  ", None),
        ],
        ids=["valid_json", "invalid_json", "empty", "whitespace"],
    )
    def test_parse_line(self, line: str, expected: dict | None):
        assert TranscriptParser.parse_line(line) == expected

    def test_event_msg_error_is_normalized_as_assistant_text(self):
        event = {
            "type": "event_msg",
            "timestamp": "2026-05-25T04:33:30.638Z",
            "payload": {
                "type": "error",
                "message": "Your access token could not be refreshed.",
            },
        }

        parsed = TranscriptParser.parse_line(json.dumps(event))

        assert parsed is not None
        assert parsed["type"] == "assistant"
        assert parsed["message"]["content"] == [
            {
                "type": "text",
                "text": "⚠️ Codex error: Your access token could not be refreshed.\n\nUse /codexlogin to start a Codex device login from Telegram.",
            }
        ]

    def test_usage_limit_error_is_left_for_monitor_handling(self):
        event = {
            "type": "event_msg",
            "payload": {
                "type": "error",
                "message": "You've hit your usage limit.",
                "codex_error_info": "usage_limit_exceeded",
            },
        }

        assert TranscriptParser.parse_line(json.dumps(event)) is None

    def test_task_complete_without_final_message_emits_completion_notice(self):
        event = {
            "type": "event_msg",
            "timestamp": "2026-06-09T07:57:37.121Z",
            "payload": {
                "type": "task_complete",
                "last_agent_message": None,
            },
        }

        parsed = TranscriptParser.parse_line(json.dumps(event))

        assert parsed is not None
        assert parsed["type"] == "assistant"
        assert parsed["text"] == (
            "✅ Codex finished. No additional final message was emitted."
        )

    def test_task_complete_with_final_message_is_skipped_to_avoid_duplicate(self):
        event = {
            "type": "event_msg",
            "payload": {
                "type": "task_complete",
                "last_agent_message": "Already emitted as response_item/message.",
            },
        }

        assert TranscriptParser.parse_line(json.dumps(event)) is None

    def test_response_item_encrypted_reasoning_renders_thinking_placeholder(
        self, monkeypatch
    ):
        monkeypatch.setattr(config, "show_commentary_messages", True)
        item = {
            "type": "response_item",
            "timestamp": "2026-05-26T00:00:00Z",
            "payload": {
                "type": "reasoning",
                "summary": [],
                "content": None,
                "encrypted_content": "encrypted",
            },
        }

        parsed = TranscriptParser.parse_line(json.dumps(item))

        assert parsed is not None
        assert parsed["type"] == "assistant"
        block = parsed["message"]["content"][0]
        assert block == {
            "type": "thinking",
            "thinking": "Working on it…",
        }

    def test_detects_encrypted_reasoning_placeholder_with_or_without_quote(self):
        assert TranscriptParser.is_encrypted_reasoning_placeholder("Working on it…")
        assert TranscriptParser.is_encrypted_reasoning_placeholder(
            f"{EXPQUOTE_START}Working on it…{EXPQUOTE_END}"
        )
        assert not TranscriptParser.is_encrypted_reasoning_placeholder("real reasoning")

    def test_response_item_reasoning_respects_commentary_config(self, monkeypatch):
        monkeypatch.setattr(config, "show_commentary_messages", False)
        item = {
            "type": "response_item",
            "payload": {
                "type": "reasoning",
                "summary": [],
                "content": None,
                "encrypted_content": "encrypted",
            },
        }

        assert TranscriptParser.parse_line(json.dumps(item)) is None

    def test_response_item_function_call_is_normalized_as_tool_use(self):
        item = {
            "type": "response_item",
            "timestamp": "2026-05-26T00:00:00Z",
            "payload": {
                "type": "function_call",
                "call_id": "call_1",
                "name": "exec_command",
                "arguments": json.dumps({"cmd": "pytest -q"}),
            },
        }

        parsed = TranscriptParser.parse_line(json.dumps(item))

        assert parsed is not None
        assert parsed["type"] == "assistant"
        block = parsed["message"]["content"][0]
        assert block == {
            "type": "tool_use",
            "id": "call_1",
            "name": "Bash",
            "input": {"cmd": "pytest -q", "command": "pytest -q"},
        }

    def test_update_plan_function_call_is_hidden_from_messages(self):
        use_item = {
            "type": "response_item",
            "timestamp": "2026-05-26T00:00:00Z",
            "payload": {
                "type": "function_call",
                "call_id": "call_plan",
                "name": "update_plan",
                "arguments": json.dumps({"plan": []}),
            },
        }
        result_item = {
            "type": "response_item",
            "timestamp": "2026-05-26T00:00:01Z",
            "payload": {
                "type": "function_call_output",
                "call_id": "call_plan",
                "output": "Plan updated",
            },
        }

        entries = [
            TranscriptParser.parse_line(json.dumps(use_item)),
            TranscriptParser.parse_line(json.dumps(result_item)),
        ]
        result, pending = TranscriptParser.parse_entries([e for e in entries if e])

        assert result == []
        assert pending == {}

    def test_private_connector_tool_calls_are_hidden_but_thinking_stays(
        self, monkeypatch
    ):
        monkeypatch.setattr(config, "show_commentary_messages", True)
        thinking_item = {
            "type": "response_item",
            "timestamp": "2026-05-26T00:00:00Z",
            "payload": {
                "type": "reasoning",
                "summary": [{"type": "summary_text", "text": "checking repos"}],
            },
        }
        use_item = {
            "type": "response_item",
            "timestamp": "2026-05-26T00:00:01Z",
            "payload": {
                "type": "function_call",
                "call_id": "call_repos",
                "name": "_list_repositories",
                "arguments": json.dumps({"owner": "QuantStrategyLab"}),
            },
        }
        result_item = {
            "type": "response_item",
            "timestamp": "2026-05-26T00:00:02Z",
            "payload": {
                "type": "function_call_output",
                "call_id": "call_repos",
                "output": '{"repositories": []}',
            },
        }

        entries = [
            TranscriptParser.parse_line(json.dumps(thinking_item)),
            TranscriptParser.parse_line(json.dumps(use_item)),
            TranscriptParser.parse_line(json.dumps(result_item)),
        ]
        result, pending = TranscriptParser.parse_entries([e for e in entries if e])

        assert [entry.content_type for entry in result] == ["thinking"]
        assert "checking repos" in result[0].text
        assert pending == {}

    def test_view_image_tool_calls_are_hidden_from_messages(self):
        use_item = {
            "type": "response_item",
            "timestamp": "2026-06-08T09:40:05Z",
            "payload": {
                "type": "function_call",
                "call_id": "call_view_image",
                "name": "view_image",
                "arguments": json.dumps(
                    {
                        "path": "/tmp/ibkr-gateway-screen.png",
                        "detail": "high",
                    }
                ),
            },
        }
        result_item = {
            "type": "response_item",
            "timestamp": "2026-06-08T09:40:06Z",
            "payload": {
                "type": "function_call_output",
                "call_id": "call_view_image",
                "output": json.dumps(
                    [
                        {
                            "type": "input_image",
                            "image_url": "data:image/png;base64,abc123",
                        }
                    ]
                ),
            },
        }

        entries = [
            TranscriptParser.parse_line(json.dumps(use_item)),
            TranscriptParser.parse_line(json.dumps(result_item)),
        ]
        result, pending = TranscriptParser.parse_entries([e for e in entries if e])

        assert result == []
        assert pending == {}

    @pytest.mark.parametrize("tool_name", ["get_goal", "create_goal", "update_goal"])
    def test_goal_lifecycle_tool_calls_are_hidden_from_messages(self, tool_name: str):
        use_item = {
            "type": "response_item",
            "timestamp": "2026-06-08T09:40:05Z",
            "payload": {
                "type": "function_call",
                "call_id": f"call_{tool_name}",
                "name": tool_name,
                "arguments": json.dumps({"status": "complete"}),
            },
        }
        result_item = {
            "type": "response_item",
            "timestamp": "2026-06-08T09:40:06Z",
            "payload": {
                "type": "function_call_output",
                "call_id": f"call_{tool_name}",
                "output": json.dumps({"goal": {"status": "complete"}}),
            },
        }

        entries = [
            TranscriptParser.parse_line(json.dumps(use_item)),
            TranscriptParser.parse_line(json.dumps(result_item)),
        ]
        result, pending = TranscriptParser.parse_entries([e for e in entries if e])

        assert result == []
        assert pending == {}

    @pytest.mark.parametrize("tool_name", ["spawn_agent", "wait_agent", "close_agent"])
    def test_agent_orchestration_tool_call_is_hidden_from_messages(
        self, tool_name: str
    ):
        use_item = {
            "type": "response_item",
            "timestamp": "2026-06-18T19:22:05Z",
            "payload": {
                "type": "function_call",
                "call_id": f"call_{tool_name}",
                "name": tool_name,
                "arguments": json.dumps(
                    {
                        "agent_type": "worker",
                        "message": "investigate independently",
                        "fork_context": True,
                    }
                ),
            },
        }
        result_item = {
            "type": "response_item",
            "timestamp": "2026-06-18T19:22:06Z",
            "payload": {
                "type": "function_call_output",
                "call_id": f"call_{tool_name}",
                "output": "worker finished",
            },
        }

        entries = [
            TranscriptParser.parse_line(json.dumps(use_item)),
            TranscriptParser.parse_line(json.dumps(result_item)),
        ]
        result, pending = TranscriptParser.parse_entries([e for e in entries if e])

        assert result == []
        assert pending == {}

    def test_response_item_function_call_output_is_normalized_as_tool_result(self):
        item = {
            "type": "response_item",
            "timestamp": "2026-05-26T00:00:01Z",
            "payload": {
                "type": "function_call_output",
                "call_id": "call_1",
                "output": "Chunk ID: abc\nWall time: 0.0\nOutput:\n3 passed",
            },
        }

        parsed = TranscriptParser.parse_line(json.dumps(item))

        assert parsed is not None
        assert parsed["type"] == "user"
        block = parsed["message"]["content"][0]
        assert block == {
            "type": "tool_result",
            "tool_use_id": "call_1",
            "content": "3 passed",
        }


# ── extract_text_only ────────────────────────────────────────────────────


class TestExtractTextOnly:
    @pytest.mark.parametrize(
        "content, expected",
        [
            ("plain string", "plain string"),
            (
                [{"type": "text", "text": "hello"}, {"type": "text", "text": "world"}],
                "hello\nworld",
            ),
            (
                [
                    {"type": "text", "text": "keep"},
                    {"type": "tool_use", "name": "Read"},
                ],
                "keep",
            ),
            ([], ""),
            (42, ""),
        ],
        ids=["string", "text_blocks", "mixed", "empty_list", "non_list_non_string"],
    )
    def test_extract_text_only(self, content: list | str | int, expected: str):
        assert TranscriptParser.extract_text_only(content) == expected


# ── format_tool_use_summary ──────────────────────────────────────────────


class TestFormatToolUseSummary:
    @pytest.mark.parametrize(
        "name, input_data, expected",
        [
            ("Read", {"file_path": "src/main.py"}, "**Read**(src/main.py)"),
            ("Write", {"file_path": "out.txt"}, "**Write**(out.txt)"),
            ("Bash", {"command": "ls -la"}, "**Bash**(ls -la)"),
            ("Grep", {"pattern": "TODO"}, "**Grep**(TODO)"),
            ("Glob", {"pattern": "*.py"}, "**Glob**(*.py)"),
            ("Task", {"description": "analyze code"}, "**Task**(analyze code)"),
            (
                "WebFetch",
                {"url": "https://example.com"},
                "**WebFetch**(https://example.com)",
            ),
            ("WebSearch", {"query": "python async"}, "**WebSearch**(python async)"),
            ("TodoWrite", {"todos": [1, 2, 3]}, "**TodoWrite**(3 item(s))"),
            ("TodoRead", {}, "**TodoRead**"),
            (
                "AskUserQuestion",
                {"questions": [{"question": "Continue?"}]},
                "**AskUserQuestion**(Continue?)",
            ),
            ("ExitPlanMode", {}, "**ExitPlanMode**"),
            ("Skill", {"skill": "code-review"}, "**Skill**(code-review)"),
            (
                "CustomTool",
                {"first_key": "value1"},
                "**CustomTool**(value1)",
            ),
        ],
        ids=[
            "Read",
            "Write",
            "Bash",
            "Grep",
            "Glob",
            "Task",
            "WebFetch",
            "WebSearch",
            "TodoWrite",
            "TodoRead",
            "AskUserQuestion",
            "ExitPlanMode",
            "Skill",
            "unknown_tool",
        ],
    )
    def test_tool_summary(self, name: str, input_data: dict, expected: str):
        assert TranscriptParser.format_tool_use_summary(name, input_data) == expected

    def test_non_dict_input(self):
        assert (
            TranscriptParser.format_tool_use_summary("Read", "not a dict") == "**Read**"
        )

    def test_truncation_at_200_chars(self):
        long_value = "x" * 250
        result = TranscriptParser.format_tool_use_summary(
            "Bash", {"command": long_value}
        )
        assert len(long_value) > 200
        assert result == f"**Bash**({'x' * 200}…)"


# ── extract_tool_result_text ─────────────────────────────────────────────


class TestExtractToolResultText:
    @pytest.mark.parametrize(
        "content, expected",
        [
            ("raw string", "raw string"),
            (
                [{"type": "text", "text": "line1"}, {"type": "text", "text": "line2"}],
                "line1\nline2",
            ),
            (
                [{"type": "text", "text": "keep"}, {"type": "image", "data": "..."}],
                "keep",
            ),
            (None, ""),
        ],
        ids=["string", "text_blocks", "mixed", "none"],
    )
    def test_extract_tool_result_text(self, content: str | list | None, expected: str):
        assert TranscriptParser.extract_tool_result_text(content) == expected


# ── parse_message ────────────────────────────────────────────────────────


class TestParseMessage:
    def test_user_text(self):
        data = {
            "type": "user",
            "message": {"content": [{"type": "text", "text": "hello"}]},
        }
        result = TranscriptParser.parse_message(data)
        assert result == ParsedMessage(message_type="user", text="hello")

    def test_assistant_text(self):
        data = {
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "hi there"}]},
        }
        result = TranscriptParser.parse_message(data)
        assert result == ParsedMessage(message_type="assistant", text="hi there")

    def test_local_command_with_stdout(self):
        data = {
            "type": "user",
            "message": {
                "content": [
                    {
                        "type": "text",
                        "text": (
                            "<command-name>/help</command-name>"
                            "<local-command-stdout>Available commands</local-command-stdout>"
                        ),
                    }
                ]
            },
        }
        result = TranscriptParser.parse_message(data)
        assert result is not None
        assert result.message_type == "local_command"
        assert result.text == "Available commands"
        assert result.tool_name == "/help"

    def test_local_command_invoke(self):
        data = {
            "type": "user",
            "message": {
                "content": [
                    {"type": "text", "text": "<command-name>/clear</command-name>"}
                ]
            },
        }
        result = TranscriptParser.parse_message(data)
        assert result is not None
        assert result.message_type == "local_command_invoke"
        assert result.text == ""
        assert result.tool_name == "/clear"

    def test_non_user_assistant_returns_none(self):
        data = {
            "type": "summary",
            "message": {"content": "summary text"},
        }
        assert TranscriptParser.parse_message(data) is None

    def test_string_content(self):
        data = {
            "type": "assistant",
            "message": {"content": "plain response"},
        }
        result = TranscriptParser.parse_message(data)
        assert result == ParsedMessage(message_type="assistant", text="plain response")


# ── _format_edit_diff ────────────────────────────────────────────────────


class TestFormatEditDiff:
    @pytest.mark.parametrize(
        "old, new, check",
        [
            (
                "hello",
                "world",
                lambda r: "-hello" in r and "+world" in r,
            ),
            (
                "line1\nline2\nline3",
                "line1\nchanged\nline3",
                lambda r: "-line2" in r and "+changed" in r,
            ),
            (
                "same",
                "same",
                lambda r: r == "",
            ),
        ],
        ids=["single_line", "multi_line", "identical"],
    )
    def test_format_edit_diff(self, old: str, new: str, check):
        result = TranscriptParser._format_edit_diff(old, new)
        assert check(result), f"Check failed for ({old!r}, {new!r}): {result!r}"


# ── _format_tool_result_text ─────────────────────────────────────────────


class TestFormatToolResultText:
    @pytest.mark.parametrize(
        "text, tool_name, check",
        [
            (
                "line1\nline2\nline3",
                "Read",
                lambda r: r == "  ⎿  Read 3 lines",
            ),
            (
                "line1\nline2",
                "Write",
                lambda r: r == "  ⎿  Wrote 2 lines",
            ),
            (
                "output line",
                "Bash",
                lambda r: (
                    r.startswith("  ⎿  Output 1 lines")
                    and EXPQUOTE_START in r
                    and EXPQUOTE_END in r
                ),
            ),
            (
                "file1.py\nfile2.py\n",
                "Grep",
                lambda r: "Found 2 matches" in r and EXPQUOTE_START in r,
            ),
            (
                "a.py\nb.py\nc.py",
                "Glob",
                lambda r: "Found 3 files" in r and EXPQUOTE_START in r,
            ),
            (
                "agent says hello",
                "Task",
                lambda r: "Agent output 1 lines" in r and EXPQUOTE_START in r,
            ),
            (
                "page content here",
                "WebFetch",
                lambda r: (
                    f"Fetched {len('page content here')} characters" in r
                    and EXPQUOTE_START in r
                ),
            ),
            (
                "",
                "Read",
                lambda r: r == "",
            ),
        ],
        ids=["Read", "Write", "Bash", "Grep", "Glob", "Task", "WebFetch", "empty"],
    )
    def test_format_tool_result_text(self, text: str, tool_name: str, check):
        result = TranscriptParser._format_tool_result_text(text, tool_name)
        assert check(result), f"Failed check for {tool_name!r}: {result!r}"


# ── parse_entries ────────────────────────────────────────────────────────


class TestParseEntries:
    def test_assistant_text(self, make_jsonl_entry, make_text_block):
        entries = [make_jsonl_entry("assistant", [make_text_block("Hello!")])]
        result, pending = TranscriptParser.parse_entries(entries)
        assert len(result) == 1
        assert result[0].role == "assistant"
        assert result[0].text == "Hello!"
        assert result[0].content_type == "text"

    def test_user_text(self, make_jsonl_entry, make_text_block):
        entries = [make_jsonl_entry("user", [make_text_block("Hi bot")])]
        result, pending = TranscriptParser.parse_entries(entries)
        assert len(result) == 1
        assert result[0].role == "user"
        assert result[0].text == "Hi bot"

    def test_tool_use_and_result_pairing(
        self,
        make_jsonl_entry,
        make_text_block,
        make_tool_use_block,
        make_tool_result_block,
    ):
        entries = [
            make_jsonl_entry(
                "assistant",
                [make_tool_use_block("t1", "Read", {"file_path": "app.py"})],
            ),
            make_jsonl_entry(
                "user",
                [make_tool_result_block("t1", "file contents line1\nline2\nline3")],
            ),
        ]
        result, pending = TranscriptParser.parse_entries(entries)
        tool_use_entries = [e for e in result if e.content_type == "tool_use"]
        tool_result_entries = [e for e in result if e.content_type == "tool_result"]
        assert len(tool_use_entries) == 1
        assert tool_use_entries[0].tool_use_id == "t1"
        assert "**Read**" in tool_use_entries[0].text
        assert len(tool_result_entries) == 1
        assert tool_result_entries[0].tool_use_id == "t1"
        assert not pending

    def test_thinking_block(self, make_jsonl_entry, make_thinking_block):
        entries = [
            make_jsonl_entry("assistant", [make_thinking_block("reasoning here")])
        ]
        result, pending = TranscriptParser.parse_entries(entries)
        assert len(result) == 1
        assert result[0].content_type == "thinking"
        assert EXPQUOTE_START in result[0].text
        assert EXPQUOTE_END in result[0].text
        assert "reasoning here" in result[0].text

    def test_response_item_encrypted_reasoning_pair_renders_thinking_entry(
        self, monkeypatch
    ):
        monkeypatch.setattr(config, "show_commentary_messages", True)
        parsed = TranscriptParser.parse_line(
            json.dumps(
                {
                    "type": "response_item",
                    "payload": {
                        "type": "reasoning",
                        "summary": [],
                        "content": None,
                        "encrypted_content": "encrypted",
                    },
                }
            )
        )

        result, pending = TranscriptParser.parse_entries([parsed])

        assert not pending
        assert len(result) == 1
        assert result[0].content_type == "thinking"
        assert EXPQUOTE_START in result[0].text
        assert EXPQUOTE_END in result[0].text
        assert "Working on it…" in result[0].text

    def test_bash_tool_calls_can_be_hidden_without_hiding_other_tools(
        self,
        make_jsonl_entry,
        make_tool_use_block,
        make_tool_result_block,
        monkeypatch,
    ):
        monkeypatch.setattr(config, "show_bash_tool_calls", False)
        entries = [
            make_jsonl_entry(
                "assistant",
                [
                    make_tool_use_block("t1", "Bash", {"command": "pytest -q"}),
                    make_tool_use_block("t2", "Grep", {"pattern": "TODO"}),
                ],
            ),
            make_jsonl_entry(
                "user",
                [
                    make_tool_result_block("t1", "3 passed"),
                    make_tool_result_block("t2", "app.py:1:TODO"),
                ],
            ),
        ]

        result, pending = TranscriptParser.parse_entries(entries)

        assert not pending
        assert [entry.content_type for entry in result] == [
            "tool_use",
            "tool_result",
        ]
        assert result[0].text == "**Grep**(TODO)"
        assert "Found 1 matches" in result[1].text
        assert all("Bash" not in entry.text for entry in result)
        assert all(entry.tool_use_id != "t1" for entry in result)

    def test_response_item_function_call_pair_renders_tool_detail(self):
        lines = [
            json.dumps(
                {
                    "type": "response_item",
                    "payload": {
                        "type": "function_call",
                        "call_id": "call_1",
                        "name": "exec_command",
                        "arguments": json.dumps({"cmd": "pytest -q"}),
                    },
                }
            ),
            json.dumps(
                {
                    "type": "response_item",
                    "payload": {
                        "type": "function_call_output",
                        "call_id": "call_1",
                        "output": "3 passed",
                    },
                }
            ),
        ]
        entries = [TranscriptParser.parse_line(line) for line in lines]
        result, pending = TranscriptParser.parse_entries([e for e in entries if e])

        assert not pending
        assert [entry.content_type for entry in result] == ["tool_use", "tool_result"]
        assert result[0].text == "**Bash**(pytest -q)"
        assert "Output 1 lines" in result[1].text
        assert "3 passed" in result[1].text

    def test_wait_function_call_renders_background_terminal_detail(self):
        parsed = TranscriptParser.parse_line(
            json.dumps(
                {
                    "type": "response_item",
                    "payload": {
                        "type": "function_call",
                        "call_id": "call_2",
                        "name": "write_stdin",
                        "arguments": json.dumps({"session_id": 123, "chars": ""}),
                    },
                }
            )
        )
        result, pending = TranscriptParser.parse_entries([parsed])

        assert "call_2" in pending
        assert result[0].content_type == "tool_use"
        assert result[0].text == "**Wait**(background terminal)"

    def test_wait_function_call_empty_output_does_not_duplicate_detail(self):
        lines = [
            json.dumps(
                {
                    "type": "response_item",
                    "payload": {
                        "type": "function_call",
                        "call_id": "call_2",
                        "name": "write_stdin",
                        "arguments": json.dumps({"session_id": 123, "chars": ""}),
                    },
                }
            ),
            json.dumps(
                {
                    "type": "response_item",
                    "payload": {
                        "type": "function_call_output",
                        "call_id": "call_2",
                        "output": "Chunk ID: empty\nWall time: 0.0\nOutput:",
                    },
                }
            ),
        ]
        entries = [TranscriptParser.parse_line(line) for line in lines]
        result, pending = TranscriptParser.parse_entries([e for e in entries if e])

        assert not pending
        assert [entry.content_type for entry in result] == ["tool_use"]
        assert result[0].text == "**Wait**(background terminal)"

    def test_wait_function_call_output_keeps_tool_name_for_auto_cleanup(self):
        lines = [
            json.dumps(
                {
                    "type": "response_item",
                    "payload": {
                        "type": "function_call",
                        "call_id": "call_2",
                        "name": "write_stdin",
                        "arguments": json.dumps({"session_id": 123, "chars": ""}),
                    },
                }
            ),
            json.dumps(
                {
                    "type": "response_item",
                    "payload": {
                        "type": "function_call_output",
                        "call_id": "call_2",
                        "output": "Chunk ID: done\nWall time: 0.0\nOutput:\nall done",
                    },
                }
            ),
        ]
        entries = [TranscriptParser.parse_line(line) for line in lines]
        result, pending = TranscriptParser.parse_entries([e for e in entries if e])

        assert not pending
        assert [entry.content_type for entry in result] == ["tool_use", "tool_result"]
        assert result[1].tool_name == "Wait"

    def test_unpaired_function_call_output_is_hidden_in_monitor_mode(self):
        parsed = TranscriptParser.parse_line(
            json.dumps(
                {
                    "type": "response_item",
                    "payload": {
                        "type": "function_call_output",
                        "call_id": "call_missing_tool_use",
                        "output": 'Chunk ID: abc\nWall time: 0.0\nOutput:\n{"conclusion":"failure"}',
                    },
                }
            )
        )

        result, pending = TranscriptParser.parse_entries([parsed], pending_tools={})

        assert result == []
        assert pending == {}

    def test_local_command_with_stdout(self, make_jsonl_entry, make_text_block):
        xml = (
            "<command-name>/status</command-name>"
            "<local-command-stdout>all good</local-command-stdout>"
        )
        entries = [make_jsonl_entry("user", [make_text_block(xml)])]
        result, pending = TranscriptParser.parse_entries(entries)
        assert len(result) == 1
        assert result[0].content_type == "local_command"
        assert "/status" in result[0].text
        assert "all good" in result[0].text

    def test_exit_plan_mode_emits_plan(self, make_jsonl_entry, make_tool_use_block):
        block = make_tool_use_block(
            "t1", "ExitPlanMode", {"plan": "Step 1: do X\nStep 2: do Y"}
        )
        entries = [make_jsonl_entry("assistant", [block])]
        result, pending = TranscriptParser.parse_entries(entries)
        texts = [e for e in result if e.content_type == "text"]
        tool_uses = [e for e in result if e.content_type == "tool_use"]
        assert len(texts) == 1
        assert "Step 1: do X" in texts[0].text
        assert len(tool_uses) >= 1

    def test_edit_tool_diff_stats(
        self,
        make_jsonl_entry,
        make_tool_use_block,
        make_tool_result_block,
    ):
        edit_input = {
            "file_path": "main.py",
            "old_string": "old line",
            "new_string": "new line",
        }
        entries = [
            make_jsonl_entry(
                "assistant",
                [make_tool_use_block("t1", "Edit", edit_input)],
            ),
            make_jsonl_entry(
                "user",
                [make_tool_result_block("t1", "OK")],
            ),
        ]
        result, pending = TranscriptParser.parse_entries(entries)
        tool_result_entries = [e for e in result if e.content_type == "tool_result"]
        assert len(tool_result_entries) == 1
        tr = tool_result_entries[0]
        assert "Added" in tr.text
        assert "removed" in tr.text
        assert EXPQUOTE_START in tr.text

    def test_error_tool_result(
        self,
        make_jsonl_entry,
        make_tool_use_block,
        make_tool_result_block,
    ):
        entries = [
            make_jsonl_entry(
                "assistant",
                [make_tool_use_block("t1", "Bash", {"command": "rm -rf /"})],
            ),
            make_jsonl_entry(
                "user",
                [make_tool_result_block("t1", "Permission denied", is_error=True)],
            ),
        ]
        result, pending = TranscriptParser.parse_entries(entries)
        tool_result_entries = [e for e in result if e.content_type == "tool_result"]
        assert len(tool_result_entries) == 1
        assert "Error: Permission denied" in tool_result_entries[0].text

    def test_interrupted_tool_result(
        self,
        make_jsonl_entry,
        make_tool_use_block,
        make_tool_result_block,
    ):
        entries = [
            make_jsonl_entry(
                "assistant",
                [make_tool_use_block("t1", "Read", {"file_path": "x.py"})],
            ),
            make_jsonl_entry(
                "user",
                [make_tool_result_block("t1", TranscriptParser._INTERRUPTED_TEXT)],
            ),
        ]
        result, pending = TranscriptParser.parse_entries(entries)
        tool_result_entries = [e for e in result if e.content_type == "tool_result"]
        assert len(tool_result_entries) == 1
        assert "Interrupted" in tool_result_entries[0].text

    def test_pending_tools_carry_over(self, make_jsonl_entry, make_tool_use_block):
        entries = [
            make_jsonl_entry(
                "assistant",
                [make_tool_use_block("t1", "Read", {"file_path": "a.py"})],
            ),
        ]
        result, pending = TranscriptParser.parse_entries(entries, pending_tools={})
        assert "t1" in pending
        flushed = [
            e for e in result if e.content_type == "tool_use" and e.tool_use_id == "t1"
        ]
        assert len(flushed) == 1

    def test_pending_tools_flushed_without_carry_over(
        self, make_jsonl_entry, make_tool_use_block
    ):
        entries = [
            make_jsonl_entry(
                "assistant",
                [make_tool_use_block("t1", "Read", {"file_path": "a.py"})],
            ),
        ]
        result, pending = TranscriptParser.parse_entries(entries, pending_tools=None)
        tool_entries = [e for e in result if e.tool_use_id == "t1"]
        assert len(tool_entries) == 2
        assert tool_entries[0].content_type == "tool_use"
        assert tool_entries[1].content_type == "tool_use"

    def test_system_tag_filtered(self, make_jsonl_entry, make_text_block):
        entries = [
            make_jsonl_entry(
                "user",
                [
                    make_text_block(
                        "<system-reminder>secret instructions</system-reminder>"
                    )
                ],
            ),
        ]
        result, pending = TranscriptParser.parse_entries(entries)
        user_entries = [e for e in result if e.role == "user"]
        assert len(user_entries) == 0
