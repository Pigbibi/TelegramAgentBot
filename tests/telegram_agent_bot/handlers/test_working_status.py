import pytest

from telegram_agent_bot.handlers import working_status


@pytest.fixture(autouse=True)
def clear_working_state():
    working_status._synthetic_working_starts.clear()
    working_status._synthetic_working_output_seen.clear()
    yield
    working_status._synthetic_working_starts.clear()
    working_status._synthetic_working_output_seen.clear()


def test_synthetic_working_clears_on_empty_idle_prompt_before_first_output():
    pane = (
        "─ Worked for 4s ─────────────────────────\n\n"
        "› \n"
        "──────────────────────────────────────\n"
        "  [Opus 4.6] Context: 50%\n"
    )
    working_status._synthetic_working_starts[(1, 42, "@5")] = 100.0

    assert working_status.status_text_for_pane(1, 42, "@5", pane, now=110.0) is None
    assert (1, 42, "@5") not in working_status._synthetic_working_starts


def test_synthetic_working_preserves_prompt_text_before_first_output():
    pane = (
        "› Find and fix a bug in @filename\n"
        "──────────────────────────────────────\n"
        "  [Opus 4.6] Context: 50%\n"
    )
    working_status._synthetic_working_starts[(1, 42, "@5")] = 100.0

    assert working_status.status_text_for_pane(1, 42, "@5", pane, now=110.0) == (
        "💭 Thinking (10s)"
    )
    assert working_status._synthetic_working_starts[(1, 42, "@5")] == 100.0


def test_synthetic_working_warns_when_run_is_unusually_long():
    status = working_status.format_working_status(100.0, now=1900.0)

    assert status.startswith("💭 Thinking (30m 00s)")
    assert "running for a long time" in status
    assert "bounded timeout" in status


def test_native_working_status_uses_telegram_interrupt_and_warns():
    status = working_status.format_native_working_status(
        "• Waiting for background terminal (381m 00s • esc to interrupt)"
    )

    assert status is not None
    assert "/interrupt" in status
    assert "esc to interrupt" not in status
    assert "running for a long time" in status
