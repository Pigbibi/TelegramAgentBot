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
        "💭 Thinking (10s) · esc to interrupt"
    )
    assert working_status._synthetic_working_starts[(1, 42, "@5")] == 100.0
