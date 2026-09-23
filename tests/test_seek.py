"""Tests for moving playback within the utterance being spoken."""

import threading
import time

import numpy as np

from speakd.channels import ChannelTable
from speakd.daemon import Daemon, ProfileView
from speakd.events import Event, EventBus
from speakd.player import FakeSink, StreamingPlayer
from speakd.protocol import Request, Response, Verb
from speakd.synth.fake import FakeEngine

TEXT = "Zero. One. Two. Three."


def profile_for(name: str) -> ProfileView:
    return ProfileView(
        voice="af_heart", speed=1.0, interrupt_on=(), prepare=lambda p: (list(p), [])
    )


class SlowSink(FakeSink):
    """Takes real time per block, so there is an utterance in flight to seek."""

    def write(self, frames: np.ndarray) -> None:
        super().write(frames)
        time.sleep(len(frames) / 24000 / 20)  # twenty times real time


class CountingEngine(FakeEngine):
    def __init__(self) -> None:
        super().__init__()
        self.texts: list[str] = []

    def synthesize(self, text: str, voice: str, speed: float) -> np.ndarray:
        self.texts.append(text)
        return super().synthesize(text, voice, speed)


def until(predicate, timeout: float = 5.0) -> bool:  # type: ignore[no-untyped-def]
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.002)
    return bool(predicate())


def build(engine: FakeEngine | None = None) -> tuple[Daemon, StreamingPlayer, list[Event]]:
    player = StreamingPlayer(SlowSink(), chunk_frames=512)
    d = Daemon(
        engine or FakeEngine(),
        player,
        profile_for,
        bus=EventBus(),
        channels=ChannelTable(),
    )
    seen: list[Event] = []
    lock = threading.Lock()

    def record(event: Event) -> None:
        with lock:
            seen.append(event)

    d.bus.subscribe(record)
    d.start()
    return d, player, seen


def positions(seen: list[Event]) -> list[int]:
    return [e.data["index"] for e in list(seen) if e.kind == "position"]


def seek(d: Daemon, **payload: int) -> Response:
    return d.handle(Request(verb=Verb.SEEK, source_id="", payload=dict(payload)))


def speak(d: Daemon) -> None:
    d.handle(Request(verb=Verb.ENQUEUE, source_id="s", payload={"text": TEXT, "kind": "response"}))


def test_seek_with_nothing_speaking_is_refused() -> None:
    d, _, _ = build()
    try:
        response = seek(d, by=1)
        assert not response.ok
        assert "nothing is speaking" in response.error
    finally:
        d.stop()


def test_seek_needs_exactly_one_integer() -> None:
    d, _, _ = build()
    try:
        assert not seek(d).ok
        assert not d.handle(Request(verb=Verb.SEEK, source_id="", payload={"index": "2"})).ok
        assert not d.handle(Request(verb=Verb.SEEK, source_id="", payload={"by": True})).ok
        assert not seek(d, index=1, by=1).ok
    finally:
        d.stop()


def test_seek_to_an_index_plays_from_there() -> None:
    d, _, seen = build()
    try:
        speak(d)
        assert until(lambda: positions(seen) == [0])
        assert seek(d, index=2).data == {"index": 2}
        assert d.wait_idle(timeout=10.0)
        assert positions(seen) == [0, 2, 3]
        kinds = [e.kind for e in seen]
        assert kinds.count("started") == 1
        assert kinds.count("finished") == 1
    finally:
        d.stop()


def test_back_from_the_first_sentence_restarts_it() -> None:
    d, _, seen = build()
    try:
        speak(d)
        assert until(lambda: positions(seen) == [0])
        assert seek(d, by=-1).data == {"index": 0}
        assert d.wait_idle(timeout=10.0)
        assert positions(seen) == [0, 0, 1, 2, 3]
    finally:
        d.stop()


def test_forward_is_relative_to_the_sentence_announced() -> None:
    d, _, seen = build()
    try:
        speak(d)
        assert until(lambda: positions(seen) == [0])
        assert seek(d, by=1).data == {"index": 1}
        assert d.wait_idle(timeout=10.0)
        assert positions(seen) == [0, 1, 2, 3]
    finally:
        d.stop()


def test_forward_past_the_end_finishes_the_utterance() -> None:
    d, _, seen = build()
    try:
        speak(d)
        assert until(lambda: positions(seen) == [0])
        assert seek(d, index=9).data == {"index": None, "ended": True}
        assert d.wait_idle(timeout=10.0)
        finished = [e for e in seen if e.kind == "finished"]
        assert len(finished) == 1
        assert finished[0].data["cancelled"] is True
        assert 3 not in positions(seen)
    finally:
        d.stop()


def test_going_back_reuses_the_audio_already_made() -> None:
    engine = CountingEngine()
    d, _, seen = build(engine)
    try:
        speak(d)
        assert until(lambda: positions(seen)[-1:] == [1])
        seek(d, by=-1)
        assert d.wait_idle(timeout=10.0)
        assert engine.texts.count("Zero.") == 1
        assert engine.texts.count("One.") == 1
    finally:
        d.stop()


def test_the_sentence_sought_is_played_in_full() -> None:
    d, player, seen = build()
    try:
        speak(d)
        assert until(lambda: positions(seen) == [0])
        before = player.frames_played
        seek(d, index=3)
        assert d.wait_idle(timeout=10.0)
        # "Three." at 15 chars/s is 0.4 s of audio. All of it must be
        # written, not lost to an interrupt left over from the seek.
        assert player.frames_played - before >= int(24000 * len("Three.") / 15)
        assert player.interrupted is False
    finally:
        d.stop()


def test_a_hush_after_a_seek_wins() -> None:
    d, _, seen = build()
    try:
        speak(d)
        assert until(lambda: positions(seen) == [0])
        seek(d, index=2)
        d.handle(Request(verb=Verb.HUSH, source_id=""))
        assert d.wait_idle(timeout=10.0)
        assert 3 not in positions(seen)
    finally:
        d.stop()


def test_a_seek_while_paused_stays_paused() -> None:
    d, player, seen = build()
    try:
        speak(d)
        assert until(lambda: positions(seen) == [0])
        d.handle(Request(verb=Verb.PAUSE, source_id=""))
        seek(d, index=2)
        assert until(lambda: positions(seen)[-1:] == [2])
        time.sleep(0.1)
        assert positions(seen) == [0, 2]
        assert player.paused
        d.handle(Request(verb=Verb.RESUME, source_id=""))
        assert d.wait_idle(timeout=10.0)
        assert positions(seen) == [0, 2, 3]
    finally:
        d.stop()
