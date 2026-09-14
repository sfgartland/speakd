"""The span-to-time map.

Built incrementally as synthesis streams, then queried in both directions:
playback position to source offset (for highlighting and resume), and source
offset to playback time (for seeking).
"""

from __future__ import annotations

import bisect

from speakd.model import Segment, Span


class Timeline:
    """Bidirectional map between source spans and playback time."""

    def __init__(self) -> None:
        self._segments: list[Segment] = []
        self._starts: list[float] = []

    def __len__(self) -> int:
        return len(self._segments)

    @property
    def segments(self) -> tuple[Segment, ...]:
        return tuple(self._segments)

    @property
    def duration(self) -> float:
        if not self._segments:
            return 0.0
        last = self._segments[-1]
        return last.audio_offset + last.duration

    @property
    def drift(self) -> float:
        """How far real playback has fallen behind the nominal audio clock.

        Measured elapsed minus nominal elapsed, across what has been played so
        far: `played_at` is when playback of a segment actually began, and
        `audio_offset` is when it was meant to. Both are already here, which
        is the point of keeping them apart -- a single measured number would
        have nothing to be compared against.

        Zero before anything has been played, and zero across a single
        segment, where nothing has elapsed on either clock to differ. Zero as
        well for segments that were never stamped, which is a timeline nothing
        was played from.
        """
        if not self._segments:
            return 0.0
        # Indexed rather than iterated: synthesis appends to this list from
        # the speech worker while a monitor reads it, and taking the two ends
        # cannot see a partial list the way a scan can.
        first = self._segments[0]
        last = self._segments[-1]
        if first.played_at is None or last.played_at is None:
            return 0.0
        return (last.played_at - first.played_at) - (last.audio_offset - first.audio_offset)

    def append(self, segment: Segment) -> None:
        if segment.duration < 0:
            raise ValueError("segment duration must not be negative")
        if self._segments and segment.audio_offset < self.duration - 1e-9:
            raise ValueError("segments must be appended in playback order without overlap")
        # Order is load-bearing, do not swap these two lines. A reader on
        # another thread (milestone 2 reads position while synthesis streams)
        # indexes _segments by a position found in _starts, so _segments must
        # never be shorter than _starts. Appending the segment first keeps the
        # worst case at a start that is not visible yet, rather than an index
        # into a list that does not have it.
        self._segments.append(segment)
        self._starts.append(segment.audio_offset)

    def span_at(self, audio_time: float) -> Span | None:
        """Which source span is being spoken at `audio_time`."""
        index = bisect.bisect_right(self._starts, audio_time) - 1
        if index < 0:
            return None
        segment = self._segments[index]
        if audio_time < segment.audio_offset + segment.duration:
            return segment.span
        return None

    def time_of(self, offset: int) -> float | None:
        """When source `offset` is spoken."""
        for segment in self._segments:
            if segment.span.start <= offset < segment.span.end:
                return segment.audio_offset
        return None
