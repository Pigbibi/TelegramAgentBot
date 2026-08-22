import asyncio

import pytest

from telegram_agent_bot.turn_admission import TurnAdmissionController


@pytest.mark.asyncio
async def test_concurrent_reservations_respect_limit() -> None:
    controller = TurnAdmissionController()

    results = await asyncio.gather(
        controller.try_acquire("@1", limit=2),
        controller.try_acquire("@2", limit=2),
        controller.try_acquire("@3", limit=2),
    )

    assert results.count(True) == 2
    assert results.count(False) == 1
    assert len(controller.snapshot().reserved_windows) == 2


@pytest.mark.asyncio
async def test_idle_observation_releases_active_capacity() -> None:
    controller = TurnAdmissionController()
    controller.observe("@1", active=True)
    assert await controller.try_acquire("@2", limit=1) is False

    controller.observe("@1", active=False)

    assert await controller.try_acquire("@2", limit=1) is True


@pytest.mark.asyncio
async def test_unlimited_mode_needs_no_reservation() -> None:
    controller = TurnAdmissionController()

    assert await controller.try_acquire("@1", limit=0) is True
    assert controller.snapshot().reserved_windows == frozenset()


def test_pending_windows_are_tracked_for_hibernation_guard() -> None:
    controller = TurnAdmissionController()

    controller.set_pending("@1", pending=True)
    assert controller.has_pending("@1") is True

    controller.set_pending("@1", pending=False)
    assert controller.has_pending("@1") is False
