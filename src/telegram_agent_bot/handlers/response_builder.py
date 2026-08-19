"""Response message building for Telegram delivery.

Builds paginated response messages from Codex output:
  - Handles different content types (text, thinking, tool_use, tool_result)
  - Splits long messages into pages within Telegram's 4096 char limit
  - Preserves long user messages and expandable tool output without truncation

Final Markdown conversion is done by the send layer. This module performs
side-effect-free conversions only while sizing expandable quote pages.

Key function:
  - build_response_parts: Build paginated response messages
"""

from ..markdown_v2 import convert_markdown, convert_markdown_tables
from ..telegram_sender import split_message
from ..transcript_parser import TranscriptParser


_FORMATTED_PAGE_BUDGET = 3800
_PLAIN_PAGE_BUDGET = 2800


def _split_expandable_quote(inner: str) -> list[str]:
    """Split quote text by its rendered MarkdownV2 size without losing content."""
    start_tag = TranscriptParser.EXPANDABLE_QUOTE_START
    end_tag = TranscriptParser.EXPANDABLE_QUOTE_END
    if not inner:
        return [""]

    chunks: list[str] = []
    remaining = inner
    while remaining:
        low = 1
        high = len(remaining)
        best = 0
        while low <= high:
            middle = (low + high) // 2
            candidate = f"{start_tag}{remaining[:middle]}{end_tag}"
            if len(convert_markdown(candidate)) <= _FORMATTED_PAGE_BUDGET:
                best = middle
                low = middle + 1
            else:
                high = middle - 1

        if best <= 0:
            # A single rendered character always fits in practice. Keep forward
            # progress if a future Markdown renderer adds unexpected overhead.
            best = 1

        if best < len(remaining):
            prefix = remaining[:best]
            newline_break = prefix.rfind("\n")
            space_break = prefix.rfind(" ")
            natural_break = max(newline_break, space_break)
            if natural_break >= best // 2:
                best = natural_break + 1

        chunks.append(remaining[:best])
        remaining = remaining[best:]

    return chunks


def _append_segment(parts: list[str], segment: str) -> None:
    """Append a complete segment, combining it with the previous page if safe."""
    if not segment:
        return
    if parts:
        combined = parts[-1] + segment
        if len(convert_markdown(combined)) <= _FORMATTED_PAGE_BUDGET:
            parts[-1] = combined
            return
    parts.append(segment)


def _split_expandable_content(text: str) -> list[str]:
    """Paginate text containing one or more expandable quote sentinel blocks."""
    start_tag = TranscriptParser.EXPANDABLE_QUOTE_START
    end_tag = TranscriptParser.EXPANDABLE_QUOTE_END
    parts: list[str] = []
    cursor = 0

    while True:
        quote_start = text.find(start_tag, cursor)
        if quote_start < 0:
            break
        quote_end = text.find(end_tag, quote_start + len(start_tag))
        if quote_end < 0:
            break

        plain = text[cursor:quote_start]
        for chunk in split_message(plain, max_length=_PLAIN_PAGE_BUDGET):
            _append_segment(parts, chunk)

        inner_start = quote_start + len(start_tag)
        inner = text[inner_start:quote_end]
        for quote_chunk in _split_expandable_quote(inner):
            block = f"{start_tag}{quote_chunk}{end_tag}"
            _append_segment(parts, block)

        cursor = quote_end + len(end_tag)

    remainder = text[cursor:]
    for chunk in split_message(remainder, max_length=_PLAIN_PAGE_BUDGET):
        _append_segment(parts, chunk)

    return parts or [text]


def _decorate_parts(parts: list[str], prefix: str, separator: str) -> list[str]:
    """Add the content label and stable page counters."""
    total = len(parts)
    decorated: list[str] = []
    for index, part in enumerate(parts, 1):
        body = f"{prefix}{separator}{part}" if prefix else part
        if total > 1:
            body = f"{body}\n\n[{index}/{total}]"
        decorated.append(body)
    return decorated


def build_response_parts(
    text: str,
    is_complete: bool,
    content_type: str = "text",
    role: str = "assistant",
) -> list[str]:
    """Build paginated response messages for Telegram.

    Returns a list of raw markdown strings, each within Telegram's 4096 char limit.
    Multi-part messages get a [1/N] suffix.
    Markdown-to-MarkdownV2 conversion is done by the send layer, not here.
    """
    text = text.strip()

    # User messages: add emoji prefix (no newline), but preserve the complete
    # transcript text by using the same pagination path as assistant messages.
    if role == "user":
        prefix = "👤 "
        separator = ""
    elif content_type == "thinking":
        # Thinking: prefix with "∴ Thinking…" and single newline
        prefix = "∴ Thinking…"
        separator = "\n"
    else:
        # Plain text: no prefix
        prefix = ""
        separator = ""

    # Each page gets a complete sentinel pair so Telegram can render all quote
    # content as expandable MarkdownV2 without the send layer truncating it.
    if TranscriptParser.EXPANDABLE_QUOTE_START in text:
        return _decorate_parts(_split_expandable_content(text), prefix, separator)

    # Convert tables to card-style before splitting so tables aren't broken
    # across messages. The send layer's convert_markdown() call is idempotent.
    text = convert_markdown_tables(text)

    # Split first, then assemble each chunk.
    # Use conservative max to leave room for MarkdownV2 expansion at send layer.
    max_text = 3000 - len(prefix) - len(separator)

    text_chunks = split_message(text, max_length=max_text)
    return _decorate_parts(text_chunks, prefix, separator)
