"""Tests for the queue a client can see before it plays.

Until this existed a client learned of an utterance when it started, so a
queue thirty deep and an empty one were indistinguishable right up to the
moment the speech came out.
"""

import threading
from collections.abc import Sequence

import numpy as np

from speakd.channels import ChannelTable
from speakd.daemon import QUEUE_PREVIEW_CHARS, Daemon, ProfileView
from speakd.events import Event, EventBus
from speakd.model import Piece
from speakd.player import Player
from speakd.protocol import Request, Verb
from speakd.synth.fake import FakeEngine


def passthrough(pieces: Sequence[Piece]) -> tuple[list[Piece], list[str]]:
    return list(pieces), []


def profile_for(name: str) -> ProfileView:
    return ProfileView(voice="af_heart", speed=1.1, interrupt_on=(), prepare=passthrough)


class HoldingPlayer:
    """Stays inside play() until released, so that there is a queue to look at.

    Nothing is ever waiting on a player that returns at once: the worker takes
    each job the instant it is put, and every assertion below would be a race
    against it.
    """

    def __init__(self) -> None:
        self.playing = threading.Event()
        self.release = threading.Event()
        self.played: list[np.ndarray] = []

    def play(self, audio: np.ndarray, sample_rate: int) -> None:
        self.playing.set()
        self.release.wait(timeout=10.0)
        self.played.append(audio)

    def stop(self) -> None:
        # A real sink's stop() ends the write in flight; this one has to be
        # let out by hand or the worker never leaves the utterance.
        self.release.set()


def build(player: Player) -> tuple[Daemon, list[Event]]:
    bus = EventBus()
    seen: list[Event] = []
    bus.subscribe(seen.append)
    daemon = Daemon(FakeEngine(), player, profile_for, bus=bus, channels=ChannelTable())
    daemon.start()
    return daemon, seen


def enqueue(source: str, text: str) -> Request:
    return Request(verb=Verb.ENQUEUE, source_id=source, payload={"text": text})


def waiting(daemon: Daemon) -> list[dict[str, object]]:
    """What STATUS says is queued, checked into a shape a test can read."""
    status = daemon.handle(Request(verb=Verb.STATUS, source_id=""))
    assert status.ok
    queue = status.data["queue"]
    assert isinstance(queue, list)
    return queue


# A sentence long enough that the preview has to cut it, built rather than
# written out so the assertions can slice the same string the daemon did.
LONG = "".join(f"Sentence number {index}. " for index in range(40))


def test_status_lists_what_is_waiting_in_play_order() -> None:
    player = HoldingPlayer()
    daemon, _seen = build(player)
    try:
        daemon.handle(enqueue("a", "Playing."))
        assert player.playing.wait(timeout=5.0), "the worker never reached the player"
        daemon.handle(enqueue("a", "First waiting."))
        daemon.handle(enqueue("b", "Second waiting."))
        assert waiting(daemon) == [
            {"source_id": "a", "text": "First waiting."},
            {"source_id": "b", "text": "Second waiting."},
        ]
    finally:
        player.release.set()
        daemon.stop()


def test_the_utterance_being_spoken_is_not_in_the_queue() -> None:
    """This is what is waiting, not what is happening.

    A client that read the sounding utterance out of here would draw it twice
    -- once as playing, once as next -- which is the one thing a queue view
    must not do.
    """
    player = HoldingPlayer()
    daemon, _seen = build(player)
    try:
        daemon.handle(enqueue("a", "Playing."))
        assert player.playing.wait(timeout=5.0), "the worker never reached the player"
        assert waiting(daemon) == []
        daemon.handle(enqueue("a", "Waiting."))
        assert [entry["text"] for entry in waiting(daemon)] == ["Waiting."]
    finally:
        player.release.set()
        daemon.stop()


def test_the_preview_stops_at_two_hundred_characters() -> None:
    player = HoldingPlayer()
    daemon, _seen = build(player)
    try:
        daemon.handle(enqueue("a", "Playing."))
        assert player.playing.wait(timeout=5.0), "the worker never reached the player"
        daemon.handle(enqueue("a", LONG))
        preview = waiting(daemon)[0]["text"]
        assert isinstance(preview, str)
        # Sliced, with nothing appended: an ellipsis is text the utterance
        # does not contain, and a client comparing this against what it sent
        # would have to strip it first.
        assert preview == LONG[:QUEUE_PREVIEW_CHARS]
        assert len(preview) == QUEUE_PREVIEW_CHARS
    finally:
        player.release.set()
        daemon.stop()


def test_an_accepted_job_announces_itself_with_how_many_are_waiting() -> None:
    """The event arrives when the job is accepted, not when it is spoken."""
    player = HoldingPlayer()
    daemon, seen = build(player)
    try:
        daemon.handle(enqueue("a", "Playing."))
        assert player.playing.wait(timeout=5.0), "the worker never reached the player"
        # Everything from here is behind the held utterance, so the counts are
        # the queue's depth rather than a race with the worker.
        seen.clear()
        daemon.handle(enqueue("a", LONG))
        daemon.handle(enqueue("b", "Second."))
        queued = [event for event in seen if event.kind == "queued"]
        assert [(event.source_id, event.data["pending"]) for event in queued] == [
            ("a", 1),
            ("b", 2),
        ]
        assert queued[0].data["text"] == LONG[:QUEUE_PREVIEW_CHARS]
        assert queued[1].data["text"] == "Second."
    finally:
        player.release.set()
        daemon.stop()


def test_a_job_that_was_never_accepted_announces_nothing() -> None:
    """`queued` means accepted. A muted channel's drop already has `declined`."""
    player = HoldingPlayer()
    daemon, seen = build(player)
    try:
        daemon.handle(Request(verb=Verb.MUTE, source_id="a", payload={"muted": True}))
        seen.clear()
        assert daemon.handle(enqueue("a", "Never said.")).ok is True
        assert [event.kind for event in seen] == ["declined"]
    finally:
        player.release.set()
        daemon.stop()
