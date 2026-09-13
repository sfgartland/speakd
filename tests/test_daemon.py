"""Tests for the daemon's request handling and speech worker."""

import threading
import time
from collections.abc import Callable, Sequence

import numpy as np
import pytest

from speakd.channels import ChannelTable
from speakd.daemon import Daemon, ProfileView
from speakd.events import Event, EventBus
from speakd.model import Piece, Role
from speakd.player import Player, RecordingPlayer
from speakd.protocol import Request, Response, Verb
from speakd.synth import Synthesizer
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


class _RejectedIdleDaemon(Daemon):
    """`wait_idle` as an Event plus a queue check — the version not shipped.

    Kept executable because nothing else can pin the difference. The two
    designs are observationally identical through the public API *except* when
    the control thread's `clear()` lands between the worker's queue check and
    its `set()`, and no public seam exists between those two statements. So the
    gap is injected here, at the point `_run` evaluated it, at a width an
    ordinary preemption reaches.
    """

    gap = 0.15

    def __init__(
        self,
        engine: Synthesizer,
        player: Player,
        profile_for: Callable[[str], ProfileView],
        bus: EventBus | None = None,
        channels: ChannelTable | None = None,
    ) -> None:
        super().__init__(engine, player, profile_for, bus=bus, channels=channels)
        self._event_idle = threading.Event()
        self._event_idle.set()

    def _enqueue(self, request: Request) -> Response:
        self._event_idle.clear()  # cleared before the put, as the original did
        response = super()._enqueue(request)
        if response.data.get("spoken") is not True:
            self._event_idle.set()
        return response

    def _finish_one(self) -> None:
        empty = self._jobs.empty()
        time.sleep(self.gap)
        if empty:
            self._event_idle.set()
        super()._finish_one()

    def wait_idle(self, timeout: float) -> bool:
        return self._event_idle.wait(timeout=timeout) and self._jobs.empty()


def _idle_report_during_handoff(d: Daemon, player: HoldingPlayer) -> tuple[bool, int]:
    """Enqueue a second utterance exactly as the first completes, then ask if idle.

    Returns what `wait_idle` answered while the second utterance was held
    mid-play, and how many segments had actually reached the player by then.
    """
    first_done = threading.Event()
    d.bus.subscribe(lambda event: first_done.set(), kinds=["finished"])
    d.start()
    assert d.handle(enqueue("s", "One.")).ok is True
    assert first_done.wait(timeout=5.0), "the first utterance never finished"
    assert d.handle(enqueue("s", "Two. Three.")).ok is True
    assert player.reached.wait(timeout=5.0), "the second utterance never started playing"
    return d.wait_idle(timeout=0.5), len(player.played)


def test_the_rejected_wait_idle_reports_idle_with_an_utterance_in_flight() -> None:
    """Why the shipped version counts work instead of watching an Event."""
    player = HoldingPlayer(threading.Event(), threading.Event(), hold_on=2)
    rejected = _RejectedIdleDaemon(
        FakeEngine(), player, profile_for, bus=EventBus(), channels=ChannelTable()
    )
    try:
        said, played = _idle_report_during_handoff(rejected, player)
    finally:
        player.release.set()
        rejected.stop()
    assert said is True, "the rejected version was expected to report idle"
    assert played < 3, "and to do so with a segment still unspoken"


def test_the_shipped_wait_idle_survives_the_same_handoff() -> None:
    """The identical interleaving, against the daemon as shipped."""
    player = HoldingPlayer(threading.Event(), threading.Event(), hold_on=2)
    d = Daemon(FakeEngine(), player, profile_for, bus=EventBus(), channels=ChannelTable())
    try:
        said, played = _idle_report_during_handoff(d, player)
        assert said is False, "idle must never mean an utterance is still playing"
        assert played == 2
        player.release.set()
        assert d.wait_idle(timeout=5.0) is True
        assert len(player.played) == 3
    finally:
        player.release.set()
        d.stop()
