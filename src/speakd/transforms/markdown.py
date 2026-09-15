"""Render markdown as speech, one block at a time.

Headings and list items get terminal punctuation, because the segmenter splits
on sentence boundaries and a heading without one runs into whatever follows.

Fenced code blocks (``` or ~~~) are announced rather than read: reading
punctuation aloud for twenty lines is worse than saying nothing. Indented
(four-space) code blocks are deliberately *not* detected and are read as
prose instead. Agent output is effectively always fenced, so the gap this
leaves is rare, whereas a correct detector needs genuine list-context
tracking: without it, a loose list item's four-space-indented continuation
paragraph is indistinguishable from an indented code block, and gets
replaced by the code-block marker instead of spoken — losing real content on
a common input to handle a rare one correctly. That was tried and reverted;
absence here is a decision, not an oversight.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

from speakd.model import Piece, Span

CODE_BLOCK_MARKER = "Code block omitted."

# A quotation read without its frame arrives as the speaker's own assertion,
# which is the one thing a quotation is not.
QUOTE_PREFIX = "Quote,"
QUOTE_SUFFIX = "End quote."

# A table row is delimited at both ends. Prose that merely mentions a pipe is
# not a table, and reading a six-by-three table aloud is worse than saying
# nothing -- the same trade the code fence already makes.
TABLE_MARKER = "Table omitted."

_FENCE = re.compile(r"^\s*(```|~~~)")
_HEADING = re.compile(r"^\s*#{1,6}\s+(.*)$")
_BULLET = re.compile(r"^\s*[-*+]\s+(.*)$")
_ORDERED = re.compile(r"^\s*\d+[.)]\s+(.*)$")
_QUOTE = re.compile(r"^\s*>\s?(.*)$")
_RULE = re.compile(r"^\s*([-*_])\s*(\1\s*){2,}$")
_TABLE_ROW = re.compile(r"^\s*\|.*\|\s*$")
_LINK = re.compile(r"\[([^\]]+)\]\([^)]*\)")
_INLINE_CODE = re.compile(r"`([^`]+)`")
_BOLD = re.compile(r"\*\*([^*]+)\*\*")
_ITALIC_STAR = re.compile(r"(?<!\*)\*([^*]+)\*(?!\*)")
_ITALIC_UNDER = re.compile(r"(?<![\w_])_([^_]+)_(?![\w_])")


def _inline(text: str) -> str:
    text = _LINK.sub(r"\1", text)
    text = _INLINE_CODE.sub(r"\1", text)
    text = _BOLD.sub(r"\1", text)
    text = _ITALIC_STAR.sub(r"\1", text)
    text = _ITALIC_UNDER.sub(r"\1", text)
    return text


def _terminate(text: str) -> str:
    text = text.strip()
    if text and text[-1] not in ".!?:;":
        return text + "."
    return text


@dataclass(frozen=True)
class _Block:
    """One rendered block, and the offsets into the input it came from."""

    text: str
    start: int
    end: int


def _blocks(text: str) -> list[_Block]:
    """Split markdown into blocks, rendering each.

    A block is the unit a listener hears as one breath: a paragraph, a
    heading, a list item, an omitted code fence. Consecutive prose lines join
    into one block because a hard-wrapped paragraph is still one paragraph; a
    blank line ends it.
    """
    blocks: list[_Block] = []
    prose: list[str] = []
    prose_start = 0
    prose_end = 0
    quote: list[str] = []
    quote_start = 0
    quote_end = 0
    table_start = 0
    table_end = 0
    in_table = False
    in_fence = False
    fence_start = 0
    offset = 0

    def flush_prose() -> None:
        nonlocal prose
        if prose:
            blocks.append(_Block(" ".join(prose), prose_start, prose_end))
            prose = []

    def flush_quote() -> None:
        nonlocal quote
        if quote:
            said = " ".join(quote)
            blocks.append(_Block(f"{QUOTE_PREFIX} {said} {QUOTE_SUFFIX}", quote_start, quote_end))
            quote = []

    def flush_table() -> None:
        nonlocal in_table
        if in_table:
            blocks.append(_Block(TABLE_MARKER, table_start, table_end))
            in_table = False

    # Every block boundary closes all three: a heading straight after a
    # quotation ends the quotation, not merely the prose that is not running.
    def flush_all() -> None:
        flush_prose()
        flush_quote()
        flush_table()

    for raw in text.splitlines(keepends=True):
        start = offset
        offset += len(raw)
        line = raw.rstrip("\n")

        if _FENCE.match(line):
            if in_fence:
                blocks.append(_Block(CODE_BLOCK_MARKER, fence_start, offset))
                in_fence = False
            else:
                flush_all()
                in_fence = True
                fence_start = start
            continue
        if in_fence:
            continue
        if _RULE.match(line):
            flush_all()
            continue

        heading = _HEADING.match(line)
        if heading:
            flush_all()
            rendered = _terminate(_inline(heading.group(1)))
            if rendered:
                blocks.append(_Block(rendered, start, offset))
            continue

        item = _BULLET.match(line) or _ORDERED.match(line)
        if item:
            flush_all()
            rendered = _terminate(_inline(item.group(1)))
            if rendered:
                blocks.append(_Block(rendered, start, offset))
            continue

        # Ordering carries the rule: a table row reaching the prose branch
        # would be read out as pipes. The flush below is where a table ends,
        # there being no closing delimiter to wait for -- likewise the quote.
        if _TABLE_ROW.match(line):
            flush_prose()
            flush_quote()
            if not in_table:
                in_table = True
                table_start = start
            table_end = offset
            continue
        flush_table()

        quoted = _QUOTE.match(line)
        if quoted:
            flush_prose()
            rendered = _terminate(_inline(quoted.group(1)))
            if rendered:
                if not quote:
                    quote_start = start
                quote_end = offset
                quote.append(rendered)
            continue
        flush_quote()

        rendered = _inline(line).strip()
        if not rendered:
            flush_prose()
            continue
        if not prose:
            prose_start = start
        prose_end = offset
        prose.append(rendered)

    flush_all()
    # An unterminated fence still swallowed its content; say so rather than
    # silently dropping it.
    if in_fence:
        blocks.append(_Block(CODE_BLOCK_MARKER, fence_start, offset))
    return blocks


def markdown(pieces: Sequence[Piece]) -> list[Piece]:
    """Render markdown as speech, one Piece per block.

    One in, many out: the segmenter splits on sentence boundaries only, so a
    single joined Piece gave a listener no pause between paragraphs. Blocks
    are those pauses.
    """
    out: list[Piece] = []
    for piece in pieces:
        for block in _blocks(piece.spoken):
            if not block.text.strip():
                continue
            # Sub-spans are only meaningful while `spoken` is still the
            # verbatim source at `span`. An upstream transform that rewrote it
            # leaves offsets that no longer correspond, and a wrong span is
            # worse than a coarse one -- it highlights the wrong words
            # silently. See Piece.exact in model.py.
            span = (
                Span(piece.span.start + block.start, piece.span.start + block.end)
                if piece.exact
                else piece.span
            )
            out.append(Piece(span=span, spoken=block.text, exact=False))
    return out
