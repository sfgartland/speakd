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
    assert say("edit settings.json now") == "edit settings dot jason now"


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
