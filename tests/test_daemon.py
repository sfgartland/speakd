"""Tests for the daemon's request handling and speech worker."""

import threading
import time
from collections.abc import Sequence

import numpy as np
import pytest

from speakd.channels import ChannelTable
from speakd.daemon import Daemon, ProfileView
from speakd.events import Event, EventBus
from speakd.model import Piece, Role
from speakd.player import RecordingPlayer
from speakd.protocol import Request, Verb
from speakd.synth.fake import FakeEngine


def passthrough(pieces: Sequence[Piece]) -> tuple[list[Piece], list[str]]:
    return list(pieces), []


def profile_for(name: str) -> ProfileView:
    interrupt_on = ("error", "done") if name == "monitor" else ()
    return ProfileView(voice="af_heart", speed=1.1, interrupt_on=interrupt_on, prepare=passthrough)


@pytest.fixture
def daemon():  # type: ignore[no-untyped-def]
    player = RecordingPlayer()
    bus = EventBus()
    d = Daemon(FakeEngine(), player, profile_for, bus=bus, channels=ChannelTable())
    d.start()
    try:
        yield d, player, bus
    finally:
        d.stop()


def enqueue(source: str, text: str, kind: str = "response") -> Request:
    return Request(verb=Verb.ENQUEUE, source_id=source, payload={"text": text, "kind": kind})


def test_enqueue_on_a_foreground_channel_speaks(daemon) -> None:  # type: ignore[no-untyped-def]
    d, player, _bus = daemon
    assert d.handle(enqueue("s", "One. Two.")).ok is True
    assert d.wait_idle(timeout=5.0)
    assert len(player.played) == 2


def test_enqueue_with_no_text_is_rejected(daemon) -> None:  # type: ignore[no-untyped-def]
    d, _player, _bus = daemon
    response = d.handle(Request(verb=Verb.ENQUEUE, source_id="s", payload={}))
    assert response.ok is False
    assert "text" in response.error


def test_a_background_channel_stays_silent_for_ordinary_output(daemon) -> None:  # type: ignore[no-untyped-def]
    d, player, _bus = daemon
    d.handle(Request(verb=Verb.SET_ROLE, source_id="s", payload={"role": "background"}))
    d.handle(
        Request(verb=Verb.ENQUEUE, source_id="s", payload={"text": "hi", "profile": "monitor"})
    )
    assert d.wait_idle(timeout=5.0)
    assert player.played == []


def test_a_background_channel_speaks_an_interrupting_kind(daemon) -> None:  # type: ignore[no-untyped-def]
    d, player, _bus = daemon
    d.handle(Request(verb=Verb.SET_ROLE, source_id="s", payload={"role": "background"}))
    d.handle(
        Request(
            verb=Verb.ENQUEUE,
            source_id="s",
            payload={"text": "it failed", "kind": "error", "profile": "monitor"},
        )
    )
    assert d.wait_idle(timeout=5.0)
    assert len(player.played) >= 1


def test_set_role_rejects_an_unknown_role(daemon) -> None:  # type: ignore[no-untyped-def]
    d, _player, _bus = daemon
    response = d.handle(Request(verb=Verb.SET_ROLE, source_id="s", payload={"role": "loud"}))
    assert response.ok is False
    assert "role" in response.error


def test_set_priority_updates_the_channel(daemon) -> None:  # type: ignore[no-untyped-def]
    d, _player, _bus = daemon
    assert (
        d.handle(Request(verb=Verb.SET_PRIORITY, source_id="s", payload={"priority": 5})).ok is True
    )
    channel = d.channels.get("s")
    assert channel is not None and channel.priority == 5


def test_speaking_publishes_lifecycle_and_position_events(daemon) -> None:  # type: ignore[no-untyped-def]
    d, _player, bus = daemon
    seen: list[Event] = []
    bus.subscribe(seen.append)
    d.handle(enqueue("s", "One. Two."))
    assert d.wait_idle(timeout=5.0)
    kinds = [e.kind for e in seen]
    assert kinds[0] == "started"
    assert kinds[-1] == "finished"
    assert "position" in kinds
    positions = [e for e in seen if e.kind == "position"]
    assert positions[0].data["text"] == "One."
    assert "span_start" in positions[0].data


def test_every_utterance_gets_a_fresh_cancel_event(daemon) -> None:  # type: ignore[no-untyped-def]
    d, player, _bus = daemon
    d.handle(enqueue("s", "One."))
    assert d.wait_idle(timeout=5.0)
    d.handle(Request(verb=Verb.HUSH, source_id="s", payload={}))
    d.handle(enqueue("s", "Two."))
    assert d.wait_idle(timeout=5.0)
    # A hush must not poison the channel: the second utterance still speaks.
    assert len(player.played) == 2


def test_transport_verbs_report_that_they_are_not_implemented(daemon) -> None:  # type: ignore[no-untyped-def]
    d, _player, _bus = daemon
    for verb in (Verb.PAUSE, Verb.RESUME, Verb.SEEK):
        response = d.handle(Request(verb=verb, source_id="s", payload={}))
        assert response.ok is False
        assert "streaming player" in response.error


def test_a_prepare_failure_is_reported_but_speech_continues(daemon) -> None:  # type: ignore[no-untyped-def]
    def failing_profile(name: str) -> ProfileView:
        def prepare(pieces: Sequence[Piece]) -> tuple[list[Piece], list[str]]:
            return list(pieces), ["markdown: transform failed"]

        return ProfileView(voice="af_heart", speed=1.1, interrupt_on=(), prepare=prepare)

    player = RecordingPlayer()
    bus = EventBus()
    seen: list[Event] = []
    bus.subscribe(seen.append, kinds=["error"])
    d = Daemon(FakeEngine(), player, failing_profile, bus=bus, channels=ChannelTable())
    d.start()
    try:
        d.handle(enqueue("s", "One."))
        assert d.wait_idle(timeout=5.0)
    finally:
        d.stop()
    assert len(player.played) == 1
    assert any("transform failed" in str(e.data.get("message", "")) for e in seen)


# --- Beyond the brief: the threading contract the tests themselves rely on ---


class HoldingPlayer(RecordingPlayer):
    """Blocks in its first play() call until released, signalling `reached` first."""

    def __init__(self, reached: threading.Event, release: threading.Event) -> None:
        super().__init__()
        self.reached = reached
        self.release = release

    def play(self, audio: np.ndarray, sample_rate: int) -> None:
        super().play(audio, sample_rate)
        if len(self.played) == 1:
            self.reached.set()
            assert self.release.wait(timeout=5), "test never released play()"


def test_a_raising_prepare_does_not_kill_the_worker(daemon) -> None:  # type: ignore[no-untyped-def]
    """A worker that dies takes every later utterance with it, silently."""

    def raising_profile(name: str) -> ProfileView:
        def prepare(pieces: Sequence[Piece]) -> tuple[list[Piece], list[str]]:
            raise RuntimeError("transform blew up")

        if name == "broken":
            return ProfileView(voice="af_heart", speed=1.1, interrupt_on=(), prepare=prepare)
        return ProfileView(voice="af_heart", speed=1.1, interrupt_on=(), prepare=passthrough)

    player = RecordingPlayer()
    bus = EventBus()
    seen: list[Event] = []
    bus.subscribe(seen.append, kinds=["error"])
    d = Daemon(FakeEngine(), player, raising_profile, bus=bus, channels=ChannelTable())
    d.start()
    try:
        d.handle(
            Request(verb=Verb.ENQUEUE, source_id="s", payload={"text": "boom", "profile": "broken"})
        )
        assert d.wait_idle(timeout=5.0)
        assert any("blew up" in str(e.data.get("message", "")) for e in seen)
        # The worker survived: the next utterance still reaches the player.
        d.handle(enqueue("s", "One."))
        assert d.wait_idle(timeout=5.0)
    finally:
        d.stop()
    assert len(player.played) == 1


def test_wait_idle_is_false_while_an_utterance_is_still_playing() -> None:
    """The invariant the other tests lean on: idle means nothing is in flight."""
    reached = threading.Event()
    release = threading.Event()
    player = HoldingPlayer(reached, release)
    d = Daemon(FakeEngine(), player, profile_for, bus=EventBus(), channels=ChannelTable())
    d.start()
    try:
        d.handle(enqueue("s", "One. Two."))
        assert reached.wait(timeout=5.0), "the worker never started playing"
        # One segment is in the player, a second is still to come.
        assert d.wait_idle(timeout=0.1) is False
        release.set()
        assert d.wait_idle(timeout=5.0) is True
        assert len(player.played) == 2
    finally:
        release.set()
        d.stop()


def test_wait_idle_covers_every_accepted_utterance() -> None:
    """Idle must mean every accepted utterance is done, queued or in flight.

    Bursts, because that is the shape that separates the two: the job queue is
    empty for the whole time a job is being spoken, so a queue check alone
    cannot tell speaking from idle.
    """
    player = RecordingPlayer()
    d = Daemon(FakeEngine(), player, profile_for, bus=EventBus(), channels=ChannelTable())
    d.start()
    try:
        for round_number in range(1, 201):
            for _ in range(3):
                assert d.handle(enqueue("s", "One. Two.")).ok is True
            assert d.wait_idle(timeout=5.0)
            assert len(player.played) == round_number * 6
    finally:
        d.stop()


def test_stop_terminates_with_a_job_in_flight() -> None:
    reached = threading.Event()
    release = threading.Event()
    player = HoldingPlayer(reached, release)
    d = Daemon(FakeEngine(), player, profile_for, bus=EventBus(), channels=ChannelTable())
    d.start()
    d.handle(enqueue("s", "One. Two. Three. Four."))
    assert reached.wait(timeout=5.0), "the worker never started playing"
    release.set()
    d.stop()
    assert not any(t.name == "speakd-speech" and t.is_alive() for t in threading.enumerate())


def test_work_still_queued_when_the_daemon_stops_is_reported() -> None:
    """An accepted utterance that never reaches the engine says so."""
    reached = threading.Event()
    release = threading.Event()
    player = HoldingPlayer(reached, release)
    bus = EventBus()
    seen: list[Event] = []
    bus.subscribe(seen.append, kinds=["error"])
    d = Daemon(FakeEngine(), player, profile_for, bus=bus, channels=ChannelTable())
    d.start()
    d.handle(enqueue("s", "One. Two."))
    assert reached.wait(timeout=5.0), "the worker never started playing"
    assert d.handle(enqueue("s", "Never spoken.")).ok is True
    # stop() blocks on the worker, which is held inside play(), so it runs on
    # its own thread. The private read is the ordering point: once stop() has
    # closed the daemon, releasing the player must not let the queued job
    # through silently.
    stopper = threading.Thread(target=d.stop, daemon=True)
    stopper.start()
    deadline = time.monotonic() + 5.0
    while d._running and time.monotonic() < deadline:
        time.sleep(0.001)
    assert d._running is False, "stop() never took effect"
    release.set()
    stopper.join(timeout=10.0)
    assert not stopper.is_alive()
    assert any("discarded" in str(e.data.get("message", "")) for e in seen)
    assert d.wait_idle(timeout=5.0), "a discarded job must not stay pending"


def test_stop_is_safe_on_an_empty_queue_and_when_called_twice() -> None:
    d = Daemon(
        FakeEngine(), RecordingPlayer(), profile_for, bus=EventBus(), channels=ChannelTable()
    )
    d.start()
    d.stop()
    d.stop()
    assert not any(t.name == "speakd-speech" and t.is_alive() for t in threading.enumerate())


def test_enqueue_after_stop_is_refused_rather_than_swallowed() -> None:
    player = RecordingPlayer()
    d = Daemon(FakeEngine(), player, profile_for, bus=EventBus(), channels=ChannelTable())
    d.start()
    d.stop()
    response = d.handle(enqueue("s", "One."))
    assert response.ok is False
    assert "running" in response.error
    assert player.played == []


def test_starting_twice_does_not_add_a_second_worker() -> None:
    d = Daemon(
        FakeEngine(), RecordingPlayer(), profile_for, bus=EventBus(), channels=ChannelTable()
    )
    d.start()
    d.start()
    try:
        assert sum(1 for t in threading.enumerate() if t.name == "speakd-speech") == 1
    finally:
        d.stop()


def test_set_priority_rejects_a_non_integer(daemon) -> None:  # type: ignore[no-untyped-def]
    d, _player, _bus = daemon
    response = d.handle(Request(verb=Verb.SET_PRIORITY, source_id="s", payload={"priority": "hi"}))
    assert response.ok is False
    assert "priority" in response.error


def test_a_declined_utterance_says_why(daemon) -> None:  # type: ignore[no-untyped-def]
    d, _player, _bus = daemon
    d.handle(Request(verb=Verb.SET_ROLE, source_id="s", payload={"role": "background"}))
    response = d.handle(
        Request(verb=Verb.ENQUEUE, source_id="s", payload={"text": "hi", "profile": "monitor"})
    )
    assert response.ok is True
    assert response.data["spoken"] is False
    assert "background" in str(response.data["reason"])


def test_an_unhandled_verb_is_reported(daemon) -> None:  # type: ignore[no-untyped-def]
    d, _player, _bus = daemon
    response = d.handle(Request(verb=Verb.SUBSCRIBE, source_id="s", payload={}))
    assert response.ok is False
    assert "subscribe" in response.error


def test_set_role_accepts_every_role(daemon) -> None:  # type: ignore[no-untyped-def]
    d, _player, _bus = daemon
    for role in Role:
        assert (
            d.handle(Request(verb=Verb.SET_ROLE, source_id="s", payload={"role": role.value})).ok
            is True
        )
        channel = d.channels.get("s")
        assert channel is not None and channel.role is role


def test_a_worker_that_exits_unexpectedly_leaves_a_diagnostic() -> None:
    """Last resort: the one thing worse than a failed utterance is a silent daemon."""
    bus = EventBus()
    seen: list[Event] = []
    bus.subscribe(seen.append, kinds=["error"])
    d = Daemon(FakeEngine(), RecordingPlayer(), profile_for, bus=bus, channels=ChannelTable())

    def boom() -> None:
        raise RuntimeError("bookkeeping exploded")

    d._finish_one = boom  # type: ignore[method-assign]
    d.start()
    try:
        d.handle(enqueue("s", "One."))
        deadline = time.monotonic() + 5.0
        while not seen and time.monotonic() < deadline:
            time.sleep(0.001)
        assert any("speech worker died" in str(e.data.get("message", "")) for e in seen)
        assert any("bookkeeping exploded" in str(e.data.get("message", "")) for e in seen)
    finally:
        d.stop()
