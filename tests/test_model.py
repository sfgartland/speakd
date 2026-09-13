"""Smoke tests for the core data model."""

from speakd.model import Job, Role, Segment, Span


def test_span_is_hashable_and_frozen() -> None:
    assert Span(0, 5) == Span(0, 5)
    assert len({Span(0, 5), Span(0, 5)}) == 1


def test_segment_carries_source_and_time() -> None:
    seg = Segment(span=Span(0, 5), text="Hello", audio_offset=0.0, duration=0.4)
    assert seg.span.end == 5
    assert seg.duration == 0.4


def test_job_defaults_to_foreground_profile() -> None:
    job = Job(source_id="session:abc", text="Hello")
    assert job.profile == "default"
    assert job.segments == []
    assert Role.FOREGROUND.value == "foreground"


def test_piece_keeps_provenance_when_rewritten() -> None:
    from dataclasses import replace

    from speakd.model import Piece

    original = Piece(span=Span(0, 14), spoken="@Stiegler-1994")
    rewritten = replace(original, spoken="Stiegler, nineteen ninety-four")
    assert rewritten.span == original.span
