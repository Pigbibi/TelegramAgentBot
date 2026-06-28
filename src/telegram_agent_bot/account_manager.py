"""Helpers for managing multiple auth snapshots for telegram-agent-bot.

Supports both Codex CLI and Claude Code CLI accounts. The active agent type
(default: "codex") is read from config to determine which auth files to manage.
"""

from __future__ import annotations

import contextlib
import logging
import os
import re
import shutil
from pathlib import Path

from .utils import app_dir

logger = logging.getLogger(__name__)

TELEGRAM_AGENT_BOT_ACCOUNTS_DIR = app_dir() / "accounts"
SNAPSHOT_DIR = TELEGRAM_AGENT_BOT_ACCOUNTS_DIR / "snapshots"
CURRENT_NAME_FILE = TELEGRAM_AGENT_BOT_ACCOUNTS_DIR / "current_name"
ACCOUNT_HOME_DIR = TELEGRAM_AGENT_BOT_ACCOUNTS_DIR / "homes"
CODEX_DIR = Path.home() / ".codex"
CLAUDE_DIR = Path.home() / ".claude"
_ACCOUNT_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")


def agent_auth_dir() -> Path:
    """Return the home auth directory for the configured agent type."""
    from .config import config

    if config.agent_type == "claude":
        return CLAUDE_DIR
    return CODEX_DIR


def agent_auth_filename() -> str:
    """Return the auth file name for the configured agent type.

    Codex stores auth in auth.json, Claude Code stores credentials.db.
    Fall back to auth.json for unknown types.
    """
    from .config import config

    if config.agent_type == "claude":
        return "credentials.db"
    return "auth.json"


def is_valid_account_name(name: str) -> bool:
    """Return whether name is safe for use as a snapshot directory."""
    return bool(_ACCOUNT_NAME_RE.fullmatch(name)) and name not in {".", ".."}


def _has_auth_file(account_dir: Path) -> bool:
    """Return True if the directory contains any known auth file."""
    return (
        (account_dir / "auth.json").is_file()
        or (account_dir / "credentials.db").is_file()
    )


def list_account_names() -> list[str]:
    """List saved account snapshot names in stable order."""
    if not SNAPSHOT_DIR.exists():
        return []
    names = [
        path.name
        for path in SNAPSHOT_DIR.iterdir()
        if path.is_dir() and _has_auth_file(path)
    ]
    return sorted(names)


def list_account_homes() -> list[Path]:
    """List prepared per-account agent home directories."""
    if not ACCOUNT_HOME_DIR.exists():
        return []
    homes = [
        path
        for path in ACCOUNT_HOME_DIR.iterdir()
        if path.is_dir() and _has_auth_file(path)
    ]
    return sorted(homes)


def get_current_account_name() -> str | None:
    """Return the manually selected snapshot name, if any."""
    if not CURRENT_NAME_FILE.is_file():
        return None
    try:
        name = CURRENT_NAME_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return name if name in list_account_names() else None


def get_default_account_name() -> str | None:
    """Return the saved account selected for new sessions, if any.

    A missing current selection intentionally means "use the service user's
    default CODEX_HOME". Saving snapshots alone should not silently move new
    sessions away from the primary account.
    """
    return get_current_account_name()


def get_next_account_name(current_name: str | None) -> str | None:
    """Return the next snapshot name for quota rotation."""
    names = list_account_names()
    if not names:
        return None
    if current_name in names:
        idx = names.index(current_name)
        if len(names) == 1:
            return None
        return names[(idx + 1) % len(names)]
    fallback_current = get_current_account_name()
    if fallback_current in names:
        return fallback_current
    return None


def remember_current_account(name: str) -> None:
    """Persist the snapshot name currently used for new sessions."""
    if not name:
        return
    TELEGRAM_AGENT_BOT_ACCOUNTS_DIR.mkdir(parents=True, exist_ok=True)
    CURRENT_NAME_FILE.write_text(f"{name}\n", encoding="utf-8")


def clear_current_account() -> None:
    """Use the service user's default CODEX_HOME for new sessions."""
    with contextlib.suppress(OSError):
        CURRENT_NAME_FILE.unlink()


def _copy_if_different(source: Path, dest: Path) -> None:
    """Copy a file when it does not exist or content changed."""
    if not source.is_file():
        return
    if dest.is_file() and source.read_bytes() == dest.read_bytes():
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, dest)


def _disable_codex_update_prompt(config_file: Path) -> None:
    """Ensure telegram-agent-bot-managed Codex starts do not block on the update prompt."""
    key = "check_for_update_on_startup"
    desired = f"{key} = false"

    if not config_file.exists():
        config_file.parent.mkdir(parents=True, exist_ok=True)
        config_file.write_text(f"{desired}\n", encoding="utf-8")
        return

    try:
        text = config_file.read_text(encoding="utf-8")
    except OSError:
        return

    lines = text.splitlines()
    replaced = False
    for index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.split("=", 1)[0].strip() == key:
            lines[index] = desired
            replaced = True
            break

    if not replaced:
        insert_at = len(lines)
        for index, line in enumerate(lines):
            if line.lstrip().startswith("["):
                insert_at = index
                break
        lines.insert(insert_at, desired)
        if insert_at < len(lines) - 1 and lines[insert_at + 1].strip():
            lines.insert(insert_at + 1, "")

    new_text = "\n".join(lines) + ("\n" if text.endswith("\n") or lines else "")
    if new_text != text:
        config_file.write_text(new_text, encoding="utf-8")


def _disable_agent_update_prompt(agent_type: str, config_file: Path) -> None:
    """Disable the startup update prompt for the active agent type.

    For Codex: sets check_for_update_on_startup = false in config.toml.
    For Claude: no equivalent config file yet (the CLI handles its own update checks).
    """
    if agent_type == "claude":
        # Claude Code does not use config.toml for update prompts.
        return
    _disable_codex_update_prompt(config_file)


def disable_codex_update_prompt(codex_home: Path | None = None) -> None:
    """Disable the agent's startup update prompt for the selected home directory."""
    from .config import config

    if codex_home is None:
        env_home = os.getenv("CODEX_HOME") or os.getenv("CLAUDE_HOME")
        if config.agent_type == "claude":
            codex_home = Path(env_home).expanduser() if env_home else agent_auth_dir()
        else:
            env_home = os.getenv("CODEX_HOME")
            codex_home = Path(env_home).expanduser() if env_home else CODEX_DIR
    _disable_agent_update_prompt(config.agent_type, codex_home / "config.toml")


def _agent_auth_path(codex_home: Path) -> Path:
    """Return the auth file path for the configured agent type."""
    from .config import config

    return codex_home / agent_auth_filename()


def save_account_snapshot(
    name: str,
    codex_home: Path | None = None,
) -> Path:
    """Save auth file from the agent home into a named account snapshot."""
    from .config import config

    if not is_valid_account_name(name):
        raise ValueError(f"Invalid account name: {name}")
    codex_home = codex_home or agent_auth_dir()
    snapshot_dir = SNAPSHOT_DIR / name
    snapshot_dir.mkdir(parents=True, exist_ok=True)

    auth_path = _agent_auth_path(codex_home)
    source_auth = codex_home / "auth.json"
    if not source_auth.is_file() and config.agent_type == "codex":
        raise FileNotFoundError(f"Codex auth file not found: {source_auth}")

    # Try agent-specific auth filename first, fall back to auth.json
    for candidate in (auth_path, codex_home / "auth.json", codex_home / "credentials.db"):
        if candidate.is_file():
            _copy_if_different(candidate, snapshot_dir / candidate.name)
            break
    else:
        raise FileNotFoundError(
            f"No auth file found in {codex_home} "
            f"(looked for {agent_auth_filename()}, auth.json, credentials.db)"
        )
    return snapshot_dir


def prepare_account_home(name: str) -> Path:
    """Create a dedicated agent home for a named account without requiring auth."""
    from .config import config

    if not is_valid_account_name(name):
        raise ValueError(f"Invalid account name: {name}")
    account_home = ACCOUNT_HOME_DIR / name
    account_home.mkdir(parents=True, exist_ok=True)
    _copy_if_different(CODEX_DIR / "config.toml", account_home / "config.toml")
    _copy_if_different(CODEX_DIR / "hooks.json", account_home / "hooks.json")
    disable_codex_update_prompt(account_home)
    for child in ("memories", "tmp"):
        (account_home / child).mkdir(parents=True, exist_ok=True)
    if config.agent_type == "claude":
        # Claude Code uses its own directory layout under .claude
        (account_home / "projects").mkdir(parents=True, exist_ok=True)
    return account_home


def ensure_account_home(name: str) -> Path:
    """Ensure a dedicated agent home exists for one saved snapshot."""
    from .config import config

    snapshot_dir = SNAPSHOT_DIR / name
    auth_filename = agent_auth_filename()
    snapshot_auth = snapshot_dir / auth_filename
    # Fallback: try auth.json for legacy codex snapshots
    fallback_auth = snapshot_dir / "auth.json"
    account_home = prepare_account_home(name)

    target_auth = account_home / auth_filename
    if not target_auth.is_file():
        if snapshot_auth.is_file():
            _copy_if_different(snapshot_auth, target_auth)
        elif fallback_auth.is_file():
            _copy_if_different(fallback_auth, account_home / "auth.json")
        else:
            raise FileNotFoundError(
                f"Account snapshot not found: {name} "
                f"(looked for {auth_filename} and auth.json)"
            )

    logger.debug(
        "Prepared agent home for %s at %s (type=%s)",
        name,
        account_home,
        config.agent_type,
    )
    return account_home
