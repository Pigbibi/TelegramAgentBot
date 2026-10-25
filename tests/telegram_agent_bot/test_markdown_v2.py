"""Tests for Markdown → Telegram MarkdownV2 conversion."""

from concurrent.futures import ThreadPoolExecutor

import mistletoe
import pytest
from mistletoe.block_token import BlockCode

from telegram_agent_bot import markdown_v2
from telegram_agent_bot.markdown_v2 import _escape_mdv2, convert_markdown
from telegram_agent_bot.transcript_parser import TranscriptParser

EXP_START = TranscriptParser.EXPANDABLE_QUOTE_START
EXP_END = TranscriptParser.EXPANDABLE_QUOTE_END


class TestEscapeMdv2:
    @pytest.mark.parametrize(
        "input_text,expected",
        [
            (
                "_*[]()~>#+\\-=|{}.!",
                "\\_\\*\\[\\]\\(\\)\\~\\>\\#\\+\\\\\\-\\=\\|\\{\\}\\.\\!",
            ),
            ("hello world 123", "hello world 123"),
            ("", ""),
        ],
        ids=["special-chars", "alphanumeric-unchanged", "empty-string"],
    )
    def test_escape(self, input_text: str, expected: str) -> None:
        assert _escape_mdv2(input_text) == expected


class TestConvertMarkdown:
    def test_plain_text(self) -> None:
        result = convert_markdown("hello world")
        assert "hello world" in result

    def test_bold(self) -> None:
        result = convert_markdown("**bold text**")
        assert "*bold text*" in result
        assert "**bold text**" not in result

    @pytest.mark.parametrize("indent", ["    ", "        ", "\t"])
    def test_indented_paragraph_is_markdown_not_code(self, indent: str) -> None:
        result = convert_markdown(
            f"{indent}**Indented paragraph**\n{indent}next line with _emphasis_"
        )

        assert "```" not in result
        assert "*Indented paragraph*\nnext line with _emphasis_" in result

    def test_nested_list_keeps_continuation_indentation(self) -> None:
        result = convert_markdown(
            "- outer\n    - nested\n        continuation **bold**\n    - second"
        )

        assert (
            "⦁ outer\n    ⦁ nested\n      continuation *bold*\n    ⦁ second" in result
        )
        assert "```" not in result

    def test_list_keeps_indented_second_paragraph(self) -> None:
        result = convert_markdown("- parent\n\n    **continued**")

        assert "⦁ parent\n\n  *continued*" in result
        assert "```" not in result

    def test_ordered_list_keeps_numbers_and_continuation(self) -> None:
        result = convert_markdown("3. first\n   continued **bold**\n4. second")

        assert "3\\. first\n   continued *bold*\n4\\. second" in result

    def test_nested_task_list_keeps_checkboxes_and_text(self) -> None:
        result = convert_markdown("- [x] **done**\n  - [ ] **todo**")

        assert "✅ *done*" in result
        assert "☑" in result
        assert "*todo*" in result
        assert "\\[x\\]" not in result

    @pytest.mark.parametrize("fence", ["```", "~~~~", "````"])
    def test_fenced_code_keeps_indented_literal_content(self, fence: str) -> None:
        result = convert_markdown(f"{fence}python\n    **literal** _value_\n{fence}")

        assert "```python\n    **literal** _value_\n```" in result

    def test_nested_list_keeps_fenced_code_and_following_item(self) -> None:
        result = convert_markdown(
            "- parent\n    - child\n\n"
            "      ```python\n      **literal**\n      ```\n    - last"
        )

        assert "    ⦁ child" in result
        assert "      ```python" in result
        assert "      **literal**" in result
        assert "    ⦁ last" in result

    def test_latex_is_converted_to_unicode(self) -> None:
        result = convert_markdown(r"Inline \(\alpha + \beta\) and \[\frac{1}{2}\]")

        assert "α" in result
        assert "β" in result
        assert "½" in result

    @pytest.mark.parametrize(
        "link,definition",
        [
            ("[label][ref]", '[ref]: https://example.com "tooltip"'),
            ("[label][]", "[label]: https://example.com"),
            ("[label]", "[label]: https://example.com"),
        ],
    )
    def test_reference_link_keeps_target_defined_in_another_block(
        self, link: str, definition: str
    ) -> None:
        result = convert_markdown(f"{link}\n\n{definition}")

        assert "[label](https://example.com)" in result

    def test_table_cards_keep_values_and_code_fences(self) -> None:
        result = convert_markdown(
            "| Name | Status |\n| --- | --- |\n| item | ready |\n\n"
            "```text\n| literal | table |\n| --- | --- |\n```"
        )

        assert "*Name*: item" in result
        assert "*Status*: ready" in result
        assert "| literal | table |" in result

    def test_parser_defaults_are_restored_after_render_failure(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def fail_render(text: str) -> str:
            raise ValueError("render failed")

        with monkeypatch.context() as patch:
            patch.setattr(markdown_v2, "markdownify", fail_render)
            with pytest.raises(ValueError, match="render failed"):
                convert_markdown("**first request**")

        document = mistletoe.Document("    code under the default parser")
        assert isinstance(next(iter(document.children)), BlockCode)
        assert "*second request*" in convert_markdown("    **second request**")

    def test_concurrent_conversions_keep_their_own_parser_context(self) -> None:
        sources = [
            "    **indented**",
            "- parent\n  - child\n    continued",
            "```text\n    literal\n```",
        ] * 10
        expected = [convert_markdown(source) for source in sources]

        with ThreadPoolExecutor(max_workers=4) as pool:
            actual = list(pool.map(convert_markdown, sources))

        assert actual == expected

    def test_code_block_preserved(self) -> None:
        result = convert_markdown("```python\nprint('hi')\n```")
        assert "```" in result
        assert "print" in result

    def test_expandable_quote_sentinels(self) -> None:
        text = f"{EXP_START}quoted content{EXP_END}"
        result = convert_markdown(text)
        assert EXP_START not in result
        assert EXP_END not in result
        assert ">quoted content||" in result

    def test_mixed_text_and_expandable_quote(self) -> None:
        text = f"before {EXP_START}inside quote{EXP_END} after"
        result = convert_markdown(text)
        assert EXP_START not in result
        assert EXP_END not in result
        assert ">inside quote||" in result
        assert "before" in result
        assert "after" in result

    @pytest.mark.parametrize("separator", ["\n", "\n\n", " "])
    def test_expandable_quote_has_separate_line_boundaries(
        self, separator: str
    ) -> None:
        result = convert_markdown(
            f"before{separator}{EXP_START}special _ output!{EXP_END}{separator}after"
        )

        assert "before\n" in result
        assert ">special \\_ output\\!||\n" in result
        assert "after" in result

    def test_adjacent_expandable_quotes_stay_on_separate_lines(self) -> None:
        result = convert_markdown(
            f"{EXP_START}first{EXP_END}{EXP_START}second{EXP_END}"
        )

        assert ">first||\n>second||" in result

    def test_expandable_quote_renderer_does_not_truncate(self) -> None:
        inner = "x" * 5000

        result = convert_markdown(f"{EXP_START}{inner}{EXP_END}")

        assert result.count("x") == len(inner)
        assert "truncated" not in result.lower()
