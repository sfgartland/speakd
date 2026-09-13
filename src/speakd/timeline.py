"""The span-to-time map.

Built incrementally as synthesis streams, then queried in both directions:
playback position to source offset (for highlighting and resume), and source
offset to playback time (for seeking).
"""

from __future__ import annotations

from speakd.model import Segment, Span


class Timeline:
    """Bidirectional map between source spans and playback time."""

    def __init__(self) -> None:
        self._segments: list[Segment] = []

    def append(self, segment: Segment) -> None:
        raise NotImplementedError

    def span_at(self, audio_time: float) -> Span | None:
        """Which source span is being spoken at `audio_time`."""
        raise NotImplementedError

    def time_of(self, offset: int) -> float | None:
        """When source `offset` is spoken."""
        raise NotImplementedError
