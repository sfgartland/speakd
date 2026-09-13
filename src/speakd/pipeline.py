"""Streaming synthesis: play segment N while segment N+1 is being made.

A queue of depth one is deliberate. Deeper buffering would synthesise far
ahead of playback, which wastes work when speech is cancelled — and speech
is cancelled often, because the user typing is a cancel.
"""

from __future__ import annotations

import queue
import threading
from collections.abc import Sequence
from dataclasses import dataclass, field

import numpy as np

from speakd.model import Piece, Segment
from speakd.player import Player
from speakd.segmenter import DEFAULT_MAX_CHARS, segment
from speakd.synth import Synthesizer
from speakd.timeline import Timeline


@dataclass
class SpeechResult:
    timeline: Timeline
    cancelled: bool = False
    errors: list[str] = field(default_factory=list)


_Item = tuple[Piece, np.ndarray] | str | None


def speak(
    pieces: Sequence[Piece],
    engine: Synthesizer,
    player: Player,
    *,
    voice: str = "af_heart",
    speed: float = 1.1,
    cancel: threading.Event | None = None,
    max_chars: int = DEFAULT_MAX_CHARS,
) -> SpeechResult:
    """Speak `pieces`, returning the timeline of what was actually played."""
    cancel = cancel or threading.Event()
    units = segment(pieces, max_chars)
    work: queue.Queue[_Item] = queue.Queue(maxsize=1)

    def produce() -> None:
        try:
            for unit in units:
                if cancel.is_set():
                    break
                try:
                    audio = engine.synthesize(unit.spoken, voice, speed)
                except Exception as exc:  # speech must not vanish on one bad segment
                    work.put(f"{unit.spoken[:40]!r}: {exc}")
                    continue
                work.put((unit, audio))
        finally:
            work.put(None)

    worker = threading.Thread(target=produce, daemon=True)
    worker.start()

    timeline = Timeline()
    errors: list[str] = []
    offset = 0.0
    while True:
        item = work.get()
        if item is None:
            break
        if isinstance(item, str):
            errors.append(item)
            continue
        if cancel.is_set():
            player.stop()
            # Drain to the sentinel: the producer may already be past its own
            # cancel check for the next unit and about to put once more. With
            # a depth-one queue, not draining here can leave it blocked on
            # that put forever, since nothing would read it again.
            while work.get() is not None:
                pass
            break
        unit, audio = item
        duration = len(audio) / engine.sample_rate
        timeline.append(
            Segment(
                span=unit.span,
                text=unit.spoken,
                audio_offset=offset,
                duration=duration,
            )
        )
        player.play(audio, engine.sample_rate)
        offset += duration

    worker.join(timeout=1.0)
    return SpeechResult(timeline=timeline, cancelled=cancel.is_set(), errors=errors)
