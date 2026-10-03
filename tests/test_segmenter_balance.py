"""Tests for whole-sentence slack, balanced splitting and sentence merging."""

import pytest

from speakd.model import Piece, Span
from speakd.segmenter import (
    MIN_FRAGMENT_CHARS,
    WHOLE_SENTENCE_SLACK,
    _split_words,
    segment,
)


def piece(text: str, start: int = 0, exact: bool = True) -> Piece:
    return Piece(span=Span(start, start + len(text)), spoken=text, exact=exact)


def spoken(pieces: list[Piece], **kwargs: int | bool) -> list[str]:
    return [p.spoken for p in segment(pieces, **kwargs)]  # type: ignore[arg-type]


def test_a_sentence_slightly_over_the_cap_stays_whole() -> None:
    text = "a" * 4 + " " + ("bcdef " * 15) + "end."
    assert 90 < len(text) <= int(90 * WHOLE_SENTENCE_SLACK)
    assert spoken([piece(text)]) == [text.strip()]


def test_a_95_character_sentence_without_commas_stays_whole() -> None:
    text = ("abcdefgh " * 11)[:94] + "."
    assert len(text) == 95
    assert spoken([piece(text)]) == [text]


def test_a_sentence_beyond_the_slack_is_split() -> None:
    text = ("abcdefgh " * 20).strip() + "."
    out = spoken([piece(text)])
    assert len(out) >= 2
    assert all(len(u) <= 90 for u in out)


def test_a_long_sentence_leaves_no_fragment_below_the_minimum() -> None:
    clauses = [
        "the first clause is of a middling length,",
        "then a short one,",
        "and another quite lengthy clause follows here,",
        "tiny one,",
        "and the closing clause rounds the whole thing off nicely.",
    ]
    text = " ".join(clauses)
    assert len(text) > int(90 * WHOLE_SENTENCE_SLACK)
    out = spoken([piece(text)])
    assert len(out) >= 2
    assert all(len(u) >= MIN_FRAGMENT_CHARS for u in out), out
    assert all(len(u) <= int(90 * WHOLE_SENTENCE_SLACK) for u in out)
    assert " ".join(out) == text


def test_clause_groups_are_roughly_even() -> None:
    text = ", ".join(["alpha beta gamma delta"] * 10) + "."
    out = spoken([piece(text)])
    lengths = [len(u) for u in out]
    assert max(lengths) <= 90
    assert max(lengths) / min(lengths) < 1.6, lengths


def test_word_splitting_is_balanced() -> None:
    text = " ".join(["word"] * 60)  # 299 chars, so four chunks at 90
    chunks = [c for _, c in _split_words(0, text, 90)]
    lengths = [len(c) for c in chunks]
    assert len(chunks) == 4
    assert max(lengths) <= 90
    assert max(lengths) / min(lengths) < 1.2, lengths
    assert " ".join(chunks) == text


def test_word_splitting_a_slightly_long_text_has_no_short_tail() -> None:
    text = " ".join(["word"] * 19)  # 94 chars
    lengths = [len(c) for _, c in _split_words(0, text, 90)]
    assert len(lengths) == 2
    assert min(lengths) >= 40


def test_word_splitting_offsets_point_at_the_chunks() -> None:
    text = " ".join(f"w{i}" for i in range(80))
    for offset, chunk in _split_words(10, text, 30):
        assert text[offset - 10 : offset - 10 + len(chunk)] == chunk
        assert chunk.strip() == chunk


def test_a_single_overlong_word_is_cut_at_the_limit_and_advances() -> None:
    chunks = [c for _, c in _split_words(0, "x" * 25 + " yy", 10)]
    assert "".join(chunks).replace(" ", "") == "x" * 25 + "yy"
    assert all(len(c) <= 10 for c in chunks)


def test_merging_joins_short_sentences_up_to_merge_chars() -> None:
    text = "One. Two. Three. Four. Five."
    assert spoken([piece(text)], merge_chars=17, merge_first=True) == [
        "One. Two. Three.",
        "Four. Five.",
    ]


def test_merge_first_false_keeps_the_first_unit_alone() -> None:
    text = "One. Two. Three. Four. Five."
    assert spoken([piece(text)], merge_chars=17) == ["One.", "Two. Three. Four.", "Five."]


def test_merging_never_crosses_pieces() -> None:
    out = spoken([piece("One. Two."), piece("Three. Four.", start=20)], merge_chars=100)
    assert out == ["One.", "Two.", "Three. Four."]
    out = spoken(
        [piece("One. Two."), piece("Three. Four.", start=20)], merge_chars=100, merge_first=True
    )
    assert out == ["One. Two.", "Three. Four."]


def test_merged_spans_cover_the_original_slice() -> None:
    source = "Pad. One.  Two. Three. Four."
    start = len("Pad. ")
    parent = piece(source[start:], start=start)
    out = segment([parent], merge_chars=16, merge_first=True)
    assert [p.spoken for p in out] == ["One.  Two.", "Three. Four."]
    for p in out:
        assert source[p.span.start : p.span.end].strip() == p.spoken
        assert p.exact


def test_merged_units_of_a_rewrite_inherit_the_parent_span() -> None:
    parent = Piece(span=Span(0, 5), spoken="One. Two.", exact=False)
    out = segment([parent], merge_chars=50, merge_first=True)
    assert [p.spoken for p in out] == ["One. Two."]
    assert out[0].span == parent.span
    assert not out[0].exact


def test_fragments_of_a_split_sentence_are_not_merged() -> None:
    long_sentence = ", ".join(["alpha beta gamma delta"] * 10) + "."
    plain = spoken([piece(f"Hi. {long_sentence}")])
    merged = spoken([piece(f"Hi. {long_sentence}")], merge_chars=500, merge_first=True)
    assert merged == plain


def test_merging_does_not_cause_splits_when_merge_chars_is_small() -> None:
    text = "x" * 40 + " " + "y" * 40 + "."
    assert spoken([piece(text)], merge_chars=10, merge_first=True) == [text]


@pytest.mark.parametrize("merge_first", [False, True])
def test_merge_chars_zero_equals_no_merging(merge_first: bool) -> None:
    text = "One. Two. " + ", ".join(["alpha beta gamma delta"] * 10) + ". Three."
    pieces = [piece(text), piece("Four. Five.", start=len(text) + 1)]
    assert segment(pieces, merge_chars=0, merge_first=merge_first) == segment(pieces)


def test_negative_merge_chars_raises() -> None:
    with pytest.raises(ValueError, match="merge_chars"):
        segment([piece("One.")], merge_chars=-1)
