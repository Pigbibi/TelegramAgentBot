"""Pure JSONL transcript discovery, tail-scanning, and message history helpers."""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aiofiles

from .session_storage import canonical_session_id
from .transcript_parser import TranscriptParser

logger = logging.getLogger(__name__)


@dataclass
class CodexSession:
    """Information about a Codex session."""

    session_id: str
    summary: str
    message_count: int
    file_path: str


def normalize_path(path_str: str) -> str:
    """Normalize cwd values for stable comparisons."""
    try:
        return str(Path(path_str).resolve())
    except OSError:
        return path_str


def encode_cwd(cwd: str) -> str:
    """Encode a cwd path to match Codex's project directory naming."""
    return re.sub(r"[^a-zA-Z0-9-]", "-", cwd)


def build_session_file_path(
    session_id: str,
    cwd: str,
    *,
    root: Path,
) -> Path | None:
    """Build the direct transcript file path for session_id and cwd under root."""
    if not session_id or not cwd:
        return None
    return root / encode_cwd(cwd) / f"{session_id}.jsonl"


def cursor_projects_root() -> Path:
    """Return the base Cursor projects directory."""
    return Path.home() / ".cursor" / "projects"


def cursor_transcript_dir(cwd: str, *, root: Path | None = None) -> Path:
    """Return the Cursor agent-transcripts directory for cwd."""
    project_slug = Path(cwd).as_posix().lstrip("/").replace("/", "-")
    return (root or cursor_projects_root()) / project_slug / "agent-transcripts"


def find_session_file(
    session_id: str,
    cwd: str,
    *,
    roots: Iterable[Path],
    cursor_root: Path | None = None,
) -> Path | None:
    """Locate the JSONL transcript file for session_id and cwd."""
    if not session_id or not cwd:
        return None
    cursor_dir = cursor_transcript_dir(cwd, root=cursor_root)
    cursor_candidate = cursor_dir / session_id / f"{session_id}.jsonl"
    if cursor_candidate.exists():
        return cursor_candidate
    try:
        cursor_matches = list(cursor_dir.rglob(f"{session_id}.jsonl"))
    except OSError:
        cursor_matches = []
    if cursor_matches:
        return cursor_matches[0]

    root_list = list(roots)
    for root in root_list:
        candidate = build_session_file_path(session_id, cwd, root=root)
        if candidate and candidate.exists():
            return candidate

    canonical_id = canonical_session_id(session_id)
    for root in root_list:
        try:
            matches = list(root.rglob(f"{session_id}.jsonl"))
            if not matches and canonical_id:
                matches = list(root.rglob(f"*{canonical_id}.jsonl"))
        except OSError:
            continue
        if matches:
            return matches[0]
    return None


def collect_transcript_files(roots: Iterable[Path]) -> list[Path]:
    """Collect deduplicated transcript files ordered newest-first by mtime."""
    deduped_files: dict[str, Path] = {}
    for root in roots:
        if not root.exists():
            continue
        for path in root.rglob("*.jsonl"):
            if not path.is_file() or path.stem == "sessions-index":
                continue
            key = str(path.resolve())
            deduped_files.setdefault(key, path)

    return sorted(
        deduped_files.values(),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )


def extract_user_text(data: dict[str, Any]) -> str:
    """Extract user text from both legacy and rollout transcript entries."""
    if TranscriptParser.is_user_message(data):
        parsed = TranscriptParser.parse_message(data)
        if parsed and parsed.text.strip():
            return parsed.text.strip()

    payload = data.get("payload")
    if isinstance(payload, dict):
        if payload.get("type") == "user_message":
            message = payload.get("message", "")
            if isinstance(message, str):
                return message.strip()

        if payload.get("type") == "message" and payload.get("role") == "user":
            content = payload.get("content", [])
            texts: list[str] = []
            if isinstance(content, list):
                for item in content:
                    if isinstance(item, str) and item.strip():
                        texts.append(item.strip())
                    elif isinstance(item, dict):
                        text = item.get("text", "")
                        if isinstance(text, str) and text.strip():
                            texts.append(text.strip())
            text = "\n".join(texts).strip()
            if text.startswith("<environment_context>") or text.startswith(
                "# AGENTS.md instructions for "
            ):
                return ""
            return text

    return ""


def normalize_transcript_text(text: str) -> str:
    """Normalize trailing whitespace across lines for transcript matching."""
    return "\n".join(line.rstrip() for line in text.strip().splitlines())


def user_text_matches(actual: str, expected: str) -> bool:
    """Return True when actual user transcript text matches expected prompt text."""
    actual_norm = normalize_transcript_text(actual)
    expected_norm = normalize_transcript_text(expected)
    if not actual_norm or not expected_norm:
        return False
    if actual_norm == expected_norm:
        return True
    if len(expected_norm) <= 120:
        return expected_norm in actual_norm
    return expected_norm[:80] in actual_norm and expected_norm[-80:] in actual_norm


def transcript_tail_contains_user_text(
    file_path: Path,
    expected_text: str,
    *,
    after_offset: int | None = None,
    max_bytes: int = 262_144,
) -> bool:
    """Return whether new transcript bytes contain the expected user input."""
    try:
        with file_path.open("rb") as f:
            f.seek(0, 2)
            size = f.tell()
            if after_offset is not None and size < after_offset:
                return False
            start = max(0, size - max_bytes)
            if after_offset is not None:
                # Cursor may have started a JSONL user record while the
                # pre-submit size was captured. Include that partial line,
                # but never accept matching text written before the cursor.
                start = max(start, after_offset - 8192)
            f.seek(start)
            raw = f.read()
    except OSError:
        return False

    line_start = start
    for encoded_line in raw.splitlines(keepends=True):
        line_end = line_start + len(encoded_line)
        if after_offset is not None and line_end <= after_offset:
            line_start = line_end
            continue
        if after_offset is not None and line_start < after_offset:
            suffix = encoded_line[after_offset - line_start :]
            encoded_text = (
                json.dumps(expected_text, ensure_ascii=False)[1:-1].encode(),
                json.dumps(expected_text, ensure_ascii=True)[1:-1].encode(),
            )
            if not any(value in suffix for value in encoded_text):
                line_start = line_end
                continue
        line_start = line_end
        line = encoded_line.decode("utf-8", errors="replace").strip()
        if not line:
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        normalized = TranscriptParser.parse_line(line)
        user_text = extract_user_text(normalized or data)
        if user_text and user_text_matches(user_text, expected_text):
            return True
    return False


def transcript_tail_contains_resume_ready(
    file_path: Path,
    *,
    after_offset: int,
) -> bool:
    """Check for Codex's post-resume settings event after the saved cursor."""
    try:
        with file_path.open("rb") as transcript:
            transcript.seek(0, 2)
            size = transcript.tell()
            if size <= after_offset:
                return False
            start = max(0, after_offset - 8192, size - 262_144)
            transcript.seek(start)
            raw = transcript.read()
    except OSError:
        return False

    line_start = start
    for encoded_line in raw.splitlines(keepends=True):
        line_end = line_start + len(encoded_line)
        if line_end <= after_offset:
            line_start = line_end
            continue
        line_start = line_end
        try:
            data = json.loads(encoded_line)
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        payload = data.get("payload")
        if (
            data.get("type") == "event_msg"
            and isinstance(payload, dict)
            and payload.get("type") == "thread_settings_applied"
        ):
            return True
    return False


def file_has_usage_limit_exceeded(
    file_path: Path,
    max_bytes: int = 128 * 1024,
) -> bool:
    """Check recent transcript tail for a usage_limit_exceeded event."""

    def _is_usage_limit_payload(payload: dict[str, Any]) -> bool:
        if payload.get("type") == "error":
            return payload.get("codex_error_info") == "usage_limit_exceeded"

        if payload.get("type") == "token_count":
            rate_limits = payload.get("rate_limits")
            if isinstance(rate_limits, dict):
                rate_limit_reached_type = rate_limits.get("rate_limit_reached_type")
                if isinstance(rate_limit_reached_type, str) and rate_limit_reached_type:
                    return True

        return False

    try:
        size = file_path.stat().st_size
        with file_path.open("rb") as handle:
            if size > max_bytes:
                handle.seek(size - max_bytes)
            chunk = handle.read().decode("utf-8", errors="ignore")
    except OSError:
        return False

    for line in reversed(chunk.splitlines()):
        try:
            payload = json.loads(line).get("payload", {})
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict) and _is_usage_limit_payload(payload):
            return True
    return False


async def read_session_from_file(
    file_path: Path,
    *,
    session_id: str,
) -> CodexSession | None:
    """Read one transcript file and build a lightweight session summary."""
    summary = ""
    last_user_msg = ""
    message_count = 0
    try:
        async with aiofiles.open(file_path, "r", encoding="utf-8") as f:
            async for line in f:
                line = line.strip()
                if not line:
                    continue
                message_count += 1
                try:
                    data = json.loads(line)
                    if data.get("type") == "summary":
                        s = data.get("summary", "")
                        if s:
                            summary = s
                    else:
                        user_text = extract_user_text(data)
                        if user_text:
                            last_user_msg = user_text
                except json.JSONDecodeError:
                    continue
    except OSError:
        return None

    if not summary:
        summary = last_user_msg[:50] if last_user_msg else "Untitled"

    return CodexSession(
        session_id=session_id,
        summary=summary,
        message_count=message_count,
        file_path=str(file_path),
    )


async def read_session_preview_from_file(
    file_path: Path,
    *,
    session_id: str,
    max_lines: int = 80,
) -> CodexSession | None:
    """Read enough transcript metadata for the resume picker without full scans."""
    summary = ""
    last_user_msg = ""
    scanned_lines = 0

    try:
        async with aiofiles.open(file_path, "r", encoding="utf-8") as f:
            async for line in f:
                line = line.strip()
                if not line:
                    continue
                scanned_lines += 1
                try:
                    data = json.loads(line)
                    if data.get("type") == "summary":
                        s = data.get("summary", "")
                        if s:
                            summary = s
                            break
                    else:
                        user_text = extract_user_text(data)
                        if user_text:
                            last_user_msg = user_text
                            break
                except json.JSONDecodeError:
                    continue

                if scanned_lines >= max_lines:
                    break
    except OSError:
        return None

    if not summary:
        summary = last_user_msg[:50] if last_user_msg else "Untitled"

    return CodexSession(
        session_id=session_id,
        summary=summary,
        message_count=0,
        file_path=str(file_path),
    )


async def read_recent_messages(
    file_path: Path,
    *,
    start_byte: int = 0,
    end_byte: int | None = None,
) -> tuple[list[dict[str, Any]], int]:
    """Read parsed user/assistant messages from a JSONL transcript within byte bounds."""
    if not file_path.exists():
        return [], 0

    entries: list[dict[str, Any]] = []
    try:
        async with aiofiles.open(file_path, "r", encoding="utf-8") as f:
            if start_byte > 0:
                await f.seek(start_byte)

            while True:
                if end_byte is not None:
                    current_pos = await f.tell()
                    if current_pos >= end_byte:
                        break

                line = await f.readline()
                if not line:
                    break

                data = TranscriptParser.parse_line(line)
                if data:
                    entries.append(data)
    except OSError as exc:
        logger.error("Error reading session file %s: %s", file_path, exc)
        return [], 0

    parsed_entries, _ = TranscriptParser.parse_entries(entries)
    # Model reasoning is an internal implementation detail and must not be
    # exposed through Telegram history, even when commentary is enabled.
    all_messages = [
        {
            "role": e.role,
            "text": e.text,
            "content_type": e.content_type,
            "timestamp": e.timestamp,
        }
        for e in parsed_entries
        if e.content_type != "thinking"
    ]
    return all_messages, len(all_messages)
