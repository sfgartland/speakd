"""Tests for playing an utterance again from a chosen sentence."""

import threading
import time
from collections.abc import Sequence

import numpy as np

from speakd.channels import ChannelTable
from speakd.daemon import Daemon, ProfileView
from speakd.events import Event, EventBus
from speakd.model import Piece
from speakd.player import FakeSink, StreamingPlayer
from speakd.protocol import Request, Response, Verb
from speakd.synth.fake import FakeEngine

TEXT = "Zero. One. Two. Three."


class SlowSink(FakeSink):
    """Takes real time per block, so an utterance is in flight long enough to act on."""

    def write(self, frames: np.ndarray) -> None:
        super().write(frames)
        time.sleep(len(frames) / 24000 / 20)


def until(predicate, timeout: float = 5.0) -> bool:  # type: ignore[no-untyped-def]
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.002)
    return bool(predicate())


class Built:
    def __init__(self) -> None:
        self.prepared = 0

        def prepare(pieces: Sequence[Piece]) -> tuple[list[Piece], list[str]]:
            self.prepared += 1
            return list(pieces), []

        self.daemon = Daemon(
            FakeEngine(),
            StreamingPlayer(SlowSink(), chunk_frames=512),
            lambda name: ProfileView(voice="af_heart", speed=1.0, interrupt_on=(), prepare=prepare),
            bus=EventBus(),
            channels=ChannelTable(),
        )
        self.seen: list[Event] = []
        lock = threading.Lock()

        def record(event: Event) -> None:
            with lock:
                self.seen.append(event)

        self.daemon.bus.subscribe(record)
        self.daemon.start()

    def positions(self) -> list[int]:
        return [e.data["index"] for e in list(self.seen) if e.kind == "position"]

    def kinds(self) -> list[str]:
        return [e.kind for e in list(self.seen)]

    def speak(self) -> None:
        self.daemon.handle(
            Request(verb=Verb.ENQUEUE, source_id="s", payload={"text": TEXT, "kind": "response"})
        )

    def replay(self, payload: dict[str, object]) -> Response:
        return self.daemon.handle(Request(verb=Verb.REPLAY, source_id="gui", payload=payload))


def test_replay_with_nothing_spoken_is_refused() -> None:
    b = Built()
    try:
        response = b.replay({"index": 0})
        assert not response.ok
        assert "nothing to replay" in response.error
    finally:
        b.daemon.stop()


def test_replay_needs_an_integer_index() -> None:
    b = Built()
    try:
        assert not b.replay({}).ok
        assert not b.replay({"index": "1"}).ok
        assert not b.replay({"index": True}).ok
        assert not b.replay({"index": -1}).ok
    finally:
        b.daemon.stop()


def test_replay_after_the_end_speaks_again_from_that_sentence() -> None:
    b = Built()
    try:
        b.speak()
        assert b.daemon.wait_idle(timeout=10.0)
        b.seen.clear()
        response = b.replay({"index": 2})
        assert response.ok
        assert response.data == {"spoken": True, "index": 2}
        assert b.daemon.wait_idle(timeout=10.0)
        started = [e for e in b.seen if e.kind == "started"]
        assert len(started) == 1
        # The same utterance, announced whole, on the channel that first spoke it.
        assert started[0].source_id == "s"
        assert started[0].data["text"] == TEXT
        assert len(started[0].data["segments"]) == 4
        assert b.positions() == [2, 3]
        assert b.kinds().count("finished") == 1
    finally:
        b.daemon.stop()


def test_replay_does_not_run_the_transforms_again() -> None:
    b = Built()
    try:
        b.speak()
        assert b.daemon.wait_idle(timeout=10.0)
        b.replay({"index": 1})
        assert b.daemon.wait_idle(timeout=10.0)
        assert b.prepared == 1
    finally:
        b.daemon.stop()


def test_replay_while_that_utterance_is_speaking_is_a_seek() -> None:
    b = Built()
    try:
        b.speak()
        assert until(lambda: b.positions() == [0])
        assert b.replay({"index": 3}).data == {"index": 3}
        assert b.daemon.wait_idle(timeout=10.0)
        assert b.positions() == [0, 3]
        assert b.kinds().count("started") == 1
    finally:
        b.daemon.stop()


def test_replay_past_the_last_sentence_is_refused() -> None:
    b = Built()
    try:
        b.speak()
        assert b.daemon.wait_idle(timeout=10.0)
        response = b.replay({"index": 4})
        assert not response.ok
        assert "no sentence 4" in response.error
    finally:
        b.daemon.stop()


def test_replay_respects_the_mute() -> None:
    b = Built()
    try:
        b.speak()
        assert b.daemon.wait_idle(timeout=10.0)
        b.daemon.handle(Request(verb=Verb.MUTE, source_id="", payload={"muted": True}))
        b.seen.clear()
        response = b.replay({"index": 0})
        assert response.ok
        assert response.data == {"spoken": False, "reason": "muted"}
        assert b.daemon.wait_idle(timeout=5.0)
        assert "started" not in b.kinds()
        assert [e.data["reason"] for e in b.seen if e.kind == "declined"] == ["muted"]
    finally:
        b.daemon.stop()
