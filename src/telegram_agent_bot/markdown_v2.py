"""Markdown → Telegram MarkdownV2 conversion layer.

Wraps `telegramify_markdown` and adds special handling for expandable
blockquotes (delimited by sentinel tokens from TranscriptParser).
Expandable quotes are escaped and formatted as Telegram >…|| syntax
separately, so the library doesn't mangle them.

Key function: convert_markdown(text) → MarkdownV2 string.
"""

import re
from collections.abc import Iterable
from copy import copy
from itertools import chain
from threading import Lock

from mistletoe import block_token, span_token
from mistletoe.markdown_renderer import Fragment, MarkdownRenderer
from mistletoe.token import Token
from telegramify_markdown import markdownify

from .transcript_parser import TranscriptParser

_TABLE_SEP_RE = re.compile(r"^[\s|:\-]+$")
_MARKDOWN_RENDER_LOCK = Lock()


def _split_table_row(line: str) -> list[str]:
    """Split a table row by pipes, respecting escaped pipes (\\|)."""
    content = line.strip().strip("|")
    cells = re.split(r"(?<!\\)\|", content)
    return [cell.strip().replace("\\|", "|") for cell in cells]


def convert_markdown_tables(text: str) -> str:
    """Convert markdown tables to card-style key-value format.

    Telegram has no table rendering. This converts each row into a card
    with **Header**: value pairs, separated by horizontal lines — similar
    to how Codex renders tables in narrow terminals.

    Skips tables inside code blocks.
    """
    lines = text.split("\n")
    result: list[str] = []
    i = 0
    in_code_block = False

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        # Track code blocks
        if stripped.startswith("```"):
            in_code_block = not in_code_block
            result.append(line)
            i += 1
            continue

        if in_code_block:
            result.append(line)
            i += 1
            continue

        # Check if this looks like a table header row
        if (
            stripped.startswith("|")
            and stripped.endswith("|")
            and "|" in stripped[1:-1]
        ):
            headers = _split_table_row(stripped)

            # Next line must be separator (---|---|---)
            if i + 1 < len(lines):
                sep_line = lines[i + 1].strip()
                if sep_line.startswith("|") and _TABLE_SEP_RE.match(sep_line):
                    i += 2  # Skip header + separator
                    rows: list[list[str]] = []
                    while i < len(lines):
                        data_line = lines[i].strip()
                        if data_line.startswith("|") and data_line.endswith("|"):
                            rows.append(_split_table_row(data_line))
                            i += 1
                        else:
                            break

                    # Build card-style output
                    separator = "────────────"
                    cards: list[str] = []
                    for row in rows:
                        card_lines: list[str] = []
                        for j, header in enumerate(headers):
                            value = row[j] if j < len(row) else ""
                            if value:
                                card_lines.append(f"**{header}**: {value}")
                            else:
                                card_lines.append(f"**{header}**: —")
                        cards.append("\n".join(card_lines))

                    result.append(f"\n{separator}\n".join(cards))
                    continue

        result.append(line)
        i += 1

    return "\n".join(result)


_EXPQUOTE_RE = re.compile(
    re.escape(TranscriptParser.EXPANDABLE_QUOTE_START)
    + r"([\s\S]*?)"
    + re.escape(TranscriptParser.EXPANDABLE_QUOTE_END)
)

# Characters that must be escaped in Telegram MarkdownV2 plain text
_MDV2_ESCAPE_RE = re.compile(r"([_*\[\]()~`>#+\-=|{}.!\\])")


def _escape_mdv2(text: str) -> str:
    """Escape special characters for Telegram MarkdownV2."""
    return _MDV2_ESCAPE_RE.sub(r"\\\1", text)


def _render_expandable_quote(m: re.Match[str]) -> str:
    """Render a complete expandable blockquote block in raw MarkdownV2.

    Message-size pagination happens before conversion in response_builder.  This
    layer must not silently discard content because callers also use it for plain
    fallback and delivery acknowledgements.
    """
    inner = m.group(1)
    escaped = _escape_mdv2(inner)
    return "\n".join(f">{line}" for line in escaped.split("\n")) + "||"


class _MarkdownV2Renderer(MarkdownRenderer):
    """Keep transcript block layout while delegating content to the public API.

    Mistletoe still parses the existing fenced-code-only dialect. Its container
    renderer preserves list continuation indentation; telegramify-markdown 1.x
    renders the normalized leaf blocks, including code escaping and LaTeX.
    """

    def render_link_or_image(
        self, token: span_token.SpanToken, target: str
    ) -> Iterable[Fragment]:
        if isinstance(
            token, (span_token.Link, span_token.Image)
        ) and token.dest_type not in ("uri", "angle_uri"):
            # Resolve reference links before leaf blocks lose document context.
            token = copy(token)
            token.dest_type = "angle_uri"
            token.title = ""  # Telegram links do not retain Markdown tooltips.
        yield from super().render_link_or_image(token, target)

    def blocks_to_lines(
        self, tokens: Iterable[Token], max_line_length: int | None
    ) -> Iterable[str]:
        for token in tokens:
            if isinstance(
                token, (block_token.List, block_token.ListItem, block_token.Quote)
            ):
                yield from self.render_map[type(token).__name__](
                    token, max_line_length=max_line_length
                )
            else:
                source = super().render(token)
                yield from markdownify(source).removesuffix("\n").split("\n")

    def render_list_item(
        self, token: block_token.ListItem, max_line_length: int | None
    ) -> Iterable[str]:
        children = list(token.children or ())
        first_source = ""
        if children and isinstance(children[0], block_token.Paragraph):
            first_source = super().render(children.pop(0))

        # Convert the marker with the first paragraph so the public API also
        # handles ordered lists and task checkboxes. Layout is applied afterward
        # rather than reparsing indented continuation lines as code.
        first = (
            markdownify(f"{token.leader} {first_source}").removesuffix("\n").split("\n")
        )
        following = self.blocks_to_lines(children, max_line_length=max_line_length)
        return self.prefix_lines(
            chain(first, following),
            first_line_prefix=" " * token.indentation,
            following_line_prefix=" " * token.prepend,
        )


def _markdownify(text: str) -> str:
    """Render using public APIs, with only fenced blocks treated as code."""
    # Mistletoe's token registry is process-global. Serialize our contexts and
    # let the public renderer context restore its defaults even after errors.
    with (
        _MARKDOWN_RENDER_LOCK,
        _MarkdownV2Renderer(normalize_whitespace=False) as renderer,
    ):
        block_token.remove_token(block_token.BlockCode)
        return renderer.render(block_token.Document(text))


def convert_markdown(text: str) -> str:
    """Convert standard Markdown to Telegram MarkdownV2 format.

    Expandable blockquote sections (marked by sentinel tokens from
    TranscriptParser) are extracted, escaped, and formatted separately
    so that telegramify_markdown doesn't mangle the >...|| syntax.
    """
    # Convert markdown tables to card-style format before telegramify
    text = convert_markdown_tables(text)

    # Extract expandable quote blocks before telegramify
    segments: list[tuple[bool, str]] = []  # (is_quote, content)
    last_end = 0
    for m in _EXPQUOTE_RE.finditer(text):
        if m.start() > last_end:
            segments.append((False, text[last_end : m.start()]))
        segments.append((True, m.group(0)))
        last_end = m.end()
    if last_end < len(text):
        segments.append((False, text[last_end:]))

    if not segments:
        return _markdownify(text)

    parts: list[str] = []
    previous_is_quote = False
    for is_quote, segment in segments:
        if is_quote:
            rendered = _EXPQUOTE_RE.sub(_render_expandable_quote, segment)
        else:
            rendered = _markdownify(segment)
        if (
            parts
            and (is_quote or previous_is_quote)
            and not parts[-1].endswith("\n")
            and not rendered.startswith("\n")
        ):
            parts.append("\n")
        parts.append(rendered)
        previous_is_quote = is_quote
    return "".join(parts)
