"""JSONL transcript parser for Codex and Cursor Agent session files.

Parses Codex session JSONL files and extracts structured messages.
Handles: text, thinking, tool_use, tool_result, local_command, and user messages.
Tool pairing: tool_use blocks in assistant messages are matched with
tool_result blocks in subsequent user messages via tool_use_id.

Shared by session.py (history) and session_monitor.py (real-time).

Key classes: TranscriptParser (static methods), ParsedEntry, ParsedMessage, PendingToolInfo.
"""

import base64
import difflib
import json
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .config import config

logger = logging.getLogger(__name__)


def _as_dict(value: Any) -> dict[str, Any] | None:
    """Return value when it is a dict, otherwise None."""
    return value if isinstance(value, dict) else None


def _as_str(value: Any) -> str | None:
    """Return value when it is a str, otherwise None."""
    return value if isinstance(value, str) else None


def _as_nonempty_str(value: Any) -> str | None:
    """Return value when it is a non-empty str, otherwise None."""
    return value if isinstance(value, str) and value else None


def _as_nonblank_str(value: Any) -> str | None:
    """Return value when it is a str with non-whitespace characters, otherwise None."""
    return value if isinstance(value, str) and value.strip() else None


@dataclass
class ParsedMessage:
    """Parsed message from a transcript."""

    message_type: str  # "user", "assistant", "tool_use", "tool_result", etc.
    text: str  # Extracted text content
    tool_name: str | None = None  # For tool_use messages


@dataclass
class ParsedEntry:
    """A single parsed message entry ready for display."""

    role: str  # "user" | "assistant"
    text: str  # Already formatted text
    content_type: (
        str  # "text" | "thinking" | "tool_use" | "tool_result" | "local_command"
    )
    tool_use_id: str | None = None
    timestamp: str | None = None  # ISO timestamp from JSONL
    tool_name: str | None = (
        None  # For tool_use entries, the tool name (e.g. "AskUserQuestion")
    )
    image_data: list[tuple[str, bytes]] | None = (
        None  # For tool_result entries with images: (media_type, raw_bytes)
    )
    source_offset: int = 0
    error_code: str | None = None


@dataclass
class PendingToolInfo:
    """Information about a pending tool_use waiting for its tool_result."""

    summary: str  # Formatted tool summary (e.g. "**Read**(file.py)")
    tool_name: str  # Tool name (e.g. "Read", "Edit")
    input_data: Any = None  # Tool input parameters (for Edit to generate diff)


@dataclass
class _EntryParseState:
    """Mutable traversal state shared across entries in one parse_entries call."""

    pending_tools: dict[str, PendingToolInfo]
    result: list[ParsedEntry] = field(default_factory=list)
    last_cmd_name: str | None = None
    current_source_offset: int = 0

    def append(self, entry: ParsedEntry) -> None:
        if self.current_source_offset and entry.source_offset <= 0:
            entry.source_offset = self.current_source_offset
        self.result.append(entry)


@dataclass
class _AssistantEntryContext:
    """Per-entry context while scanning blocks of one assistant message."""

    timestamp: str | None
    is_agent_error: bool
    error_code: str | None
    has_text: bool = False


def _summarize_first_string_value(input_data: dict[str, Any]) -> str:
    for value in input_data.values():
        text = _as_nonempty_str(value)
        if text is not None:
            return text
    return ""


def _summarize_todo_write(input_data: dict[str, Any]) -> str:
    todos = input_data.get("todos", [])
    return f"{len(todos)} item(s)" if isinstance(todos, list) else ""


def _summarize_ask_user_question(input_data: dict[str, Any]) -> str:
    questions = input_data.get("questions", [])
    if not isinstance(questions, list) or not questions:
        return ""
    first = _as_dict(questions[0])
    if first is None:
        return ""
    question = first.get("question", "")
    return question if isinstance(question, str) else ""


def _count_nonempty_lines(text: str) -> int:
    return sum(1 for line in text.split("\n") if line.strip())


def _wrap_stats_quote(
    stats: str,
    text: str,
    format_quote: Callable[[str], str],
) -> str:
    return f"{stats}\n{format_quote(text)}"


class TranscriptParser:
    """Parser for Codex and Cursor Agent JSONL session files.

    Expected JSONL entry structure:
    - type: "user" | "assistant" | "summary" | "file-history-snapshot" | ...
    - message.content: list of blocks (text, tool_use, tool_result, thinking)
    - sessionId, cwd, timestamp, uuid: Codex metadata fields
    - role/message: Cursor Agent message records

    Tool pairing model: tool_use blocks appear in assistant messages,
    matching tool_result blocks appear in the next user message (keyed by tool_use_id).
    """

    # Magic string constants
    _NO_CONTENT_PLACEHOLDER = "(no content)"
    _INTERRUPTED_TEXT = "[Request interrupted by user for tool use]"
    _INTERNAL_TOOL_NAMES = {
        "close_agent",
        "create_goal",
        "get_goal",
        "spawn_agent",
        "tool_search_tool",
        "wait_agent",
        "update_goal",
        "update_plan",
        "view_image",
        "viewimage",
    }
    _MAX_SUMMARY_LENGTH = 200
    _AUTH_ERROR_HINT_MARKERS = (
        "could not be refreshed",
        "log out and sign in again",
        "refresh token",
        "refresh_token_reused",
        "token_invalidated",
    )
    _AUTH_ERROR_HINT = "Use /agentlogin to start an agent device login from Telegram."
    _ENCRYPTED_REASONING_PLACEHOLDER = "Working on it…"

    EXPANDABLE_QUOTE_START = "\x02EXPQUOTE_START\x02"
    EXPANDABLE_QUOTE_END = "\x02EXPQUOTE_END\x02"

    _RE_ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*m")
    _RE_COMMAND_NAME = re.compile(r"<command-name>(.*?)</command-name>")
    _RE_LOCAL_STDOUT = re.compile(
        r"<local-command-stdout>(.*?)</local-command-stdout>", re.DOTALL
    )
    _RE_SYSTEM_TAGS = re.compile(
        r"<(bash-input|bash-stdout|bash-stderr|codex_internal_context|"
        r"local-command-caveat|system-reminder)"
    )

    @classmethod
    def is_encrypted_reasoning_placeholder(cls, text: str) -> bool:
        """Return whether text is the synthetic encrypted-reasoning placeholder."""
        stripped = (
            text.replace(cls.EXPANDABLE_QUOTE_START, "")
            .replace(cls.EXPANDABLE_QUOTE_END, "")
            .strip()
        )
        return stripped == cls._ENCRYPTED_REASONING_PLACEHOLDER

    @classmethod
    def _format_error_message(cls, message: str) -> str:
        """Format an agent runtime error for Telegram."""
        formatted = f"⚠️ Agent error: {message.strip()}"
        lowered = message.lower()
        if any(marker in lowered for marker in cls._AUTH_ERROR_HINT_MARKERS):
            formatted = f"{formatted}\n\n{cls._AUTH_ERROR_HINT}"
        return formatted

    @staticmethod
    def _extract_nested_text(content: Any) -> list[str]:
        """Collect text from both old and new Codex content blocks."""
        texts: list[str] = []

        text_value = _as_nonempty_str(content)
        if text_value is not None:
            texts.append(text_value)
            return texts

        if isinstance(content, list):
            for item in content:
                texts.extend(TranscriptParser._extract_nested_text(item))
            return texts

        block = _as_dict(content)
        if block is None:
            return texts

        text = _as_nonempty_str(block.get("text"))
        if text is not None:
            texts.append(text)

        nested_content = block.get("content")
        if nested_content is not None:
            texts.extend(TranscriptParser._extract_nested_text(nested_content))

        return texts

    @classmethod
    def _normalize_message_content(cls, content: Any) -> list[dict[str, str]]:
        """Normalize message content into legacy text blocks."""
        blocks: list[dict[str, str]] = []
        for text in cls._extract_nested_text(content):
            if text:
                blocks.append({"type": "text", "text": text})
        return blocks

    @classmethod
    def _build_message_entry(
        cls,
        *,
        role: str,
        timestamp: str | None,
        content: Any,
    ) -> dict | None:
        blocks = cls._normalize_message_content(content)
        text = "\n".join(block["text"] for block in blocks if block.get("text")).strip()
        if not text:
            return None

        return {
            "type": role,
            "role": role,
            "text": text,
            "timestamp": timestamp,
            "message": {
                "role": role,
                "content": blocks,
            },
        }

    @staticmethod
    def _load_function_arguments(arguments: Any) -> dict:
        """Decode Codex response_item function_call arguments."""
        arg_dict = _as_dict(arguments)
        if arg_dict is not None:
            return arg_dict
        arg_str = _as_nonblank_str(arguments)
        if arg_str is not None:
            try:
                parsed = json.loads(arg_str)
            except json.JSONDecodeError:
                return {"arguments": arguments}
            return _as_dict(parsed) or {"arguments": parsed}
        return {}

    @staticmethod
    def _display_tool_name(name: str) -> str:
        """Map Codex runtime tool names to compact Telegram labels."""
        return {
            "exec_command": "Bash",
            "write_stdin": "Wait",
        }.get(name, name)

    @classmethod
    def _is_internal_tool_name(cls, name: str | None) -> bool:
        tool_name = _as_str(name)
        if tool_name is None:
            return False
        # Codex connector/app tools are exposed in transcripts with private
        # underscore-prefixed names (for example _list_repositories). These are
        # implementation details and should stay behind the Thinking status
        # instead of becoming persistent Telegram bubbles.
        return (
            tool_name.startswith("_") or tool_name.lower() in cls._INTERNAL_TOOL_NAMES
        )

    @staticmethod
    def _clean_runtime_tool_output(output: str) -> str:
        """Strip Codex runtime wrapper metadata from tool output text."""
        normalized = output.replace("\r\n", "\n")
        for marker in ("\nOutput:\n", "\nOutput:"):
            if marker in normalized:
                return normalized.rsplit(marker, 1)[1].strip()
        return output.strip()

    @classmethod
    def _normalize_reasoning(cls, payload: dict, timestamp: str | None) -> dict | None:
        """Normalize Codex reasoning into a visible thinking entry when allowed."""
        if not config.show_commentary_messages:
            return None

        texts: list[str] = []
        for key in ("summary", "content"):
            texts.extend(cls._extract_nested_text(payload.get(key)))
        thinking_text = "\n".join(
            text.strip() for text in texts if text.strip()
        ).strip()

        if not thinking_text:
            encrypted_content = _as_nonempty_str(payload.get("encrypted_content"))
            if encrypted_content is None:
                return None
            thinking_text = cls._ENCRYPTED_REASONING_PLACEHOLDER

        return {
            "type": "assistant",
            "role": "assistant",
            "timestamp": timestamp,
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "thinking",
                        "thinking": thinking_text,
                    }
                ],
            },
        }

    @classmethod
    def _normalize_function_call(
        cls, payload: dict, timestamp: str | None
    ) -> dict | None:
        call_id = _as_str(payload.get("call_id") or payload.get("id"))
        raw_name = _as_str(payload.get("name"))
        if call_id is None or raw_name is None:
            return None

        input_data = cls._load_function_arguments(payload.get("arguments"))
        cmd = _as_str(input_data.get("cmd"))
        if raw_name == "exec_command" and cmd is not None:
            input_data = {**input_data, "command": cmd}
        elif raw_name == "write_stdin":
            chars = input_data.get("chars")
            input_data = {
                **input_data,
                "summary": "background terminal" if chars in (None, "") else "stdin",
            }

        return {
            "type": "assistant",
            "role": "assistant",
            "timestamp": timestamp,
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": call_id,
                        "name": cls._display_tool_name(raw_name),
                        "input": input_data,
                    }
                ],
            },
        }

    @classmethod
    def _normalize_function_call_output(
        cls, payload: dict, timestamp: str | None
    ) -> dict | None:
        call_id = _as_str(payload.get("call_id"))
        if call_id is None:
            return None
        raw_output = payload.get("output", "")
        output = (
            raw_output
            if isinstance(raw_output, str)
            else json.dumps(raw_output, ensure_ascii=False)
        )
        output = cls._clean_runtime_tool_output(output)
        return {
            "type": "user",
            "role": "user",
            "timestamp": timestamp,
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": call_id,
                        "content": output,
                    }
                ],
            },
        }

    @classmethod
    def _normalize_response_message(
        cls, payload: dict, timestamp: str | None
    ) -> dict | None:
        role = payload.get("role")
        if role not in ("assistant", "user"):
            return None

        if (
            role == "assistant"
            and payload.get("phase") == "commentary"
            and not config.show_commentary_messages
        ):
            return None

        return cls._build_message_entry(
            role=role,
            timestamp=timestamp,
            content=payload.get("content"),
        )

    @classmethod
    def _normalize_task_complete(
        cls, payload: dict, timestamp: str | None
    ) -> dict | None:
        # Codex already emits assistant output as response_item/message.
        # When last_agent_message is present, task_complete repeats the
        # final answer, so skip it to avoid duplicate Telegram notifications
        # and duplicate history entries. A null last_agent_message means
        # Codex ended without a final assistant message; emit a small
        # completion notice so Telegram topics do not look silently stuck.
        if _as_nonblank_str(payload.get("last_agent_message")) is not None:
            return None

        error = _as_dict(payload.get("error"))
        if error is not None:
            raw_message = _as_nonblank_str(error.get("message"))
            message = (
                raw_message.strip()
                if raw_message is not None
                else "The agent stopped before completing the task."
            )
            entry = cls._build_message_entry(
                role="assistant",
                timestamp=timestamp,
                content=f"⚠️ Agent stopped before completing the task: {message}",
            )
            if entry is not None:
                entry["agent_error"] = True
                error_code = _as_nonempty_str(error.get("codex_error_info"))
                if error_code is not None:
                    entry["agent_error_code"] = error_code
            return entry

        return cls._build_message_entry(
            role="assistant",
            timestamp=timestamp,
            content="✅ Agent finished. No additional final message was emitted.",
        )

    @classmethod
    def _normalize_error_event(
        cls, payload: dict, timestamp: str | None
    ) -> tuple[bool, dict | None]:
        if payload.get("codex_error_info") == "usage_limit_exceeded":
            return True, None
        message = _as_nonblank_str(payload.get("message"))
        if message is not None:
            return True, cls._build_message_entry(
                role="assistant",
                timestamp=timestamp,
                content=cls._format_error_message(message),
            )
        return False, None

    @classmethod
    def _parse_passthrough_line(
        cls, data: dict, timestamp: str | None
    ) -> tuple[bool, dict | None]:
        del timestamp
        return True, data

    @classmethod
    def _parse_response_item_line(
        cls, data: dict, timestamp: str | None
    ) -> tuple[bool, dict | None]:
        payload = _as_dict(data.get("payload"))
        if payload is None:
            return True, None
        payload_type = _as_str(payload.get("type"))
        handler = (
            cls._RESPONSE_ITEM_HANDLERS.get(payload_type)
            if payload_type is not None
            else None
        )
        return True, (handler(cls, payload, timestamp) if handler is not None else None)

    @classmethod
    def _parse_event_msg_line(
        cls, data: dict, timestamp: str | None
    ) -> tuple[bool, dict | None]:
        payload = _as_dict(data.get("payload"))
        if payload is None:
            return True, None
        payload_type = _as_str(payload.get("type"))
        handler = (
            cls._EVENT_MSG_HANDLERS.get(payload_type)
            if payload_type is not None
            else None
        )
        if handler is None:
            return False, None
        return handler(cls, payload, timestamp)

    @classmethod
    def _parse_fallback_line(
        cls, data: dict, entry_type: Any, timestamp: str | None
    ) -> dict | None:
        # Cursor Agent stores JSONL records with role/message fields instead
        # of Codex's top-level type field. Normalize the shared message shape.
        role = data.get("role")
        if (
            entry_type is None
            and role in ("assistant", "user")
            and _as_dict(data.get("message")) is not None
        ):
            data["type"] = role
            return data

        if "role" in data and "text" in data and role in ("assistant", "user"):
            return cls._build_message_entry(
                role=role,
                timestamp=timestamp,
                content=data.get("text"),
            )

        return None

    _RESPONSE_ITEM_HANDLERS: dict[
        str, Callable[[Any, dict, str | None], dict | None]
    ] = {
        "reasoning": lambda cls, payload, ts: cls._normalize_reasoning(payload, ts),
        "function_call": lambda cls, payload, ts: cls._normalize_function_call(
            payload, ts
        ),
        "function_call_output": lambda cls, payload, ts: (
            cls._normalize_function_call_output(payload, ts)
        ),
        "message": lambda cls, payload, ts: cls._normalize_response_message(
            payload, ts
        ),
    }

    _EVENT_MSG_HANDLERS: dict[
        str, Callable[[Any, dict, str | None], tuple[bool, dict | None]]
    ] = {
        "task_complete": lambda cls, payload, ts: (
            True,
            cls._normalize_task_complete(payload, ts),
        ),
        "error": lambda cls, payload, ts: cls._normalize_error_event(payload, ts),
    }

    _LINE_TYPE_HANDLERS: dict[
        str, Callable[[Any, dict, str | None], tuple[bool, dict | None]]
    ] = {
        "assistant": lambda cls, data, ts: cls._parse_passthrough_line(data, ts),
        "user": lambda cls, data, ts: cls._parse_passthrough_line(data, ts),
        "response_item": lambda cls, data, ts: cls._parse_response_item_line(data, ts),
        "event_msg": lambda cls, data, ts: cls._parse_event_msg_line(data, ts),
    }

    @classmethod
    def parse_line(cls, line: str) -> dict | None:
        """Parse one JSONL line and normalize new Codex transcript formats."""
        try:
            raw = json.loads(line)
        except json.JSONDecodeError:
            return None

        data = _as_dict(raw)
        if data is None:
            return None

        entry_type = data.get("type")
        timestamp = data.get("timestamp")
        handler = (
            cls._LINE_TYPE_HANDLERS.get(entry_type)
            if isinstance(entry_type, str)
            else None
        )
        if handler is not None:
            handled, normalized = handler(cls, data, timestamp)
            if handled:
                return normalized

        return cls._parse_fallback_line(data, entry_type, timestamp)

    @staticmethod
    def get_message_type(data: dict) -> str | None:
        """Get the message type from parsed data.

        Returns:
            Message type: "user", "assistant", "file-history-snapshot", etc.
        """
        return data.get("type")

    @staticmethod
    def is_user_message(data: dict) -> bool:
        """Check if this is a user message."""
        return data.get("type") == "user"

    @staticmethod
    def extract_text_only(content_list: list[Any]) -> str:
        """Extract only text content from structured content.

        This is used for Telegram notifications where we only want
        the actual text response, not tool calls or thinking.

        Args:
            content_list: List of content blocks

        Returns:
            Combined text content only
        """
        if not isinstance(content_list, list):
            return content_list if isinstance(content_list, str) else ""

        texts: list[str] = []
        for item in content_list:
            if isinstance(item, str):
                texts.append(item)
                continue
            block = _as_dict(item)
            if block is not None and block.get("type") == "text":
                text = block.get("text", "")
                if text:
                    texts.append(text)

        return "\n".join(texts)

    @staticmethod
    def _format_edit_diff(old_string: str, new_string: str) -> str:
        """Generate a compact unified diff between old_string and new_string."""
        old_lines = old_string.splitlines(keepends=True)
        new_lines = new_string.splitlines(keepends=True)
        diff = difflib.unified_diff(old_lines, new_lines, lineterm="")
        # Skip the --- / +++ header lines
        result_lines: list[str] = []
        for line in diff:
            if line.startswith("---") or line.startswith("+++"):
                continue
            # Strip trailing newline for clean display
            result_lines.append(line.rstrip("\n"))
        return "\n".join(result_lines)

    _TOOL_SUMMARY_EXTRACTORS: dict[str, Callable[[dict[str, Any]], str]] = {
        "Read": lambda inp: inp.get("file_path") or inp.get("pattern", ""),
        "Glob": lambda inp: inp.get("file_path") or inp.get("pattern", ""),
        "Write": lambda inp: inp.get("file_path", ""),
        "Edit": lambda inp: inp.get("file_path") or inp.get("notebook_path", ""),
        "NotebookEdit": lambda inp: (
            inp.get("file_path") or inp.get("notebook_path", "")
        ),
        "Bash": lambda inp: inp.get("command", ""),
        "Wait": lambda inp: inp.get("summary", ""),
        "Grep": lambda inp: inp.get("pattern", ""),
        "Task": lambda inp: inp.get("description", ""),
        "WebFetch": lambda inp: inp.get("url", ""),
        "WebSearch": lambda inp: inp.get("query", ""),
        "TodoWrite": _summarize_todo_write,
        "TodoRead": lambda _inp: "",
        "AskUserQuestion": _summarize_ask_user_question,
        "ExitPlanMode": lambda _inp: "",
        "Skill": lambda inp: inp.get("skill", ""),
    }

    @classmethod
    def format_tool_use_summary(cls, name: str, input_data: dict | Any) -> str:
        """Format a tool_use block into a brief summary line.

        Args:
            name: Tool name (e.g. "Read", "Write", "Bash")
            input_data: The tool input dict

        Returns:
            Formatted string like "**Read**(file.py)"
        """
        data = _as_dict(input_data)
        if data is None:
            return f"**{name}**"

        extractor = cls._TOOL_SUMMARY_EXTRACTORS.get(
            name, _summarize_first_string_value
        )
        summary = extractor(data)

        if summary:
            if len(summary) > cls._MAX_SUMMARY_LENGTH:
                summary = summary[: cls._MAX_SUMMARY_LENGTH] + "…"
            return f"**{name}**({summary})"
        return f"**{name}**"

    @staticmethod
    def extract_tool_result_text(content: list | Any) -> str:
        """Extract text from a tool_result content block."""
        if isinstance(content, str):
            return content
        if not isinstance(content, list):
            return ""
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
                continue
            block = _as_dict(item)
            if block is not None and block.get("type") == "text":
                text = block.get("text", "")
                if text:
                    parts.append(text)
        return "\n".join(parts)

    @staticmethod
    def extract_tool_result_images(
        content: list | Any,
    ) -> list[tuple[str, bytes]] | None:
        """Extract base64-encoded images from a tool_result content block.

        Returns list of (media_type, raw_bytes) tuples, or None if no images found.
        """
        if not isinstance(content, list):
            return None
        images: list[tuple[str, bytes]] = []
        for item in content:
            block = _as_dict(item)
            if block is None or block.get("type") != "image":
                continue
            source = _as_dict(block.get("source"))
            if source is None or source.get("type") != "base64":
                continue
            media_type = source.get("media_type", "image/png")
            data_str = source.get("data", "")
            if not data_str:
                continue
            try:
                raw_bytes = base64.b64decode(data_str)
                images.append((media_type, raw_bytes))
            except Exception:
                logger.debug("Failed to decode base64 image in tool_result")
        return images if images else None

    @classmethod
    def parse_message(cls, data: dict) -> ParsedMessage | None:
        """Parse a message entry from the JSONL data.

        Args:
            data: Parsed JSON dict from a JSONL line

        Returns:
            ParsedMessage or None if not a parseable message
        """
        msg_type = cls.get_message_type(data)
        if msg_type not in ("user", "assistant"):
            return None

        message = _as_dict(data.get("message"))
        if message is None:
            return None
        content = message.get("content", "")

        text = (
            cls.extract_text_only(content)
            if isinstance(content, list)
            else (str(content) if content else "")
        )
        text = cls._RE_ANSI_ESCAPE.sub("", text)

        # Detect local command responses in user messages.
        # These are rendered as bot replies: "❯ /cmd\n  ⎿  output"
        if msg_type == "user" and text:
            stdout_match = cls._RE_LOCAL_STDOUT.search(text)
            if stdout_match:
                stdout = stdout_match.group(1).strip()
                cmd_match = cls._RE_COMMAND_NAME.search(text)
                cmd = cmd_match.group(1) if cmd_match else None
                return ParsedMessage(
                    message_type="local_command",
                    text=stdout,
                    tool_name=cmd,  # reuse field for command name
                )
            # Pure command invocation (no stdout) — carry command name
            cmd_match = cls._RE_COMMAND_NAME.search(text)
            if cmd_match:
                return ParsedMessage(
                    message_type="local_command_invoke",
                    text="",
                    tool_name=cmd_match.group(1),
                )

        return ParsedMessage(
            message_type=msg_type,
            text=text,
        )

    @staticmethod
    def get_timestamp(data: dict) -> str | None:
        """Extract timestamp from message data."""
        return data.get("timestamp")

    @classmethod
    def _format_expandable_quote(cls, text: str) -> str:
        """Format text as a Telegram expandable blockquote.

        Wraps text with sentinel markers. The actual MarkdownV2 formatting
        (> prefix, || suffix, escaping) is done in convert_markdown() after
        telegramify processes the surrounding content.
        """
        return f"{cls.EXPANDABLE_QUOTE_START}{text}{cls.EXPANDABLE_QUOTE_END}"

    _TOOL_RESULT_FORMATTERS: dict[str, Callable[[str, Callable[[str], str]], str]] = {
        "Read": lambda text, _q: f"  ⎿  Read {text.count(chr(10)) + 1} lines",
        "Write": lambda text, _q: f"  ⎿  Wrote {text.count(chr(10)) + 1} lines",
        "Bash": lambda text, q: _wrap_stats_quote(
            f"  ⎿  Output {text.count(chr(10)) + 1} lines", text, q
        ),
        "Grep": lambda text, q: _wrap_stats_quote(
            f"  ⎿  Found {_count_nonempty_lines(text)} matches", text, q
        ),
        "Glob": lambda text, q: _wrap_stats_quote(
            f"  ⎿  Found {_count_nonempty_lines(text)} files", text, q
        ),
        "Task": lambda text, q: _wrap_stats_quote(
            f"  ⎿  Agent output {text.count(chr(10)) + 1} lines", text, q
        ),
        "WebFetch": lambda text, q: _wrap_stats_quote(
            f"  ⎿  Fetched {len(text)} characters", text, q
        ),
        "WebSearch": lambda text, q: _wrap_stats_quote(
            f"  ⎿  {text.count(chr(10) * 2) + 1} search results", text, q
        ),
    }

    @classmethod
    def _format_tool_result_text(cls, text: str, tool_name: str | None = None) -> str:
        """Format tool result text with statistics summary.

        Shows relevant statistics for each tool type, with expandable quote for full content.

        No truncation here — per project principles, truncation is handled
        only at the send layer (split_message / _render_expandable_quote).
        """
        if not text:
            return ""
        formatter = (
            cls._TOOL_RESULT_FORMATTERS.get(tool_name)
            if tool_name is not None
            else None
        )
        if formatter is not None:
            return formatter(text, cls._format_expandable_quote)
        return cls._format_expandable_quote(text)

    @classmethod
    def _handle_local_command_message(
        cls,
        parsed: ParsedMessage | None,
        entry_timestamp: str | None,
        state: _EntryParseState,
    ) -> bool:
        if parsed is None:
            return False
        if parsed.message_type == "local_command_invoke":
            state.last_cmd_name = parsed.tool_name
            return True
        if parsed.message_type != "local_command":
            return False

        cmd = parsed.tool_name or state.last_cmd_name or ""
        text = parsed.text
        if cmd:
            formatted = (
                f"❯ `{cmd}`\n```\n{text}\n```"
                if "\n" in text
                else f"❯ `{cmd}`\n`{text}`"
            )
        else:
            formatted = f"```\n{text}\n```" if "\n" in text else f"`{text}`"
        state.append(
            ParsedEntry(
                role="assistant",
                text=formatted,
                content_type="local_command",
                timestamp=entry_timestamp,
            )
        )
        state.last_cmd_name = None
        return True

    @classmethod
    def _handle_assistant_text_block(
        cls,
        block: dict[str, Any],
        state: _EntryParseState,
        ctx: _AssistantEntryContext,
    ) -> None:
        text = block.get("text", "").strip()
        if not text or text == cls._NO_CONTENT_PLACEHOLDER:
            return
        state.append(
            ParsedEntry(
                role="assistant",
                text=text,
                content_type="agent_error" if ctx.is_agent_error else "text",
                timestamp=ctx.timestamp,
                error_code=ctx.error_code,
            )
        )
        ctx.has_text = True

    @classmethod
    def _handle_assistant_tool_use_block(
        cls,
        block: dict[str, Any],
        state: _EntryParseState,
        ctx: _AssistantEntryContext,
    ) -> None:
        tool_id = block.get("id", "")
        name = block.get("name", "unknown")
        inp = block.get("input", {})
        summary = cls.format_tool_use_summary(name, inp)
        should_emit_tool = not cls._is_internal_tool_name(name) and (
            name != "Bash" or config.show_bash_tool_calls
        )

        # ExitPlanMode: emit plan content as text before tool_use entry
        inp_dict = _as_dict(inp)
        if name == "ExitPlanMode" and inp_dict is not None:
            plan = inp_dict.get("plan", "")
            if plan:
                state.append(
                    ParsedEntry(
                        role="assistant",
                        text=plan,
                        content_type="text",
                        timestamp=ctx.timestamp,
                    )
                )

        if tool_id:
            # Edit tool needs input_data to generate diff in tool_result stage
            input_data = inp if name in ("Edit", "NotebookEdit") else None
            state.pending_tools[tool_id] = PendingToolInfo(
                summary=summary,
                tool_name=name,
                input_data=input_data,
            )

        if should_emit_tool:
            state.append(
                ParsedEntry(
                    role="assistant",
                    text=summary,
                    content_type="tool_use",
                    tool_use_id=tool_id or None,
                    timestamp=ctx.timestamp,
                    tool_name=name,
                )
            )

    @classmethod
    def _handle_assistant_thinking_block(
        cls,
        block: dict[str, Any],
        state: _EntryParseState,
        ctx: _AssistantEntryContext,
    ) -> None:
        thinking_text = block.get("thinking", "")
        if thinking_text:
            state.append(
                ParsedEntry(
                    role="assistant",
                    text=cls._format_expandable_quote(thinking_text),
                    content_type="thinking",
                    timestamp=ctx.timestamp,
                )
            )
        elif not ctx.has_text:
            state.append(
                ParsedEntry(
                    role="assistant",
                    text="(thinking)",
                    content_type="thinking",
                    timestamp=ctx.timestamp,
                )
            )

    _ASSISTANT_BLOCK_HANDLERS: dict[
        str,
        Callable[[Any, dict[str, Any], _EntryParseState, _AssistantEntryContext], None],
    ] = {
        "text": lambda cls, b, s, ctx: cls._handle_assistant_text_block(b, s, ctx),
        "tool_use": lambda cls, b, s, ctx: cls._handle_assistant_tool_use_block(
            b, s, ctx
        ),
        "thinking": lambda cls, b, s, ctx: cls._handle_assistant_thinking_block(
            b, s, ctx
        ),
    }

    @classmethod
    def _process_assistant_entry(
        cls,
        data: dict,
        content: list[Any],
        entry_timestamp: str | None,
        state: _EntryParseState,
    ) -> None:
        error_code = _as_nonempty_str(data.get("agent_error_code"))
        ctx = _AssistantEntryContext(
            timestamp=entry_timestamp,
            is_agent_error=data.get("agent_error") is True,
            error_code=error_code,
        )
        for raw_block in content:
            block = _as_dict(raw_block)
            if block is None:
                continue
            handler = cls._ASSISTANT_BLOCK_HANDLERS.get(block.get("type", ""))
            if handler is not None:
                handler(cls, block, state, ctx)

    @classmethod
    def _format_error_tool_result(
        cls,
        tool_summary: str | None,
        result_text: str,
    ) -> str:
        entry_text = tool_summary or "**Error**"
        if not result_text:
            return entry_text + "\n  ⎿  Error"
        error_summary = result_text.split("\n")[0]
        if len(error_summary) > 100:
            error_summary = error_summary[:100] + "…"
        entry_text += f"\n  ⎿  Error: {error_summary}"
        if "\n" in result_text:
            entry_text += "\n" + cls._format_expandable_quote(result_text)
        return entry_text

    @classmethod
    def _format_success_tool_result(
        cls,
        tool_summary: str,
        tool_name: str | None,
        tool_input_data: Any,
        result_text: str,
    ) -> str:
        if tool_name == "Edit" and tool_input_data and result_text:
            old_s = tool_input_data.get("old_string", "")
            new_s = tool_input_data.get("new_string", "")
            if old_s and new_s:
                diff_text = cls._format_edit_diff(old_s, new_s)
                if diff_text:
                    added = sum(
                        1
                        for line in diff_text.split("\n")
                        if line.startswith("+") and not line.startswith("+++")
                    )
                    removed = sum(
                        1
                        for line in diff_text.split("\n")
                        if line.startswith("-") and not line.startswith("---")
                    )
                    stats = f"  ⎿  Added {added} lines, removed {removed} lines"
                    return (
                        f"{tool_summary}\n{stats}\n"
                        f"{cls._format_expandable_quote(diff_text)}"
                    )
            return tool_summary

        if result_text and cls.EXPANDABLE_QUOTE_START not in tool_summary:
            return f"{tool_summary}\n{cls._format_tool_result_text(result_text, tool_name)}"
        return tool_summary

    @classmethod
    def _handle_user_tool_result_block(
        cls,
        block: dict[str, Any],
        entry_timestamp: str | None,
        state: _EntryParseState,
        _user_text_parts: list[str],
    ) -> None:
        tool_use_id = block.get("tool_use_id", "")
        result_content = block.get("content", "")
        result_text = cls.extract_tool_result_text(result_content)
        result_images = cls.extract_tool_result_images(result_content)
        tool_info = state.pending_tools.pop(tool_use_id, None)
        tool_summary = tool_info.summary if tool_info is not None else None
        tool_name = tool_info.tool_name if tool_info is not None else None
        tool_input_data = tool_info.input_data if tool_info is not None else None

        if cls._is_internal_tool_name(tool_name):
            return
        if tool_name == "Bash" and not config.show_bash_tool_calls:
            return
        if not result_text and not result_images:
            return

        tuid = tool_use_id or None
        if result_text == cls._INTERRUPTED_TEXT:
            entry_text = (
                f"{tool_summary}\n⏹ Interrupted" if tool_summary else "⏹ Interrupted"
            )
            state.append(
                ParsedEntry(
                    role="assistant",
                    text=entry_text,
                    content_type="tool_result",
                    tool_use_id=tuid,
                    timestamp=entry_timestamp,
                    tool_name=tool_name,
                )
            )
            return

        if block.get("is_error", False):
            entry_text = cls._format_error_tool_result(tool_summary, result_text)
        elif tool_summary:
            entry_text = cls._format_success_tool_result(
                tool_summary, tool_name, tool_input_data, result_text
            )
        else:
            # If the matching tool_use was already skipped or the monitor
            # resumed after it, the output has no context.
            return

        state.append(
            ParsedEntry(
                role="assistant",
                text=entry_text,
                content_type="tool_result",
                tool_use_id=tuid,
                timestamp=entry_timestamp,
                tool_name=tool_name,
                image_data=result_images,
            )
        )

    @classmethod
    def _handle_user_text_block(
        cls,
        block: dict[str, Any],
        _entry_timestamp: str | None,
        _state: _EntryParseState,
        user_text_parts: list[str],
    ) -> None:
        text = block.get("text", "").strip()
        if text and not cls._RE_SYSTEM_TAGS.search(text):
            user_text_parts.append(text)

    _USER_BLOCK_HANDLERS: dict[
        str,
        Callable[[Any, dict[str, Any], str | None, _EntryParseState, list[str]], None],
    ] = {
        "tool_result": lambda cls, b, ts, s, parts: cls._handle_user_tool_result_block(
            b, ts, s, parts
        ),
        "text": lambda cls, b, ts, s, parts: cls._handle_user_text_block(
            b, ts, s, parts
        ),
    }

    @classmethod
    def _process_user_entry(
        cls,
        _data: dict,
        content: list[Any],
        entry_timestamp: str | None,
        state: _EntryParseState,
    ) -> None:
        user_text_parts: list[str] = []
        for raw_block in content:
            block = _as_dict(raw_block)
            if block is None:
                plain = _as_nonblank_str(raw_block)
                if plain is not None:
                    user_text_parts.append(plain.strip())
                continue
            handler = cls._USER_BLOCK_HANDLERS.get(block.get("type", ""))
            if handler is not None:
                handler(cls, block, entry_timestamp, state, user_text_parts)

        if not user_text_parts:
            return
        combined = "\n".join(user_text_parts)
        if not cls._RE_LOCAL_STDOUT.search(
            combined
        ) and not cls._RE_COMMAND_NAME.search(combined):
            state.append(
                ParsedEntry(
                    role="user",
                    text=combined,
                    content_type="text",
                    timestamp=entry_timestamp,
                )
            )

    _ENTRY_ROLE_HANDLERS: dict[
        str, Callable[[Any, dict, list[Any], str | None, _EntryParseState], None]
    ] = {
        "assistant": lambda cls, d, c, ts, s: cls._process_assistant_entry(d, c, ts, s),
        "user": lambda cls, d, c, ts, s: cls._process_user_entry(d, c, ts, s),
    }

    @classmethod
    def parse_entries(
        cls,
        entries: list[dict],
        pending_tools: dict[str, PendingToolInfo] | None = None,
    ) -> tuple[list[ParsedEntry], dict[str, PendingToolInfo]]:
        """Parse a list of JSONL entries into a flat list of display-ready messages.

        This is the shared core logic used by both get_recent_messages (history)
        and check_for_updates (monitor).

        Args:
            entries: List of parsed JSONL dicts (already filtered through parse_line)
            pending_tools: Optional carry-over pending tool_use state from a
                previous call (tool_use_id -> formatted summary). Used by the
                monitor to handle tool_use and tool_result arriving in separate
                poll cycles.

        Returns:
            Tuple of (parsed entries, remaining pending_tools state)
        """
        carry_over = pending_tools is not None
        state = _EntryParseState(
            pending_tools=dict(pending_tools) if pending_tools is not None else {}
        )

        for data in entries:
            raw_source_offset = data.get("_telegram_agent_bot_end_offset")
            state.current_source_offset = (
                raw_source_offset
                if isinstance(raw_source_offset, int) and raw_source_offset > 0
                else 0
            )
            msg_type = cls.get_message_type(data)
            entry_handler = (
                cls._ENTRY_ROLE_HANDLERS.get(msg_type) if msg_type is not None else None
            )
            if entry_handler is None:
                continue

            message = _as_dict(data.get("message"))
            if message is None:
                continue
            raw_content = message.get("content", "")
            content = (
                raw_content
                if isinstance(raw_content, list)
                else (
                    [{"type": "text", "text": str(raw_content)}] if raw_content else []
                )
            )

            entry_timestamp = cls.get_timestamp(data)
            parsed = cls.parse_message(data)
            if cls._handle_local_command_message(parsed, entry_timestamp, state):
                continue
            state.last_cmd_name = None

            entry_handler(cls, data, content, entry_timestamp, state)

        # Flush remaining pending tools at end.
        # In carry-over mode (monitor), keep them pending for the next call
        # without emitting entries. In one-shot mode (history), emit them.
        remaining_pending = dict(state.pending_tools)
        if not carry_over:
            for tool_id, tool_info in state.pending_tools.items():
                if cls._is_internal_tool_name(tool_info.tool_name):
                    continue
                state.append(
                    ParsedEntry(
                        role="assistant",
                        text=tool_info.summary,
                        content_type="tool_use",
                        tool_use_id=tool_id,
                        tool_name=tool_info.tool_name,
                    )
                )

        for entry in state.result:
            entry.text = entry.text.strip()

        return state.result, remaining_pending
