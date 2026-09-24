"""Tests for the daemon's request handling and speech worker."""

import queue
import threading
import time
from collections.abc import Sequence

import numpy as np
import pytest

from speakd.channels import ChannelTable
from speakd.daemon import Daemon, ProfileView, _Job
from speakd.events import Event, EventBus
from speakd.model import Piece, Role
from speakd.player import FakeSink, Player, RecordingPlayer, StreamingPlayer
from speakd.protocol import Request, Response, Verb
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


@pytest.fixture
def running_daemon():  # type: ignore[no-untyped-def]
    """A started daemon, built with whichever player the test needs.

    A factory rather than a plain fixture because the player is the variable
    under test here: `StreamingPlayer` can pause and `RecordingPlayer` cannot,
    and the daemon has to answer for both.
    """
    built: list[Daemon] = []

    def build(player: Player | None = None) -> Daemon:
        d = Daemon(
            FakeEngine(),
            player if player is not None else RecordingPlayer(),
            profile_for,
            bus=EventBus(),
            channels=ChannelTable(),
        )
        d.start()
        built.append(d)
        return d

    try:
        yield build
    finally:
        for d in built:
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


def _capture_accepted_job(d: Daemon) -> list[_Job]:
    """Wrap `_accept` to record every job the daemon puts on the queue."""
    captured: list[_Job] = []
    original = d._accept

    def spy(job: _Job, at: float) -> Response:
        captured.append(job)
        return original(job, at)

    d._accept = spy  # type: ignore[method-assign]
    return captured


def test_enqueue_lang_is_normalised_and_stored_on_the_job(daemon) -> None:  # type: ignore[no-untyped-def]
    d, _player, _bus = daemon
    captured = _capture_accepted_job(d)
    response = d.handle(
        Request(verb=Verb.ENQUEUE, source_id="s", payload={"text": "hi", "lang": "EN_GB"})
    )
    assert response.ok
    assert captured[0].lang == "en-gb"


def test_enqueue_lang_that_cannot_be_parsed_is_kept_as_unsupported(daemon) -> None:  # type: ignore[no-untyped-def]
    d, _player, _bus = daemon
    captured = _capture_accepted_job(d)
    response = d.handle(
        Request(verb=Verb.ENQUEUE, source_id="s", payload={"text": "hi", "lang": "klingon"})
    )
    assert response.ok
    assert captured[0].lang == "und"


def test_enqueue_with_no_lang_leaves_the_job_lang_unset(daemon) -> None:  # type: ignore[no-untyped-def]
    d, _player, _bus = daemon
    captured = _capture_accepted_job(d)
    response = d.handle(Request(verb=Verb.ENQUEUE, source_id="s", payload={"text": "hi"}))
    assert response.ok
    assert captured[0].lang is None


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


def test_set_label_reaches_status(daemon) -> None:  # type: ignore[no-untyped-def]
    d, _player, _bus = daemon
    d.handle(Request(verb=Verb.SET_LABEL, source_id="s", payload={"label": "named"}))
    status = d.handle(Request(verb=Verb.STATUS, source_id=""))
    channels = status.data["channels"]
    assert [c["label"] for c in channels if c["source_id"] == "s"] == ["named"]


def test_set_label_without_a_label_is_refused() -> None:
    table = ChannelTable()
    d = Daemon(FakeEngine(), RecordingPlayer(), profile_for, bus=EventBus(), channels=table)
    response = d.handle(Request(verb=Verb.SET_LABEL, source_id="s", payload={}))
    assert not response.ok


def test_speaking_publishes_lifecycle_and_position_events(daemon) -> None:  # type: ignore[no-untyped-def]
    d, _player, bus = daemon
    seen: list[Event] = []
    bus.subscribe(seen.append)
    d.handle(enqueue("s", "One. Two."))
    assert d.wait_idle(timeout=5.0)
    kinds = [e.kind for e in seen]
    # `queued` comes first and on the accepting thread: an utterance is now
    # announced when it is taken, not when it reaches the front of the queue.
    assert kinds[0] == "queued"
    assert kinds[1] == "started"
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


def test_pause_pauses_a_pausable_player(running_daemon) -> None:  # type: ignore[no-untyped-def]
    daemon = running_daemon(player=StreamingPlayer(FakeSink()))
    response = daemon.handle(Request(verb=Verb.PAUSE, source_id="s"))
    assert response.ok
    assert response.data["paused"] is True
    assert daemon.player.paused is True


def test_resume_unpauses(running_daemon) -> None:  # type: ignore[no-untyped-def]
    daemon = running_daemon(player=StreamingPlayer(FakeSink()))
    daemon.handle(Request(verb=Verb.PAUSE, source_id="s"))
    response = daemon.handle(Request(verb=Verb.RESUME, source_id="s"))
    assert response.ok
    assert response.data["paused"] is False
    assert daemon.player.paused is False


def test_pausing_a_player_that_cannot_pause_says_why(running_daemon) -> None:  # type: ignore[no-untyped-def]
    daemon = running_daemon(player=RecordingPlayer())
    response = daemon.handle(Request(verb=Verb.PAUSE, source_id="s"))
    assert not response.ok
    assert "pause" in response.error
    assert "RecordingPlayer" in response.error


def test_a_transport_change_is_announced(running_daemon) -> None:  # type: ignore[no-untyped-def]
    daemon = running_daemon(player=StreamingPlayer(FakeSink()))
    seen: list[Event] = []
    daemon.bus.subscribe(seen.append)
    daemon.handle(Request(verb=Verb.PAUSE, source_id="s"))
    transport = [event for event in seen if event.kind == "transport"]
    assert len(transport) == 1
    assert transport[0].data["paused"] is True


def test_pausing_twice_announces_once(running_daemon) -> None:  # type: ignore[no-untyped-def]
    """A GUI that redraws on every event must not flicker on a no-op."""
    daemon = running_daemon(player=StreamingPlayer(FakeSink()))
    seen: list[Event] = []
    daemon.bus.subscribe(seen.append)
    daemon.handle(Request(verb=Verb.PAUSE, source_id="s"))
    daemon.handle(Request(verb=Verb.PAUSE, source_id="s"))
    assert len([event for event in seen if event.kind == "transport"]) == 1


def test_a_player_that_cannot_be_paused_is_reported_not_raised(running_daemon) -> None:  # type: ignore[no-untyped-def]
    """A refusing sink must not come back as a traceback on the control path."""

    class UnpausablePlayer(StreamingPlayer):
        def pause(self) -> None:
            raise RuntimeError("PortAudioError: device unavailable")

    daemon = running_daemon(player=UnpausablePlayer(FakeSink()))
    seen: list[Event] = []
    daemon.bus.subscribe(seen.append)
    response = daemon.handle(Request(verb=Verb.PAUSE, source_id="s"))
    assert not response.ok
    assert "device unavailable" in response.error
    # Nothing changed, so nothing is announced: a GUI told it is paused when
    # the audio is still running is worse off than one told nothing.
    assert [event for event in seen if event.kind == "transport"] == []
    assert daemon.player.paused is False


def test_hush_reports_discards_as_their_own_kind(running_daemon) -> None:  # type: ignore[no-untyped-def]
    daemon = running_daemon()
    seen: list[Event] = []
    daemon.bus.subscribe(seen.append)
    # Queue more than the worker can take, then hush.
    for index in range(3):
        daemon.handle(Request(verb=Verb.ENQUEUE, source_id="s", payload={"text": f"Item {index}."}))
    response = daemon.handle(Request(verb=Verb.HUSH, source_id="s"))
    assert response.ok

    discarded = [event for event in seen if event.kind == "discarded"]
    if response.data["discarded"]:
        assert discarded, "a discard was counted but never announced"
        # Summed by hand rather than with `int(...)`: `Event.data` is
        # `dict[str, object]`, and pinning the type here is the assertion
        # anyway -- a count a GUI has to coerce is not a count.
        announced = 0
        for event in discarded:
            count = event.data["count"]
            assert isinstance(count, int)
            announced += count
        assert announced == response.data["discarded"]


def test_a_discard_is_not_reported_as_an_error(running_daemon) -> None:  # type: ignore[no-untyped-def]
    daemon = running_daemon()
    seen: list[Event] = []
    daemon.bus.subscribe(seen.append)
    for index in range(3):
        daemon.handle(Request(verb=Verb.ENQUEUE, source_id="s", payload={"text": f"Item {index}."}))
    daemon.handle(Request(verb=Verb.HUSH, source_id="s"))
    for event in seen:
        if event.kind == "error":
            assert "discard" not in str(event.data).lower()


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
    """Blocks in its `hold_on`-th play() call until released, signalling first."""

    def __init__(
        self, reached: threading.Event, release: threading.Event, hold_on: int = 1
    ) -> None:
        super().__init__()
        self.reached = reached
        self.release = release
        self.hold_on = hold_on

    def play(self, audio: np.ndarray, sample_rate: int) -> None:
        super().play(audio, sample_rate)
        if len(self.played) == self.hold_on:
            self.reached.set()
            assert self.release.wait(timeout=5), "test never released play()"


def test_a_raising_prepare_does_not_kill_the_worker() -> None:
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
    # stop() blocks on a worker that is held inside play(), so it runs on its
    # own thread: releasing first would let the utterance finish and the test
    # would no longer be about a job in flight at all.
    stopper = threading.Thread(target=d.stop, daemon=True)
    stopper.start()
    deadline = time.monotonic() + 5.0
    while d._running and time.monotonic() < deadline:
        time.sleep(0.001)
    assert d._running is False, "stop() never took effect"
    release.set()
    stopper.join(timeout=10.0)
    assert not stopper.is_alive(), "stop() did not return"
    assert d._worker is None
    assert len(player.played) < 4, "stop must cancel the rest of the utterance"


class SilenceablePlayer(RecordingPlayer):
    """A real player in the one respect that matters: only `stop()` cuts `play()`.

    `HoldingPlayer` is released by the test, which is why the test above can
    pass whether or not `stop()` ever silences anything. Here nothing but
    `stop()` ends the segment, so a daemon that fails to call it is visible.
    """

    def __init__(self) -> None:
        super().__init__()
        self.reached = threading.Event()
        self.silenced = threading.Event()

    def play(self, audio: np.ndarray, sample_rate: int) -> None:
        super().play(audio, sample_rate)
        self.reached.set()
        assert self.silenced.wait(timeout=10.0), "play() was never interrupted"

    def stop(self) -> None:
        super().stop()
        self.silenced.set()


def test_stop_silences_the_player_rather_than_waiting_out_the_utterance() -> None:
    """Ctrl-C must cut the sentence being spoken, not let it finish.

    A worker cannot leave mid-`play()`, so without the `player.stop()` that
    HUSH and CANCEL both make, a real device plays the segment out, the 5s
    join expires, and `stop()` returns while the daemon is still audibly
    speaking — leaving a worker behind that makes the next `start()` refuse.
    """
    player = SilenceablePlayer()
    d = Daemon(FakeEngine(), player, profile_for, bus=EventBus(), channels=ChannelTable())
    d.start()
    d.handle(enqueue("s", "One. Two. Three. Four."))
    assert player.reached.wait(timeout=5.0), "the worker never started playing"

    began = time.monotonic()
    d.stop()
    elapsed = time.monotonic() - began

    assert player.stopped is True, "stop() left the player playing"
    assert elapsed < 5.0, f"stop() waited out its join instead of silencing ({elapsed:.1f}s)"
    assert d._worker is None, "the worker outlived stop()"
    # The consequence a supervisor sees: a daemon that can actually restart.
    assert d.start().data.get("started") is True
    d.stop()


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
    worker = d._worker
    assert worker is not None
    d.stop()
    d.stop()
    assert d._worker is None
    assert not worker.is_alive()


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
    worker = d._worker
    d.start()
    try:
        # Same thread object, not a second consumer racing it for jobs.
        assert d._worker is worker
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


# --- Review round: reproductions for items 1, 2 and 3 ---


def test_a_dead_worker_stops_accepting_speech_and_can_be_restarted() -> None:
    """Item 1. No worker means nothing can be spoken; saying `ok` would lie."""
    bus = EventBus()
    seen: list[Event] = []
    bus.subscribe(seen.append, kinds=["error"])
    player = RecordingPlayer()
    d = Daemon(FakeEngine(), player, profile_for, bus=bus, channels=ChannelTable())
    healthy = d._finish_one

    def boom() -> None:
        raise RuntimeError("bookkeeping exploded")

    d._finish_one = boom  # type: ignore[method-assign]
    d.start()
    d.handle(enqueue("s", "One."))

    def died() -> bool:
        return any("speech worker died" in str(e.data.get("message", "")) for e in seen)

    deadline = time.monotonic() + 5.0
    while not died() and time.monotonic() < deadline:
        time.sleep(0.001)
    assert died(), "a worker that exits must leave a diagnostic"
    assert any("bookkeeping exploded" in str(e.data.get("message", "")) for e in seen), (
        "the diagnostic must carry the traceback, not just the fact"
    )

    # The failure that matters: the daemon must stop taking work it cannot speak.
    assert d.handle(enqueue("s", "Two.")).ok is False
    assert d.wait_idle(timeout=5.0), "a dead worker must not leave work pending forever"

    # And it must be restartable rather than wedged behind a stale worker.
    d._finish_one = healthy  # type: ignore[method-assign]
    d.start()
    try:
        assert d.handle(enqueue("s", "Three.")).ok is True
        assert d.wait_idle(timeout=5.0)
    finally:
        d.stop()
    assert len(player.played) == 2, "the restarted worker must speak again"


def test_work_queued_when_the_worker_dies_is_reported() -> None:
    """Item 1. Anything already accepted is reported rather than dropped."""
    reached = threading.Event()
    release = threading.Event()
    player = HoldingPlayer(reached, release)
    bus = EventBus()
    seen: list[Event] = []
    bus.subscribe(seen.append, kinds=["error"])
    d = Daemon(FakeEngine(), player, profile_for, bus=bus, channels=ChannelTable())
    calls = [0]
    healthy = d._finish_one

    def boom_once() -> None:
        calls[0] += 1
        if calls[0] == 1:
            raise RuntimeError("bookkeeping exploded")
        healthy()

    d._finish_one = boom_once  # type: ignore[method-assign]
    d.start()
    try:
        d.handle(enqueue("s", "One. Two."))
        assert reached.wait(timeout=5.0), "the worker never started playing"
        assert d.handle(enqueue("s", "Never spoken.")).ok is True
        release.set()
        deadline = time.monotonic() + 5.0
        while not any("discarded" in str(e.data.get("message", "")) for e in seen):
            assert time.monotonic() < deadline, "queued work vanished when the worker died"
            time.sleep(0.001)
    finally:
        release.set()
        d.stop()
    assert d.wait_idle(timeout=5.0)


def test_a_base_exception_from_a_profile_is_reported_not_silent() -> None:
    """Item 2. A plugin calling sys.exit() must not take the daemon with it."""

    def exiting_profile(name: str) -> ProfileView:
        def prepare(pieces: Sequence[Piece]) -> tuple[list[Piece], list[str]]:
            raise SystemExit(2)

        if name == "exits":
            return ProfileView(voice="af_heart", speed=1.1, interrupt_on=(), prepare=prepare)
        return ProfileView(voice="af_heart", speed=1.1, interrupt_on=(), prepare=passthrough)

    player = RecordingPlayer()
    bus = EventBus()
    seen: list[Event] = []
    bus.subscribe(seen.append, kinds=["error"])
    d = Daemon(FakeEngine(), player, exiting_profile, bus=bus, channels=ChannelTable())
    d.start()
    try:
        d.handle(
            Request(verb=Verb.ENQUEUE, source_id="s", payload={"text": "bye", "profile": "exits"})
        )
        assert d.wait_idle(timeout=5.0)
        assert any("SystemExit" in str(e.data.get("message", "")) for e in seen), (
            "a BaseException left no trace at all"
        )
        # One bad plugin costs one utterance, not the process.
        assert d.handle(enqueue("s", "One.")).ok is True
        assert d.wait_idle(timeout=5.0)
    finally:
        d.stop()
    assert len(player.played) == 1


def test_a_hush_during_prepare_is_not_lost() -> None:
    """Item 3. `prepare` is plugin code; a hush landing in it must still bite."""
    in_prepare = threading.Event()
    go = threading.Event()

    def slow_profile(name: str) -> ProfileView:
        def prepare(pieces: Sequence[Piece]) -> tuple[list[Piece], list[str]]:
            in_prepare.set()
            assert go.wait(timeout=5.0), "test never released prepare()"
            return list(pieces), []

        return ProfileView(voice="af_heart", speed=1.1, interrupt_on=(), prepare=prepare)

    player = RecordingPlayer()
    bus = EventBus()
    seen: list[Event] = []
    bus.subscribe(seen.append, kinds=["finished"])
    d = Daemon(FakeEngine(), player, slow_profile, bus=bus, channels=ChannelTable())
    d.start()
    try:
        d.handle(enqueue("s", "One. Two. Three. Four. Five."))
        assert in_prepare.wait(timeout=5.0), "prepare never ran"
        assert d.handle(Request(verb=Verb.HUSH, source_id="s", payload={})).ok is True
        go.set()
        assert d.wait_idle(timeout=5.0)
    finally:
        go.set()
        d.stop()
    assert player.played == [], "a hush during prepare was swallowed"
    assert seen and seen[-1].data["cancelled"] is True


def test_a_hush_sent_on_the_started_event_is_not_lost() -> None:
    """Item 3. The most natural client behaviour there is: hush on `started`."""
    player = RecordingPlayer()
    bus = EventBus()
    d = Daemon(FakeEngine(), player, profile_for, bus=bus, channels=ChannelTable())

    def hush_on_start(event: Event) -> None:
        d.handle(Request(verb=Verb.HUSH, source_id=event.source_id, payload={}))

    bus.subscribe(hush_on_start, kinds=["started"])
    d.start()
    try:
        d.handle(enqueue("s", "One. Two. Three. Four. Five."))
        assert d.wait_idle(timeout=5.0)
    finally:
        d.stop()
    assert player.played == [], "a hush sent on `started` was swallowed"


def test_a_declined_utterance_is_announced_on_the_bus(daemon) -> None:  # type: ignore[no-untyped-def]
    """A subscriber must be able to show why nothing was said."""
    d, _player, bus = daemon
    seen: list[Event] = []
    bus.subscribe(seen.append, kinds=["declined"])
    d.handle(Request(verb=Verb.SET_ROLE, source_id="s", payload={"role": "background"}))
    d.handle(
        Request(verb=Verb.ENQUEUE, source_id="s", payload={"text": "hi", "profile": "monitor"})
    )
    assert len(seen) == 1
    assert "background" in str(seen[0].data["reason"])
    assert seen[0].data["text"] == "hi"


def test_set_priority_rejects_a_boolean(daemon) -> None:  # type: ignore[no-untyped-def]
    """bool is an int: JSON `true` would otherwise quietly mean priority 1."""
    d, _player, _bus = daemon
    response = d.handle(Request(verb=Verb.SET_PRIORITY, source_id="s", payload={"priority": True}))
    assert response.ok is False
    assert "priority" in response.error
    assert d.channels.get("s") is None


# --- Review round 3: reproductions for the two retirement defects ---


def test_a_worker_that_outlives_stop_does_not_retire_its_successor() -> None:
    """A timed-out join leaves a live worker; the daemon must not be restarted
    around it, and the sentinel it has not yet eaten must not close the next one."""
    reached = threading.Event()
    release = threading.Event()
    player = HoldingPlayer(reached, release)
    d = Daemon(FakeEngine(), player, profile_for, bus=EventBus(), channels=ChannelTable())
    d.start()
    worker = d._worker
    assert worker is not None
    d.handle(enqueue("s", "One. Two. Three."))
    assert reached.wait(timeout=5.0), "the worker never started playing"
    # Simulate the join timing out rather than waiting five seconds for it.
    worker.join = lambda timeout=None: None  # type: ignore[method-assign]
    d.stop()
    assert worker.is_alive(), "the join was meant to time out"

    d.start()
    assert d._worker is worker, "start() put a second consumer on the queue"

    release.set()
    deadline = time.monotonic() + 5.0
    while d._worker is not None and time.monotonic() < deadline:
        time.sleep(0.001)
    assert d._worker is None, "the worker never retired itself"
    assert not worker.is_alive()

    # Only now can the daemon come back -- and it must actually work.
    d.start()
    try:
        assert d.handle(enqueue("s", "Four.")).ok is True
        assert d.wait_idle(timeout=5.0)
    finally:
        d.stop()
    assert len(player.played) == 2, "the restarted worker spoke nothing"


def test_a_supervisor_can_restart_the_daemon_from_the_death_event() -> None:
    """The obituary must not reach subscribers before the door is open again."""
    bus = EventBus()
    player = RecordingPlayer()
    d = Daemon(FakeEngine(), player, profile_for, bus=bus, channels=ChannelTable())
    healthy = d._finish_one
    calls = [0]

    def boom_once() -> None:
        calls[0] += 1
        if calls[0] == 1:
            raise RuntimeError("bookkeeping exploded")
        healthy()

    d._finish_one = boom_once  # type: ignore[method-assign]
    restarted = threading.Event()

    def supervise(event: Event) -> None:
        if "speech worker died" in str(event.data.get("message", "")):
            d.start()
            restarted.set()

    bus.subscribe(supervise, kinds=["error"])
    d.start()
    d.handle(enqueue("s", "One."))
    assert restarted.wait(timeout=5.0), "the supervisor never saw the death"
    try:
        assert d.handle(enqueue("s", "Two.")).ok is True, "the restart was a no-op"
        assert d.wait_idle(timeout=5.0)
    finally:
        d.stop()
    assert len(player.played) == 2


class RendezvousQueue(queue.Queue[_Job | None]):
    """The job queue, instrumented to park the worker where it finishes a job.

    Two hooks, one rendezvous. `empty()` is the question an Event-and-queue
    `wait_idle` asks in its completion path, and the gap between asking and
    acting on the answer is the whole defect — so the worker is stopped inside
    the question, with the answer already taken. The daemon as shipped asks
    nothing there, so that hook never fires, and the second `get()` stands in:
    a worker waiting for its next job has finished the last one either way.
    """

    def __init__(self) -> None:
        super().__init__()
        self.parked = threading.Event()
        self.resume = threading.Event()
        self.gets = 0

    def _is_worker(self) -> bool:
        return threading.current_thread().name == "speakd-speech"

    def empty(self) -> bool:
        answer = super().empty()
        if self._is_worker() and not self.parked.is_set():
            self.parked.set()
            assert self.resume.wait(timeout=5.0), "the harness never resumed the worker"
        return answer

    def get(self, block: bool = True, timeout: float | None = None) -> _Job | None:
        if self._is_worker():
            self.gets += 1
            if self.gets == 2:
                self.parked.set()
        return super().get(block, timeout)


def test_wait_idle_does_not_rest_on_an_unsynchronised_queue_check() -> None:
    """Catches a revert to `_idle.wait(timeout) and self._jobs.empty()`.

    The interleaving is forced through the queue the daemon holds rather than
    guessed at with a sleep: the worker is parked where it finishes the first
    utterance, the second is enqueued behind it, and only then is it released.
    An implementation whose idea of idle is an Event plus a queue check has by
    then recorded the queue as empty and sets idle with the second utterance
    still to come.
    """
    jobs = RendezvousQueue()
    player = HoldingPlayer(threading.Event(), threading.Event(), hold_on=2)
    d = Daemon(FakeEngine(), player, profile_for, bus=EventBus(), channels=ChannelTable())
    d._jobs = jobs
    d.start()
    try:
        assert d.handle(enqueue("s", "One.")).ok is True
        assert jobs.parked.wait(timeout=5.0), "the worker never finished the first utterance"
        assert d.handle(enqueue("s", "Two. Three.")).ok is True
        jobs.resume.set()
        assert player.reached.wait(timeout=5.0), "the second utterance never started playing"
        assert d.wait_idle(timeout=0.3) is False, "reported idle with an utterance still playing"
        assert len(player.played) == 2
        player.release.set()
        assert d.wait_idle(timeout=5.0) is True
        assert len(player.played) == 3
    finally:
        jobs.resume.set()
        player.release.set()
        d.stop()


def test_a_hush_discards_the_queue_behind_the_utterance_it_cancels() -> None:
    """Hush means silence now, not silence once the backlog has been read out."""
    reached = threading.Event()
    release = threading.Event()
    player = HoldingPlayer(reached, release)
    bus = EventBus()
    seen: list[Event] = []
    bus.subscribe(seen.append, kinds=["discarded"])
    d = Daemon(FakeEngine(), player, profile_for, bus=bus, channels=ChannelTable())
    d.start()
    try:
        d.handle(enqueue("s", "One. Two. Three."))
        assert reached.wait(timeout=5.0), "the worker never started playing"
        assert d.handle(enqueue("s", "Queued one.")).ok is True
        assert d.handle(enqueue("s", "Queued two.")).ok is True
        response = d.handle(Request(verb=Verb.HUSH, source_id="s", payload={}))
        assert response.ok is True
        assert response.data["discarded"] == 2
        release.set()
        assert d.wait_idle(timeout=5.0), "the discarded jobs were never released"
        # Nothing vanished: the drop was reported, once, with its count and
        # the channel it came off.
        assert len(seen) == 1, "one hush, one announcement"
        assert seen[0].data["count"] == 2
        assert seen[0].data["sources"] == ["s"]
        assert len(player.played) == 1, "the hushed utterance kept playing"
        # And the channel is not poisoned — the next utterance still speaks.
        assert d.handle(enqueue("s", "After.")).ok is True
        assert d.wait_idle(timeout=5.0)
    finally:
        release.set()
        d.stop()
    assert len(player.played) == 2


def test_a_hush_that_names_a_channel_leaves_the_other_channels_alone() -> None:
    """Sessions are separate, so silencing one must not empty another's queue.

    The Claude Code hook has sent one hush per channel on every prompt since
    it was written, and `send.hush` has documented itself as stopping that
    source the whole time. Under the daemon-wide meaning these verbs used to
    have, the first of that pair also threw away whatever a second session
    was waiting to hear.
    """
    reached = threading.Event()
    release = threading.Event()
    player = HoldingPlayer(reached, release)
    bus = EventBus()
    seen: list[Event] = []
    bus.subscribe(seen.append, kinds=["started"])
    d = Daemon(FakeEngine(), player, profile_for, bus=bus, channels=ChannelTable())
    d.start()
    try:
        d.handle(enqueue("a", "Holding."))
        assert reached.wait(timeout=5.0), "the worker never started playing"
        assert d.handle(enqueue("a", "Noisy one.")).ok is True
        assert d.handle(enqueue("b", "Wanted one.")).ok is True
        response = d.handle(Request(verb=Verb.HUSH, source_id="a", payload={}))
        assert response.ok is True
        assert response.data["discarded"] == 1
        assert response.data["scope"] == "channel"
        release.set()
        assert d.wait_idle(timeout=5.0), "the queue never drained"
    finally:
        release.set()
        d.stop()
    spoken = [event.data["text"] for event in seen]
    assert "Wanted one." in spoken, "a channel hush ate another channel's queue"
    assert "Noisy one." not in spoken, "the hushed channel spoke anyway"


def test_a_hush_with_no_source_still_stops_every_channel() -> None:
    """Empty means everything, exactly as it did before the verbs took a scope."""
    reached = threading.Event()
    release = threading.Event()
    player = HoldingPlayer(reached, release)
    bus = EventBus()
    seen: list[Event] = []
    bus.subscribe(seen.append, kinds=["started"])
    d = Daemon(FakeEngine(), player, profile_for, bus=bus, channels=ChannelTable())
    d.start()
    try:
        d.handle(enqueue("a", "Holding."))
        assert reached.wait(timeout=5.0), "the worker never started playing"
        assert d.handle(enqueue("a", "One.")).ok is True
        assert d.handle(enqueue("b", "Two.")).ok is True
        response = d.handle(Request(verb=Verb.HUSH, source_id="", payload={}))
        assert response.ok is True
        assert response.data["discarded"] == 2
        assert response.data["scope"] == "all"
        release.set()
        assert d.wait_idle(timeout=5.0), "the discarded jobs were never released"
    finally:
        release.set()
        d.stop()
    assert [event.data["text"] for event in seen] == ["Holding."], "a hush left speech behind"


def test_a_hush_for_one_channel_does_not_cut_off_another_mid_sentence() -> None:
    """The voice in the room belongs to someone. Only its own hush stops it."""
    reached = threading.Event()
    release = threading.Event()
    player = HoldingPlayer(reached, release)
    d = Daemon(FakeEngine(), player, profile_for, bus=EventBus(), channels=ChannelTable())
    d.start()
    try:
        d.handle(enqueue("a", "One. Two. Three."))
        assert reached.wait(timeout=5.0), "the worker never started playing"
        assert d.handle(Request(verb=Verb.HUSH, source_id="b", payload={})).ok is True
        release.set()
        assert d.wait_idle(timeout=5.0)
    finally:
        release.set()
        d.stop()
    assert len(player.played) == 3, "a hush meant for another channel cut this one short"


def test_cancel_reports_its_scope_as_well() -> None:
    """Read off the response rather than inferred from the request that was sent.

    A client that assumed which of the two it had asked for is how the scope
    came to be reported at all.
    """
    d = Daemon(
        FakeEngine(), RecordingPlayer(), profile_for, bus=EventBus(), channels=ChannelTable()
    )
    d.start()
    try:
        named = d.handle(Request(verb=Verb.CANCEL, source_id="s", payload={}))
        assert named.data["scope"] == "channel"
        everything = d.handle(Request(verb=Verb.CANCEL, source_id="", payload={}))
        assert everything.data["scope"] == "all"
    finally:
        d.stop()


def test_a_hush_does_not_eat_a_stop_that_is_already_under_way() -> None:
    """A sentinel met while draining goes back: stop() is waiting on it."""
    reached = threading.Event()
    release = threading.Event()
    player = HoldingPlayer(reached, release)
    d = Daemon(FakeEngine(), player, profile_for, bus=EventBus(), channels=ChannelTable())
    d.start()
    d.handle(enqueue("s", "One. Two. Three."))
    assert reached.wait(timeout=5.0), "the worker never started playing"
    stopper = threading.Thread(target=d.stop, daemon=True)
    stopper.start()
    deadline = time.monotonic() + 5.0
    while d._running and time.monotonic() < deadline:
        time.sleep(0.001)
    assert d._running is False, "stop() never took effect"
    d.handle(Request(verb=Verb.HUSH, source_id="s", payload={}))
    release.set()
    stopper.join(timeout=10.0)
    assert not stopper.is_alive(), "the hush swallowed the stop sentinel"
    assert d._worker is None


class ExitingPlayer(RecordingPlayer):
    """A sink that fails outside `Exception` — `pipeline.speak` lets this through."""

    def play(self, audio: np.ndarray, sample_rate: int) -> None:
        super().play(audio, sample_rate)
        raise SystemExit(3)


def test_a_base_exception_from_the_player_still_pairs_started_with_finished() -> None:
    """A subscriber pairing the two must never be left waiting on a `finished`."""
    player = ExitingPlayer()
    bus = EventBus()
    seen: list[Event] = []
    bus.subscribe(seen.append)
    d = Daemon(FakeEngine(), player, profile_for, bus=bus, channels=ChannelTable())
    d.start()
    try:
        d.handle(enqueue("s", "One."))
        assert d.wait_idle(timeout=5.0)
    finally:
        d.stop()
    kinds = [e.kind for e in seen]
    assert kinds.count("started") == 1
    assert kinds.count("finished") == 1, "started went out without a finished"
    assert any("SystemExit" in str(e.data.get("message", "")) for e in seen if e.kind == "error")


# --- Consolidation round: reproductions for the stranded sentinel and the leak ---


class GatedQueue(queue.Queue[_Job | None]):
    """Holds a control thread's stop sentinel at a gate the test opens.

    `stop()` reads `_worker` and only then deposits the sentinel. Holding the
    deposit open lets a retiring worker get in between, which is the whole
    question: does the sentinel still land, and if it does, who is left to
    eat it?
    """

    def __init__(self) -> None:
        super().__init__()
        self.parked = threading.Event()
        self.gate = threading.Event()

    def put(self, item: _Job | None, block: bool = True, timeout: float | None = None) -> None:
        if item is None and threading.current_thread().name != "speakd-speech":
            self.parked.set()
            assert self.gate.wait(timeout=5.0), "the test never opened the gate"
        super().put(item, block, timeout)


def test_a_stop_racing_a_dying_worker_strands_no_sentinel() -> None:
    reached = threading.Event()
    release = threading.Event()
    player = HoldingPlayer(reached, release)
    jobs = GatedQueue()
    d = Daemon(FakeEngine(), player, profile_for, bus=EventBus(), channels=ChannelTable())
    d._jobs = jobs
    healthy = d._finish_one
    calls = [0]

    def boom_once() -> None:
        calls[0] += 1
        if calls[0] == 1:
            raise RuntimeError("bookkeeping exploded")
        healthy()

    d._finish_one = boom_once  # type: ignore[method-assign]
    d.start()
    d.handle(enqueue("s", "One. Two."))
    assert reached.wait(timeout=5.0), "the worker never started playing"

    stopper = threading.Thread(target=d.stop, daemon=True)
    stopper.start()
    assert jobs.parked.wait(timeout=5.0), "stop() never reached its sentinel"
    # The worker dies and retires with the sentinel still held at the gate.
    release.set()
    # Bounded rather than open-ended: a daemon that deposits the sentinel under
    # the lock leaves the worker blocked on `_retire`, so `_worker` stays put
    # and this wait is expected to run out. One that deposits it outside the
    # lock lets the worker retire immediately, and the wait ends at once.
    deadline = time.monotonic() + 0.3
    while d._worker is not None and time.monotonic() < deadline:
        time.sleep(0.001)
    jobs.gate.set()
    stopper.join(timeout=10.0)
    assert not stopper.is_alive(), "stop() never returned"

    # Whichever order won, no sentinel may be left for the next worker to eat.
    d.start()
    try:
        assert d.handle(enqueue("s", "Three.")).ok is True
        assert d.wait_idle(timeout=5.0)
    finally:
        d.stop()
    assert len(player.played) == 2, "a stranded sentinel closed the restarted daemon"


def test_a_subscriber_that_fails_mid_hush_does_not_strand_the_queue() -> None:
    """`EventBus.publish` swallows a subscriber's Exception, not its BaseException."""
    reached = threading.Event()
    release = threading.Event()
    player = HoldingPlayer(reached, release)
    bus = EventBus()

    def explode(event: Event) -> None:
        raise SystemExit(7)

    bus.subscribe(explode, kinds=["discarded"])
    d = Daemon(FakeEngine(), player, profile_for, bus=bus, channels=ChannelTable())
    d.start()
    try:
        d.handle(enqueue("s", "One. Two."))
        assert reached.wait(timeout=5.0), "the worker never started playing"
        assert d.handle(enqueue("s", "Queued one.")).ok is True
        assert d.handle(enqueue("s", "Queued two.")).ok is True
        with pytest.raises(SystemExit):
            d.handle(Request(verb=Verb.HUSH, source_id="s", payload={}))
        release.set()
        assert d.wait_idle(timeout=2.0), (
            "the hush took jobs off the queue without releasing their count"
        )
    finally:
        release.set()
        d.stop()


def test_cancel_skips_one_utterance_and_lets_the_queue_continue() -> None:
    """`cancel` is "skip this one"; `hush` is "stop talking". Not synonyms."""
    reached = threading.Event()
    release = threading.Event()
    player = HoldingPlayer(reached, release)
    d = Daemon(FakeEngine(), player, profile_for, bus=EventBus(), channels=ChannelTable())
    d.start()
    try:
        d.handle(enqueue("s", "One. Two. Three."))
        assert reached.wait(timeout=5.0), "the worker never started playing"
        assert d.handle(enqueue("s", "Second.")).ok is True
        assert d.handle(enqueue("s", "Third.")).ok is True
        response = d.handle(Request(verb=Verb.CANCEL, source_id="s", payload={}))
        assert response.ok is True
        assert response.data["discarded"] == 0, "cancel must leave the queue alone"
        release.set()
        assert d.wait_idle(timeout=5.0)
    finally:
        release.set()
        d.stop()
    # One held segment of the cancelled utterance, then both queued ones.
    assert len(player.played) == 3


def test_start_reports_which_of_the_three_things_happened() -> None:
    d = Daemon(
        FakeEngine(), RecordingPlayer(), profile_for, bus=EventBus(), channels=ChannelTable()
    )
    first = d.start()
    try:
        assert first.ok is True
        assert first.data["started"] is True
        again = d.start()
        assert again.ok is True
        assert again.data["started"] is False
        assert "already running" in str(again.data["reason"])
    finally:
        d.stop()


def test_start_refuses_visibly_while_a_previous_worker_is_still_finishing() -> None:
    reached = threading.Event()
    release = threading.Event()
    player = HoldingPlayer(reached, release)
    d = Daemon(FakeEngine(), player, profile_for, bus=EventBus(), channels=ChannelTable())
    d.start()
    worker = d._worker
    assert worker is not None
    d.handle(enqueue("s", "One. Two. Three."))
    assert reached.wait(timeout=5.0), "the worker never started playing"
    worker.join = lambda timeout=None: None  # type: ignore[method-assign]
    d.stop()
    assert worker.is_alive(), "the join was meant to time out"

    refused = d.start()
    assert refused.ok is False, "a supervisor cannot tell this from 'already running'"
    assert "not finished" in refused.error

    release.set()
    deadline = time.monotonic() + 5.0
    while d._worker is not None and time.monotonic() < deadline:
        time.sleep(0.001)
    assert d._worker is None, "the worker never retired itself"
    try:
        assert d.start().data["started"] is True
    finally:
        d.stop()


def test_a_thread_that_will_not_start_leaves_the_daemon_recoverable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`can't start new thread` must not wedge the daemon permanently."""

    class UnstartableThread(threading.Thread):
        def start(self) -> None:
            raise RuntimeError("can't start new thread")

    d = Daemon(
        FakeEngine(), RecordingPlayer(), profile_for, bus=EventBus(), channels=ChannelTable()
    )
    monkeypatch.setattr(threading, "Thread", UnstartableThread)
    with pytest.raises(RuntimeError):
        d.start()
    monkeypatch.undo()
    # Rolled back rather than left accepting speech with nothing to consume it.
    assert d.handle(enqueue("s", "One.")).ok is False
    assert d.start().data["started"] is True
    try:
        assert d.handle(enqueue("s", "One.")).ok is True
        assert d.wait_idle(timeout=5.0)
    finally:
        d.stop()


def test_a_worker_that_is_no_longer_the_daemons_still_reports_its_death() -> None:
    """The guarded branch of `_retire`: an orphan announces, and changes nothing."""
    bus = EventBus()
    seen: list[Event] = []
    bus.subscribe(seen.append, kinds=["error"])
    player = RecordingPlayer()
    d = Daemon(FakeEngine(), player, profile_for, bus=bus, channels=ChannelTable())
    d.start()
    try:
        # Called from a thread that is not the daemon's worker, which is what
        # a worker orphaned by a timed-out stop() amounts to.
        d._retire("Traceback (most recent call last):\n  RuntimeError: boom")
        assert any("speech worker died" in str(e.data.get("message", "")) for e in seen)
        assert any("boom" in str(e.data.get("message", "")) for e in seen)
        # The running daemon is untouched.
        assert d._running is True
        assert d._worker is not None
        assert d.handle(enqueue("s", "One.")).ok is True
        assert d.wait_idle(timeout=5.0)
    finally:
        d.stop()
    assert len(player.played) == 1


# --- Whole-branch review: the races per-task review could not see ---


def test_stop_cannot_join_a_worker_that_start_has_not_started(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`_worker` must not be visible to stop() before the thread is running.

    start() published `_worker` and released the lock, then started the
    thread. A stop() landing in between saw a worker, deposited its sentinel
    and called join() on a thread that had never been started, which is a
    RuntimeError out of stop() -- and out of serve(), as a traceback.
    """
    real_start = threading.Thread.start
    reached = threading.Event()
    proceed = threading.Event()

    def slow_start(self: threading.Thread) -> None:
        # Only the speech worker: the test's own threads must still start.
        if self.name == "speakd-speech":
            reached.set()
            assert proceed.wait(timeout=5.0), "the test never released the worker's start()"
        real_start(self)

    monkeypatch.setattr(threading.Thread, "start", slow_start)
    d = Daemon(
        FakeEngine(), RecordingPlayer(), profile_for, bus=EventBus(), channels=ChannelTable()
    )
    starter = threading.Thread(target=d.start, name="test-starter")
    starter.start()
    assert reached.wait(timeout=5.0), "start() never reached the worker's start()"
    # Released a moment from now: with the fix, stop() waits out the rest of
    # start() on the lock; without it, stop() runs straight through and joins
    # a thread that has not been started.
    threading.Timer(0.5, proceed.set).start()
    try:
        d.stop()
    finally:
        proceed.set()
        starter.join(timeout=5.0)
        d.stop()
    assert d._worker is None, "stop() left a worker behind"


def _lock_is_held(condition: threading.Condition) -> bool:
    """Is `condition`'s lock held right now?

    Probed from a thread that cannot own it: the lock is reentrant, so the
    thread being asked about would acquire its own lock and learn nothing.
    """
    outcome: list[bool] = []

    def probe() -> None:
        if condition.acquire(timeout=0.25):
            condition.release()
            outcome.append(False)
        else:
            outcome.append(True)

    prober = threading.Thread(target=probe, name="lock-probe")
    prober.start()
    prober.join(timeout=5.0)
    assert outcome, "the lock probe never finished"
    return outcome[0]


class LockWatchingEvent(threading.Event):
    """A cancel Event that records whether its setter held `_idle`."""

    def __init__(self, condition: threading.Condition) -> None:
        super().__init__()
        self._condition = condition
        self.held_at_set: bool | None = None
        self.sets = 0

    def set(self) -> None:
        self.sets += 1
        self.held_at_set = _lock_is_held(self._condition)
        super().set()


def test_hush_sets_the_cancel_event_under_the_lock_that_guards_it() -> None:
    """Reading `_cancel` under the lock and setting it outside loses hushes.

    In that window the worker can finish job N, take job N+1 off the queue and
    install its fresh Event. Hush then sets an Event nobody is watching:
    player.stop() cuts the current segment, hush answers `ok`, the drain finds
    the queue already empty -- and job N+1 speaks on. The Event must be set
    under the same lock it was read under, which `Event.set()` never blocks
    for. Only `player.stop()` has to stay outside.
    """
    d = Daemon(
        FakeEngine(), RecordingPlayer(), profile_for, bus=EventBus(), channels=ChannelTable()
    )
    d.start()
    try:
        watcher = LockWatchingEvent(d._idle)
        with d._idle:
            d._cancel = watcher
        # No source, because a hush now reaches only the channel it names and
        # nothing here is speaking on one: a named hush would find `_speaking`
        # empty, set no Event, and measure nothing. The locking under test is
        # the same either way.
        assert d.handle(Request(verb=Verb.HUSH, source_id="", payload={})).ok is True
        # Read before the teardown can touch it. `_cancel` is still this
        # Event -- nothing has been spoken, so `_speak` never replaced it --
        # so the `d.stop()` below sets the very same object, under the lock,
        # and would overwrite the one measurement this test exists to take.
        held_at_set = watcher.held_at_set
        was_set = watcher.is_set()
    finally:
        d.stop()
    assert was_set
    assert held_at_set is True, "hush set the cancel Event with the lock released"


def test_stop_sets_the_cancel_event_under_the_lock_that_guards_it() -> None:
    """The same window, and the same loss, on the way down.

    One stop() and no teardown after it, deliberately: a second set() on the
    same Event would overwrite the measurement.
    """
    d = Daemon(
        FakeEngine(), RecordingPlayer(), profile_for, bus=EventBus(), channels=ChannelTable()
    )
    d.start()
    watcher = LockWatchingEvent(d._idle)
    with d._idle:
        d._cancel = watcher
    d.stop()
    assert watcher.is_set()
    assert watcher.held_at_set is True, "stop() set the cancel Event with the lock released"
    assert watcher.sets == 1, "something set the Event again and overwrote the measurement"


class UnstoppablePlayer(RecordingPlayer):
    """A sink whose stop() raises, as PortAudio's does when the device goes."""

    def stop(self) -> None:
        super().stop()
        raise RuntimeError("PortAudioError: device unavailable")


def test_a_player_that_cannot_be_silenced_still_lets_the_worker_leave() -> None:
    """An unguarded player.stop() in stop() kills the daemon permanently.

    It raises before the sentinel is deposited, so the worker is never told to
    leave, `_worker` keeps pointing at it, and every later start() returns
    `_STILL_FINISHING`. In serve() it escapes as a traceback. The headset that
    walked out of range is exactly the case `pipeline` already anticipates.
    """
    bus = EventBus()
    seen: list[Event] = []
    bus.subscribe(seen.append, kinds=["error"])
    d = Daemon(FakeEngine(), UnstoppablePlayer(), profile_for, bus=bus, channels=ChannelTable())
    d.start()
    d.stop()
    assert d._worker is None, "the worker was never told to leave"
    assert any("device unavailable" in str(e.data.get("message", "")) for e in seen), (
        "a sink that failed must be reported, like every other sink failure here"
    )
    # And the daemon is startable again, rather than refusing forever.
    assert d.start().data["started"] is True
    try:
        assert d.handle(enqueue("s", "One.")).ok is True
        assert d.wait_idle(timeout=5.0)
    finally:
        d.stop()


def test_a_hushed_utterance_is_not_reported_as_a_stopped_daemon() -> None:
    """One message for two causes sends a reader hunting a stop that never was.

    `_drain_queued` is the hush path; `_retire` and `_speak`'s recheck are the
    stop path. A user who hushes and reads "the daemon stopped before this
    reached the engine" is being told something false about a daemon that is
    still running and about to speak again. The two now differ by kind as
    well as by wording, and both differences are load-bearing.
    """
    reached = threading.Event()
    release = threading.Event()
    player = HoldingPlayer(reached, release)
    bus = EventBus()
    seen: list[Event] = []
    bus.subscribe(seen.append)
    d = Daemon(FakeEngine(), player, profile_for, bus=bus, channels=ChannelTable())
    d.start()
    try:
        d.handle(enqueue("s", "One. Two. Three."))
        assert reached.wait(timeout=5.0), "the worker never started playing"
        assert d.handle(enqueue("s", "Queued one.")).ok is True
        assert d.handle(Request(verb=Verb.HUSH, source_id="s", payload={})).data["discarded"] == 1
        release.set()
        assert d.wait_idle(timeout=5.0)
    finally:
        release.set()
        d.stop()
    discarded = [e for e in seen if e.kind == "discarded"]
    assert len(discarded) == 1
    reason = str(discarded[0].data.get("reason", ""))
    assert "hush" in reason
    assert "stopped" not in reason, f"a hush reported as a stop: {reason!r}"
    # And it is nowhere among the errors: the kind is the thing a GUI matches
    # on, so a hush that also raised an error would be counted twice.
    assert not [e for e in seen if e.kind == "error" and "discarded" in str(e.data)]


def test_work_the_daemon_stopped_on_still_says_so() -> None:
    """The stop path keeps its own message: the two causes stay distinguishable."""
    reached = threading.Event()
    release = threading.Event()
    player = HoldingPlayer(reached, release)
    bus = EventBus()
    seen: list[Event] = []
    bus.subscribe(seen.append, kinds=["error"])
    d = Daemon(FakeEngine(), player, profile_for, bus=bus, channels=ChannelTable())
    d.start()
    d.handle(enqueue("s", "One. Two. Three."))
    assert reached.wait(timeout=5.0), "the worker never started playing"
    assert d.handle(enqueue("s", "Queued one.")).ok is True
    release.set()
    d.stop()
    discarded = [str(e.data.get("message", "")) for e in seen if "discarded" in str(e.data)]
    assert discarded, "the queued utterance vanished without a word"
    assert all("stopped" in message for message in discarded)
    assert all("hush" not in message for message in discarded)


# --- Whole-branch review: guards nothing was pinning ---


class RetiringPlayer(RecordingPlayer):
    """Retires the daemon's worker from inside stop(), once.

    stop() calls player.stop() between reading `_worker` and depositing its
    sentinel, which is exactly the window in which the worker it read can
    retire itself and stop being the daemon's.
    """

    def __init__(self) -> None:
        super().__init__()
        self.daemon: Daemon | None = None
        self.retired = False

    def stop(self) -> None:
        super().stop()
        if self.retired or self.daemon is None:
            return
        self.retired = True
        d = self.daemon
        d._jobs.put(None)
        deadline = time.monotonic() + 5.0
        while d._worker is not None and time.monotonic() < deadline:
            time.sleep(0.001)


def test_a_stop_whose_worker_retires_first_strands_no_sentinel() -> None:
    """The identity check on stop()'s deposit is what keeps the queue clean.

    Deposited unconditionally, a sentinel left behind by a worker that had
    already retired is unowned: the next start()'s worker eats it and closes
    the daemon it was just asked to run.
    """
    player = RetiringPlayer()
    d = Daemon(FakeEngine(), player, profile_for, bus=EventBus(), channels=ChannelTable())
    player.daemon = d
    d.start()
    d.stop()
    assert d._worker is None, "the worker never retired"
    assert d._jobs.qsize() == 0, "stop() left an unowned sentinel on the queue"
    assert d.start().data["started"] is True
    try:
        assert d.handle(enqueue("s", "One.")).ok is True
        assert d.wait_idle(timeout=5.0)
        assert len(player.played) == 1, "the new worker was closed by a sentinel meant for no one"
    finally:
        d.stop()


def test_an_enqueue_racing_a_stop_is_refused_rather_than_queued_unspoken() -> None:
    """The second `_running` check, under the lock, is what refuses it.

    Without it a job accepted just before a stop lands in the queue behind an
    `ok` response, with no worker left to say it: speech that disappears in
    silence, which is the one thing this daemon must never do.
    """
    reached = threading.Event()
    release = threading.Event()

    def parking_profile(name: str) -> ProfileView:
        if name == "slow":
            # Called after `_enqueue`'s first `_running` check and before it
            # takes the lock -- the whole of the window, held open.
            reached.set()
            assert release.wait(timeout=5.0), "the test never released the enqueue"
        return ProfileView(voice="af_heart", speed=1.1, interrupt_on=(), prepare=passthrough)

    player = RecordingPlayer()
    d = Daemon(FakeEngine(), player, parking_profile, bus=EventBus(), channels=ChannelTable())
    d.start()
    answers: list[object] = []

    def ask() -> None:
        answers.append(
            d.handle(
                Request(
                    verb=Verb.ENQUEUE,
                    source_id="s",
                    payload={"text": "One.", "profile": "slow"},
                )
            )
        )

    asker = threading.Thread(target=ask, name="test-enqueue")
    asker.start()
    assert reached.wait(timeout=5.0), "the enqueue never reached the window"
    d.stop()
    release.set()
    asker.join(timeout=5.0)
    assert not asker.is_alive()

    answer = answers[0]
    assert isinstance(answer, Response)
    assert answer.ok is False, "an enqueue that can never be spoken was answered ok"
    assert "not running" in answer.error
    assert d._jobs.qsize() == 0, "the refused job was queued anyway"
    assert d.wait_idle(timeout=1.0), "the refused job was counted as pending"
    assert player.played == []


def test_a_double_release_cannot_drive_the_pending_count_negative() -> None:
    """The floor in `_release` is what keeps wait_idle able to return.

    A count released twice goes negative without it, and a negative count
    never reaches zero: wait_idle blocks to its timeout forever after, on a
    daemon that is in fact idle.
    """
    player = RecordingPlayer()
    d = Daemon(FakeEngine(), player, profile_for, bus=EventBus(), channels=ChannelTable())
    d.start()
    try:
        assert d.handle(enqueue("s", "One.")).ok is True
        assert d.wait_idle(timeout=5.0)
        # The double release the floor exists for.
        d._release(1)
        assert d.wait_idle(timeout=1.0), "wait_idle stopped coming back"
        assert d.handle(enqueue("s", "Two.")).ok is True
        assert d.wait_idle(timeout=5.0)
        assert len(player.played) == 2
    finally:
        d.stop()


# --- Re-review: a hush that meets a failing sink ---


class UnstoppableHoldingPlayer(HoldingPlayer):
    """Holds inside play(), and refuses to be stopped, as PortAudio can."""

    def stop(self) -> None:
        raise RuntimeError("PortAudioError: device unavailable")


def test_a_hush_whose_sink_fails_still_clears_the_queue() -> None:
    """A raise from player.stop() must not carry off the drain behind it.

    Unguarded, it skipped `_drain_queued` entirely: hush reported an error and
    every queued utterance survived it, so the daemon answered a request to
    stop talking by going on talking. Reporting the sink failure is right;
    losing the drain with it is the inverse of what hush is for.
    """
    reached = threading.Event()
    release = threading.Event()
    player = UnstoppableHoldingPlayer(reached, release)
    bus = EventBus()
    seen: list[Event] = []
    bus.subscribe(seen.append, kinds=["discarded"])
    d = Daemon(FakeEngine(), player, profile_for, bus=bus, channels=ChannelTable())
    d.start()
    try:
        d.handle(enqueue("s", "One. Two. Three."))
        assert reached.wait(timeout=5.0), "the worker never started playing"
        assert d.handle(enqueue("s", "Queued one.")).ok is True
        assert d.handle(enqueue("s", "Queued two.")).ok is True

        # The sink failure still reaches the caller -- it is not swallowed.
        with pytest.raises(RuntimeError):
            d.handle(Request(verb=Verb.HUSH, source_id="s", payload={}))
        assert d._jobs.qsize() == 0, "a hush that met a failing sink kept the queue"
    finally:
        release.set()
        d.stop()
    assert d.wait_idle(timeout=5.0)
    assert len(seen) == 1, "the dropped utterances were never reported"
    assert seen[0].data["count"] == 2
    assert "hush" in str(seen[0].data.get("reason", ""))
    assert len(player.played) == 1, "the daemon went on speaking through the hush"


# --- Re-review: where speech has reached, while it is still speaking ---


class PlayerThatLosesTheDevice(RecordingPlayer):
    """Raises on its `fail_on`-th play(), as a sink whose device went does."""

    def __init__(self, fail_on: int = 1) -> None:
        super().__init__()
        self.fail_on = fail_on
        self.calls = 0

    def play(self, audio: np.ndarray, sample_rate: int) -> None:
        self.calls += 1
        if self.calls == self.fail_on:
            raise RuntimeError("PortAudioError: device unavailable")
        super().play(audio, sample_rate)


def test_a_position_reaches_subscribers_while_that_sentence_is_still_playing() -> None:
    """A monitor highlights the sentence being spoken, so it needs it then.

    Published off `result.timeline` instead, every position of an utterance
    arrives in one burst after the last word -- measured against the real
    daemon as five positions and `finished` at the same instant, eight
    seconds of speech after `started`. The events' own `played_at` stamps are
    right either way, which is why only arrival can show this.
    """
    reached = threading.Event()
    release = threading.Event()
    player = HoldingPlayer(reached, release)
    bus = EventBus()
    seen: list[Event] = []
    arrived = threading.Event()

    def watch(event: Event) -> None:
        seen.append(event)
        if event.kind == "position":
            arrived.set()

    bus.subscribe(watch, kinds=["started", "position", "finished"])
    d = Daemon(FakeEngine(), player, profile_for, bus=bus, channels=ChannelTable())
    d.start()
    try:
        assert d.handle(enqueue("s", "One. Two. Three.")).ok is True
        assert reached.wait(timeout=5.0), "playback of the first sentence never began"
        assert arrived.wait(timeout=5.0), "no position arrived while the first sentence played"
        positions = [event for event in seen if event.kind == "position"]
        assert [event.data["text"] for event in positions] == ["One."]
        assert positions[0].data["span_start"] == 0
        assert positions[0].data["played_at"] is not None
        assert "finished" not in [event.kind for event in seen], (
            "the utterance was already over by the time the position arrived"
        )
    finally:
        release.set()
        d.stop()


def test_every_sentence_is_announced_once_and_before_finished() -> None:
    """Live publishing must not cost the guarantee it replaced.

    One `position` per segment, in playback order, with the fields a
    subscriber already reads -- and all of them out before `finished`, so a
    monitor never sees the utterance end with a sentence still highlighted.
    """
    bus = EventBus()
    seen: list[Event] = []
    bus.subscribe(seen.append, kinds=["position", "finished"])
    d = Daemon(FakeEngine(), RecordingPlayer(), profile_for, bus=bus, channels=ChannelTable())
    d.start()
    try:
        assert d.handle(enqueue("s", "One. Two. Three.")).ok is True
        assert d.wait_idle(timeout=5.0)
    finally:
        d.stop()
    kinds = [event.kind for event in seen]
    assert kinds == ["position", "position", "position", "finished"], kinds
    positions = [event for event in seen if event.kind == "position"]
    assert [event.data["text"] for event in positions] == ["One.", "Two.", "Three."]
    assert [event.data["span_start"] for event in positions] == [0, 5, 10]
    offsets = [event.data["audio_offset"] for event in positions]
    assert offsets == sorted(offsets)  # type: ignore[type-var]


def test_a_sentence_whose_playback_failed_is_still_announced() -> None:
    """The deliberate change, pinned so it cannot move back unnoticed.

    A position is published before `play()` is called, because after it the
    sentence has been spoken. Nothing can know then that the sink is about to
    fail, so the failing sentence is announced -- where publishing off the
    finished timeline said nothing about it, since only a segment that played
    is ever appended there. A sink that fails mid-write has already put part
    of that sentence through the speakers, so announcing it is this daemon's
    promise rather than an exception to it. The failure is still reported
    too, on its own `error` event.
    """
    bus = EventBus()
    seen: list[Event] = []
    bus.subscribe(seen.append, kinds=["position", "error", "finished"])
    player = PlayerThatLosesTheDevice(fail_on=2)
    d = Daemon(FakeEngine(), player, profile_for, bus=bus, channels=ChannelTable())
    d.start()
    try:
        assert d.handle(enqueue("s", "One. Two. Three.")).ok is True
        assert d.wait_idle(timeout=5.0)
    finally:
        d.stop()
    positions = [event for event in seen if event.kind == "position"]
    assert [event.data["text"] for event in positions] == ["One.", "Two."]
    errors = [event for event in seen if event.kind == "error"]
    assert any("device unavailable" in str(event.data.get("message", "")) for event in errors)
    finished = [event for event in seen if event.kind == "finished"]
    assert finished and finished[0].data["aborted"] is True


def test_started_announces_every_segment_and_positions_say_which(daemon) -> None:  # type: ignore[no-untyped-def]
    d, _player, bus = daemon
    seen: list[Event] = []
    bus.subscribe(seen.append)
    d.handle(enqueue("s", "One. Two. Six."))
    assert d.wait_idle(timeout=5.0)
    started = next(e for e in seen if e.kind == "started")
    segments = started.data["segments"]
    assert isinstance(segments, list)
    assert [s["text"] for s in segments] == ["One.", "Two.", "Six."]
    assert [s["index"] for s in segments] == [0, 1, 2]
    assert [e.data["index"] for e in seen if e.kind == "position"] == [0, 1, 2]
