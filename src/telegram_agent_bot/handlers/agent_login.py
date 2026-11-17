"""Agent device-login and saved-account management handlers."""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shlex
import shutil
from typing import Any

from telegram import Bot, Update
from telegram.ext import ContextTypes

from ..account_manager import (
    clear_current_account,
    get_current_account_name,
    is_valid_account_name,
    list_account_names,
    prepare_account_home,
    remember_current_account,
    save_account_snapshot,
)
from ..agent_profile import (
    AGENT_CLAUDE,
    AGENT_CLAUDE_OFFICIAL,
    AGENT_CODEX,
    AGENT_CURSOR,
    agent_capabilities,
    agent_display_name,
    normalize_agent_type,
)
from ..config import config
from .message_sender import safe_reply, safe_send

logger = logging.getLogger(__name__)

ACCOUNT_COMMAND_DESCRIPTION = "Manage agent login accounts"
AGENT_LOGIN_COMMAND_DESCRIPTION = "Start agent device login"
AGENT_LOGIN_TIMEOUT_SECONDS = 16 * 60
AGENT_AUTH_RECOVERY_MESSAGE = (
    "Agent login expired or was revoked. Use /agentlogin to sign in again, "
    "then send your message again."
)

_AGENT_LOGIN_DEFAULT_KEY = "__default__"
_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_DEVICE_LOGIN_URL_RE = re.compile(r"https?://\S+")
_DEVICE_LOGIN_CODE_RE = re.compile(r"\b[A-Z0-9]{4}-[A-Z0-9]{4,}\b")
_agent_login_tasks: dict[str, asyncio.Task[None]] = {}


def _dep(runtime: Any, name: str, default: Any) -> Any:
    return getattr(runtime, name, default) if runtime is not None else default


def _default_thread_id(update: Update) -> int | None:
    msg = update.message or (
        update.callback_query.message if update.callback_query else None
    )
    if msg is None:
        return None
    tid = getattr(msg, "message_thread_id", None)
    if tid is None or tid == 1:
        return None
    return tid


def _default_is_user_allowed(user_id: int | None, cfg: Any) -> bool:
    return user_id is not None and cfg.is_user_allowed(user_id)


def requested_agent_type(agent_type: str | None = None, *, runtime: Any = None) -> str:
    """Resolve explicit or configured agent type."""
    cfg = _dep(runtime, "config", config)
    return normalize_agent_type(agent_type, cfg.agent_type)


def agent_command_name(agent_type: str, action: str) -> str:
    """Return the Telegram slash command name for one agent action."""
    prefix = {
        AGENT_CLAUDE: "claude",
        AGENT_CLAUDE_OFFICIAL: "claude",
        AGENT_CURSOR: "cursor",
    }.get(agent_type, "codex")
    return f"/{prefix}{action}"


def agent_login_executable(
    agent_type: str | None = None, *, runtime: Any = None
) -> str:
    """Return the agent executable to use for device login.

    Supports Codex (``codex login``), Claude Code (``claude auth login``),
    and Cursor Agent (``agent login``).
    """
    cfg = _dep(runtime, "config", config)
    selected_agent = requested_agent_type(agent_type, runtime=runtime)
    if agent_type is None:
        command = cfg.codex_command
    elif selected_agent in {AGENT_CLAUDE, AGENT_CLAUDE_OFFICIAL}:
        command = cfg.claude_command
    elif selected_agent == AGENT_CURSOR:
        command = cfg.cursor_command
    else:
        command = getattr(cfg, "codex_cli_command", cfg.codex_command)
    try:
        command_parts = shlex.split(command)
    except ValueError:
        command_parts = []

    which_fn = _dep(runtime, "shutil", shutil).which
    for part in command_parts:
        name, sep, _value = part.partition("=")
        if sep and name.isidentifier():
            continue
        return which_fn(part) or part
    default = {
        AGENT_CLAUDE: "claude",
        AGENT_CLAUDE_OFFICIAL: "claude",
        AGENT_CURSOR: "agent",
    }.get(selected_agent, "codex")
    return which_fn(default) or default


def agent_login_args(
    agent_type: str | None = None, *, runtime: Any = None
) -> list[str]:
    """Return the argv used to start an interactive agent login."""
    selected_agent = requested_agent_type(agent_type, runtime=runtime)
    executable = _dep(
        runtime,
        "_agent_login_executable",
        lambda agent=None: agent_login_executable(agent, runtime=runtime),
    )(agent_type)
    if selected_agent in {AGENT_CLAUDE, AGENT_CLAUDE_OFFICIAL}:
        return [executable, "auth", "login"]
    if selected_agent == AGENT_CURSOR:
        return [executable, "login"]
    return [executable, "login", "--device-auth"]


def extract_device_login_details(output: str) -> tuple[str | None, str | None]:
    """Extract the browser URL and one-time device code from Codex login output."""
    clean = _ANSI_ESCAPE_RE.sub("", output)
    url_match = _DEVICE_LOGIN_URL_RE.search(clean)
    code_match = _DEVICE_LOGIN_CODE_RE.search(clean)
    return (
        url_match.group(0).rstrip(".,)") if url_match else None,
        code_match.group(0) if code_match else None,
    )


def login_display_name(
    account_name: str | None,
    agent_type: str | None = None,
    *,
    runtime: Any = None,
) -> str:
    """Return a Telegram-safe label for the target login account."""
    agent_label = agent_display_name(requested_agent_type(agent_type, runtime=runtime))
    if account_name:
        return f"account `{account_name}`"
    return f"the service user's default {agent_label} account"


def codex_auth_recovery_message(_pane_error: str | None = None) -> str:
    """Return the Telegram-facing recovery instruction for Codex auth failures."""
    return AGENT_AUTH_RECOVERY_MESSAGE


def account_command_usage(agent_type: str | None = None, *, runtime: Any = None) -> str:
    """Build usage help text for account management commands."""
    selected_agent = requested_agent_type(agent_type, runtime=runtime)
    account_command = agent_command_name(selected_agent, "account")
    login_command = agent_command_name(selected_agent, "login")
    return (
        "Usage:\n"
        f"{account_command} list\n"
        f"{account_command} use <name>\n"
        f"{account_command} clear\n"
        f"{account_command} save <name>\n"
        f"{login_command} [name] — login to {agent_display_name(selected_agent)}"
    )


def format_account_status(agent_type: str | None = None, *, runtime: Any = None) -> str:
    """Format current and saved accounts status for one agent type."""
    cfg = _dep(runtime, "config", config)
    list_accounts = _dep(runtime, "list_account_names", list_account_names)
    get_current = _dep(runtime, "get_current_account_name", get_current_account_name)

    names = list_accounts(agent_type)
    current = get_current(agent_type)
    rotation = "enabled" if cfg.enable_account_rotation else "disabled"
    selected_agent = requested_agent_type(agent_type, runtime=runtime)
    agent_label = agent_display_name(selected_agent)
    env_var = (
        "HOME" if agent_capabilities(selected_agent).uses_claude_home else "CODEX_HOME"
    )
    login_command = agent_command_name(selected_agent, "login")
    lines = [
        f"🔐 {agent_label} account status",
        f"Automatic quota rotation: {rotation}",
        (
            f"New sessions: saved account `{current}`"
            if current
            else f"New sessions: service user's default {env_var}"
        ),
    ]
    if names:
        lines.append("Saved accounts:")
        for name in names:
            suffix = " (selected)" if name == current else ""
            lines.append(f"- `{name}`{suffix}")
    else:
        lines.append("Saved accounts: none")
    lines.append(f"Use {login_command} [name] to refresh login from Telegram.")
    return "\n".join(lines)


async def wait_for_agent_login_details(
    process: asyncio.subprocess.Process,
    *,
    require_code: bool = True,
) -> tuple[str | None, str | None]:
    """Read login output until its required browser details appear."""
    if process.stdout is None:
        return None, None

    output = ""
    deadline = asyncio.get_running_loop().time() + 30
    while asyncio.get_running_loop().time() < deadline:
        timeout = max(0.1, deadline - asyncio.get_running_loop().time())
        try:
            line = await asyncio.wait_for(process.stdout.readline(), timeout=timeout)
        except TimeoutError:
            break
        if not line:
            break
        output += line.decode("utf-8", errors="replace")
        login_url, login_code = extract_device_login_details(output)
        if login_url and (login_code or not require_code):
            return login_url, login_code
    return extract_device_login_details(output)


async def agent_login_worker(
    *,
    bot: Bot,
    chat_id: int,
    thread_id: int | None,
    account_name: str | None,
    login_key: str,
    agent_type: str,
    runtime: Any = None,
) -> None:
    """Run agent login and report status back to Telegram.

    For Codex: runs ``codex login --device-auth``.
    For Claude Code: runs ``claude auth login``.
    For Cursor Agent: runs ``agent login`` and sends its browser URL to Telegram.
    """
    send_fn = _dep(runtime, "safe_send", safe_send)
    prepare_home_fn = _dep(runtime, "prepare_account_home", prepare_account_home)
    save_snapshot_fn = _dep(runtime, "save_account_snapshot", save_account_snapshot)
    remember_account_fn = _dep(
        runtime, "remember_current_account", remember_current_account
    )
    login_args_fn = _dep(
        runtime,
        "_agent_login_args",
        lambda agent=None: agent_login_args(agent, runtime=runtime),
    )
    wait_details_fn = _dep(
        runtime, "_wait_for_agent_login_details", wait_for_agent_login_details
    )
    login_tasks = _dep(runtime, "_agent_login_tasks", _agent_login_tasks)
    timeout_seconds = _dep(
        runtime, "AGENT_LOGIN_TIMEOUT_SECONDS", AGENT_LOGIN_TIMEOUT_SECONDS
    )

    account_home = None
    process: asyncio.subprocess.Process | None = None
    try:
        env = os.environ.copy()
        if agent_type == AGENT_CURSOR:
            env["NO_OPEN_BROWSER"] = "1"
        if account_name:
            account_home = prepare_home_fn(account_name, agent_type)
            if agent_type in {AGENT_CLAUDE, AGENT_CLAUDE_OFFICIAL}:
                env["HOME"] = str(account_home)
            else:
                env["CODEX_HOME"] = str(account_home)

        login_args = login_args_fn(agent_type)
        process = await asyncio.create_subprocess_exec(
            *login_args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=env,
        )

        require_code = agent_type != AGENT_CURSOR
        login_url, login_code = await wait_details_fn(
            process,
            require_code=require_code,
        )
        if not login_url or (require_code and not login_code):
            if process.returncode is None:
                process.terminate()
            await send_fn(
                bot,
                chat_id,
                "❌ Agent login did not print the required browser details. "
                "Please check the service logs or try again.",
                message_thread_id=thread_id,
            )
            return

        login_details = f"Open: {login_url}\n"
        if login_code:
            login_details += f"Code: `{login_code}`\n"
        await send_fn(
            bot,
            chat_id,
            "🔐 Agent login started for "
            f"{login_display_name(account_name, agent_type, runtime=runtime)}.\n"
            f"{login_details}"
            "Expires in about 15 minutes. Only complete this login if you requested it.",
            message_thread_id=thread_id,
        )

        try:
            return_code = await asyncio.wait_for(
                process.wait(), timeout=timeout_seconds
            )
        except TimeoutError:
            process.terminate()
            await send_fn(
                bot,
                chat_id,
                f"❌ Agent login timed out. Run {agent_command_name(agent_type, 'login')} again when ready.",
                message_thread_id=thread_id,
            )
            return

        if return_code != 0:
            await send_fn(
                bot,
                chat_id,
                f"❌ Agent login failed or was cancelled. Run {agent_command_name(agent_type, 'login')} again if needed.",
                message_thread_id=thread_id,
            )
            return

        if account_name and account_home is not None:
            save_snapshot_fn(account_name, account_home, agent_type)
            remember_account_fn(account_name, agent_type)
            await send_fn(
                bot,
                chat_id,
                f"✅ Agent login completed for account `{account_name}` and saved.\n"
                "New topics will use this account. Existing topics will be "
                "recreated automatically if their current pane is still blocked "
                "by the old login.",
                message_thread_id=thread_id,
            )
        else:
            await send_fn(
                bot,
                chat_id,
                "✅ Agent login completed. Send your message again. Existing topics "
                "will be recreated automatically if their current pane is still "
                "blocked by the old login.",
                message_thread_id=thread_id,
            )
    except Exception as exc:
        logger.exception("Agent login failed")
        await send_fn(
            bot,
            chat_id,
            f"❌ Agent login failed: {exc}",
            message_thread_id=thread_id,
        )
    finally:
        login_tasks.pop(login_key, None)


async def run_agent_login_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    agent_type: str | None = None,
    *,
    runtime: Any = None,
) -> None:
    """Start login for the configured or explicitly selected agent."""
    cfg = _dep(runtime, "config", config)
    allowed_fn = _dep(
        runtime,
        "is_user_allowed",
        lambda uid: _default_is_user_allowed(uid, cfg),
    )
    reply_fn = _dep(runtime, "safe_reply", safe_reply)
    get_tid = _dep(runtime, "_get_thread_id", _default_thread_id)
    valid_name_fn = _dep(runtime, "is_valid_account_name", is_valid_account_name)
    login_tasks = _dep(runtime, "_agent_login_tasks", _agent_login_tasks)

    user = update.effective_user
    if not user or not allowed_fn(user.id):
        return
    if not update.message or not update.effective_chat:
        return

    selected_agent = requested_agent_type(agent_type, runtime=runtime)
    args = context.args or []
    if selected_agent == AGENT_CURSOR:
        if args:
            await reply_fn(update.message, "Usage: /cursorlogin")
            return
        await reply_fn(
            update.message,
            "⏳ Starting Cursor Agent browser login. I will send the login URL here.",
        )
    if len(args) > 1:
        await reply_fn(
            update.message,
            f"Usage: {agent_command_name(selected_agent, 'login')} [account-name]",
        )
        return

    account_name = args[0].strip() if args else None
    if account_name and not valid_name_fn(account_name):
        await reply_fn(
            update.message,
            "❌ Invalid account name. Use letters, numbers, dot, underscore, or dash.",
        )
        return

    login_key = f"{selected_agent}:{account_name or _AGENT_LOGIN_DEFAULT_KEY}"
    existing = login_tasks.get(login_key)
    if existing and not existing.done():
        await reply_fn(
            update.message,
            f"⏳ {agent_display_name(selected_agent)} login is already running for "
            f"{login_display_name(account_name, selected_agent, runtime=runtime)}.",
        )
        return

    async def _run_worker() -> None:
        custom_worker = (
            getattr(runtime, "_agent_login_worker", None)
            if runtime is not None
            else None
        )
        if custom_worker is not None and not getattr(
            custom_worker, "_is_bot_wrapper", False
        ):
            await custom_worker(
                bot=context.bot,
                chat_id=update.effective_chat.id,  # type: ignore[union-attr]
                thread_id=get_tid(update),
                account_name=account_name,
                login_key=login_key,
                agent_type=selected_agent,
            )
            return
        await agent_login_worker(
            bot=context.bot,
            chat_id=update.effective_chat.id,  # type: ignore[union-attr]
            thread_id=get_tid(update),
            account_name=account_name,
            login_key=login_key,
            agent_type=selected_agent,
            runtime=runtime,
        )

    task = asyncio.create_task(_run_worker())
    login_tasks[login_key] = task
    await reply_fn(
        update.message,
        f"⏳ Starting {agent_display_name(selected_agent)} login for "
        f"{login_display_name(account_name, selected_agent, runtime=runtime)}...",
    )


async def run_agent_account_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    agent_type: str | None = None,
    *,
    runtime: Any = None,
) -> None:
    """Manage saved account snapshots for one agent."""
    cfg = _dep(runtime, "config", config)
    allowed_fn = _dep(
        runtime,
        "is_user_allowed",
        lambda uid: _default_is_user_allowed(uid, cfg),
    )
    reply_fn = _dep(runtime, "safe_reply", safe_reply)
    clear_account_fn = _dep(runtime, "clear_current_account", clear_current_account)
    valid_name_fn = _dep(runtime, "is_valid_account_name", is_valid_account_name)
    save_snapshot_fn = _dep(runtime, "save_account_snapshot", save_account_snapshot)
    list_accounts_fn = _dep(runtime, "list_account_names", list_account_names)
    remember_account_fn = _dep(
        runtime, "remember_current_account", remember_current_account
    )

    user = update.effective_user
    if not user or not allowed_fn(user.id):
        return
    if not update.message:
        return

    selected_agent = requested_agent_type(agent_type, runtime=runtime)
    if selected_agent == AGENT_CURSOR:
        await reply_fn(
            update.message,
            "Cursor Agent account snapshots are not supported. Use /cursorlogin "
            "to authenticate the service user's Cursor CLI account.",
        )
        return
    args = context.args or []
    if not args or args[0].lower() == "list":
        await reply_fn(
            update.message,
            format_account_status(selected_agent, runtime=runtime),
        )
        return

    action = args[0].lower()
    if action == "clear":
        clear_account_fn(selected_agent)
        await reply_fn(
            update.message,
            f"✅ New {agent_display_name(selected_agent)} sessions will use the "
            f"service user's default {'HOME' if agent_capabilities(selected_agent).uses_claude_home else 'CODEX_HOME'}. "
            "Existing topics keep their current window; use /unbind to start fresh.",
        )
        return

    if action not in {"save", "use", "switch"} or len(args) != 2:
        await reply_fn(
            update.message,
            account_command_usage(selected_agent, runtime=runtime),
        )
        return

    account_name = args[1].strip()
    if not valid_name_fn(account_name):
        await reply_fn(
            update.message,
            "❌ Invalid account name. Use letters, numbers, dot, underscore, or dash.",
        )
        return

    if action == "save":
        try:
            save_snapshot_fn(account_name, agent_type=selected_agent)
        except FileNotFoundError:
            await reply_fn(
                update.message,
                f"❌ {agent_display_name(selected_agent)} auth data was not found. "
                f"Use {agent_command_name(selected_agent, 'login')} first.",
            )
            return
        await reply_fn(
            update.message,
            f"✅ Saved current {agent_display_name(selected_agent)} login as account "
            f"`{account_name}`. Use {agent_command_name(selected_agent, 'account')} "
            f"use {account_name} to select it for new sessions.",
        )
        return

    names = list_accounts_fn(selected_agent)
    if account_name not in names:
        await reply_fn(
            update.message,
            f"❌ Account `{account_name}` is not saved yet. Use "
            f"{agent_command_name(selected_agent, 'login')} {account_name} first.",
        )
        return

    remember_account_fn(account_name, selected_agent)
    await reply_fn(
        update.message,
        f"✅ New sessions will use saved account `{account_name}`. Existing topics "
        "keep their current window; use /unbind if you want this topic to start fresh.",
    )


async def agent_login_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    await run_agent_login_command(update, context)


async def codex_login_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    await run_agent_login_command(update, context, AGENT_CODEX)


async def claude_login_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    await run_agent_login_command(update, context, AGENT_CLAUDE)


async def cursor_login_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    await run_agent_login_command(update, context, AGENT_CURSOR)


async def agent_account_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    await run_agent_account_command(update, context)


async def codex_account_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    await run_agent_account_command(update, context, AGENT_CODEX)


async def claude_account_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    await run_agent_account_command(update, context, AGENT_CLAUDE)
