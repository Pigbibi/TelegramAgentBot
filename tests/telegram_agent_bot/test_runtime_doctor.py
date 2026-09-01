from __future__ import annotations

from telegram_agent_bot.runtime_doctor import diagnose_routing_state


def _state(*, bindings: dict | None = None, targets: dict | None = None) -> dict:
    return {
        "thread_bindings": bindings or {"1": {"2": "@1"}},
        "thread_targets": targets
        or {
            "1": {
                "2": {
                    "backend_id": "local",
                    "node_id": "local",
                    "session_id": "session-1",
                    "window_id": "@1",
                }
            }
        },
        "window_states": {"@1": {"session_id": "session-1"}},
    }


def _session_map(*windows: str) -> dict:
    return {
        f"telegram-agent-bot:{window}": {"session_id": "session-1"}
        for window in windows
    }


def test_diagnose_routing_state_accepts_one_consistent_local_topic() -> None:
    report = diagnose_routing_state(
        state=_state(),
        session_map=_session_map("@1"),
        tmux_session_name="telegram-agent-bot",
        live_window_ids={"@1"},
        tmux_available=True,
    )

    assert report.status == "ok"
    assert report.exit_code == 0


def test_diagnose_routing_state_rejects_duplicate_topic_binding() -> None:
    report = diagnose_routing_state(
        state=_state(bindings={"1": {"2": "@1"}, "3": {"4": "@1"}}),
        session_map=_session_map("@1"),
        tmux_session_name="telegram-agent-bot",
        live_window_ids={"@1"},
        tmux_available=True,
    )

    assert report.exit_code == 1
    assert "duplicate_window_binding" in {finding.code for finding in report.findings}


def test_diagnose_routing_state_rejects_target_window_mismatch() -> None:
    report = diagnose_routing_state(
        state=_state(
            targets={
                "1": {
                    "2": {
                        "backend_id": "local",
                        "node_id": "local",
                        "window_id": "@9",
                    }
                }
            }
        ),
        session_map=_session_map("@1"),
        tmux_session_name="telegram-agent-bot",
        live_window_ids={"@1"},
        tmux_available=True,
    )

    assert report.exit_code == 1
    assert "local_target_mismatch" in {finding.code for finding in report.findings}


def test_diagnose_routing_state_rejects_bound_window_missing_from_tmux() -> None:
    report = diagnose_routing_state(
        state=_state(),
        session_map=_session_map("@1"),
        tmux_session_name="telegram-agent-bot",
        live_window_ids=set(),
        tmux_available=True,
    )

    assert report.exit_code == 1
    assert "bound_window_missing" in {finding.code for finding in report.findings}


def test_diagnose_routing_state_warns_about_stale_session_map_entry() -> None:
    report = diagnose_routing_state(
        state=_state(),
        session_map=_session_map("@1", "@9"),
        tmux_session_name="telegram-agent-bot",
        live_window_ids={"@1"},
        tmux_available=True,
    )

    assert report.status == "warning"
    assert report.exit_code == 0
    assert "stale_session_map_entry" in {finding.code for finding in report.findings}
