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
#
# \b on both ends because these are whole words, not substrings: without it
# every all-caps word containing an entry was mangled -- CAPITAL became
# "CA P ITAL", CURL "CU R L", REPLACE "reppleACE" -- and markdown, which runs
# first and preserves case, hands this table all-caps headings intact.
_WORDS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bREADME\b", re.I), "read me"),
    (re.compile(r"\bJSONL\b", re.I), "jason L"),
    (re.compile(r"\bJSON\b", re.I), "jason"),
    (re.compile(r"\bYAML\b", re.I), "yammel"),
    (re.compile(r"\bTOML\b", re.I), "tommel"),
    (re.compile(r"\bHTTPS\b"), "H T T P S"),
    (re.compile(r"\bHTTP\b"), "H T T P"),
    (re.compile(r"\bAPIs\b"), "A P I s"),
    (re.compile(r"\bAPI\b"), "A P I"),
    (re.compile(r"\bCLI\b"), "C L I"),
    (re.compile(r"\bSQL\b", re.I), "sequel"),
    (re.compile(r"\bURLs\b"), "U R L s"),
    (re.compile(r"\bURL\b"), "U R L"),
    (re.compile(r"\bUUID\b", re.I), "you you I D"),
    (re.compile(r"\bPyPI\b"), "pie P I"),
    (re.compile(r"\bOAuth\b", re.I), "oh auth"),
    (re.compile(r"\bCORS\b"), "cores"),
    (re.compile(r"\bREPL\b"), "repple"),
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
