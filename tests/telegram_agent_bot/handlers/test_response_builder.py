"""Tests for response_builder.build_response_parts."""

from telegram_agent_bot.handlers.response_builder import build_response_parts
from telegram_agent_bot.markdown_v2 import convert_markdown
from telegram_agent_bot.transcript_parser import TranscriptParser

EXP_START = TranscriptParser.EXPANDABLE_QUOTE_START
EXP_END = TranscriptParser.EXPANDABLE_QUOTE_END


class TestBuildResponseParts:
    def test_user_message_has_emoji_prefix(self):
        parts = build_response_parts("hello", is_complete=True, role="user")
        assert len(parts) == 1
        assert "\U0001f464" in parts[0]

    def test_long_user_message_is_paginated_without_truncation(self):
        long_text = "a" * 4000
        parts = build_response_parts(long_text, is_complete=True, role="user")
        assert len(parts) == 2
        assert sum(part.count("a") for part in parts) == len(long_text)
        assert all(part.startswith("👤 ") for part in parts)
        assert "truncated" not in "".join(parts).lower()

    def test_thinking_content_is_not_truncated(self):
        inner = "x" * 800
        text = f"{EXP_START}{inner}{EXP_END}"
        parts = build_response_parts(text, is_complete=True, content_type="thinking")
        assert len(parts) == 1
        assert parts[0].count("x") == len(inner)
        assert "truncated" not in parts[0].lower()

    def test_plain_text_single_part(self):
        parts = build_response_parts("short text", is_complete=True)
        assert len(parts) == 1

    def test_plain_text_multi_part_has_page_suffix(self):
        long_text = "\n".join(f"line {i} " + "padding" * 50 for i in range(200))
        parts = build_response_parts(long_text, is_complete=True)
        assert len(parts) > 1
        assert "1/" in parts[0]

    def test_short_expandable_quote_stays_atomic(self):
        inner = "thought " * 100
        text = f"{EXP_START}{inner}{EXP_END}"
        parts = build_response_parts(text, is_complete=False, content_type="thinking")
        assert len(parts) == 1

    def test_long_expandable_quote_is_paginated_without_content_loss(self):
        inner = "special _ output!\n" * 600
        text = f"**Bash**(command)\n{EXP_START}{inner}{EXP_END}"

        parts = build_response_parts(text, is_complete=True, content_type="tool_result")

        assert len(parts) > 1
        assert all(part.count(EXP_START) == part.count(EXP_END) for part in parts)
        recovered = "".join(
            part.split(EXP_START, 1)[1].split(EXP_END, 1)[0]
            for part in parts
            if EXP_START in part
        )
        assert recovered == inner
        assert all(len(convert_markdown(part)) < 4096 for part in parts)

    def test_thinking_has_prefix(self):
        parts = build_response_parts(
            "some thought", is_complete=True, content_type="thinking"
        )
        assert len(parts) == 1
        assert "Thinking" in parts[0]

    def test_assistant_text_no_prefix(self):
        parts = build_response_parts(
            "hello world", is_complete=True, content_type="text", role="assistant"
        )
        assert len(parts) == 1
        assert "\U0001f464" not in parts[0]
        assert "Thinking" not in parts[0]
