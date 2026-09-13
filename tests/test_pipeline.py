"""Tests for the streaming pipeline."""

import threading
import time

import numpy as np
import pytest

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


class HoldingEngine(FakeEngine):
    """Blocks its third synthesize() call until released.

    Signals `reached` on entry, so a driver thread can order events around
    it: by the time `reached` fires, this call's own cancel check already
    passed, and both earlier units are already enqueued (the first has
    necessarily been dequeued, since a depth-one queue could not otherwise
    have held the second for this call to be reached at all).
    """

    def __init__(self, reached: threading.Event, release: threading.Event) -> None:
        super().__init__()
        self.calls = 0
        self.reached = reached
        self.release = release

    def synthesize(self, text: str, voice: str, speed: float) -> np.ndarray:
        idx = self.calls
        self.calls += 1
        if idx == 2:
            self.reached.set()
            assert self.release.wait(timeout=5), "driver never released synthesize()"
        return super().synthesize(text, voice, speed)


class HoldingPlayer(RecordingPlayer):
    """Holds after its first play() call until released, signalling `reached` first."""

    def __init__(self, reached: threading.Event, release: threading.Event) -> None:
        super().__init__()
        self.reached = reached
        self.release = release

    def play(self, audio: np.ndarray, sample_rate: int) -> None:
        super().play(audio, sample_rate)
        if len(self.played) == 1:
            self.reached.set()
            assert self.release.wait(timeout=5), "driver never released play()"


class RaisingPlayer(RecordingPlayer):
    """Raises on one specific play() call, simulating a lost audio device."""

    def __init__(self, bad_index: int) -> None:
        super().__init__()
        self.bad_index = bad_index

    def play(self, audio: np.ndarray, sample_rate: int) -> None:
        if len(self.played) == self.bad_index:
            raise RuntimeError("device disappeared")
        super().play(audio, sample_rate)


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


def test_cancel_before_start_plays_nothing() -> None:
    # Cancel is already set when speak() is called, so the producer breaks on
    # its first check and the consumer takes the sentinel branch: player.stop()
    # is never reached. Mid-stream cancellation, which does reach it, is
    # covered by test_cancellation_mid_stream_does_not_leak_the_producer_thread.
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


def test_cancellation_mid_stream_does_not_leak_the_producer_thread() -> None:
    # Three segments. The producer is held right as it enters synthesizing
    # the third (having already enqueued the second, past its own cancel
    # check for it), and the consumer is held right after playing the
    # first. Only once both are confirmed parked do we set cancel and
    # release both — forcing exactly the interleaving where the producer
    # commits one more item after the consumer has already discovered
    # cancellation and stopped reading. Without draining on that path, the
    # producer's later put (and its unconditional final put(None)) blocks
    # forever on the depth-one queue: this test fails against the brief's
    # verbatim implementation and passes against the drain fix.
    baseline = threading.active_count()

    cancel = threading.Event()
    engine_reached = threading.Event()
    engine_release = threading.Event()
    player_reached = threading.Event()
    player_release = threading.Event()

    engine = HoldingEngine(engine_reached, engine_release)
    player = HoldingPlayer(player_reached, player_release)

    def driver() -> None:
        assert engine_reached.wait(timeout=5), "producer never reached the third segment"
        assert player_reached.wait(timeout=5), "consumer never finished playing the first"
        cancel.set()
        player_release.set()
        engine_release.set()

    driver_thread = threading.Thread(target=driver, daemon=True)
    driver_thread.start()

    result = speak([piece("One. Two. Three.")], engine, player, cancel=cancel)
    driver_thread.join(timeout=5)

    assert player.stopped is True
    assert result.cancelled is True
    assert len(player.played) == 1
    assert not driver_thread.is_alive()
    assert threading.active_count() == baseline


def test_a_raising_player_does_not_leak_the_producer_thread() -> None:
    # The producer will have a second unit ready (or in flight) when
    # play() raises on the first. Without draining on the exception path,
    # the producer's later put blocks forever on the depth-one queue with
    # no reader left: this test fails against the brief's verbatim
    # implementation and passes against the shared drain-in-finally fix.
    before = set(threading.enumerate())
    player = RaisingPlayer(bad_index=0)

    with pytest.raises(RuntimeError, match="device disappeared"):
        speak([piece("One. Two.")], FakeEngine(), player)

    new_threads = set(threading.enumerate()) - before
    for new_thread in new_threads:
        new_thread.join(timeout=2.0)
    assert not any(new_thread.is_alive() for new_thread in new_threads)


def test_a_raising_player_does_not_set_a_caller_supplied_cancel_event() -> None:
    # Teardown after an exception used to run through the caller's own Event.
    # The caller then saw cancelled=True for an utterance nobody cancelled,
    # and the next speak() reusing that Event played nothing, reported no
    # error and returned an empty timeline — a silent failure.
    cancel = threading.Event()
    player = RaisingPlayer(bad_index=0)

    with pytest.raises(RuntimeError, match="device disappeared"):
        speak([piece("One. Two.")], FakeEngine(), player, cancel=cancel)

    assert not cancel.is_set(), "speak() mutated the caller's Event"

    reused = RecordingPlayer()
    result = speak([piece("One. Two.")], FakeEngine(), reused, cancel=cancel)
    assert len(reused.played) == 2
    assert len(result.timeline) == 2
    assert result.cancelled is False
    assert result.errors == []


def test_a_cancelled_utterance_leaves_the_caller_event_as_the_caller_set_it() -> None:
    # The other half of the same property: speak() reports cancellation, it
    # does not author it. An Event the caller never set stays unset.
    cancel = threading.Event()
    player = RecordingPlayer()
    result = speak([piece("One. Two. Three.")], FakeEngine(), player, cancel=cancel)
    assert not cancel.is_set()
    assert result.cancelled is False
    assert len(player.played) == 3
