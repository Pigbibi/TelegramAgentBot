"""Tests for terminal screenshot rendering and optional font discovery."""

from __future__ import annotations

from pathlib import Path

import pytest

from telegram_agent_bot import screenshot


class TestFontDiscovery:
    def test_bundled_primary_font_exists(self) -> None:
        assert screenshot._PRIMARY_FONT_PATH.is_file()

    def test_env_override_takes_precedence(self, tmp_path: Path) -> None:
        custom_cjk = tmp_path / "CustomCJK.otf"
        custom_cjk.write_bytes(b"font-bytes")
        custom_emoji = tmp_path / "CustomEmoji.ttf"
        custom_emoji.write_bytes(b"font-bytes")

        resolved = screenshot._resolve_font_paths(
            env={
                screenshot._CJK_ENV_VAR: str(custom_cjk),
                screenshot._EMOJI_ENV_VAR: str(custom_emoji),
            },
            search_dirs=(),
        )
        assert resolved == [
            screenshot._PRIMARY_FONT_PATH,
            custom_cjk,
            custom_emoji,
        ]

    def test_directory_discovery_searches_subdirectories(self, tmp_path: Path) -> None:
        nested = tmp_path / "opentype" / "noto"
        nested.mkdir(parents=True)
        noto = nested / "NotoSansMonoCJKsc-Regular.otf"
        noto.write_bytes(b"font-bytes")

        found = screenshot._discover_font_path(
            screenshot._CJK_ENV_VAR,
            screenshot._CJK_CANDIDATE_NAMES,
            screenshot._CJK_GLOB_PATTERNS,
            env={},
            search_dirs=(tmp_path,),
        )
        assert found == noto

    def test_missing_system_fonts_fall_back_to_primary(self, tmp_path: Path) -> None:
        resolved = screenshot._resolve_font_paths(
            env={},
            search_dirs=(tmp_path / "missing",),
        )
        assert resolved == [
            screenshot._PRIMARY_FONT_PATH,
            screenshot._PRIMARY_FONT_PATH,
            screenshot._PRIMARY_FONT_PATH,
        ]

    def test_load_font_falls_back_to_primary_on_corrupt_override(
        self, tmp_path: Path
    ) -> None:
        broken = tmp_path / "broken.ttf"
        broken.write_bytes(b"not-a-valid-font")

        loaded = screenshot._load_font(broken, 24)
        assert loaded is not None


class TestTextToImage:
    @pytest.mark.asyncio
    async def test_renders_png_without_system_cjk_or_symbol_fonts(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            screenshot,
            "_resolve_font_paths",
            lambda: [
                screenshot._PRIMARY_FONT_PATH,
                screenshot._PRIMARY_FONT_PATH,
                screenshot._PRIMARY_FONT_PATH,
            ],
        )
        png = await screenshot.text_to_image(
            "\x1b[32mhello\x1b[0m 中文 ✔",
            font_size=20,
            with_ansi=True,
        )
        assert png.startswith(b"\x89PNG\r\n\x1a\n")
        assert len(png) > 100
