"""Tests for extracted agent-profile resolution helpers and bot.py parity."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from telegram_agent_bot import bot, profile_helpers
from telegram_agent_bot.agent_profile import (
    AGENT_CLAUDE,
    AGENT_CLAUDE_OFFICIAL,
    AGENT_CODEX,
    AGENT_CURSOR,
    DEFAULT_CLAUDE_EFFORTS,
    DEFAULT_CODEX_EFFORTS,
    DEFAULT_CURSOR_EFFORTS,
)
from telegram_agent_bot.handlers.directory_browser import (
    PROFILE_AGENT_KEY,
    PROFILE_EFFORT_KEY,
    PROFILE_FAST_MODE_KEY,
    PROFILE_MODEL_KEY,
    PROFILE_PERMISSION_MODE_KEY,
)


def _sample_cfg() -> SimpleNamespace:
    return SimpleNamespace(
        agent_type=AGENT_CODEX,
        codex_models=("gpt-5.6-sol", "gpt-5.6-luna"),
        claude_models=("claude-sonnet",),
        cursor_models=("gpt-5",),
        codex_model_efforts={
            "gpt-5.6-sol": ("low", "medium", "high", "ultra"),
            "gpt-5.6-luna": ("low", "high"),
            "gpt-none": (),
        },
        codex_model_default_efforts={"gpt-5.6-luna": "high"},
        codex_reasoning_effort="medium",
        claude_reasoning_effort="high",
        claude_default_permission_mode="full",
        cursor_permission_mode="full",
    )


def test_profile_models_and_effort_values() -> None:
    cfg = _sample_cfg()
    assert profile_helpers.profile_models(AGENT_CLAUDE_OFFICIAL, cfg=cfg) == ()
    assert profile_helpers.profile_models(AGENT_CLAUDE, cfg=cfg) == ("claude-sonnet",)
    assert profile_helpers.profile_models(AGENT_CURSOR, cfg=cfg) == ("gpt-5",)
    assert profile_helpers.profile_models(AGENT_CODEX, cfg=cfg) == (
        "gpt-5.6-sol",
        "gpt-5.6-luna",
    )

    assert (
        profile_helpers.profile_effort_values(AGENT_CLAUDE, "claude-sonnet", cfg=cfg)
        == DEFAULT_CLAUDE_EFFORTS
    )
    assert (
        profile_helpers.profile_effort_values(AGENT_CURSOR, "gpt-5", cfg=cfg)
        == DEFAULT_CURSOR_EFFORTS
    )
    assert profile_helpers.profile_effort_values(
        AGENT_CODEX, "gpt-5.6-sol", cfg=cfg
    ) == ("low", "medium", "high", "ultra")
    assert (
        profile_helpers.profile_effort_values(AGENT_CODEX, "unknown-model", cfg=cfg)
        == DEFAULT_CODEX_EFFORTS
    )

    with patch.object(bot, "config", cfg):
        assert bot._profile_models(AGENT_CLAUDE) == ("claude-sonnet",)
        assert bot._profile_effort_values(AGENT_CODEX, "gpt-5.6-luna") == (
            "low",
            "high",
        )


def test_resolve_profile_effort_fallbacks_and_bot_patch_parity() -> None:
    cfg = _sample_cfg()
    assert (
        profile_helpers.resolve_profile_effort(
            AGENT_CODEX, "gpt-5.6-sol", "ultra", cfg=cfg
        )
        == "ultra"
    )
    assert (
        profile_helpers.resolve_profile_effort(
            AGENT_CODEX, "gpt-5.6-luna", "ultra", cfg=cfg
        )
        == "high"
    )
    assert (
        profile_helpers.resolve_profile_effort(
            AGENT_CODEX, "gpt-none", "medium", cfg=cfg
        )
        == ""
    )
    assert (
        profile_helpers.resolve_profile_effort(
            AGENT_CLAUDE, "claude-sonnet", "ultra", cfg=cfg
        )
        == "high"
    )

    with patch.object(bot, "_profile_effort_values", return_value=("low", "high")):
        assert bot._resolve_profile_effort(AGENT_CODEX, "any", "high") == "high"


def test_default_profile_permission_and_context_resolution() -> None:
    cfg = _sample_cfg()
    assert profile_helpers.default_profile_permission(AGENT_CLAUDE, cfg=cfg) == "full"
    assert (
        profile_helpers.default_profile_permission(AGENT_CLAUDE_OFFICIAL, cfg=cfg)
        == "full"
    )
    assert profile_helpers.default_profile_permission(AGENT_CURSOR, cfg=cfg) == "full"
    assert profile_helpers.default_profile_permission(AGENT_CODEX, cfg=cfg) == "ask"

    empty_profile = profile_helpers.profile_from_context(None, cfg=cfg)
    assert empty_profile.agent_type == AGENT_CODEX
    assert empty_profile.reasoning_effort == "medium"
    assert empty_profile.permission_mode == "ask"

    cursor_profile = profile_helpers.profile_from_context(
        {
            PROFILE_AGENT_KEY: AGENT_CURSOR,
            PROFILE_MODEL_KEY: 99,
            PROFILE_EFFORT_KEY: "high",
            PROFILE_FAST_MODE_KEY: 1,
            PROFILE_PERMISSION_MODE_KEY: "ask",
        },
        cfg=cfg,
    )
    assert cursor_profile.agent_type == AGENT_CURSOR
    assert cursor_profile.model == ""
    assert cursor_profile.reasoning_effort == ""
    assert cursor_profile.fast_mode is True
    assert cursor_profile.permission_mode == "full"

    with (
        patch.object(bot, "config", cfg),
        patch.object(bot, "_resolve_profile_effort", return_value="ultra"),
    ):
        bot_profile = bot._profile_from_context({PROFILE_AGENT_KEY: AGENT_CODEX})
    assert bot_profile.reasoning_effort == "ultra"
    with patch.object(bot, "config", cfg):
        assert bot._default_profile_permission(AGENT_CLAUDE) == "full"
