"""Backend registry and plugin loader."""

from __future__ import annotations

import importlib
import logging
from collections.abc import Callable, Iterable
from importlib import metadata
from typing import Any, cast

from .base import AgentBackend

logger = logging.getLogger(__name__)

ENTRY_POINT_GROUP = "telegram_codex_bot.backends"
BackendFactory = Callable[[], AgentBackend]

_BACKEND_FACTORIES: dict[str, BackendFactory] = {}
_BACKEND_CACHE: dict[str, AgentBackend] = {}
_ENTRY_POINTS_DISCOVERED = False


def _normalize_backend_id(backend_id: str) -> str:
    return backend_id.strip().lower().replace("_", "-")


def register_backend(backend_id: str, factory: BackendFactory) -> None:
    """Register one backend factory."""
    normalized = _normalize_backend_id(backend_id)
    if not normalized:
        raise ValueError("backend_id must not be empty")
    _BACKEND_FACTORIES[normalized] = factory
    logger.debug("Registered backend: %s", normalized)


def _local_backend_factory() -> AgentBackend:
    from .local import LocalTmuxBackend

    return LocalTmuxBackend()


register_backend("local", _local_backend_factory)


def _factory_from_loaded(value: Any) -> BackendFactory:
    def factory() -> AgentBackend:
        backend = value() if callable(value) else value
        required_attrs = (
            "info",
            "prepare",
            "start",
            "stop",
            "create_session",
            "send_message",
            "send_control",
            "capture",
        )
        if not all(hasattr(backend, attr) for attr in required_attrs):
            raise TypeError(
                f"backend object does not implement AgentBackend: {backend!r}"
            )
        return cast(AgentBackend, backend)

    return factory


def _discover_entry_points() -> None:
    global _ENTRY_POINTS_DISCOVERED
    if _ENTRY_POINTS_DISCOVERED:
        return
    _ENTRY_POINTS_DISCOVERED = True

    try:
        entry_points = metadata.entry_points().select(group=ENTRY_POINT_GROUP)
    except Exception as exc:
        logger.warning("Unable to discover backend entry points: %s", exc)
        return

    for entry_point in entry_points:
        backend_id = _normalize_backend_id(entry_point.name)
        if backend_id in _BACKEND_FACTORIES:
            continue

        def factory(ep=entry_point) -> AgentBackend:
            return _factory_from_loaded(ep.load())()

        register_backend(backend_id, factory)


def load_backend_plugins(plugin_modules: Iterable[str] = ()) -> None:
    """Import optional backend plugin modules.

    A module can expose `register_backends(register_backend)`, `create_backend`,
    or `BACKEND_CLASS`. Entry-point based plugins do not need this setting.
    """
    for module_name in plugin_modules:
        module_name = module_name.strip()
        if not module_name:
            continue
        module = importlib.import_module(module_name)
        register_backends = getattr(module, "register_backends", None)
        if callable(register_backends):
            register_backends(register_backend)
            continue

        backend_id = getattr(module, "BACKEND_ID", module_name.rsplit(".", 1)[-1])
        create_backend = getattr(module, "create_backend", None)
        if callable(create_backend):
            register_backend(backend_id, cast(BackendFactory, create_backend))
            continue

        backend_class = getattr(module, "BACKEND_CLASS", None)
        if backend_class is not None:
            register_backend(backend_id, _factory_from_loaded(backend_class))
            continue

        raise ValueError(
            f"Backend plugin {module_name!r} must define register_backends(), "
            "create_backend, or BACKEND_CLASS"
        )


def available_backends(plugin_modules: Iterable[str] = ()) -> list[str]:
    """Return registered backend IDs."""
    load_backend_plugins(plugin_modules)
    _discover_entry_points()
    return sorted(_BACKEND_FACTORIES)


def load_backend(
    backend_id: str,
    *,
    plugin_modules: Iterable[str] = (),
    use_cache: bool = True,
) -> AgentBackend:
    """Load one backend by ID."""
    load_backend_plugins(plugin_modules)
    _discover_entry_points()

    normalized = _normalize_backend_id(backend_id or "local")
    if use_cache and normalized in _BACKEND_CACHE:
        return _BACKEND_CACHE[normalized]

    factory = _BACKEND_FACTORIES.get(normalized)
    if factory is None:
        known = ", ".join(available_backends()) or "none"
        raise ValueError(f"Unknown agent backend {backend_id!r}; available: {known}")

    backend = factory()
    if use_cache:
        _BACKEND_CACHE[normalized] = backend
    return backend


def get_configured_backend() -> AgentBackend:
    """Load the backend configured through environment/config."""
    from ..config import config

    return load_backend(
        config.agent_backend,
        plugin_modules=config.backend_plugins,
    )
