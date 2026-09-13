"""Render markdown as speech.

Headings and list items get terminal punctuation, because the segmenter splits
on sentence boundaries and a heading without one runs into whatever follows.
Code blocks are announced rather than read: reading punctuation aloud for
twenty lines is worse than saying nothing.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from speakd.model import Piece

CODE_BLOCK_MARKER = "Code block omitted."

_FENCE = re.compile(r"^\s*(```|~~~)")
_INDENT_CODE = re.compile(r"^ {4,}\S")
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
    in_indent_code = False
    # Only a blank line can open an indented code block; without this, an
    # ordinary indented continuation line right after prose would be mistaken
    # for one.
    prev_blank = True
    for line in text.splitlines():
        is_blank = not line.strip()
        if _FENCE.match(line):
            if in_fence:
                parts.append(CODE_BLOCK_MARKER)
            in_fence = not in_fence
            prev_blank = is_blank
            continue
        if in_fence or _RULE.match(line):
            prev_blank = is_blank
            continue
        if in_indent_code:
            if is_blank:
                # Could still be a blank line inside the block; wait for the
                # next non-blank line to decide whether the run continues.
                continue
            if _INDENT_CODE.match(line):
                prev_blank = False
                continue
            parts.append(CODE_BLOCK_MARKER)
            in_indent_code = False
            # Fall through: this line is not part of the block.
        heading = _HEADING.match(line)
        if heading:
            parts.append(_terminate(_inline(heading.group(1))))
            prev_blank = is_blank
            continue
        # Bullets and ordered items are tried before indented-code detection
        # so a nested list item (itself indented 4+ spaces) is never
        # swallowed into a code block.
        item = _BULLET.match(line) or _ORDERED.match(line)
        if item:
            parts.append(_terminate(_inline(item.group(1))))
            prev_blank = is_blank
            continue
        if prev_blank and not is_blank and _INDENT_CODE.match(line):
            in_indent_code = True
            prev_blank = False
            continue
        quote = _QUOTE.match(line)
        if quote:
            line = quote.group(1)
        rendered = _inline(line).strip()
        if rendered:
            parts.append(rendered)
        prev_blank = is_blank
    # An unterminated fence or indented block still swallowed its content;
    # say so rather than silently dropping it.
    if in_fence or in_indent_code:
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
