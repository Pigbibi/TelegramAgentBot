"""Extracted bot runtime helpers (ADR 0001 Phase D)."""

from __future__ import annotations

from telegram_agent_bot._bot_shared import (
    AgentProfile,
    Any,
    Bot,
    ContextTypes,
    NO_LINK_PREVIEW,
    _b,
    _bash_capture_tasks,
    asyncio,
    capture_agent_output,
    config,
    convert_markdown,
    extract_auth_error_message,
    extract_bash_output,
    extract_interactive_content,
    get_next_account_name,
    idle_session_hibernator,
    is_codex_input_ready,
    logger,
    mark_window_working,
    parse_status_update,
    remember_current_account,
    safe_send,
    send_with_fallback,
    session_manager,
    tmux_manager,
    turn_admission,
)
from telegram_agent_bot._bot_input_routing import (
    _agent_max_active_turns,
    _send_message_to_agent,
)
from telegram_agent_bot._bot_login import (
    _codex_auth_recovery_message,
)
from telegram_agent_bot._bot_queue_drain import (
    _send_or_queue_agent_input,
)


def _call__create_agent_local_window():
    from telegram_agent_bot._bot_media_topics import _create_agent_local_window as _fn

    return _b("_create_agent_local_window", _fn)


def _cancel_bash_capture(user_id: int, thread_id: int) -> None:
    """Cancel any running bash capture for this topic."""
    key = (user_id, thread_id)
    task = _b("_bash_capture_tasks", _bash_capture_tasks).pop(key, None)
    if task and not task.done():
        task.cancel()


async def _capture_bash_output(
    bot: Bot,
    user_id: int,
    thread_id: int,
    window_id: str,
    command: str,
) -> None:
    """Background task: capture ``!`` bash command output from tmux pane.

    Sends the first captured output as a new message, then edits it
    in-place as more output appears.  Stops after 30 s or when cancelled
    (e.g. user sends a new message, which pushes content down).
    """
    try:
        # Wait for the command to start producing output
        await _b("asyncio", asyncio).sleep(2.0)

        chat_id = _b("session_manager", session_manager).resolve_chat_id(
            user_id, thread_id
        )
        msg_id: int | None = None
        last_output: str = ""

        for _ in range(30):
            capture = await _b("capture_agent_output", capture_agent_output)(
                user_id, thread_id, window_id
            )
            if capture is None or capture.missing:
                return
            raw = capture.text
            if raw is None:
                return

            output = _b("extract_bash_output", extract_bash_output)(raw, command)
            if not output:
                await _b("asyncio", asyncio).sleep(1.0)
                continue

            # Skip edit if nothing changed
            if output == last_output:
                await _b("asyncio", asyncio).sleep(1.0)
                continue

            last_output = output

            # Truncate to fit Telegram's 4096-char limit
            if len(output) > 3800:
                output = "… " + output[-3800:]

            if msg_id is None:
                # First capture — send a new message
                sent = await _b("send_with_fallback", send_with_fallback)(
                    bot,
                    chat_id,
                    output,
                    message_thread_id=thread_id,
                )
                if sent:
                    msg_id = sent.message_id
            else:
                # Subsequent captures — edit in place
                try:
                    await bot.edit_message_text(
                        chat_id=chat_id,
                        message_id=msg_id,
                        text=_b("convert_markdown", convert_markdown)(output),
                        parse_mode="MarkdownV2",
                        link_preview_options=_b("NO_LINK_PREVIEW", NO_LINK_PREVIEW),
                    )
                except Exception:
                    try:
                        await bot.edit_message_text(
                            chat_id=chat_id,
                            message_id=msg_id,
                            text=output,
                            link_preview_options=_b("NO_LINK_PREVIEW", NO_LINK_PREVIEW),
                        )
                    except Exception:
                        pass

            await _b("asyncio", asyncio).sleep(1.0)
    except _b("asyncio", asyncio).CancelledError:
        return
    finally:
        _b("_bash_capture_tasks", _bash_capture_tasks).pop((user_id, thread_id), None)


async def _rotate_thread_after_usage_limit(
    *,
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
    thread_id: int,
    current_window_id: str,
    current_window_cwd: str,
    text: str,
) -> bool:
    """Rotate the topic onto a fresh account-backed window after quota exhaustion."""
    if not _b("config", config).enable_account_rotation:
        await _b("safe_send", safe_send)(
            context.bot,
            _b("session_manager", session_manager).resolve_chat_id(user_id, thread_id),
            "⚠️ This session has hit its usage limit. Automatic account rotation "
            "is disabled. Use /agentlogin to refresh the current agent login, "
            "or /agentaccount to choose a saved account. Then /unbind if you "
            "want this topic to start a fresh session.",
            message_thread_id=thread_id,
        )
        return True

    current_state = _b("session_manager", session_manager).get_window_state(
        current_window_id
    )
    next_account = _b("get_next_account_name", get_next_account_name)(
        current_state.account_name
    )
    if not next_account:
        await _b("safe_send", safe_send)(
            context.bot,
            _b("session_manager", session_manager).resolve_chat_id(user_id, thread_id),
            "⚠️ This session has hit its usage limit, but no backup account is "
            "selected for rotation.\n"
            "Use /agentlogin <name> to add a saved account, then "
            "/agentaccount use <name> to select it.",
            message_thread_id=thread_id,
        )
        return True

    selected_path = current_window_cwd or current_state.cwd
    if not selected_path:
        await _b("safe_send", safe_send)(
            context.bot,
            _b("session_manager", session_manager).resolve_chat_id(user_id, thread_id),
            "⚠️ This session has hit its usage limit, but the window working "
            "directory is unavailable, so I cannot open a replacement "
            "session automatically. Please `/unbind` and choose New Session.",
            message_thread_id=thread_id,
        )
        return True

    (
        success,
        message,
        created_wname,
        created_wid,
        created_target,
    ) = await _call__create_agent_local_window()(
        cwd=selected_path,
        account_name=next_account,
    )
    if not success:
        await _b("safe_send", safe_send)(
            context.bot,
            _b("session_manager", session_manager).resolve_chat_id(user_id, thread_id),
            f"⚠️ This session hit its usage limit, and automatic rotation to "
            f"`{next_account}` failed: {message}",
            message_thread_id=thread_id,
        )
        return True

    _b("remember_current_account", remember_current_account)(next_account)
    _b("session_manager", session_manager).prepare_window_launch(
        created_wid,
        cwd=str(selected_path),
        window_name=created_wname,
        account_name=next_account,
    )
    if created_target:
        _b("session_manager", session_manager).bind_thread_target(
            user_id,
            thread_id,
            created_target,
            window_name=created_wname,
        )
    else:
        _b("session_manager", session_manager).bind_thread(
            user_id,
            thread_id,
            created_wid,
            window_name=created_wname,
        )

    resolved_chat = _b("session_manager", session_manager).resolve_chat_id(
        user_id, thread_id
    )

    send_ok, send_msg, queued = await _b(
        "_send_or_queue_to_starting_window", _send_or_queue_to_starting_window
    )(
        context.bot,
        user_id,
        thread_id,
        created_wid,
        text,
        auto_confirm_startup_trust=True,
    )
    if send_ok and not queued:
        await _b("mark_window_working", mark_window_working)(
            context.bot, user_id, created_wid, thread_id
        )
    if send_ok:
        delivery = (
            "queued your message there" if queued else "forwarded your message there"
        )
        await _b("safe_send", safe_send)(
            context.bot,
            resolved_chat,
            f"♻️ This session hit its usage limit, so I switched to a new "
            f"`{next_account}` session and {delivery}.",
            message_thread_id=thread_id,
        )
    else:
        await _b("safe_send", safe_send)(
            context.bot,
            resolved_chat,
            "♻️ This session hit its usage limit, and I switched to a new "
            f"session automatically, but forwarding failed: {send_msg}\n"
            "Please send the message again.",
            message_thread_id=thread_id,
        )
    return True


async def _maybe_confirm_startup_trust_prompt(
    window_id: str,
    pane_text: str,
) -> tuple[bool, str]:
    """Confirm safe startup-only trust prompts before the first forwarded text."""
    content = _b("extract_interactive_content", extract_interactive_content)(pane_text)
    if content is None:
        return False, ""
    if content.name != "DirectoryTrust":
        return False, f"Agent is waiting for interactive input: {content.name}"
    _b("logger", logger).info(
        "Auto-confirming Codex directory trust prompt in window %s", window_id
    )
    if await _b("tmux_manager", tmux_manager).send_control_key(window_id, "Enter"):
        return True, "Confirmed Codex directory trust prompt"
    return False, "Failed to confirm Codex directory trust prompt"


async def _handle_startup_interactive_prompt(
    window_id: str,
    pane_text: str,
    interactive: Any,
    *,
    auto_confirm_startup_trust: bool,
    interval: float,
) -> tuple[bool, bool, str]:
    """Handle an interactive prompt while waiting for Codex TUI startup."""
    if auto_confirm_startup_trust:
        confirmed, trust_message = await _b(
            "_maybe_confirm_startup_trust_prompt",
            _maybe_confirm_startup_trust_prompt,
        )(window_id, pane_text)
        if confirmed:
            await _b("asyncio", asyncio).sleep(interval)
            return False, False, trust_message
        if trust_message.startswith("Failed to confirm"):
            return True, False, trust_message
    msg = f"Agent is waiting for interactive input: {interactive.name}"
    await _b("asyncio", asyncio).sleep(interval)
    return False, False, msg


async def _send_to_window_when_codex_ready(
    user_id: int,
    thread_id: int | None,
    window_id: str,
    text: str,
    *,
    timeout: float | None = None,
    interval: float = 0.5,
    auto_confirm_startup_trust: bool = False,
    return_on_capacity_full: bool = False,
) -> tuple[bool, str]:
    """Send text once the new Codex TUI is ready to accept input."""
    startup_timeout = (
        timeout
        if timeout is not None
        else getattr(_b("config", config), "agent_startup_timeout_seconds", 180.0)
    )
    deadline = _b("asyncio", asyncio).get_event_loop().time() + startup_timeout
    last_message = ""
    while _b("asyncio", asyncio).get_event_loop().time() < deadline:
        capture = await _b("capture_agent_output", capture_agent_output)(
            user_id, thread_id, window_id
        )
        if capture is None:
            return False, "No session bound"
        if capture.missing:
            return False, "Window not found (may have been closed)"
        pane_text = capture.text or ""
        auth_error = _b("extract_auth_error_message", extract_auth_error_message)(
            pane_text
        )
        if auth_error:
            return False, _b(
                "_codex_auth_recovery_message", _codex_auth_recovery_message
            )(auth_error)
        interactive = _b("extract_interactive_content", extract_interactive_content)(
            pane_text
        )
        if interactive is not None:
            done, ok, last_message = await _handle_startup_interactive_prompt(
                window_id,
                pane_text,
                interactive,
                auto_confirm_startup_trust=auto_confirm_startup_trust,
                interval=interval,
            )
            if done:
                return ok, last_message
            continue
        if not _b("is_codex_input_ready", is_codex_input_ready)(pane_text):
            status = _b("parse_status_update", parse_status_update)(pane_text)
            last_message = (
                f"Agent is still busy: {status}"
                if status
                else "Agent UI is not ready for input"
            )
            await _b("asyncio", asyncio).sleep(interval)
            continue
        _b("turn_admission", turn_admission).observe(window_id, active=False)
        admitted = await _b("turn_admission", turn_admission).try_acquire(
            window_id,
            limit=_b("_agent_max_active_turns", _agent_max_active_turns)(),
        )
        if not admitted:
            last_message = (
                "Server is at its active-task limit; waiting for an available slot"
            )
            if return_on_capacity_full:
                return False, last_message
            await _b("asyncio", asyncio).sleep(interval)
            continue
        try:
            send_ok, send_msg = await _b(
                "_send_message_to_agent", _send_message_to_agent
            )(user_id, thread_id, window_id, text)
        except BaseException:
            _b("turn_admission", turn_admission).release(window_id)
            raise
        if send_ok:
            _b("turn_admission", turn_admission).mark_submitted(window_id)
            _b("idle_session_hibernator", idle_session_hibernator).forget(window_id)
            return True, send_msg
        _b("turn_admission", turn_admission).release(window_id)
        last_message = send_msg
        if (
            "Window is not running an agent" not in send_msg
            and "Window is not running Codex" not in send_msg
        ):
            return False, send_msg
        await _b("asyncio", asyncio).sleep(interval)
    return False, last_message or "Agent did not become ready"


async def _send_or_queue_to_starting_window(
    bot: Bot,
    user_id: int,
    thread_id: int | None,
    window_id: str,
    text: str,
    *,
    auto_confirm_startup_trust: bool = False,
) -> tuple[bool, str, bool]:
    """Persist a first prompt and use the same lifecycle as bound-window input."""
    del auto_confirm_startup_trust
    return await _b("_send_or_queue_agent_input", _send_or_queue_agent_input)(
        bot,
        user_id,
        thread_id,
        window_id,
        text,
    )


async def _enable_fast_mode_if_requested(
    bot: Bot,
    user_id: int,
    thread_id: int | None,
    window_id: str,
    profile: AgentProfile,
    chat_id: int,
) -> None:
    """Enable Fast mode after the selected agent session is ready."""
    if not profile.fast_mode:
        return

    success, message = await _b(
        "_send_to_window_when_codex_ready", _send_to_window_when_codex_ready
    )(
        user_id,
        thread_id,
        window_id,
        "/fast",
        auto_confirm_startup_trust=True,
    )
    if success:
        return
    _b("logger", logger).warning(
        "Failed to enable %s Fast mode for window %s: %s",
        profile.display_name,
        window_id,
        message,
    )
    await _b("safe_send", safe_send)(
        bot,
        chat_id,
        "⚠️ Fast mode could not be enabled for this model. "
        "The session will continue with the selected reasoning level.",
        message_thread_id=thread_id,
    )


async def _refresh_session_map_after_first_prompt(
    window_id: str,
    *,
    text: str | None = None,
    confirm_existing_session: bool = False,
    timeout: float = 20.0,
    transcript_session_id: str | None = None,
    transcript_after_offset: int | None = None,
    submit_key: str = "Enter",
) -> bool:
    """Load the session_map entry that Codex writes after the first prompt starts."""
    if transcript_session_id and not _b(
        "session_manager", session_manager
    ).window_matches_session(
        window_id,
        transcript_session_id,
    ):
        return False
    if _b("session_manager", session_manager).get_window_state(window_id).session_id:
        if text and confirm_existing_session:
            return await _b(
                "_confirm_first_prompt_delivery", _confirm_first_prompt_delivery
            )(
                window_id,
                text,
                transcript_session_id=transcript_session_id,
                transcript_after_offset=transcript_after_offset,
                submit_key=submit_key,
            )
        return True
    hook_ok = await _b("session_manager", session_manager).wait_for_session_map_entry(
        window_id, timeout=timeout
    )
    if hook_ok:
        if text:
            return await _b(
                "_confirm_first_prompt_delivery", _confirm_first_prompt_delivery
            )(
                window_id,
                text,
                transcript_session_id=transcript_session_id,
                transcript_after_offset=transcript_after_offset,
                submit_key=submit_key,
            )
        return True

    if text:
        if await _b("tmux_manager", tmux_manager).prompt_still_pending(window_id, text):
            _b("logger", logger).warning(
                "Codex window %s still has the first prompt in the input row; "
                "retrying %s before waiting for session_map again",
                window_id,
                submit_key,
            )
            if await _b("tmux_manager", tmux_manager).send_control_key(
                window_id, submit_key
            ):
                hook_ok = await _b(
                    "session_manager", session_manager
                ).wait_for_session_map_entry(
                    window_id,
                    timeout=min(timeout, 10.0),
                )
                if hook_ok:
                    return await _b(
                        "_confirm_first_prompt_delivery", _confirm_first_prompt_delivery
                    )(
                        window_id,
                        text,
                        transcript_session_id=transcript_session_id,
                        transcript_after_offset=transcript_after_offset,
                        submit_key=submit_key,
                    )
        _b("logger", logger).warning(
            "Codex window %s accepted input but did not register session_map",
            window_id,
        )
    else:
        _b("logger", logger).warning(
            "Codex window %s did not register session_map",
            window_id,
        )
    return hook_ok


async def _confirm_first_prompt_delivery(
    window_id: str,
    text: str,
    *,
    transcript_timeout: float | None = None,
    transcript_session_id: str | None = None,
    transcript_after_offset: int | None = None,
    submit_key: str = "Enter",
) -> bool:
    """Confirm that the first forwarded prompt reached the Codex transcript."""
    if transcript_timeout is None:
        transcript_timeout = _b("config", config).transcript_confirm_timeout_seconds
    transcript_ok = await _b(
        "session_manager", session_manager
    ).wait_for_transcript_user_message(
        window_id,
        text,
        timeout=transcript_timeout,
        expected_session_id=transcript_session_id,
        after_offset=transcript_after_offset,
    )
    if transcript_ok:
        return True

    if not await _b("tmux_manager", tmux_manager).prompt_still_pending(window_id, text):
        _b("logger", logger).warning(
            "Agent input cleared in window %s but was not recorded in the transcript",
            window_id,
        )
        return False

    _b("logger", logger).warning(
        "Agent input is still pending in window %s after transcript confirmation; "
        "retrying %s",
        window_id,
        submit_key,
    )
    if not await _b("tmux_manager", tmux_manager).send_control_key(
        window_id, submit_key
    ):
        return False

    transcript_ok = await _b(
        "session_manager", session_manager
    ).wait_for_transcript_user_message(
        window_id,
        text,
        timeout=transcript_timeout,
        expected_session_id=transcript_session_id,
        after_offset=transcript_after_offset,
    )
    if transcript_ok:
        return True

    if await _b("tmux_manager", tmux_manager).prompt_still_pending(window_id, text):
        _b("logger", logger).warning(
            "Agent input is still pending in window %s after submit retry",
            window_id,
        )
        return False

    # Unlike an input that was never observed in the prompt row, this text was
    # visibly pending before we retried its submit key. Some Codex builds update
    # the transcript after the short confirmation window, so clearing here is
    # sufficient secondary evidence that the explicit retry was accepted.
    _b("logger", logger).info(
        "Agent input cleared in window %s after explicit submit retry; transcript "
        "confirmation is still pending",
        window_id,
    )
    return True
