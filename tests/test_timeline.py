"""Tests for the span-to-time map."""

import pytest

from speakd.model import Segment, Span
from speakd.timeline import Timeline


def seg(start: int, end: int, offset: float, duration: float) -> Segment:
    return Segment(span=Span(start, end), text="x", audio_offset=offset, duration=duration)


def test_empty_timeline_has_no_duration_and_no_spans() -> None:
    t = Timeline()
    assert len(t) == 0
    assert t.duration == 0.0
    assert t.span_at(0.0) is None
    assert t.time_of(0) is None


def test_span_at_returns_the_span_being_spoken() -> None:
    t = Timeline()
    t.append(seg(0, 5, 0.0, 1.0))
    t.append(seg(5, 12, 1.0, 2.0))
    assert t.span_at(0.5) == Span(0, 5)
    assert t.span_at(1.5) == Span(5, 12)


def test_boundary_belongs_to_the_later_segment() -> None:
    t = Timeline()
    t.append(seg(0, 5, 0.0, 1.0))
    t.append(seg(5, 12, 1.0, 2.0))
    assert t.span_at(1.0) == Span(5, 12)


def test_span_at_past_the_end_is_none() -> None:
    t = Timeline()
    t.append(seg(0, 5, 0.0, 1.0))
    assert t.span_at(1.0) is None
    assert t.span_at(99.0) is None


def test_time_of_returns_when_an_offset_is_spoken() -> None:
    t = Timeline()
    t.append(seg(0, 5, 0.0, 1.0))
    t.append(seg(5, 12, 1.0, 2.0))
    assert t.time_of(0) == 0.0
    assert t.time_of(4) == 0.0
    assert t.time_of(5) == 1.0


def test_time_of_unknown_offset_is_none() -> None:
    t = Timeline()
    t.append(seg(0, 5, 0.0, 1.0))
    assert t.time_of(50) is None


def test_duration_is_the_end_of_the_last_segment() -> None:
    t = Timeline()
    t.append(seg(0, 5, 0.0, 1.0))
    t.append(seg(5, 12, 1.0, 2.5))
    assert t.duration == pytest.approx(3.5)


def test_append_rejects_out_of_order_segments() -> None:
    t = Timeline()
    t.append(seg(0, 5, 0.0, 1.0))
    with pytest.raises(ValueError, match="playback order"):
        t.append(seg(5, 12, 0.5, 1.0))


def test_append_rejects_negative_duration() -> None:
    t = Timeline()
    with pytest.raises(ValueError, match="duration"):
        t.append(seg(0, 5, 0.0, -1.0))


def test_played_at_defaults_to_none() -> None:
    segment = seg(0, 5, 0.0, 1.0)
    assert segment.played_at is None


def test_a_segment_can_carry_a_playback_stamp() -> None:
    from speakd.model import Segment, Span

    stamped = Segment(span=Span(0, 5), text="x", audio_offset=0.0, duration=1.0, played_at=12.5)
    assert stamped.played_at == 12.5
