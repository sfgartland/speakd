"""Substitutions that make technical prose survive a TTS engine.

The table is ported from `claude-code-narrator`'s `speak.sh` (MIT licence,
Shreyas S Rao). It is good work, tuned against a real engine, and reinventing
it would have produced something worse.

The filename rule earns its place: "settings.json" spoken with the dot intact
reads as a sentence boundary, which fragments a segment mid-phrase. Two or more
characters are required before the dot so that genuine abbreviations survive,
and the extension is required to be lowercase (the convention for real file
extensions) so a no-space abbreviation before a capitalised word — "vs.Smith" —
is not mistaken for one.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from speakd.model import Piece

_LITERAL: tuple[tuple[str, str], ...] = (
    # Trailing space guards against "i.e.reasons" concatenating into
    # "that isreasons" when prose omits the space after the abbreviation;
    # the final whitespace normalisation collapses the extra space back down
    # when the source already had one.
    ("e.g.", "for example "),
    ("i.e.", "that is "),
    ("=>", " arrow "),
    ("->", " arrow "),
    ("<=", " less or equal "),
    (">=", " greater or equal "),
    ("!=", " not equal "),
    ("==", " equals "),
    ("&&", " and "),
    ("||", " or "),
    ("→", " to "),
    ("...", " "),
    ("/dev/null", "dev null"),
    ("stderr", "standard error"),
    ("stdout", "standard output"),
)

_FILENAME = re.compile(r"([A-Za-z0-9_-]{2,})\.([a-z]{1,10})")
_CHAINED = re.compile(r"(dot [A-Za-z0-9_-]+)\.([A-Za-z]{1,10})")

# Longer forms first: JSONL before JSON, HTTPS before HTTP.
_WORDS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"README", re.I), "read me"),
    (re.compile(r"JSONL", re.I), "jason L"),
    (re.compile(r"JSON", re.I), "jason"),
    (re.compile(r"YAML", re.I), "yammel"),
    (re.compile(r"TOML", re.I), "tommel"),
    (re.compile(r"HTTPS"), "H T T P S"),
    (re.compile(r"HTTP"), "H T T P"),
    (re.compile(r"APIs"), "A P I s"),
    (re.compile(r"API"), "A P I"),
    (re.compile(r"CLI"), "C L I"),
    (re.compile(r"SQL", re.I), "sequel"),
    (re.compile(r"URLs"), "U R L s"),
    (re.compile(r"URL"), "U R L"),
    (re.compile(r"UUID", re.I), "you you I D"),
    (re.compile(r"PyPI"), "pie P I"),
    (re.compile(r"OAuth", re.I), "oh auth"),
    (re.compile(r"CORS"), "cores"),
    (re.compile(r"REPL"), "repple"),
)


def _apply(text: str) -> str:
    for needle, replacement in _LITERAL:
        text = text.replace(needle, replacement)
    text = _FILENAME.sub(r"\1 dot \2", text)
    text = _CHAINED.sub(r"\1 dot \2", text)
    for pattern, replacement in _WORDS:
        text = pattern.sub(replacement, text)
    return " ".join(text.split())


def pronunciation(pieces: Sequence[Piece]) -> list[Piece]:
    """Apply the substitution table, marking every result inexact."""
    out: list[Piece] = []
    for piece in pieces:
        spoken = _apply(piece.spoken)
        if spoken.strip():
            out.append(Piece(span=piece.span, spoken=spoken, exact=False))
    return out
