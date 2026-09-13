"""Render markdown as speech.

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

from speakd.model import Piece

CODE_BLOCK_MARKER = "Code block omitted."

_FENCE = re.compile(r"^\s*(```|~~~)")
_HEADING = re.compile(r"^\s*#{1,6}\s+(.*)$")
_BULLET = re.compile(r"^\s*[-*+]\s+(.*)$")
_ORDERED = re.compile(r"^\s*\d+[.)]\s+(.*)$")
_QUOTE = re.compile(r"^\s*>\s?(.*)$")
_RULE = re.compile(r"^\s*([-*_])\s*(\1\s*){2,}$")
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


def _render(text: str) -> str:
    parts: list[str] = []
    in_fence = False
    for line in text.splitlines():
        if _FENCE.match(line):
            if in_fence:
                parts.append(CODE_BLOCK_MARKER)
            in_fence = not in_fence
            continue
        if in_fence or _RULE.match(line):
            continue
        heading = _HEADING.match(line)
        if heading:
            parts.append(_terminate(_inline(heading.group(1))))
            continue
        item = _BULLET.match(line) or _ORDERED.match(line)
        if item:
            parts.append(_terminate(_inline(item.group(1))))
            continue
        quote = _QUOTE.match(line)
        if quote:
            line = quote.group(1)
        rendered = _inline(line).strip()
        if rendered:
            parts.append(rendered)
    # An unterminated fence still swallowed its content; say so rather than
    # silently dropping it.
    if in_fence:
        parts.append(CODE_BLOCK_MARKER)
    return " ".join(p for p in parts if p)


def markdown(pieces: Sequence[Piece]) -> list[Piece]:
    """Render each piece's markdown as speech, dropping pieces that render empty."""
    out: list[Piece] = []
    for piece in pieces:
        rendered = _render(piece.spoken)
        if rendered.strip():
            out.append(Piece(span=piece.span, spoken=rendered, exact=False))
    return out
