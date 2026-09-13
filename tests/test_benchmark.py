"""Guards the property this project exists for: first audio comes early."""

import time

from speakd.model import Piece, Span
from speakd.pipeline import speak
from speakd.player import RecordingPlayer
from speakd.synth.fake import FakeEngine

PASSAGE = (
    "Kant gives the official definition at B25. "
    "The crucial thing is that the term is reflexive. "
    "It does not name a special class of objects. "
    "It names an inquiry that turns back on cognition itself. "
    "Hence the second distinction, the one Kant polices hardest."
)


def test_first_audio_does_not_wait_for_the_last_segment() -> None:
    player = RecordingPlayer()
    engine = FakeEngine(synthesis_cost=0.05)
    start = time.monotonic()
    result = speak([Piece(span=Span(0, len(PASSAGE)), spoken=PASSAGE)], engine, player)
    total = time.monotonic() - start

    assert len(result.timeline) == 5
    time_to_first_audio = player.timestamps[0] - start
    # One segment's synthesis, plus overhead — not five.
    assert time_to_first_audio < 0.12, f"first audio took {time_to_first_audio:.3f}s"
    assert time_to_first_audio < total / 2
