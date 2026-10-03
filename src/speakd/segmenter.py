"""Splits pieces into units small enough to keep time-to-first-audio low.

Time-to-first-audio tracks the length of the first unit, so a single long
sentence would otherwise dominate it. Sentence boundaries come first because
they preserve prosody; clause and word splitting are fallbacks that only
apply when a unit is too long to wait for. A sentence only slightly over the
cap is kept whole (`WHOLE_SENTENCE_SLACK`), because the engine synthesises each
unit separately and a tiny trailing unit is heard as a strange pause. When a
split is unavoidable it is balanced: clauses are regrouped into roughly even
chunks, and word splitting cuts at the space nearest each even target instead
of filling greedily and leaving a short tail. Short consecutive sentences can
optionally be merged back into longer units (`merge_chars`).
"""

from __future__ import annotations

import re
from bisect import bisect_left
from collections.abc import Iterator, Sequence
from math import ceil

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
#
# The cap is a target for splits, not a hard wall for whole sentences: see
# WHOLE_SENTENCE_SLACK. Splits that do happen are balanced rather than greedy.
DEFAULT_MAX_CHARS = 90

# A sentence up to this multiple of max_chars is kept whole. Splitting a
# 95-character sentence at 90 yields a 90-character unit and a one-word unit,
# and since the engine synthesises units separately that tail is heard as a
# strange pause; speaking 95 characters in one go costs a fraction of a second
# of extra latency at most. Only sentences beyond the slack are split.
WHOLE_SENTENCE_SLACK = 1.25

# No chunk of a split sentence should be shorter than this, for the same reason
# as the slack: a tiny unit is an audible hiccup. A fragment under the minimum
# is absorbed into a neighbour even if that neighbour then reaches the slack
# limit. Scaled down for small caps (see `_min_fragment`) so a deliberately
# tiny max_chars still splits at every clause.
MIN_FRAGMENT_CHARS = 24

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
# follows, so "cap. 3" -- a word merely ending in p, which the lookbehind keeps
# this off -- still ends a sentence.
_PAGE_ABBREVIATION = re.compile(r"(?<!\w)pp?\.")
# What may follow an ellipsis for the sentence to carry on through it:
# "Wait... what", "It was... 3 of them", "Well..., maybe".
_CONTINUES = re.compile(r"[0-9,;]")
_WORD = re.compile(r"[^\W_]")
_CLAUSE_END = re.compile(r"(?<=[,;:])\s+")

# (offset in the text, text, whether it is a whole sentence). Only whole
# sentences are candidates for merging; fragments of a split one are not.
_Unit = tuple[int, str, bool]


def is_only_dots(text: str) -> bool:
    """Whether `text` is nothing but dots, ellipsis characters and spaces."""
    return not text.strip(". …")


def _ends_with_page_abbreviation(text: str, end: int) -> bool:
    # Anchored on the few characters before `end`, never the whole prefix: this
    # runs once per candidate break, and a book's worth of them must stay linear.
    for width in (3, 2):
        if end >= width:
            match = _PAGE_ABBREVIATION.match(text, end - width, end)
            if match is not None and match.end() == end:
                return True
    return False


def _sentence_breaks(text: str) -> list[tuple[int, int]]:
    """The whitespace runs at which `text` really is cut into sentences.

    An ellipsis is at most one break and always stays at the end of the
    sentence it closes, and it never makes a unit of nothing but punctuation:
    a candidate break with no word on either side is no break, which is also
    what keeps the spaces inside ". . ." from each counting.

    Every check is positional and bounded -- no slicing of the prefix or the
    rest of the text per candidate -- so this is linear in the text.
    """
    inside_ellipsis: set[int] = set()
    for run in _ELLIPSIS.finditer(text):
        inside_ellipsis.update(range(run.start(), run.end()))
    last_word = len(text) - 1
    while last_word >= 0 and not text[last_word].isalnum():
        last_word -= 1
    breaks: list[tuple[int, int]] = []
    last = 0
    first_word = _WORD.search(text, last)
    for match in _SENTENCE_END.finditer(text):
        start, end = match.span()
        if start - 1 in inside_ellipsis and start in inside_ellipsis:
            continue  # a space inside a spaced ellipsis
        if start - 1 in inside_ellipsis:
            following = text[end : end + 1]
            if following.islower() or _CONTINUES.match(text, end):
                continue
        elif _ends_with_page_abbreviation(text, start) and text[end : end + 1].isdigit():
            continue
        words_before = first_word is not None and first_word.start() < start
        if not words_before or last_word < end:
            continue
        breaks.append((start, end))
        last = end
        first_word = _WORD.search(text, last)
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
    """Cut `text` into roughly equal chunks of at most `max_chars` at spaces.

    Filling greedily leaves a short tail ("...the global flag is" / "set."), so
    each cut aims at an even share of what remains: remaining / ceil(remaining
    / max_chars), snapped to the nearest space that keeps the chunk within the
    limit. Recomputing from what remains, rather than fixing targets up front,
    absorbs any earlier cut that landed short. A word longer than the limit has
    no space to snap to and is cut hard at the limit, which also guarantees
    every iteration advances, so this cannot spin.
    """
    length = len(text)
    start = 0
    while start < length and text[start] == " ":
        start += 1
    while start < length:
        remaining = length - start
        if remaining <= max_chars:
            end = length
        else:
            target = start + ceil(remaining / ceil(remaining / max_chars))
            limit = start + max_chars
            before = text.rfind(" ", start + 1, target + 1)
            after = text.find(" ", target, limit + 1)
            if before == -1 and after == -1:
                end = limit
            elif before == -1:
                end = after
            elif after == -1 or target - before <= after - target:
                end = before
            else:
                end = after
        chunk = text[start:end]
        if chunk.strip():
            yield base + start, chunk
        start = end
        while start < length and text[start] == " ":
            start += 1


def _min_fragment(max_chars: int) -> int:
    return min(MIN_FRAGMENT_CHARS, max_chars // 3)


def _span_len(clauses: list[tuple[int, int]], first: int, last: int) -> int:
    """Characters from the start of clause `first` to the end of clause `last - 1`."""
    return clauses[last - 1][1] - clauses[first][0]


def _group_clauses(
    clauses: list[tuple[int, int]], starts: list[int], first: int, last: int, max_chars: int
) -> list[tuple[int, int]]:
    """Regroup clauses [first, last) into roughly even chunks of at most `max_chars`.

    The number of chunks is ceil(length / max_chars) and each cut goes to the
    clause boundary nearest its even target. A lopsided text can still leave a
    chunk over the limit, so such a chunk is regrouped in turn; this ends
    because a range of two or more clauses always gets at least one cut. What
    remains over the limit is a single clause, left for word splitting.
    """
    total = _span_len(clauses, first, last)
    if last - first == 1 or total <= max_chars:
        return [(first, last)]
    n = ceil(total / max_chars)
    origin = clauses[first][0]
    cuts = [first]
    for k in range(1, n):
        target = origin + total * k / n
        at = bisect_left(starts, target, cuts[-1] + 1, last)
        options = [c for c in (at - 1, at) if cuts[-1] < c < last]
        if not options:
            break
        cuts.append(min(options, key=lambda c: abs(starts[c] - target)))
    cuts.append(last)
    groups: list[tuple[int, int]] = []
    for a, b in zip(cuts, cuts[1:], strict=False):
        groups.extend(_group_clauses(clauses, starts, a, b, max_chars))
    return groups


def _absorb_fragments(
    clauses: list[tuple[int, int]], groups: list[tuple[int, int]], max_chars: int
) -> None:
    """Merge groups shorter than the minimum into a neighbour, in place.

    The shorter of the two possible merged results wins, and only if it stays
    within the slack limit: a neighbour may grow past `max_chars` here, which
    is the point, since a slightly long unit sounds better than a tiny one.
    """
    minimum = _min_fragment(max_chars)
    limit = int(max_chars * WHOLE_SENTENCE_SLACK)
    i = 0
    while i < len(groups) and len(groups) > 1:
        first, last = groups[i]
        if _span_len(clauses, first, last) >= minimum:
            i += 1
            continue
        into_prev = _span_len(clauses, groups[i - 1][0], last) if i > 0 else limit + 1
        into_next = (
            _span_len(clauses, first, groups[i + 1][1]) if i + 1 < len(groups) else limit + 1
        )
        if min(into_prev, into_next) > limit:
            i += 1
        elif into_prev <= into_next:
            groups[i - 1] = (groups[i - 1][0], last)
            del groups[i]
        else:
            groups[i] = (first, groups[i + 1][1])
            del groups[i + 1]


def _split_sentence(offset: int, sentence: str, max_chars: int) -> Iterator[_Unit]:
    ranges: list[tuple[int, int]] = []
    for pos, clause in _split_keep_offsets(sentence, _CLAUSE_END):
        lead = len(clause) - len(clause.lstrip())
        ranges.append((pos + lead, pos + len(clause.rstrip())))
    starts = [start for start, _ in ranges]
    groups = _group_clauses(ranges, starts, 0, len(ranges), max_chars)
    _absorb_fragments(ranges, groups, max_chars)
    for first, last in groups:
        begin, end = ranges[first][0], ranges[last - 1][1]
        # More than one clause over the limit only comes from absorbing a
        # fragment, which is allowed up to the slack; a lone clause that is
        # still over the limit has nothing left to split on but words.
        if end - begin <= max_chars or last - first > 1:
            yield offset + begin, sentence[begin:end], False
        else:
            for word_offset, words in _split_words(offset + begin, sentence[begin:end], max_chars):
                yield word_offset, words, False


def _units(text: str, max_chars: int) -> Iterator[_Unit]:
    whole_limit = int(max_chars * WHOLE_SENTENCE_SLACK)
    for offset, sentence in _split_keep_offsets(text):
        if len(sentence.strip()) <= whole_limit:
            yield offset, sentence, True
        else:
            yield from _split_sentence(offset, sentence, max_chars)


def _merge(units: list[_Unit], merge_chars: int, lock_first: bool) -> list[tuple[int, int]]:
    """Ranges of `units` with runs of whole sentences merged up to `merge_chars`.

    A range runs from the first unit's offset to the end of the last one, so
    the text between them -- the original whitespace -- is kept verbatim and a
    merged unit's span covers exactly that slice. Lengths are those of the
    stripped text, tracked arithmetically so a long run costs nothing extra.
    `lock_first` keeps the first unit out of any merge.
    """
    out: list[tuple[int, int]] = []
    cur_start = cur_end = cur_spoken_start = 0
    mergeable = False
    for index, (offset, chunk, whole) in enumerate(units):
        end = offset + len(chunk)
        spoken_start = offset + len(chunk) - len(chunk.lstrip())
        spoken_end = offset + len(chunk.rstrip())
        if index > 0 and mergeable and whole and spoken_end - cur_spoken_start <= merge_chars:
            cur_end = end
            continue
        if index > 0:
            out.append((cur_start, cur_end))
        cur_start, cur_end = offset, end
        cur_spoken_start = spoken_start
        mergeable = whole and not (index == 0 and lock_first)
    if units:
        out.append((cur_start, cur_end))
    return out


def segment(
    pieces: Sequence[Piece],
    max_chars: int = DEFAULT_MAX_CHARS,
    *,
    merge_chars: int = 0,
    merge_first: bool = False,
) -> list[Piece]:
    """Split pieces into speakable units, preserving provenance where it is real.

    With `merge_chars` > 0, consecutive whole sentences of the same piece are
    merged while the merged text stays within `merge_chars`; fragments of a
    split sentence never merge, and nothing merges across pieces. Unless
    `merge_first`, the very first unit overall is left alone: time-to-first-
    audio tracks its length, so it must stay short, and merging starts from the
    second unit. `merge_chars` below `max_chars` only limits merging; it never
    causes a split.

    Raises `ValueError` if `max_chars` is below 1. `_split_words` cannot
    advance at that setting — the computed end index equals the start index
    and a non-space character never moves it — so it would spin forever.
    The guard lives here rather than only at the CLI because `speak()`
    segments on the caller's thread: a library or socket caller passing 0
    would otherwise wedge with no timeout, no exception and no log. A
    negative `merge_chars` is rejected too; 0 means no merging.
    """
    if max_chars < 1:
        raise ValueError(f"max_chars must be at least 1, got {max_chars}")
    if merge_chars < 0:
        raise ValueError(f"merge_chars must not be negative, got {merge_chars}")
    result: list[Piece] = []
    for piece in pieces:
        # The flag is the claim; the length check is a secondary guard that
        # can only downgrade it. A piece asserting `exact` whose spoken text
        # no longer spans its source range is wrong either way, so offsets
        # into it are not trusted.
        exact = piece.exact and len(piece.spoken) == piece.span.end - piece.span.start
        # A text that is nothing but an ellipsis has no sentence to attach
        # it to; it is silence either way, and an engine handed bare dots
        # may say something stranger. Dropped before merging so it cannot
        # become the anchor of a merged unit.
        units = [
            unit
            for unit in _units(piece.spoken, max_chars)
            if unit[1].strip() and not is_only_dots(unit[1].strip())
        ]
        if merge_chars > 0:
            ranges = _merge(units, merge_chars, lock_first=not merge_first and not result)
        else:
            ranges = [(offset, offset + len(chunk)) for offset, chunk, _ in units]
        for begin, end in ranges:
            spoken = piece.spoken[begin:end].strip()
            if exact:
                span = Span(piece.span.start + begin, piece.span.start + end)
            else:
                # Nothing better is available: the whole parent span is the
                # tightest honest provenance for any part of a rewrite.
                span = piece.span
            result.append(Piece(span=span, spoken=spoken, exact=exact))
    return result
