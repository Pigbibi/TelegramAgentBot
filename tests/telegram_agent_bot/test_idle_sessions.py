from telegram_agent_bot.idle_sessions import IdleSessionHibernator


def test_requires_continuous_idle_period() -> None:
    hibernator = IdleSessionHibernator()

    assert (
        hibernator.observe(
            "@1", idle=True, protected=False, timeout_seconds=30, now=100
        )
        is False
    )
    assert (
        hibernator.observe(
            "@1", idle=True, protected=False, timeout_seconds=30, now=129
        )
        is False
    )
    assert (
        hibernator.observe(
            "@1", idle=True, protected=False, timeout_seconds=30, now=130
        )
        is True
    )


def test_busy_or_pending_observation_resets_idle_clock() -> None:
    hibernator = IdleSessionHibernator()
    hibernator.observe("@1", idle=True, protected=False, timeout_seconds=30, now=100)

    assert (
        hibernator.observe("@1", idle=True, protected=True, timeout_seconds=30, now=140)
        is False
    )
    assert (
        hibernator.observe(
            "@1", idle=True, protected=False, timeout_seconds=30, now=141
        )
        is False
    )
    assert (
        hibernator.observe(
            "@1", idle=False, protected=False, timeout_seconds=30, now=200
        )
        is False
    )


def test_zero_timeout_disables_hibernation() -> None:
    hibernator = IdleSessionHibernator()

    assert (
        hibernator.observe(
            "@1", idle=True, protected=False, timeout_seconds=0, now=1000
        )
        is False
    )
