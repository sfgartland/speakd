"""Core data model: jobs, segments, and the span-to-time map.

These are the types every other module speaks in. Deliberately free of
behaviour so the transport, the synthesiser and the plugin host can be
designed and tested against them independently.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Role(str, Enum):
    """How much of a channel's output reaches the speakers."""

    FOREGROUND = "foreground"
    BACKGROUND = "background"


@dataclass(frozen=True)
class Span:
    """A half-open range of offsets into a job's source text."""

    start: int
    end: int


@dataclass(frozen=True)
class Segment:
    """One synthesised unit, and where it came from.

    `span` indexes the job's source text; `audio_offset` and `duration` are
    seconds relative to the start of the job's audio. Together these form the
    span-to-time map that position events, seeking and resume all read from.
    """

    span: Span
    text: str
    audio_offset: float
    duration: float


@dataclass
class Job:
    """A unit of speech requested by a source."""

    source_id: str
    text: str
    profile: str = "default"
    priority: int = 0
    segments: list[Segment] = field(default_factory=list)
