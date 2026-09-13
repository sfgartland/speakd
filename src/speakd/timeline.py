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
    def duration(self) -> float:
        if not self._segments:
            return 0.0
        last = self._segments[-1]
        return last.audio_offset + last.duration

    def append(self, segment: Segment) -> None:
        if segment.duration < 0:
            raise ValueError("segment duration must not be negative")
        if self._segments and segment.audio_offset < self.duration - 1e-9:
            raise ValueError("segments must be appended in playback order without overlap")
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
