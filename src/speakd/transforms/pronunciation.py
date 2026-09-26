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

Two orderings in `_rewrite` are load-bearing and will not survive being
rearranged for tidiness. The em dash is taken before the literal table,
because that table rewrites "..." and "->" and either could otherwise carve a
dash out of a run of punctuation this rule has not seen yet. And the path rule
runs before the filename rule, because the filename rule would otherwise
rewrite the extension inside a path and leave its "dot" sitting beside the
comma the path rule puts there.
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

# "speakable()" reaches the engine with its parentheses intact and is spoken
# as such. Only the empty pair: a call with arguments in prose is rare, and
# stripping its brackets would weld the arguments onto the name.
_EMPTY_CALL = re.compile(r"(?<=\w)\(\)")

# A path is a run of name-and-slash with no spaces. Read literally the engine
# says "slash" between every segment; as commas it reads as the list of names
# it is. A leading "~/" or "./" carries no information a listener wants, and a
# hidden name keeps its dot as a word -- "dot claude" is how one is said aloud,
# and a bare dot would read as a sentence boundary mid-segment, which is the
# same damage the filename rule exists to undo.
_PATH = re.compile(r"(?<![\w:/])(?:~/|\./)?(?:[\w.@+-]+/)+[\w.@+-]+")

# A URL is exempt from every rule in this module, which is why the exemption
# sits above them all rather than inside the path rule: three of them bite a
# URL independently. The path rule takes its host and directories, the
# filename rule takes the dot out of "example.com" even when the path rule has
# already declined the whole token, and the range rule is free to rewrite
# digits in a query string. A URL read back as separated names is not
# something a listener can type, so none of that is a trade worth making.
_URL = re.compile(r"(\w+://\S+)")

# An em dash between clauses is a prosodic break the engine does not take on
# its own. The surrounding whitespace goes with it, or the comma arrives with
# a space in front of it.
_EM_DASH = re.compile(r"\s*—\s*")

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
#      underscore or further dash, and no dot except one at the very end
#      that is sentence punctuation rather than a decimal. The lookbehind
#      still refuses any dot touching the left operand ("2.1-2.2", "v1-2"),
#      but the lookahead allows a trailing dot that is not followed by a
#      digit, so a sentence-final range keeps its full stop ("34-38." is a
#      range, "34-38.5" is not). That lookaround pair is what excludes
#      ISBNs, ISO dates and full phone numbers (all longer dash chains)
#      along with version strings and decimal ranges.
#
# A name that merely ends in a number — "COVID-19", "GPT-4", "Kokoro-82M" — is
# excluded a step earlier, by the requirement of digits on *both* sides; there
# is no test for those because no slip in this rule could reach them.
_NUMERIC_RANGE = re.compile(
    r"(?<![\w.–−-])"
    r"(\d{1,4})"
    r"(?:[-–−]|[ ][-–−][ ])"
    r"(\d{1,4})"
    r"(?![\w–−-]|[.]\d)"
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


def _spoken_path(match: re.Match[str]) -> str:
    """Read a path as the list of names it is."""
    token = match.group(0)
    for prefix in ("~/", "./"):
        if token.startswith(prefix):
            token = token[len(prefix) :]
            break
    if token.startswith("."):
        token = "dot " + token[1:]
    return token.replace("/", ", ")


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
    (re.compile(r"\bGUI\b"), "gooey"),
    (re.compile(r"\bHTML\b"), "H T M L"),
    (re.compile(r"\bTTS\b"), "T T S"),
    (re.compile(r"\bPDF\b"), "P D F"),
    (re.compile(r"\bXDG\b"), "X D G"),
    (re.compile(r"\bRTF\b"), "R T F"),
    (re.compile(r"\bOCR\b"), "O C R"),
    (re.compile(r"\bCSV\b"), "C S V"),
    (re.compile(r"\bSSH\b"), "S S H"),
    (re.compile(r"\bCPU\b"), "C P U"),
    (re.compile(r"\bGPU\b"), "G P U"),
    (re.compile(r"\bIDE\b"), "I D E"),
    (re.compile(r"\bCI\b"), "C I"),
    (re.compile(r"\bSQL\b", re.I), "sequel"),
    (re.compile(r"\bURLs\b"), "U R L s"),
    (re.compile(r"\bURL\b"), "U R L"),
    (re.compile(r"\bUUID\b", re.I), "you you I D"),
    (re.compile(r"\bPyPI\b"), "pie P I"),
    (re.compile(r"\bOAuth\b", re.I), "oh auth"),
    (re.compile(r"\bCORS\b"), "cores"),
    (re.compile(r"\bREPL\b"), "repple"),
    # "pp." is "pages", and espeak reads the letters "pee pee" otherwise.
    # A literal-table entry is out of the question: substring replacement
    # would rewrite the tail of "app." as well. Here the \b keeps it off
    # "app.", the lookahead keeps it out of runs like "pp.e.g.", and the
    # trailing space — collapsed back down by the final whitespace
    # normalisation when the source already had one — keeps "pp.34" from
    # concatenating into "pages34".
    (re.compile(r"\bpp\.(?![A-Za-z])"), "pages "),
)


def _rewrite(text: str) -> str:
    # Ranges first: the literal table rewrites "->" and "..." and there is no
    # reason to let either reach a range before this rule has seen it.
    text = _NUMERIC_RANGE.sub(_spoken_range, text)
    # Ahead of that table for the same reason: it is free to carve a piece out
    # of a run of punctuation before this rule has seen the dash in it.
    text = _EM_DASH.sub(", ", text)
    text = _EMPTY_CALL.sub("", text)
    for needle, replacement in _LITERAL:
        text = text.replace(needle, replacement)
    # Before _FILENAME, which would otherwise rewrite the extension inside a
    # path and leave a "dot" for this rule's comma to land beside.
    text = _PATH.sub(_spoken_path, text)
    text = _FILENAME.sub(r"\1 dot \2", text)
    text = _CHAINED.sub(r"\1 dot \2", text)
    for pattern, replacement in _WORDS:
        text = pattern.sub(replacement, text)
    return text


def _apply(text: str) -> str:
    # Splitting on a capturing pattern hands the delimiters back too, so the
    # odd-numbered parts are the URLs and go through untouched. Whitespace is
    # normalised once, at the end: the literal table pads its replacements and
    # relies on that collapse, and the parts have to be rejoined verbatim or
    # the spaces that bound a URL vanish with it.
    rewritten = "".join(
        part if index % 2 else _rewrite(part) for index, part in enumerate(_URL.split(text))
    )
    return " ".join(rewritten.split())


def pronunciation(pieces: Sequence[Piece]) -> list[Piece]:
    """Apply the substitution table, marking every result inexact."""
    out: list[Piece] = []
    for piece in pieces:
        spoken = _apply(piece.spoken)
        if spoken.strip():
            out.append(Piece(span=piece.span, spoken=spoken, exact=False))
    return out
