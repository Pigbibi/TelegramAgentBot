"""Optional native input-routing capability for agent backends."""

from __future__ import annotations

from typing import Literal, Protocol

from .base import AgentTarget, SendResult


AgentInputMode = Literal["steer", "queue"]


class AgentInputRouter(Protocol):
    """Backend capability for steering or queueing input in an active turn."""

    async def send_input(
        self,
        target: AgentTarget,
        text: str,
        *,
        mode: AgentInputMode,
    ) -> SendResult:
        """Submit text using the agent's native active-turn input semantics."""
        ...
