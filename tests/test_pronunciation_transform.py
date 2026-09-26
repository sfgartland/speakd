"""Tests for pronunciation substitutions."""

from speakd.model import Piece, Span
from speakd.transforms.pronunciation import pronunciation


def say(text: str) -> str:
    return pronunciation([Piece(span=Span(0, len(text)), spoken=text)])[0].spoken


def test_latin_abbreviations_are_expanded() -> None:
    assert say("e.g. this") == "for example this"
    assert say("i.e. that") == "that is that"


def test_latin_abbreviations_do_not_concatenate_with_a_following_word() -> None:
    assert say("e.g.this") == "for example this"
    assert say("(i.e.reasons)") == "(that is reasons)"


def test_filename_dots_become_the_word_dot() -> None:
    assert say("edit settings.json now") == "edit settings dot jason now"


def test_chained_extensions_are_handled() -> None:
    assert say("types.d.ts") == "types dot d dot ts"


def test_short_abbreviations_are_not_mangled_as_filenames() -> None:
    assert "dot" not in say("a.b")


def test_a_no_space_abbreviation_before_a_capital_is_not_mistaken_for_a_filename() -> None:
    assert say("vs.Smith") == "vs.Smith"


def test_lowercase_filename_extensions_still_convert() -> None:
    # A different extension from the settings.json case above, and one the word
    # table does not also rewrite, so this covers the filename rule alone.
    assert say("edit main.py now") == "edit main dot py now"


def test_arrows_and_operators_are_spoken() -> None:
    assert say("a -> b") == "a arrow b"
    assert say("x != y") == "x not equal y"


def test_acronyms_are_spelled_out() -> None:
    assert say("the API") == "the A P I"
    assert say("a URL") == "a U R L"


def test_format_names_are_pronounced() -> None:
    assert say("JSON") == "jason"
    assert say("YAML") == "yammel"
    assert say("TOML") == "tommel"


def test_longer_acronyms_win_over_shorter_ones() -> None:
    assert say("JSONL") == "jason L"
    assert say("HTTPS") == "H T T P S"


def test_ellipsis_is_collapsed() -> None:
    assert say("wait... now") == "wait now"


def test_output_is_marked_inexact() -> None:
    out = pronunciation([Piece(span=Span(0, 4), spoken="JSON")])
    assert out[0].exact is False
    assert out[0].span == Span(0, 4)


def test_plain_text_is_untouched_apart_from_spacing() -> None:
    assert say("a plain sentence") == "a plain sentence"


def test_ordinary_all_caps_words_are_not_mangled() -> None:
    """markdown runs first and preserves case, so a heading arrives all-caps."""
    assert say("CAPITAL") == "CAPITAL"
    assert say("REPLACE") == "REPLACE"
    assert say("MySQL") == "MySQL"
    assert say("CURL") == "CURL"
    assert say("CLICK") == "CLICK"
    assert say("RAPID") == "RAPID"
    assert say("CORSET") == "CORSET"


def test_acronyms_still_match_next_to_punctuation() -> None:
    assert say("the (API), and JSON.") == "the (A P I), and jason."


# --- numeric ranges -------------------------------------------------------
#
# Years are deliberately absent from these tests: the engine already reads
# them correctly, and every rewrite measured was neutral or worse. See the
# module comment in pronunciation.py.

RANGES_THAT_FIRE = [
    ("34-38", "34 to 38", "hyphen, the common page range"),
    ("34–38", "34 to 38", "en dash, what a word processor produces"),
    ("34−38", "34 to 38", "true minus sign U+2212"),
    ("34 - 38", "34 to 38", "spaced hyphen"),
    ("34 – 38", "34 to 38", "spaced en dash"),
    ("9-12", "9 to 12", "uneven digit counts, both short"),
    ("99-101", "99 to 101", "crossing a power of ten"),
    ("1787-1799", "1787 to 1799", "a year range: four digits both sides"),
    ("§12-14", "§12 to 14", "a Bekker-style section range"),
    ("34-38.", "34 to 38.", "sentence-final: the full stop is punctuation, kept"),
    ("34–38.", "34 to 38.", "sentence-final en dash keeps its full stop too"),
]


def test_numeric_ranges_are_read_as_to() -> None:
    for text, expected, why in RANGES_THAT_FIRE:
        assert say(text) == expected, f"{why}: {text!r}"


RANGES_THAT_MUST_NOT_FIRE = [
    ("555-1234", "a phone number: 4-digit half paired with a shorter one"),
    ("978-0-262-03384-8", "an ISBN: part of a longer dash chain"),
    ("2024-01-15", "an ISO date: dash chain, and a leading zero"),
    ("01-15", "leading zeros mean an identifier, not a quantity"),
    ("5-3", "a score reads high-first, so descending is not a range"),
    ("38-34", "descending: subtraction or a reversed pair"),
    ("12-12", "not ascending, so not a range"),
    ("v1-2", "a letter touches the left operand: a version"),
    ("2.1-2.2", "dots touch the operands: version numbers"),
    ("34-38a", "a letter touches the right operand"),
    ("12345-12346", "five digits: too big to be a locator"),
    ("1-800-555-1212", "a full phone number is a dash chain"),
    ("8601-1", "a standard's part number: four digits paired with one"),
    ("1.5-2.5", "a decimal range: the dots put it out of scope"),
    ("34-38.5", "a decimal continuation after the range: not a full stop"),
]


def test_things_that_look_like_ranges_but_are_not_are_left_alone() -> None:
    for text, why in RANGES_THAT_MUST_NOT_FIRE:
        assert say(text) == text, f"{why}: {text!r}"


def test_a_four_digit_year_is_left_for_the_engine_to_read() -> None:
    """misaki already says "nineteen ninety-four"; rewriting only degrades it."""
    assert say("Stiegler 1994, Technics and Time, volume 1.") == (
        "Stiegler 1994, Technics and Time, volume 1."
    )
    assert say("The survey covered 1500 people.") == "The survey covered 1500 people."


def test_a_citation_range_survives_the_rest_of_the_table() -> None:
    assert say("See §12, e.g. pp. 34-38, ca. 1787.") == (
        "See §12, for example pages 34 to 38, ca. 1787."
    )


def test_pp_is_read_as_pages() -> None:
    assert say("pp. 34-38") == "pages 34 to 38"


def test_pp_does_not_bite_a_word_ending_in_pp() -> None:
    assert say("app. 34") == "app. 34"


def test_a_sentence_final_range_is_still_rewritten() -> None:
    assert say("See pp. 34–38.") == "See pages 34 to 38."


# --- paths, calls, dashes -------------------------------------------------


def test_an_empty_call_loses_its_parentheses() -> None:
    assert say("Call speakable() first.") == "Call speakable first."


def test_a_path_is_read_as_separated_names() -> None:
    assert say("~/.claude/settings.json") == "dot claude, settings dot jason"


def test_a_leading_dot_slash_goes_too() -> None:
    assert say("./src/speakd") == "src, speakd"


def test_a_url_is_left_alone() -> None:
    # Speaking "https, , example dot com" is worse than speaking the URL.
    assert "https://example.com/x" in say("See https://example.com/x now.")


def test_an_em_dash_becomes_a_comma() -> None:
    assert say("One thing — and another.") == "One thing, and another."


def test_gui_is_pronounced() -> None:
    assert say("the GUI") == "the gooey"


def test_the_new_acronyms_are_spelled_out() -> None:
    assert say("PDF") == "P D F"
    assert say("CPU") == "C P U"


def test_a_url_survives_the_filename_rule_too() -> None:
    # The path rule declines this URL on its own, so the domain would still
    # reach the filename rule as "example dot com" if the exemption lived
    # inside the path rule rather than above every rule.
    assert say("Fetch https://example.com/a/b.json now.") == (
        "Fetch https://example.com/a/b.json now."
    )


def test_prose_around_a_url_is_still_rewritten() -> None:
    assert say("The API at https://example.com/x — see settings.json.") == (
        "The A P I at https://example.com/x, see settings dot jason."
    )
