"""Shared utility functions used across TelegramAgentBot modules.

Provides:
  - app_dir(): resolve config directory from TELEGRAM_AGENT_BOT_DIR env var.
  - atomic_write_json(): crash-safe JSON file writes via temp+rename.
  - read_cwd_from_jsonl(): extract the cwd field from the first JSONL entry.
  - is_subagent_transcript(): detect Codex spawned-agent transcripts.
"""

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any

TELEGRAM_AGENT_BOT_DIR_ENV = "TELEGRAM_AGENT_BOT_DIR"

_LEADING_AGENTS_HEADER_RE = re.compile(
    r"^\s*(?:\S+\s+)?# AGENTS\.md instructions for [^\n]+\n+"
)
_INSTRUCTIONS_BLOCK_RE = re.compile(
    r"^\s*<INSTRUCTIONS>\s*.*?</INSTRUCTIONS>\s*",
    re.DOTALL,
)
_ENV_CONTEXT_BLOCK_RE = re.compile(
    r"^\s*<environment_context>\s*.*?</environment_context>\s*",
    re.DOTALL,
)


def app_dir() -> Path:
    """Resolve config directory from TELEGRAM_AGENT_BOT_DIR env var or default ~/.telegram-agent-bot."""
    raw = os.environ.get(TELEGRAM_AGENT_BOT_DIR_ENV, "")
    return Path(raw) if raw else Path.home() / ".telegram-agent-bot"


def sanitize_forward_text(text: str) -> str:
    """Strip bridge-added wrapper metadata before sending text to Codex."""
    sanitized = text.strip()
    if not sanitized:
        return ""

    while True:
        changed = False
        for pattern in (
            _LEADING_AGENTS_HEADER_RE,
            _INSTRUCTIONS_BLOCK_RE,
            _ENV_CONTEXT_BLOCK_RE,
        ):
            match = pattern.match(sanitized)
            if not match:
                continue
            sanitized = sanitized[match.end() :].lstrip()
            changed = True
        if not changed:
            break

    return sanitized


def atomic_write_json(path: Path, data: Any, indent: int = 2) -> None:
    """Write JSON data to a file atomically.

    Writes to a temporary file in the same directory, then renames it
    to the target path. This prevents data corruption if the process
    is interrupted mid-write.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(data, indent=indent)

    # Write to temp file in same directory (same filesystem for atomic rename)
    fd, tmp_path = tempfile.mkstemp(
        dir=str(path.parent), suffix=".tmp", prefix=f".{path.name}."
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, str(path))
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def read_cwd_from_jsonl(file_path: str | Path) -> str:
    """Read the cwd field from the first JSONL entry that has one.

    Shared by session.py and session_monitor.py.
    """
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                    cwd = data.get("cwd")
                    if not cwd:
                        payload = data.get("payload")
                        if isinstance(payload, dict):
                            cwd = payload.get("cwd")
                    if cwd:
                        return cwd
                except json.JSONDecodeError:
                    continue
    except OSError:
        pass
    return ""


def is_subagent_transcript(file_path: str | Path) -> bool:
    """Return True when a Codex transcript belongs to a spawned subagent."""
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if data.get("type") != "session_meta":
                    continue
                payload = data.get("payload")
                if not isinstance(payload, dict):
                    return False
                source = payload.get("source")
                return isinstance(source, dict) and "subagent" in source
    except OSError:
        pass
    return False
