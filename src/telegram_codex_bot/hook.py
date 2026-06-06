"""Hook subcommand for Codex session tracking.

Called by a SessionStart hook to maintain a window↔session mapping in
<TELEGRAM_CODEX_BOT_DIR>/session_map.json. Also provides `--install` to enable Codex hooks
in the active Codex home (`$CODEX_HOME` when set, else `~/.codex`) and
register the SessionStart hook there.

This module must NOT import config.py (which requires TELEGRAM_BOT_TOKEN),
since hooks run inside tmux panes where bot env vars are not set.
Config directory resolution uses utils.app_dir() (shared with config.py).

Key functions: hook_main() (CLI entry), _install_hook().
"""

import argparse
import fcntl
import json
import logging
import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Validate session_id looks like a UUID or a Codex rollout session id
_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_ROLLOUT_SESSION_RE = re.compile(
    r"^rollout-[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}-[0-9]{2}-[0-9]{2}-[0-9a-f-]+$"
)
_CONFIG_SECTION_RE = re.compile(r"^\s*\[(.+?)\]\s*$")
_HOOKS_FLAG_RE = re.compile(r"^\s*hooks\s*=")
_LEGACY_CODEX_HOOKS_FLAG_RE = re.compile(r"^\s*codex_hooks\s*=")

_SESSION_START_MATCHER = "startup|resume"
_HOOK_STATUS_MESSAGE = "Registering Codex session"
_HOOK_TIMEOUT_SECONDS = 5

# The hook command suffix for detection
_HOOK_COMMAND_SUFFIX = "telegram-codex-bot hook"


def _is_non_interactive_session(payload: dict[str, Any]) -> bool:
    """Return True for non-TUI Codex sessions that should not own a tmux window."""
    source = str(payload.get("source") or "").strip().lower()
    originator = str(payload.get("originator") or "").strip().lower()
    return source == "exec" or originator == "codex_exec"


def _codex_dir() -> Path:
    """Resolve the active Codex home for hook install/runtime."""
    codex_home = os.getenv("CODEX_HOME")
    return Path(codex_home).expanduser() if codex_home else Path.home() / ".codex"


def _codex_config_file() -> Path:
    return _codex_dir() / "config.toml"


def _codex_hooks_file() -> Path:
    return _codex_dir() / "hooks.json"


def _find_cli_path() -> str:
    """Find the full path to the telegram-codex-bot executable.

    Priority:
    1. shutil.which("telegram-codex-bot") - if telegram-codex-bot is in PATH
    2. Same directory as the Python interpreter (for venv installs)
    """
    # Try PATH first
    cli_path = shutil.which("telegram-codex-bot")
    if cli_path:
        return cli_path

    # Fall back to the directory containing the Python interpreter
    # This handles the case where telegram-codex-bot is installed in a venv
    python_dir = Path(sys.executable).parent
    cli_in_venv = python_dir / "telegram-codex-bot"
    if cli_in_venv.exists():
        return str(cli_in_venv)

    # Last resort: assume it will be in PATH
    return "telegram-codex-bot"


def _is_telegram_codex_bot_hook_command(command: str) -> bool:
    """Return True if command invokes ``telegram-codex-bot hook``."""
    try:
        parts = shlex.split(command)
    except ValueError:
        return command == _HOOK_COMMAND_SUFFIX or command.endswith(
            "/" + _HOOK_COMMAND_SUFFIX
        )
    if len(parts) < 2 or parts[-1] != "hook":
        return False
    return Path(parts[-2]).name == "telegram-codex-bot"


def _find_installed_hook(settings: dict) -> dict[str, Any] | None:
    """Find the first installed telegram-codex-bot hook command, if any."""
    hooks = settings.get("hooks", {})
    session_start = hooks.get("SessionStart", [])

    for entry in session_start:
        if not isinstance(entry, dict):
            continue
        inner_hooks = entry.get("hooks", [])
        for h in inner_hooks:
            if not isinstance(h, dict):
                continue
            cmd = h.get("command", "")
            if isinstance(cmd, str) and _is_telegram_codex_bot_hook_command(cmd):
                return h
    return None


def _is_hook_installed(settings: dict) -> bool:
    """Check if telegram-codex-bot hook is already installed in hooks.json."""
    return _find_installed_hook(settings) is not None


def _hook_command_has_missing_absolute_executable(command: str) -> bool:
    """Return True when an installed absolute hook path no longer exists."""
    try:
        parts = shlex.split(command)
    except ValueError:
        return False
    if len(parts) < 2 or parts[-1] != "hook":
        return False
    executable = Path(parts[-2]).expanduser()
    return executable.is_absolute() and not executable.exists()


def _read_json_file(path: Path) -> dict[str, Any]:
    """Read a JSON object from disk, returning {} when the file is absent."""
    if not path.exists():
        return {}

    data = json.loads(path.read_text())
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a top-level JSON object")
    return data


def _write_json_file(path: Path, payload: dict[str, Any]) -> None:
    """Write JSON with a trailing newline for easier diffs."""
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")


def _enable_codex_hooks_feature(config_file: Path) -> None:
    """Ensure `[features] hooks = true` exists in config.toml."""
    if config_file.exists():
        text = config_file.read_text()
    else:
        text = ""

    lines = text.splitlines()

    section_starts: list[tuple[int, str]] = []
    for idx, line in enumerate(lines):
        match = _CONFIG_SECTION_RE.match(line)
        if match:
            section_starts.append((idx, match.group(1).strip()))

    features_start = None
    features_end = len(lines)
    for pos, (idx, section_name) in enumerate(section_starts):
        if section_name == "features":
            features_start = idx
            if pos + 1 < len(section_starts):
                features_end = section_starts[pos + 1][0]
            break

    changed = False
    if features_start is None:
        if lines and lines[-1].strip():
            lines.append("")
        lines.extend(["[features]", "hooks = true"])
        changed = True
    else:
        hooks_idx: int | None = None
        legacy_idx: int | None = None
        for idx in range(features_start + 1, features_end):
            if hooks_idx is None and _HOOKS_FLAG_RE.match(lines[idx]):
                hooks_idx = idx
            elif legacy_idx is None and _LEGACY_CODEX_HOOKS_FLAG_RE.match(lines[idx]):
                legacy_idx = idx

        if hooks_idx is not None:
            if lines[hooks_idx].strip() != "hooks = true":
                lines[hooks_idx] = "hooks = true"
                changed = True
            if legacy_idx is not None:
                del lines[legacy_idx]
                changed = True
        elif legacy_idx is not None:
            lines[legacy_idx] = "hooks = true"
            changed = True
        else:
            lines.insert(features_end, "hooks = true")
            changed = True

    if changed or not config_file.exists():
        config_file.parent.mkdir(parents=True, exist_ok=True)
        config_file.write_text("\n".join(lines).rstrip() + "\n")


def _install_hook() -> int:
    """Install the telegram-codex-bot hook into Codex's config.toml and hooks.json.

    Returns 0 on success, 1 on error.
    """
    config_file = _codex_config_file()
    hooks_file = _codex_hooks_file()

    try:
        _enable_codex_hooks_feature(config_file)
    except (OSError, ValueError) as e:
        logger.error("Error updating %s: %s", config_file, e)
        print(f"Error updating {config_file}: {e}", file=sys.stderr)
        return 1

    try:
        settings = _read_json_file(hooks_file)
    except (json.JSONDecodeError, OSError, ValueError) as e:
        logger.error("Error reading %s: %s", hooks_file, e)
        print(f"Error reading {hooks_file}: {e}", file=sys.stderr)
        return 1

    hooks = settings.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        message = f"{hooks_file} has invalid 'hooks' shape"
        logger.error(message)
        print(message, file=sys.stderr)
        return 1

    session_start = hooks.setdefault("SessionStart", [])
    if not isinstance(session_start, list):
        message = f"{hooks_file} has invalid 'hooks.SessionStart' shape"
        logger.error(message)
        print(message, file=sys.stderr)
        return 1

    # Find the full path to telegram-codex-bot
    cli_path = _find_cli_path()
    hook_command = f"{cli_path} hook"

    # Check if already installed. Older installs may point at a deleted venv or
    # checkout; repair that in place so hook --install remains self-healing.
    installed_hook = _find_installed_hook(settings)
    if installed_hook is not None:
        installed_command = str(installed_hook.get("command") or "")
        if _hook_command_has_missing_absolute_executable(installed_command):
            installed_hook["command"] = hook_command
            installed_hook.setdefault("type", "command")
            installed_hook.setdefault("statusMessage", _HOOK_STATUS_MESSAGE)
            installed_hook.setdefault("timeout", _HOOK_TIMEOUT_SECONDS)
            try:
                hooks_file.parent.mkdir(parents=True, exist_ok=True)
                _write_json_file(hooks_file, settings)
            except OSError as e:
                logger.error("Error writing %s: %s", hooks_file, e)
                print(f"Error writing {hooks_file}: {e}", file=sys.stderr)
                return 1
            logger.info(
                "Repaired stale hook command in %s: %s -> %s",
                hooks_file,
                installed_command,
                hook_command,
            )
            print(
                "Hook command repaired in "
                f"{hooks_file} (Codex hooks enabled in {config_file})"
            )
            return 0

        logger.info("Hook already installed in %s", hooks_file)
        print(
            f"Hook already installed in {hooks_file} (Codex hooks enabled in {config_file})"
        )
        return 0

    hook_config = {
        "type": "command",
        "command": hook_command,
        "statusMessage": _HOOK_STATUS_MESSAGE,
        "timeout": _HOOK_TIMEOUT_SECONDS,
    }
    logger.info("Installing hook command: %s", hook_command)

    session_start.append(
        {
            "matcher": _SESSION_START_MATCHER,
            "hooks": [hook_config],
        }
    )

    # Write back
    try:
        hooks_file.parent.mkdir(parents=True, exist_ok=True)
        _write_json_file(hooks_file, settings)
    except OSError as e:
        logger.error("Error writing %s: %s", hooks_file, e)
        print(f"Error writing {hooks_file}: {e}", file=sys.stderr)
        return 1

    logger.info("Hook installed successfully in %s", hooks_file)
    print(
        "Hook installed successfully in "
        f"{hooks_file} (Codex hooks enabled in {config_file})"
    )
    return 0


def hook_main() -> None:
    """Process a Codex hook event from stdin, or install the hook."""
    # Configure logging for the hook subprocess (main.py logging doesn't apply here)
    logging.basicConfig(
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        level=logging.DEBUG,
        stream=sys.stderr,
    )

    parser = argparse.ArgumentParser(
        prog="telegram-codex-bot hook",
        description="Codex session tracking hook",
    )
    parser.add_argument(
        "--install",
        action="store_true",
        help="Enable Codex hooks and install the SessionStart hook in the active Codex home",
    )
    # Parse only known args to avoid conflicts with stdin JSON
    args, _ = parser.parse_known_args(sys.argv[2:])

    if args.install:
        logger.info("Hook install requested")
        sys.exit(_install_hook())

    # Normal hook processing: read JSON from stdin
    logger.debug("Processing hook event from stdin")
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError) as e:
        logger.warning("Failed to parse stdin JSON: %s", e)
        return

    session_id = payload.get("session_id", "")
    cwd = payload.get("cwd", "")
    event = payload.get("hook_event_name", "")

    if not session_id or not event:
        logger.debug("Empty session_id or event, ignoring")
        return

    if _is_non_interactive_session(payload):
        logger.debug(
            "Ignoring non-interactive Codex session: session_id=%s, source=%s, originator=%s",
            session_id,
            payload.get("source"),
            payload.get("originator"),
        )
        return

    # Validate session_id format (Codex rollout ids or legacy UUIDs)
    if not (_UUID_RE.match(session_id) or _ROLLOUT_SESSION_RE.match(session_id)):
        logger.warning("Invalid session_id format: %s", session_id)
        return

    # Validate cwd is an absolute path (if provided)
    if cwd and not os.path.isabs(cwd):
        logger.warning("cwd is not absolute: %s", cwd)
        return

    if event != "SessionStart":
        logger.debug("Ignoring non-SessionStart event: %s", event)
        return

    # Get tmux session:window key for the pane running this hook.
    # TMUX_PANE is set by tmux for every process inside a pane.
    pane_id = os.environ.get("TMUX_PANE", "")
    if not pane_id:
        logger.warning("TMUX_PANE not set, cannot determine window")
        return

    result = subprocess.run(
        [
            "tmux",
            "display-message",
            "-t",
            pane_id,
            "-p",
            "#{session_name}:#{window_id}:#{window_name}",
        ],
        capture_output=True,
        text=True,
    )
    raw_output = result.stdout.strip()
    # Expected format: "session_name:@id:window_name"
    parts = raw_output.split(":", 2)
    if len(parts) < 3:
        logger.warning(
            "Failed to parse session:window_id:window_name from tmux (pane=%s, output=%s)",
            pane_id,
            raw_output,
        )
        return
    tmux_session_name, window_id, window_name = parts
    # Key uses window_id for uniqueness
    session_window_key = f"{tmux_session_name}:{window_id}"

    logger.debug(
        "tmux key=%s, window_name=%s, session_id=%s, cwd=%s",
        session_window_key,
        window_name,
        session_id,
        cwd,
    )

    # Read-modify-write with file locking to prevent concurrent hook races
    from .utils import app_dir

    map_file = app_dir() / "session_map.json"
    map_file.parent.mkdir(parents=True, exist_ok=True)

    lock_path = map_file.with_suffix(".lock")
    try:
        with open(lock_path, "w") as lock_f:
            fcntl.flock(lock_f, fcntl.LOCK_EX)
            logger.debug("Acquired lock on %s", lock_path)
            try:
                session_map: dict[str, dict[str, str]] = {}
                if map_file.exists():
                    try:
                        session_map = json.loads(map_file.read_text())
                    except (json.JSONDecodeError, OSError):
                        logger.warning(
                            "Failed to read existing session_map, starting fresh"
                        )

                session_map[session_window_key] = {
                    "session_id": session_id,
                    "cwd": cwd,
                    "window_name": window_name,
                }

                # Clean up old-format key ("session:window_name") if it exists.
                # Previous versions keyed by window_name instead of window_id.
                old_key = f"{tmux_session_name}:{window_name}"
                if old_key != session_window_key and old_key in session_map:
                    del session_map[old_key]
                    logger.info("Removed old-format session_map key: %s", old_key)

                from .utils import atomic_write_json

                atomic_write_json(map_file, session_map)
                logger.info(
                    "Updated session_map: %s -> session_id=%s, cwd=%s",
                    session_window_key,
                    session_id,
                    cwd,
                )
            finally:
                fcntl.flock(lock_f, fcntl.LOCK_UN)
    except OSError as e:
        logger.error("Failed to write session_map: %s", e)
