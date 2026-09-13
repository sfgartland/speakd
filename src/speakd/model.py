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
class Piece:
    """A unit of text and where it came from.

    Transforms map pieces to pieces. Rewriting changes `spoken` and keeps
    `span`, so provenance survives arbitrary rewriting and follow-along
    display keeps working under a profile that rewrites heavily.

    `exact` says whether `spoken` is still the verbatim source text at
    `span`. Only then can a consumer index into `span` by offset — the
    segmenter does exactly that to give each sub-unit its own span. A
    transform that rewrites must clear it. It cannot be inferred from
    lengths: `café` to `cafe`, an em-dash swap, and much of milestone 3's
    pronunciation table all preserve length while destroying the
    correspondence, and the resulting sub-spans point at text that no
    longer matches — silently, and wrongly highlighted.
    """

    span: Span
    spoken: str
    exact: bool = True


@dataclass(frozen=True)
class Segment:
    """One synthesised unit, and where it came from.

    `span` indexes the job's source text. `audio_offset` and `duration` are
    nominal seconds within the job's audio — what seek and resume describe.
    `played_at` is a `time.monotonic()` stamp taken when playback of this
    segment actually began, which is what subscribers describe. The two differ
    whenever synthesis stalls playback, and that difference is the point:
    a single measured number would hide the stall.
    """

    span: Span
    text: str
    audio_offset: float
    duration: float
    played_at: float | None = None


@dataclass
class Job:
    """A unit of speech requested by a source."""

    source_id: str
    text: str
    profile: str = "default"
    priority: int = 0
    segments: list[Segment] = field(default_factory=list)
