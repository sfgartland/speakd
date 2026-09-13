"""Splits pieces into units small enough to keep time-to-first-audio low.

Time-to-first-audio tracks the length of the first unit, so a single long
sentence would otherwise dominate it. Sentence boundaries come first because
they preserve prosody; clause and word splitting are fallbacks that only
apply when a unit is too long to wait for.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Sequence

from speakd.model import Piece, Span

DEFAULT_MAX_CHARS = 180

_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")
_CLAUSE_END = re.compile(r"(?<=[,;:])\s+")


def _split_keep_offsets(text: str, pattern: re.Pattern[str]) -> list[tuple[int, str]]:
    out: list[tuple[int, str]] = []
    pos = 0
    for match in pattern.finditer(text):
        chunk = text[pos : match.start()]
        if chunk.strip():
            out.append((pos, chunk))
        pos = match.end()
    tail = text[pos:]
    if tail.strip():
        out.append((pos, tail))
    return out


def _split_words(base: int, text: str, max_chars: int) -> Iterator[tuple[int, str]]:
    start = 0
    length = len(text)
    while start < length:
        end = min(start + max_chars, length)
        if end < length:
            cut = text.rfind(" ", start + 1, end + 1)
            if cut != -1:
                end = cut
        chunk = text[start:end]
        if chunk.strip():
            yield base + start, chunk
        start = end
        while start < length and text[start] == " ":
            start += 1


def _units(text: str, max_chars: int) -> Iterator[tuple[int, str]]:
    for offset, sentence in _split_keep_offsets(text, _SENTENCE_END):
        if len(sentence) <= max_chars:
            yield offset, sentence
            continue
        for clause_offset, clause in _split_keep_offsets(sentence, _CLAUSE_END):
            if len(clause) <= max_chars:
                yield offset + clause_offset, clause
            else:
                yield from _split_words(offset + clause_offset, clause, max_chars)


def segment(pieces: Sequence[Piece], max_chars: int = DEFAULT_MAX_CHARS) -> list[Piece]:
    """Split pieces into speakable units, preserving provenance where it is real."""
    result: list[Piece] = []
    for piece in pieces:
        exact = len(piece.spoken) == piece.span.end - piece.span.start
        for offset, chunk in _units(piece.spoken, max_chars):
            spoken = chunk.strip()
            if not spoken:
                continue
            if exact:
                span = Span(piece.span.start + offset, piece.span.start + offset + len(chunk))
            else:
                span = piece.span
            result.append(Piece(span=span, spoken=spoken))
    return result
