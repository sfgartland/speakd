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

There is deliberately no rule for years, and that is a measured decision
rather than an omission. Against the engine this project ships (kokoro, via
the misaki phonemiser) a bare "1994" already phonemises as "nineteen
ninety-four", "1787" as "seventeen eighty-seven", "1900" as "nineteen
hundred", "1809" as "eighteen oh nine", "2020" as "twenty twenty" and "2005"
as "two thousand five" — every awkward case, correct without help.

Spelling years out was tested rather than assumed. A *hyphenated* spelling is
strictly worse: "nineteen ninety-four" fuses the compound into one word
(nˈIndifˌɔɹ) where the digits give two properly stressed words (nˈIndi fˈɔɹ).
An *unhyphenated* spelling is exactly neutral — every year from 1100 to 2099
was phonemised both ways and all 1000 pairs came back byte-identical. So on
this engine a year rule cannot change what anyone hears; it can only add a
way to be wrong, such as firing on "1500 people".

That neutrality is also why the rule is not here pre-emptively for the ONNX /
espeak engine, which does read years wrong ("nineteen hundred ninety four").
Adopting that engine would need this rule, and the speller is straightforward
(century pair; "hundred" when the last two digits are zero, "oh" when they are
under ten; "two thousand N" for 2000-2009). It belongs in the change that
adopts the engine, where its cost — firing on four-digit quantities — buys
something. Added today it buys nothing.

Citation keys are likewise absent by design, not oversight: reading
"@Stiegler-1994" as an author and a year is domain knowledge, and the design
doc assigns it to the separately installable vault pack.
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

# A numeric range — "pp. 34-38" — is the one number pattern the engine gets
# wrong on its own. It fuses the two halves into a single run with no
# separator at all ("34-38" -> θˌɜɹTi fˌɔɹθˈɜɹTi ˈAt, "four" and "thirty"
# welded together), and an en dash or minus becomes an unknown-token ❓.
# Replacing the dash with the word "to" and leaving the digits alone fixes
# both: "34 to 38" phonemises exactly as "thirty four to thirty eight" does,
# so the engine's own number reading — already correct — does the work, and
# this module never has to own a number speller that could disagree with it.
#
# The dash characters are the three that carry a range in real text: hyphen,
# en dash, and true minus. The em dash is excluded because between two numbers
# in prose it is far more often a sentence break than a range.
#
# Firing conditions, all required, chosen so the rule stays silent whenever a
# digit-dash-digit could plausibly be something other than a range. Ordinary
# numbers are much more common than citations, so a miss is cheap and a false
# fire is not:
#
#   1. one to four digits each side, no leading zeros — a leading zero means
#      an identifier or a date part ("01-15"), never a quantity;
#   2. the operands are the same length, or both are at most three digits —
#      a four-digit half paired with a shorter one is a phone number
#      ("555-1234"), not a locator. This is what keeps the rule off phone
#      numbers, and it costs us wide ranges like "950-1020";
#   3. strictly ascending — a score reads high-first ("Kant 5-3"), and
#      subtraction written in prose descends, so neither is a range;
#   4. spacing around the dash is symmetric — both sides or neither, so a
#      parenthetical "34 -38" is not swept up;
#   5. nothing in the same token touches either operand: no letter, digit,
#      underscore, dot or further dash. That lookaround pair is what excludes
#      ISBNs, ISO dates and full phone numbers (all longer dash chains) along
#      with version strings ("2.1-2.2", "v1-2") and decimal ranges.
#
# A name that merely ends in a number — "COVID-19", "GPT-4", "Kokoro-82M" — is
# excluded a step earlier, by the requirement of digits on *both* sides; there
# is no test for those because no slip in this rule could reach them.
_NUMERIC_RANGE = re.compile(
    r"(?<![\w.–−-])"
    r"(\d{1,4})"
    r"(?:[-–−]|[ ][-–−][ ])"
    r"(\d{1,4})"
    r"(?![\w.–−-])"
)


def _spoken_range(match: re.Match[str]) -> str:
    """Rewrite a matched range, or hand back the original text unchanged."""
    left, right = match.group(1), match.group(2)
    if (len(left) > 1 and left[0] == "0") or (len(right) > 1 and right[0] == "0"):
        return match.group(0)
    if len(left) != len(right) and max(len(left), len(right)) > 3:
        return match.group(0)
    if int(left) >= int(right):
        return match.group(0)
    return f"{left} to {right}"


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
    # Ranges first: the literal table rewrites "->" and "..." and there is no
    # reason to let either reach a range before this rule has seen it.
    text = _NUMERIC_RANGE.sub(_spoken_range, text)
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
