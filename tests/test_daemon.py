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
    bus.subscribe(seen.append, kinds=["error"])
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
        # Nothing vanished: each dropped utterance was reported.
        assert len([e for e in seen if "discarded" in str(e.data.get("message", ""))]) == 2
        assert len(player.played) == 1, "the hushed utterance kept playing"
        # And the channel is not poisoned — the next utterance still speaks.
        assert d.handle(enqueue("s", "After.")).ok is True
        assert d.wait_idle(timeout=5.0)
    finally:
        release.set()
        d.stop()
    assert len(player.played) == 2


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
        if "discarded" in str(event.data.get("message", "")):
            raise SystemExit(7)

    bus.subscribe(explode, kinds=["error"])
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
