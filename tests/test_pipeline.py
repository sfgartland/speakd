"""Tests for the streaming pipeline."""

import threading
import time

import numpy as np

from speakd.model import Piece, Span
from speakd.pipeline import speak
from speakd.player import RecordingPlayer
from speakd.synth.fake import FakeEngine


def piece(text: str) -> Piece:
    return Piece(span=Span(0, len(text)), spoken=text)


class SleepingPlayer(RecordingPlayer):
    """A sink that takes real time, so overlap can be measured."""

    def __init__(self, cost: float) -> None:
        super().__init__()
        self.cost = cost

    def play(self, audio: np.ndarray, sample_rate: int) -> None:
        super().play(audio, sample_rate)
        time.sleep(self.cost)


class ExplodingEngine(FakeEngine):
    """Fails on one specific text and works for everything else."""

    def __init__(self, bad: str) -> None:
        super().__init__()
        self.bad = bad

    def synthesize(self, text: str, voice: str, speed: float) -> np.ndarray:
        if text == self.bad:
            raise RuntimeError("engine exploded")
        return super().synthesize(text, voice, speed)


def test_plays_every_segment_in_order() -> None:
    player = RecordingPlayer()
    result = speak([piece("One. Two. Three.")], FakeEngine(), player)
    assert len(player.played) == 3
    assert len(result.timeline) == 3
    assert result.cancelled is False
    assert result.errors == []


def test_timeline_offsets_accumulate() -> None:
    player = RecordingPlayer()
    result = speak(
        [piece("One. Two.")], FakeEngine(sample_rate=1000, chars_per_second=10.0), player
    )
    assert result.timeline.span_at(0.0) is not None
    assert result.timeline.duration > 0.0


def test_synthesis_overlaps_playback() -> None:
    # Four segments, each costing 0.05s to synthesise and 0.05s to play.
    # Serial would be ~0.40s; overlapped should be ~0.25s.
    player = SleepingPlayer(cost=0.05)
    engine = FakeEngine(synthesis_cost=0.05)
    start = time.monotonic()
    speak([piece("One. Two. Three. Four.")], engine, player)
    elapsed = time.monotonic() - start
    assert len(player.played) == 4
    assert elapsed < 0.35, f"no overlap: {elapsed:.2f}s"


def test_cancel_stops_early_and_stops_the_player() -> None:
    cancel = threading.Event()
    cancel.set()
    player = RecordingPlayer()
    result = speak([piece("One. Two. Three.")], FakeEngine(), player, cancel=cancel)
    assert result.cancelled is True
    assert player.played == []


def test_a_failing_segment_does_not_lose_the_others() -> None:
    player = RecordingPlayer()
    result = speak([piece("One. Two. Three.")], ExplodingEngine(bad="Two."), player)
    assert len(player.played) == 2
    assert len(result.errors) == 1
    assert "exploded" in result.errors[0]
