"""Tests for sentence and clause segmentation."""

import threading

import pytest

from speakd.model import Piece, Span
from speakd.segmenter import DEFAULT_MAX_CHARS, segment


def piece(text: str, start: int = 0) -> Piece:
    return Piece(span=Span(start, start + len(text)), spoken=text)


def assert_spans_are_sound(source: str, parent: Piece, out: list[Piece]) -> None:
    """Every returned span sits inside the parent, and exact ones still match.

    The multi-tier path composes three offsets — sentence, then clause, then
    word — so this is where an arithmetic slip would hide.
    """
    for p in out:
        assert parent.span.start <= p.span.start <= p.span.end <= parent.span.end, (
            f"{p.spoken!r} escaped the parent span"
        )
        if p.exact:
            assert source[p.span.start : p.span.end].strip() == p.spoken, (
                f"span of {p.spoken!r} points at {source[p.span.start : p.span.end]!r}"
            )


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
    # Every fallback case below starts with a short sentence, so the long one
    # sits at a non-zero sentence-tier offset, and a piece offset into a
    # longer source. Both terms of the offset composition therefore have to be
    # right for the spans to land — dropping either is caught, not absorbed.
    prefix = "Skip. "
    body = "Lead in. alpha alpha alpha, beta beta beta; gamma gamma gamma"
    source = prefix + body
    parent = piece(body, start=len(prefix))
    out = segment([parent], max_chars=30)
    assert [p.spoken for p in out] == [
        "Lead in.",
        "alpha alpha alpha,",
        "beta beta beta;",
        "gamma gamma gamma",
    ]
    assert_spans_are_sound(source, parent, out)


def test_a_long_clause_falls_through_to_word_splitting() -> None:
    # Both clauses are still over the limit, so this runs the full
    # sentence -> clause -> word offset composition.
    prefix = "Skip. "
    body = "Lead in. " + "alpha " * 20 + ", " + "beta " * 20 + "."
    source = prefix + body
    parent = piece(body, start=len(prefix))
    out = segment([parent], max_chars=60)
    assert len(out) > 2
    assert all(len(p.spoken) <= 60 for p in out)
    assert_spans_are_sound(source, parent, out)


def test_unbroken_run_is_split_at_word_boundaries() -> None:
    prefix = "Skip this. "
    body = "Lead in. " + "word " * 100
    source = prefix + body
    parent = piece(body, start=len(prefix))
    out = segment([parent], max_chars=40)
    assert all(len(p.spoken) <= 40 for p in out)
    assert "".join(p.spoken.replace(" ", "") for p in out) == body.replace(" ", "")
    assert_spans_are_sound(source, parent, out)


def test_whitespace_only_input_produces_nothing() -> None:
    assert segment([piece("   \n  ")]) == []


def test_default_max_chars_is_exported() -> None:
    assert DEFAULT_MAX_CHARS == 180


@pytest.mark.parametrize("max_chars", [0, -5])
def test_max_chars_below_one_raises_instead_of_looping_forever(max_chars: int) -> None:
    """The guard is what stops `_split_words` spinning at max_chars < 1.

    Run it on a throwaway daemon thread and join with a timeout, so a
    regression that removes the guard fails this assertion in five seconds
    instead of wedging the whole suite with no timeout and no output.
    """
    outcome: list[BaseException | None] = []

    def run() -> None:
        try:
            segment([piece("word " * 10)], max_chars)
        except BaseException as exc:  # recorded here, asserted on by the caller
            outcome.append(exc)
        else:
            outcome.append(None)

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    worker.join(timeout=5.0)

    assert not worker.is_alive(), "segment() did not return: the max_chars guard regressed"
    assert isinstance(outcome[0], ValueError)


def test_a_length_preserving_rewrite_is_not_mistaken_for_the_source() -> None:
    # "cafe" for "café" is the same length, so the old length heuristic saw
    # an untouched piece and handed out sub-spans into text that no longer
    # matched the source. The flag is what carries the rewrite.
    source = "Le café est ouvert. Il ferme a six."
    spoken = "Le cafe est ouvert. Il ferme a six."
    assert len(spoken) == len(source), "the length heuristic must be fooled here"

    parent = Piece(span=Span(0, len(source)), spoken=spoken, exact=False)
    out = segment([parent])

    assert [p.spoken for p in out] == ["Le cafe est ouvert.", "Il ferme a six."]
    assert all(p.span == parent.span for p in out), "sub-pieces must inherit the parent span"
    assert all(p.exact is False for p in out)


def test_pieces_are_exact_by_default_and_sub_pieces_stay_exact() -> None:
    source = "One. Two."
    out = segment([piece(source)])
    assert all(p.exact for p in out)
    assert_spans_are_sound(source, piece(source), out)
