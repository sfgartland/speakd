"""Tests for sentence and clause segmentation."""

from speakd.model import Piece, Span
from speakd.segmenter import DEFAULT_MAX_CHARS, segment


def piece(text: str, start: int = 0) -> Piece:
    return Piece(span=Span(start, start + len(text)), spoken=text)


def test_splits_on_sentence_boundaries() -> None:
    out = segment([piece("One. Two. Three.")])
    assert [p.spoken for p in out] == ["One.", "Two.", "Three."]


def test_unrewritten_pieces_get_exact_spans() -> None:
    source = "One. Two."
    out = segment([piece(source)])
    assert [source[p.span.start : p.span.end].strip() for p in out] == ["One.", "Two."]


def test_rewritten_pieces_inherit_the_parent_span() -> None:
    # spoken length differs from the span length, so offsets cannot be trusted
    parent = Piece(span=Span(0, 14), spoken="Stiegler, nineteen ninety-four. And more.")
    out = segment([parent])
    assert len(out) == 2
    assert all(p.span == Span(0, 14) for p in out)


def test_long_sentence_is_split_at_clause_boundaries() -> None:
    text = "alpha " * 20 + ", " + "beta " * 20 + "."
    out = segment([piece(text)], max_chars=60)
    assert len(out) > 1
    assert all(len(p.spoken) <= 60 for p in out)


def test_unbroken_run_is_split_at_word_boundaries() -> None:
    text = "word " * 100
    out = segment([piece(text)], max_chars=40)
    assert all(len(p.spoken) <= 40 for p in out)
    assert "".join(p.spoken.replace(" ", "") for p in out) == text.replace(" ", "")


def test_whitespace_only_input_produces_nothing() -> None:
    assert segment([piece("   \n  ")]) == []


def test_default_max_chars_is_exported() -> None:
    assert DEFAULT_MAX_CHARS == 180
