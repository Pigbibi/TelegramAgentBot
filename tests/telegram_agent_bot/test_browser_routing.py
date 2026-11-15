"""Tests for extracted forward-command and directory/backend routing helpers."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from telegram_agent_bot import bot, browser_routing
from telegram_agent_bot.agent_profile import (
    AGENT_CLAUDE,
    AGENT_CLAUDE_OFFICIAL,
    AGENT_CODEX,
    AGENT_CURSOR,
)
from telegram_agent_bot.backends.base import BackendInfo
from telegram_agent_bot.backends.browser import BrowserRoot, DirectoryListing
from telegram_agent_bot.handlers.directory_browser import (
    BROWSE_BACKEND_ID_KEY,
    BROWSE_NODE_ID_KEY,
    BROWSE_ROOT_LABEL_KEY,
    BROWSE_ROOT_PATH_KEY,
)


class _DummyRemoteBrowser:
    backend_id = "cluster"

    def __init__(
        self, *, mode: str = "remote", listing: DirectoryListing | None = None
    ):
        self._mode = mode
        self._listing = listing or DirectoryListing(
            path="/srv/work",
            subdirs=["alpha", "beta"],
        )

    def info(self) -> BackendInfo:
        return BackendInfo(
            backend_id=self.backend_id,
            display_name="Cluster",
            mode=self._mode,
        )

    async def list_roots(self) -> list[BrowserRoot]:
        return [
            BrowserRoot(
                label="Cluster", path="/srv", backend_id="cluster", node_id="n1"
            )
        ]

    async def list_directory(
        self, node_id: str, path: str, *, root_path: str = ""
    ) -> DirectoryListing:
        return self._listing

    async def list_sessions(self, node_id: str, cwd: str) -> list[object]:
        return []


def test_normalize_forward_command_text_and_bot_parity() -> None:
    cases = [
        ("plain text", "plain text"),
        ("/goal@MyBot inspect @file", "/goal inspect @file"),
        ("/goal.继续验证", "/goal 继续验证"),
        ("/GOAL。下一步", "/GOAL 下一步"),
        ("/unknown.keepdot", "/unknown.keepdot"),
    ]
    for raw, expected in cases:
        assert (
            browser_routing.normalize_forward_command_text(
                raw, known_commands=bot.CC_COMMANDS
            )
            == expected
        )
        assert bot._normalize_forward_command_text(raw) == expected


def test_adapt_forward_command_for_agent_and_bot_parity() -> None:
    cases = [
        ("/plugins", AGENT_CLAUDE, "/plugin"),
        ("/plugins", AGENT_CLAUDE_OFFICIAL, "/plugin"),
        ("/plugin install foo", AGENT_CODEX, "/plugins install foo"),
        ("/plugin", AGENT_CURSOR, "/plugins"),
        ("/goal.check", AGENT_CODEX, "/goal check"),
        ("hello", AGENT_CODEX, "hello"),
    ]
    for raw, agent_type, expected in cases:
        assert (
            browser_routing.adapt_forward_command_for_agent(
                raw, agent_type, known_commands=bot.CC_COMMANDS
            )
            == expected
        )
        assert bot._adapt_forward_command_for_agent(raw, agent_type) == expected


def test_default_directory_browser_path_prefers_existing_root_then_projects(
    tmp_path: Path,
) -> None:
    root_dir = tmp_path / "root"
    projects_dir = tmp_path / "projects"
    root_dir.mkdir()
    projects_dir.mkdir()

    assert browser_routing.default_directory_browser_path(
        str(root_dir), default_projects_path=projects_dir
    ) == str(root_dir)
    assert browser_routing.default_directory_browser_path(
        str(tmp_path / "missing"), default_projects_path=projects_dir
    ) == str(projects_dir)
    assert browser_routing.default_directory_browser_path(
        None, default_projects_path=tmp_path / "missing_projects"
    ) == str(Path.home())
    with patch.object(bot.config, "default_projects_path", projects_dir):
        assert bot._default_directory_browser_path() == str(projects_dir)
        assert bot._default_directory_browser_path(str(root_dir)) == str(root_dir)


def test_browse_context_and_kwargs_and_bot_aliases() -> None:
    assert browser_routing.browse_root_context(None) == (None, None)
    assert browser_routing.browse_root_context({BROWSE_ROOT_LABEL_KEY: ""}) == (
        None,
        None,
    )
    user_data = {
        BROWSE_ROOT_LABEL_KEY: "MacBook",
        BROWSE_ROOT_PATH_KEY: "/srv/projects",
        BROWSE_BACKEND_ID_KEY: "cluster",
        BROWSE_NODE_ID_KEY: "macbook",
    }
    assert browser_routing.browse_root_context(user_data) == (
        "MacBook",
        "/srv/projects",
    )
    assert browser_routing.directory_browser_kwargs(user_data) == {
        "root_label": "MacBook",
        "root_path": "/srv/projects",
    }
    assert browser_routing.browse_backend_context(user_data) == ("cluster", "macbook")
    assert browser_routing.browse_backend_context(None) == ("", "")
    assert browser_routing.browse_backend_context(
        {BROWSE_BACKEND_ID_KEY: 123, BROWSE_NODE_ID_KEY: None}
    ) == ("", "")

    assert bot._browse_root_context is browser_routing.browse_root_context
    assert bot._directory_browser_kwargs is browser_routing.directory_browser_kwargs
    assert bot._browse_backend_context is browser_routing.browse_backend_context


def test_as_browser_and_remote_root_selection() -> None:
    assert browser_routing.as_browser(None) is None
    assert browser_routing.as_browser(SimpleNamespace(list_roots=lambda: [])) is None  # type: ignore[arg-type]

    local_backend = _DummyRemoteBrowser(mode="local")
    local_backend.backend_id = "local"
    assert browser_routing.active_remote_root_browser(local_backend) is None  # type: ignore[arg-type]

    broken_backend = MagicMock()
    broken_backend.info.side_effect = RuntimeError("boom")
    assert browser_routing.active_remote_root_browser(broken_backend) is None

    remote_backend = _DummyRemoteBrowser(mode="remote")
    assert browser_routing.active_remote_root_browser(remote_backend) is remote_backend  # type: ignore[arg-type]
    assert browser_routing.browser_for_backend_id("", remote_backend) is None  # type: ignore[arg-type]
    assert browser_routing.browser_for_backend_id("other", remote_backend) is None  # type: ignore[arg-type]
    assert (
        browser_routing.browser_for_backend_id("cluster", remote_backend)
        is remote_backend
    )  # type: ignore[arg-type]

    with patch.object(
        browser_routing, "get_configured_backend", return_value=remote_backend
    ):
        assert browser_routing.browser_for_backend_id("cluster") is remote_backend

    with patch.object(bot, "agent_backend", remote_backend):
        assert bot._active_remote_root_browser() is remote_backend
        assert bot._browser_for_backend_id("cluster") is remote_backend
    assert bot._as_browser is browser_routing.as_browser


def test_parse_root_selection_and_remote_paths() -> None:
    assert browser_routing.parse_root_selection("bad") is None
    assert browser_routing.parse_root_selection(["only-one"]) is None
    assert browser_routing.parse_root_selection([123, "/path"]) is None
    assert browser_routing.parse_root_selection(("Local", "/srv")) == {
        "label": "Local",
        "path": "/srv",
        "backend_id": "",
        "node_id": "",
    }
    assert browser_routing.parse_root_selection(
        ["Remote", "/srv", "cluster", "n1"]
    ) == {
        "label": "Remote",
        "path": "/srv",
        "backend_id": "cluster",
        "node_id": "n1",
    }

    assert browser_routing.remote_child_path("/", "repo") == "/repo"
    assert (
        browser_routing.remote_child_path("/srv/projects/", "app")
        == "/srv/projects/app"
    )
    assert (
        browser_routing.remote_parent_path("/srv/projects", "/srv/projects")
        == "/srv/projects"
    )
    assert (
        browser_routing.remote_parent_path("/srv/projects/app", "/srv/projects")
        == "/srv/projects"
    )
    assert (
        browser_routing.remote_parent_path("/etc", "/srv/projects") == "/srv/projects"
    )
    assert browser_routing.remote_parent_path("/srv/projects", None) == "/srv"

    assert bot._parse_root_selection is browser_routing.parse_root_selection
    assert bot._remote_child_path is browser_routing.remote_child_path
    assert bot._remote_parent_path is browser_routing.remote_parent_path


def test_directory_listing_with_root_and_clamp(tmp_path: Path) -> None:
    blank = DirectoryListing(path="/srv/work", subdirs=["repo"])
    filled = browser_routing.directory_listing_with_root(
        blank, root_label="Cluster", root_path="/srv"
    )
    assert filled.root_label == "Cluster"
    assert filled.root_path == "/srv"

    existing = DirectoryListing(
        path="/srv/work", subdirs=["repo"], root_label="Preset", root_path="/preset"
    )
    assert (
        browser_routing.directory_listing_with_root(
            existing, root_label="Cluster", root_path="/srv"
        )
        is existing
    )

    root = tmp_path / "root"
    child = root / "child"
    outside = tmp_path / "outside"
    root.mkdir()
    child.mkdir()
    outside.mkdir()

    assert browser_routing.clamp_to_selected_root(str(outside), None) == str(outside)
    user_data = {BROWSE_ROOT_PATH_KEY: str(root)}
    assert browser_routing.clamp_to_selected_root(str(child), user_data) == str(
        child.resolve()
    )
    assert browser_routing.clamp_to_selected_root(str(outside), user_data) == str(
        root.resolve()
    )

    assert (
        bot._directory_listing_with_root is browser_routing.directory_listing_with_root
    )
    assert bot._clamp_to_selected_root is browser_routing.clamp_to_selected_root


@pytest.mark.asyncio
async def test_build_backend_directory_browser_and_bot_wrapper() -> None:
    assert (
        await browser_routing.build_backend_directory_browser(
            backend_id="",
            node_id="n1",
            path="/srv",
        )
        is None
    )

    failing_browser = MagicMock()
    failing_browser.list_directory = AsyncMock(side_effect=RuntimeError("offline"))
    assert (
        await browser_routing.build_backend_directory_browser(
            backend_id="cluster",
            node_id="n1",
            path="/srv",
            browser=failing_browser,
        )
        is None
    )

    remote_backend = _DummyRemoteBrowser()
    with patch.object(bot, "agent_backend", remote_backend):
        result = await bot._build_backend_directory_browser(
            backend_id="cluster",
            node_id="n1",
            path="/srv/work",
            root_label="Cluster",
            root_path="/srv",
        )
    assert result is not None
    text, _keyboard, subdirs, resolved_path = result
    assert "Cluster" in text
    assert subdirs == ["alpha", "beta"]
    assert resolved_path == "/srv/work"
