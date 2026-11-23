"""Extracted bot runtime helpers (ADR 0001 Phase D)."""

from __future__ import annotations

from telegram_agent_bot._bot_shared import (
    AGENT_AUTH_RECOVERY_MESSAGE,
    AGENT_CLAUDE,
    AGENT_CLAUDE_OFFICIAL,
    AGENT_CODEX,
    AGENT_CURSOR,
    AGENT_LOGIN_TIMEOUT_SECONDS,
    Bot,
    ContextTypes,
    Update,
    _AGENT_LOGIN_DEFAULT_KEY,
    _ANSI_ESCAPE_RE,
    _DEVICE_LOGIN_CODE_RE,
    _DEVICE_LOGIN_URL_RE,
    _agent_login_tasks,
    _b,
    agent_capabilities,
    agent_display_name,
    asyncio,
    clear_current_account,
    config,
    get_current_account_name,
    is_valid_account_name,
    list_account_names,
    logger,
    normalize_agent_type,
    os,
    prepare_account_home,
    remember_current_account,
    safe_reply,
    safe_send,
    save_account_snapshot,
    shlex,
    shutil,
)
from telegram_agent_bot._bot_helpers import (
    _get_thread_id,
    is_user_allowed,
)


def _requested_agent_type(agent_type: str | None = None) -> str:
    return _b("normalize_agent_type", normalize_agent_type)(
        agent_type, _b("config", config).agent_type
    )


def _agent_command_name(agent_type: str, action: str) -> str:
    prefix = {
        _b("AGENT_CLAUDE", AGENT_CLAUDE): "claude",
        _b("AGENT_CLAUDE_OFFICIAL", AGENT_CLAUDE_OFFICIAL): "claude",
        _b("AGENT_CURSOR", AGENT_CURSOR): "cursor",
    }.get(agent_type, "codex")
    return f"/{prefix}{action}"


def _agent_login_executable(agent_type: str | None = None) -> str:
    """Return the agent executable to use for device login.

    Supports Codex (``codex login``), Claude Code (``claude auth login``),
    and Cursor Agent (``agent login``).
    """
    selected_agent = _b("_requested_agent_type", _requested_agent_type)(agent_type)
    if agent_type is None:
        command = _b("config", config).codex_command
    elif selected_agent in {
        _b("AGENT_CLAUDE", AGENT_CLAUDE),
        _b("AGENT_CLAUDE_OFFICIAL", AGENT_CLAUDE_OFFICIAL),
    }:
        command = _b("config", config).claude_command
    elif selected_agent == _b("AGENT_CURSOR", AGENT_CURSOR):
        command = _b("config", config).cursor_command
    else:
        command = getattr(
            _b("config", config),
            "codex_cli_command",
            _b("config", config).codex_command,
        )
    try:
        command_parts = _b("shlex", shlex).split(command)
    except ValueError:
        command_parts = []

    for part in command_parts:
        name, sep, _value = part.partition("=")
        if sep and name.isidentifier():
            continue
        return _b("shutil", shutil).which(part) or part
    default = {
        _b("AGENT_CLAUDE", AGENT_CLAUDE): "claude",
        _b("AGENT_CLAUDE_OFFICIAL", AGENT_CLAUDE_OFFICIAL): "claude",
        _b("AGENT_CURSOR", AGENT_CURSOR): "agent",
    }.get(selected_agent, "codex")
    return _b("shutil", shutil).which(default) or default


def _agent_login_args(agent_type: str | None = None) -> list[str]:
    """Return the argv used to start an interactive agent login."""
    selected_agent = _b("_requested_agent_type", _requested_agent_type)(agent_type)
    executable = _b("_agent_login_executable", _agent_login_executable)(agent_type)
    if selected_agent in {
        _b("AGENT_CLAUDE", AGENT_CLAUDE),
        _b("AGENT_CLAUDE_OFFICIAL", AGENT_CLAUDE_OFFICIAL),
    }:
        return [executable, "auth", "login"]
    if selected_agent == _b("AGENT_CURSOR", AGENT_CURSOR):
        return [executable, "login"]
    return [executable, "login", "--device-auth"]


def _extract_device_login_details(output: str) -> tuple[str | None, str | None]:
    """Extract the browser URL and one-time device code from Codex login output."""
    clean = _b("_ANSI_ESCAPE_RE", _ANSI_ESCAPE_RE).sub("", output)
    url_match = _b("_DEVICE_LOGIN_URL_RE", _DEVICE_LOGIN_URL_RE).search(clean)
    code_match = _b("_DEVICE_LOGIN_CODE_RE", _DEVICE_LOGIN_CODE_RE).search(clean)
    return (
        url_match.group(0).rstrip(".,)") if url_match else None,
        code_match.group(0) if code_match else None,
    )


def _login_display_name(account_name: str | None, agent_type: str | None = None) -> str:
    agent_label = _b("agent_display_name", agent_display_name)(
        _b("_requested_agent_type", _requested_agent_type)(agent_type)
    )
    if account_name:
        return f"account `{account_name}`"
    return f"the service user's default {agent_label} account"


def _codex_auth_recovery_message(_pane_error: str | None = None) -> str:
    """Return the Telegram-facing recovery instruction for Codex auth failures."""
    return _b("AGENT_AUTH_RECOVERY_MESSAGE", AGENT_AUTH_RECOVERY_MESSAGE)


def _account_command_usage(agent_type: str | None = None) -> str:
    selected_agent = _b("_requested_agent_type", _requested_agent_type)(agent_type)
    account_command = _b("_agent_command_name", _agent_command_name)(
        selected_agent, "account"
    )
    login_command = _b("_agent_command_name", _agent_command_name)(
        selected_agent, "login"
    )
    return (
        "Usage:\n"
        f"{account_command} list\n"
        f"{account_command} use <name>\n"
        f"{account_command} clear\n"
        f"{account_command} save <name>\n"
        f"{login_command} [name] — login to {_b('agent_display_name', agent_display_name)(selected_agent)}"
    )


def _format_account_status(agent_type: str | None = None) -> str:
    names = _b("list_account_names", list_account_names)(agent_type)
    current = _b("get_current_account_name", get_current_account_name)(agent_type)
    rotation = "enabled" if _b("config", config).enable_account_rotation else "disabled"
    selected_agent = _b("_requested_agent_type", _requested_agent_type)(agent_type)
    agent_label = _b("agent_display_name", agent_display_name)(selected_agent)
    env_var = (
        "HOME"
        if _b("agent_capabilities", agent_capabilities)(selected_agent).uses_claude_home
        else "CODEX_HOME"
    )
    login_command = _b("_agent_command_name", _agent_command_name)(
        selected_agent, "login"
    )
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


async def _wait_for_agent_login_details(
    process: asyncio.subprocess.Process,
    *,
    require_code: bool = True,
) -> tuple[str | None, str | None]:
    """Read login output until its required browser details appear."""
    if process.stdout is None:
        return None, None

    output = ""
    deadline = _b("asyncio", asyncio).get_running_loop().time() + 30
    while _b("asyncio", asyncio).get_running_loop().time() < deadline:
        timeout = max(0.1, deadline - _b("asyncio", asyncio).get_running_loop().time())
        try:
            line = await _b("asyncio", asyncio).wait_for(
                process.stdout.readline(), timeout=timeout
            )
        except TimeoutError:
            break
        if not line:
            break
        output += line.decode("utf-8", errors="replace")
        login_url, login_code = _b(
            "_extract_device_login_details", _extract_device_login_details
        )(output)
        if login_url and (login_code or not require_code):
            return login_url, login_code
    return _b("_extract_device_login_details", _extract_device_login_details)(output)


async def _agent_login_worker(
    *,
    bot: Bot,
    chat_id: int,
    thread_id: int | None,
    account_name: str | None,
    login_key: str,
    agent_type: str,
) -> None:
    """Run agent login and report status back to Telegram.

    For Codex: runs ``codex login --device-auth``.
    For Claude Code: runs ``claude auth login``.
    For Cursor Agent: runs ``agent login`` and sends its browser URL to Telegram.
    """
    account_home = None
    process: asyncio.subprocess.Process | None = None
    try:
        env = _b("os", os).environ.copy()
        if agent_type == _b("AGENT_CURSOR", AGENT_CURSOR):
            env["NO_OPEN_BROWSER"] = "1"
        if account_name:
            account_home = _b("prepare_account_home", prepare_account_home)(
                account_name, agent_type
            )
            if agent_type in {
                _b("AGENT_CLAUDE", AGENT_CLAUDE),
                _b("AGENT_CLAUDE_OFFICIAL", AGENT_CLAUDE_OFFICIAL),
            }:
                env["HOME"] = str(account_home)
            else:
                env["CODEX_HOME"] = str(account_home)

        login_args = _b("_agent_login_args", _agent_login_args)(agent_type)

        process = await _b("asyncio", asyncio).create_subprocess_exec(
            *login_args,
            stdout=_b("asyncio", asyncio).subprocess.PIPE,
            stderr=_b("asyncio", asyncio).subprocess.STDOUT,
            env=env,
        )

        require_code = agent_type != _b("AGENT_CURSOR", AGENT_CURSOR)
        login_url, login_code = await _b(
            "_wait_for_agent_login_details", _wait_for_agent_login_details
        )(
            process,
            require_code=require_code,
        )
        if not login_url or (require_code and not login_code):
            if process.returncode is None:
                process.terminate()
            await _b("safe_send", safe_send)(
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
        await _b("safe_send", safe_send)(
            bot,
            chat_id,
            "🔐 Agent login started for "
            f"{_b('_login_display_name', _login_display_name)(account_name, agent_type)}.\n"
            f"{login_details}"
            "Expires in about 15 minutes. Only complete this login if you requested it.",
            message_thread_id=thread_id,
        )

        try:
            return_code = await _b("asyncio", asyncio).wait_for(
                process.wait(),
                timeout=_b("AGENT_LOGIN_TIMEOUT_SECONDS", AGENT_LOGIN_TIMEOUT_SECONDS),
            )
        except TimeoutError:
            process.terminate()
            await _b("safe_send", safe_send)(
                bot,
                chat_id,
                f"❌ Agent login timed out. Run {_b('_agent_command_name', _agent_command_name)(agent_type, 'login')} again when ready.",
                message_thread_id=thread_id,
            )
            return

        if return_code != 0:
            await _b("safe_send", safe_send)(
                bot,
                chat_id,
                f"❌ Agent login failed or was cancelled. Run {_b('_agent_command_name', _agent_command_name)(agent_type, 'login')} again if needed.",
                message_thread_id=thread_id,
            )
            return

        if account_name and account_home is not None:
            _b("save_account_snapshot", save_account_snapshot)(
                account_name, account_home, agent_type
            )
            _b("remember_current_account", remember_current_account)(
                account_name, agent_type
            )
            await _b("safe_send", safe_send)(
                bot,
                chat_id,
                f"✅ Agent login completed for account `{account_name}` and saved.\n"
                "New topics will use this account. Existing topics will be "
                "recreated automatically if their current pane is still blocked "
                "by the old login.",
                message_thread_id=thread_id,
            )
        else:
            await _b("safe_send", safe_send)(
                bot,
                chat_id,
                "✅ Agent login completed. Send your message again. Existing topics "
                "will be recreated automatically if their current pane is still "
                "blocked by the old login.",
                message_thread_id=thread_id,
            )
    except Exception as exc:
        _b("logger", logger).exception("Agent login failed")
        await _b("safe_send", safe_send)(
            bot,
            chat_id,
            f"❌ Agent login failed: {exc}",
            message_thread_id=thread_id,
        )
    finally:
        _b("_agent_login_tasks", _agent_login_tasks).pop(login_key, None)


async def _agent_login_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE, agent_type: str | None = None
) -> None:
    """Start login for the configured or explicitly selected agent."""
    user = update.effective_user
    if not user or not _b("is_user_allowed", is_user_allowed)(user.id):
        return
    if not update.message or not update.effective_chat:
        return

    selected_agent = _b("_requested_agent_type", _requested_agent_type)(agent_type)
    args = context.args or []
    if selected_agent == _b("AGENT_CURSOR", AGENT_CURSOR):
        if args:
            await _b("safe_reply", safe_reply)(update.message, "Usage: /cursorlogin")
            return
        await _b("safe_reply", safe_reply)(
            update.message,
            "⏳ Starting Cursor Agent browser login. I will send the login URL here.",
        )
    if len(args) > 1:
        await _b("safe_reply", safe_reply)(
            update.message,
            f"Usage: {_b('_agent_command_name', _agent_command_name)(selected_agent, 'login')} [account-name]",
        )
        return

    account_name = args[0].strip() if args else None
    if account_name and not _b("is_valid_account_name", is_valid_account_name)(
        account_name
    ):
        await _b("safe_reply", safe_reply)(
            update.message,
            "❌ Invalid account name. Use letters, numbers, dot, underscore, or dash.",
        )
        return

    login_key = f"{selected_agent}:{account_name or _b('_AGENT_LOGIN_DEFAULT_KEY', _AGENT_LOGIN_DEFAULT_KEY)}"
    existing = _b("_agent_login_tasks", _agent_login_tasks).get(login_key)
    if existing and not existing.done():
        await _b("safe_reply", safe_reply)(
            update.message,
            f"⏳ {_b('agent_display_name', agent_display_name)(selected_agent)} login is already running for "
            f"{_b('_login_display_name', _login_display_name)(account_name, selected_agent)}.",
        )
        return

    task = _b("asyncio", asyncio).create_task(
        _b("_agent_login_worker", _agent_login_worker)(
            bot=context.bot,
            chat_id=update.effective_chat.id,
            thread_id=_b("_get_thread_id", _get_thread_id)(update),
            account_name=account_name,
            login_key=login_key,
            agent_type=selected_agent,
        )
    )
    _b("_agent_login_tasks", _agent_login_tasks)[login_key] = task
    await _b("safe_reply", safe_reply)(
        update.message,
        f"⏳ Starting {_b('agent_display_name', agent_display_name)(selected_agent)} login for "
        f"{_b('_login_display_name', _login_display_name)(account_name, selected_agent)}...",
    )


async def agent_login_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    await _b("_agent_login_command", _agent_login_command)(update, context)


async def codex_login_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    await _b("_agent_login_command", _agent_login_command)(
        update, context, _b("AGENT_CODEX", AGENT_CODEX)
    )


async def claude_login_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    await _b("_agent_login_command", _agent_login_command)(
        update, context, _b("AGENT_CLAUDE", AGENT_CLAUDE)
    )


async def cursor_login_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    await _b("_agent_login_command", _agent_login_command)(
        update, context, _b("AGENT_CURSOR", AGENT_CURSOR)
    )


async def _agent_account_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE, agent_type: str | None = None
) -> None:
    """Manage saved account snapshots for one agent."""
    user = update.effective_user
    if not user or not _b("is_user_allowed", is_user_allowed)(user.id):
        return
    if not update.message:
        return

    selected_agent = _b("_requested_agent_type", _requested_agent_type)(agent_type)
    if selected_agent == _b("AGENT_CURSOR", AGENT_CURSOR):
        await _b("safe_reply", safe_reply)(
            update.message,
            "Cursor Agent account snapshots are not supported. Use /cursorlogin "
            "to authenticate the service user's Cursor CLI account.",
        )
        return
    args = context.args or []
    if not args or args[0].lower() == "list":
        await _b("safe_reply", safe_reply)(
            update.message,
            _b("_format_account_status", _format_account_status)(selected_agent),
        )
        return

    action = args[0].lower()
    if action == "clear":
        _b("clear_current_account", clear_current_account)(selected_agent)
        await _b("safe_reply", safe_reply)(
            update.message,
            f"✅ New {_b('agent_display_name', agent_display_name)(selected_agent)} sessions will use the "
            f"service user's default {'HOME' if _b('agent_capabilities', agent_capabilities)(selected_agent).uses_claude_home else 'CODEX_HOME'}. "
            "Existing topics keep their current window; use /unbind to start fresh.",
        )
        return

    if action not in {"save", "use", "switch"} or len(args) != 2:
        await _b("safe_reply", safe_reply)(
            update.message,
            _b("_account_command_usage", _account_command_usage)(selected_agent),
        )
        return

    account_name = args[1].strip()
    if not _b("is_valid_account_name", is_valid_account_name)(account_name):
        await _b("safe_reply", safe_reply)(
            update.message,
            "❌ Invalid account name. Use letters, numbers, dot, underscore, or dash.",
        )
        return

    if action == "save":
        try:
            _b("save_account_snapshot", save_account_snapshot)(
                account_name, agent_type=selected_agent
            )
        except FileNotFoundError:
            await _b("safe_reply", safe_reply)(
                update.message,
                f"❌ {_b('agent_display_name', agent_display_name)(selected_agent)} auth data was not found. "
                f"Use {_b('_agent_command_name', _agent_command_name)(selected_agent, 'login')} first.",
            )
            return
        await _b("safe_reply", safe_reply)(
            update.message,
            f"✅ Saved current {_b('agent_display_name', agent_display_name)(selected_agent)} login as account "
            f"`{account_name}`. Use {_b('_agent_command_name', _agent_command_name)(selected_agent, 'account')} "
            f"use {account_name} to select it for new sessions.",
        )
        return

    names = _b("list_account_names", list_account_names)(selected_agent)
    if account_name not in names:
        await _b("safe_reply", safe_reply)(
            update.message,
            f"❌ Account `{account_name}` is not saved yet. Use "
            f"{_b('_agent_command_name', _agent_command_name)(selected_agent, 'login')} {account_name} first.",
        )
        return

    _b("remember_current_account", remember_current_account)(
        account_name, selected_agent
    )
    await _b("safe_reply", safe_reply)(
        update.message,
        f"✅ New sessions will use saved account `{account_name}`. Existing topics "
        "keep their current window; use /unbind if you want this topic to start fresh.",
    )


async def agent_account_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    await _b("_agent_account_command", _agent_account_command)(update, context)


async def codex_account_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    await _b("_agent_account_command", _agent_account_command)(
        update, context, _b("AGENT_CODEX", AGENT_CODEX)
    )


async def claude_account_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    await _b("_agent_account_command", _agent_account_command)(
        update, context, _b("AGENT_CLAUDE", AGENT_CLAUDE)
    )
