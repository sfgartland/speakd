"""Tests for applying a chain of transforms."""

from collections.abc import Sequence

from speakd.model import Piece, Span
from speakd.plugins.host import RegisteredTransform
from speakd.transforms.chain import apply_chain


def registered(name: str, fn, scope: str = "piece") -> RegisteredTransform:  # type: ignore[no-untyped-def]
    return RegisteredTransform(name=name, fn=fn, scope=scope, plugin="test")


def upper(pieces: Sequence[Piece]) -> Sequence[Piece]:
    return [Piece(span=p.span, spoken=p.spoken.upper(), exact=False) for p in pieces]


def exclaim(pieces: Sequence[Piece]) -> Sequence[Piece]:
    return [Piece(span=p.span, spoken=p.spoken + "!", exact=False) for p in pieces]


def boom(pieces: Sequence[Piece]) -> Sequence[Piece]:
    raise RuntimeError("transform failed")


def one(text: str) -> list[Piece]:
    return [Piece(span=Span(0, len(text)), spoken=text)]


def test_an_empty_chain_returns_the_input() -> None:
    result = apply_chain(one("hello"), [])
    assert [p.spoken for p in result.pieces] == ["hello"]
    assert result.errors == []


def test_transforms_apply_in_order() -> None:
    result = apply_chain(one("hi"), [registered("u", upper), registered("e", exclaim)])
    assert [p.spoken for p in result.pieces] == ["HI!"]


def test_a_failing_transform_is_skipped_and_its_input_survives() -> None:
    chain = [registered("u", upper), registered("b", boom), registered("e", exclaim)]
    result = apply_chain(one("hi"), chain)
    assert [p.spoken for p in result.pieces] == ["HI!"]
    assert len(result.errors) == 1
    assert "b" in result.errors[0] and "transform failed" in result.errors[0]


def test_a_transform_returning_the_wrong_type_is_skipped() -> None:
    result = apply_chain(one("hi"), [registered("bad", lambda pieces: "not pieces")])
    assert [p.spoken for p in result.pieces] == ["hi"]
    assert len(result.errors) == 1


def test_a_transform_may_drop_pieces() -> None:
    result = apply_chain(one("hi"), [registered("drop", lambda pieces: [])])
    assert result.pieces == []
    assert result.errors == []


def test_spans_survive_the_chain() -> None:
    result = apply_chain(one("hello"), [registered("u", upper)])
    assert result.pieces[0].span == Span(0, 5)
    assert result.pieces[0].exact is False
