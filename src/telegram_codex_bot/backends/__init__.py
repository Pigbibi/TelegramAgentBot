"""Agent backend interfaces and loaders."""

from .base import (
    AgentBackend,
    AgentTarget,
    BackendInfo,
    CreateSessionRequest,
    CreateSessionResult,
    MessageCallback,
    SendResult,
)
from .registry import get_configured_backend, load_backend, register_backend

__all__ = [
    "AgentBackend",
    "AgentTarget",
    "BackendInfo",
    "CreateSessionRequest",
    "CreateSessionResult",
    "MessageCallback",
    "SendResult",
    "get_configured_backend",
    "load_backend",
    "register_backend",
]
