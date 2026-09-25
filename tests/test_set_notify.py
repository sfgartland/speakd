"""The notifications off-switch: stop the reader, hush its channels, persist."""

import threading
from collections.abc import Sequence

import numpy as np

from speakd.channels import ChannelTable
from speakd.daemon import Daemon, ProfileView
from speakd.events import Event, EventBus
from speakd.model import Piece
from speakd.player import FakeSink, RecordingPlayer, StreamingPlayer
from speakd.protocol import Request, Verb
from speakd.synth.fake import FakeEngine


def passthrough(pieces: Sequence[Piece]) -> tuple[list[Piece], list[str]]:
    return list(pieces), []


def profile_for(name: str) -> ProfileView:
    return ProfileView(voice="v", speed=1.0, interrupt_on=(), prepare=passthrough)


class FakeSupervisor:
    """The real supervisor's contract, minus the process: start() is a no-op
    while running, stop() takes it down, and each call is counted."""

    def __init__(self) -> None:
        self.running = False
        self.starts = 0
        self.stops = 0

    def start(self) -> None:
        if self.running:
            return
        self.running = True
        self.starts += 1

    def stop(self) -> None:
        self.running = False
        self.stops += 1


class RudeHoldingPlayer:
    """Blocks inside play() until released, and refuses to stop."""

    def __init__(self) -> None:
        self.playing = threading.Event()
        self.release = threading.Event()
        self.stops = 0

    def play(self, audio: np.ndarray, sample_rate: int) -> None:
        self.playing.set()
        self.release.wait(timeout=10.0)

    def stop(self) -> None:
        self.stops += 1
        raise RuntimeError("the sink refuses")


def build(supervisor: FakeSupervisor | None = None) -> tuple[Daemon, list[Event]]:
    bus = EventBus()
    seen: list[Event] = []
    bus.subscribe(seen.append)
    daemon = Daemon(FakeEngine(), RecordingPlayer(), profile_for, bus=bus, channels=ChannelTable())
    if supervisor is not None:
        daemon.add_connector("notify", supervisor)
    return daemon, seen


def test_off_stops_the_reader_persists_and_announces() -> None:
    from speakd import state

    supervisor = FakeSupervisor()
    supervisor.start()
    daemon, seen = build(supervisor)
    try:
        response = daemon.handle(
            Request(verb=Verb.SET_NOTIFY, source_id="", payload={"enabled": False})
        )
        assert response.ok
        assert supervisor.stops == 1 and not supervisor.running
        assert state.load().notify_enabled is False
        notify = [e for e in seen if e.kind == "notify"]
        assert notify and notify[-1].data == {"enabled": False}
    finally:
        daemon.stop()


def test_on_starts_the_reader_persists_and_announces() -> None:
    from speakd import state

    supervisor = FakeSupervisor()
    daemon, seen = build(supervisor)
    try:
        response = daemon.handle(
            Request(verb=Verb.SET_NOTIFY, source_id="", payload={"enabled": True})
        )
        assert response.ok
        assert supervisor.starts == 1 and supervisor.running
        assert state.load().notify_enabled is True
        notify = [e for e in seen if e.kind == "notify"]
        assert notify and notify[-1].data == {"enabled": True}
        # Idempotent: a second on does not respawn the child.
        daemon.handle(Request(verb=Verb.SET_NOTIFY, source_id="", payload={"enabled": True}))
        assert supervisor.starts == 1
    finally:
        daemon.stop()


def test_off_hushes_the_readers_channels_and_no_others() -> None:
    daemon = Daemon(
        FakeEngine(),
        StreamingPlayer(FakeSink()),
        profile_for,
        bus=EventBus(),
        channels=ChannelTable(),
    )
    daemon.add_connector("notify", FakeSupervisor())
    daemon.start()
    try:
        daemon.handle(Request(verb=Verb.PAUSE, source_id=""))
        daemon.handle(
            Request(verb=Verb.ENQUEUE, source_id="notify:chrome", payload={"text": "Mail."})
        )
        daemon.handle(Request(verb=Verb.ENQUEUE, source_id="s", payload={"text": "Keep me."}))
        daemon.handle(Request(verb=Verb.SET_NOTIFY, source_id="", payload={"enabled": False}))
        status = daemon.handle(Request(verb=Verb.STATUS, source_id=""))
        queue = status.data["queue"]
        assert isinstance(queue, list)
        texts = [job["text"] for job in queue]
        assert "Mail." not in texts
        assert "Keep me." in texts
    finally:
        daemon.handle(Request(verb=Verb.RESUME, source_id=""))
        daemon.stop()


def test_on_with_no_reader_registered_is_refused() -> None:
    daemon, _seen = build(None)
    try:
        response = daemon.handle(
            Request(verb=Verb.SET_NOTIFY, source_id="", payload={"enabled": True})
        )
        assert not response.ok
        assert "SPEAKD_NO_NOTIFY" in response.error
    finally:
        daemon.stop()


def test_off_with_no_reader_registered_still_persists() -> None:
    from speakd import state

    daemon, _seen = build(None)
    try:
        response = daemon.handle(
            Request(verb=Verb.SET_NOTIFY, source_id="", payload={"enabled": False})
        )
        assert response.ok
        assert state.load().notify_enabled is False
    finally:
        daemon.stop()


def test_set_notify_needs_a_boolean() -> None:
    daemon, _seen = build(FakeSupervisor())
    try:
        response = daemon.handle(
            Request(verb=Verb.SET_NOTIFY, source_id="", payload={"enabled": "off"})
        )
        assert not response.ok
        assert "enabled" in response.error
    finally:
        daemon.stop()


def test_status_reports_the_switch() -> None:
    daemon, _seen = build(FakeSupervisor())
    try:
        status = daemon.handle(Request(verb=Verb.STATUS, source_id=""))
        assert status.data["notify"] == {"enabled": True}
        daemon.handle(Request(verb=Verb.SET_NOTIFY, source_id="", payload={"enabled": False}))
        status = daemon.handle(Request(verb=Verb.STATUS, source_id=""))
        assert status.data["notify"] == {"enabled": False}
    finally:
        daemon.stop()


def test_the_switch_survives_a_restart() -> None:
    first, _seen = build(FakeSupervisor())
    try:
        first.handle(Request(verb=Verb.SET_NOTIFY, source_id="", payload={"enabled": False}))
    finally:
        first.stop()
    second, _seen = build(FakeSupervisor())
    try:
        assert second.notify_enabled is False
    finally:
        second.stop()


def test_off_still_persists_when_the_player_refuses_to_stop() -> None:
    from speakd import state

    player = RudeHoldingPlayer()
    bus = EventBus()
    seen: list[Event] = []
    bus.subscribe(seen.append)
    daemon = Daemon(FakeEngine(), player, profile_for, bus=bus, channels=ChannelTable())
    daemon.add_connector("notify", FakeSupervisor())
    daemon.start()
    try:
        daemon.handle(
            Request(verb=Verb.ENQUEUE, source_id="notify:chrome", payload={"text": "One. Two."})
        )
        assert player.playing.wait(timeout=5.0), "the worker never reached the player"
        response = daemon.handle(
            Request(verb=Verb.SET_NOTIFY, source_id="", payload={"enabled": False})
        )
        assert response.ok, "a failing sink must not fail a switch that did flip"
        assert state.load().notify_enabled is False
        errors = [e for e in seen if e.kind == "error"]
        assert errors, "the failing sink must be reported on the bus"
    finally:
        player.release.set()
        daemon.stop()
