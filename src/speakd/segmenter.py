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

# Measured, 2026-09-15, against the case that exposed it: a bold lead-in, which
# renders to a very short sentence followed by a long one. The pipeline
# prefetches exactly one segment and its producer is serial, so while a short
# segment plays, the next one is still being made. Playback runs dry.
#
#   cap   silence after a 1.33s "Global wins."
#   180   +7.65s
#    90   +2.63s, and playback ahead of synthesis from the next segment on
#    60   +0.66s
#
# 60 is not the answer despite the number. A cap only forces a split when a
# sentence exceeds it, and the split falls back sentence -> clause -> word: at
# 60 the 76-character clause in that fixture no longer fits, so it breaks
# between "while the" and "global flag is set", which is a pause in the middle
# of a phrase. At 90 every split in it lands on a comma.
#
# The residual 2.63s cannot be segmented away. After a 1.33s segment there is
# 1.33s to synthesise the next, which at RTF 0.7 buys under two seconds of
# audio -- some seventeen characters. No natural sentence is that small. The
# remedy for the rest is to stop the segmentation producing a one-second unit
# in the first place, which belongs in the markdown transform, not here.
DEFAULT_MAX_CHARS = 90

# A candidate sentence break: whitespace after terminal punctuation. Which
# candidates are real is `_sentence_breaks`'s decision.
_SENTENCE_END = re.compile(r"(?<=[.!?…])\s+")
# An ellipsis in any of its spellings: three or more dots, spaced or not, or
# the single character. Matched as a run so that ". . ." is one thing rather
# than three full stops with a space after each.
_ELLIPSIS = re.compile(r"\.(?:\s*\.){2,}|…+")
# "pp. 34-38" and "p. 12" are a citation, not two sentences: breaking after the
# abbreviation cut a live utterance mid-citation and, worse, handed the
# pronunciation rule the halves of a range. Withheld only when a number
# follows, so "cap. 3" -- a word merely ending in p, which the \b keeps this
# off -- still ends a sentence.
_PAGE_ABBREVIATION = re.compile(r"\bpp?\.$")
# What may follow an ellipsis for the sentence to carry on through it:
# "Wait... what", "It was... 3 of them", "Well..., maybe".
_CONTINUES = re.compile(r"[0-9,;]")
_CLAUSE_END = re.compile(r"(?<=[,;:])\s+")


def _has_words(text: str) -> bool:
    return any(c.isalnum() for c in text)


def _sentence_breaks(text: str) -> list[tuple[int, int]]:
    """The whitespace runs at which `text` really is cut into sentences.

    An ellipsis is at most one break and always stays at the end of the
    sentence it closes, and it never makes a unit of nothing but punctuation:
    a candidate break with no word on either side is no break, which is also
    what keeps the spaces inside ". . ." from each counting.
    """
    inside_ellipsis: set[int] = set()
    for run in _ELLIPSIS.finditer(text):
        inside_ellipsis.update(range(run.start(), run.end()))
    breaks: list[tuple[int, int]] = []
    last = 0
    for match in _SENTENCE_END.finditer(text):
        start, end = match.span()
        if start - 1 in inside_ellipsis and start in inside_ellipsis:
            continue  # a space inside a spaced ellipsis
        after = text[end:]
        if start - 1 in inside_ellipsis:
            if after[:1].islower() or _CONTINUES.match(after):
                continue
        elif _PAGE_ABBREVIATION.search(text[:start]) and after[:1].isdigit():
            continue
        if not _has_words(text[last:start]) or not _has_words(after):
            continue
        breaks.append((start, end))
        last = end
    return breaks


def _split_keep_offsets(text: str, pattern: re.Pattern[str] | None = None) -> list[tuple[int, str]]:
    out: list[tuple[int, str]] = []
    pos = 0
    if pattern is None:
        spans = _sentence_breaks(text)
    else:
        spans = [m.span() for m in pattern.finditer(text)]
    for start, end in spans:
        chunk = text[pos:start]
        if chunk.strip():
            out.append((pos, chunk))
        pos = end
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
    for offset, sentence in _split_keep_offsets(text):
        if len(sentence) <= max_chars:
            yield offset, sentence
            continue
        for clause_offset, clause in _split_keep_offsets(sentence, _CLAUSE_END):
            if len(clause) <= max_chars:
                yield offset + clause_offset, clause
            else:
                yield from _split_words(offset + clause_offset, clause, max_chars)


def segment(pieces: Sequence[Piece], max_chars: int = DEFAULT_MAX_CHARS) -> list[Piece]:
    """Split pieces into speakable units, preserving provenance where it is real.

    Raises `ValueError` if `max_chars` is below 1. `_split_words` cannot
    advance at that setting — the computed end index equals the start index
    and a non-space character never moves it — so it would spin forever.
    The guard lives here rather than only at the CLI because `speak()`
    segments on the caller's thread: a library or socket caller passing 0
    would otherwise wedge with no timeout, no exception and no log.
    """
    if max_chars < 1:
        raise ValueError(f"max_chars must be at least 1, got {max_chars}")
    result: list[Piece] = []
    for piece in pieces:
        # The flag is the claim; the length check is a secondary guard that
        # can only downgrade it. A piece asserting `exact` whose spoken text
        # no longer spans its source range is wrong either way, so offsets
        # into it are not trusted.
        exact = piece.exact and len(piece.spoken) == piece.span.end - piece.span.start
        for offset, chunk in _units(piece.spoken, max_chars):
            spoken = chunk.strip()
            # A text that is nothing but an ellipsis has no sentence to attach
            # it to; it is silence either way, and an engine handed bare dots
            # may say something stranger.
            if not spoken or not spoken.strip(". …"):
                continue
            if exact:
                span = Span(piece.span.start + offset, piece.span.start + offset + len(chunk))
            else:
                # Nothing better is available: the whole parent span is the
                # tightest honest provenance for any part of a rewrite.
                span = piece.span
            result.append(Piece(span=span, spoken=spoken, exact=exact))
    return result
