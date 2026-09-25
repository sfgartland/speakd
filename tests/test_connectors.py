"""The daemon owns its connectors, so the off-switch verb can stop one."""

from collections.abc import Callable, Sequence
from pathlib import Path

from speakd.channels import ChannelTable
from speakd.daemon import Daemon, ProfileView
from speakd.events import EventBus
from speakd.model import Piece
from speakd.player import RecordingPlayer
from speakd.protocol import Request, Response
from speakd.synth.fake import FakeEngine
from speakd.transport import SocketServer


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


class LoggingSupervisor:
    def __init__(self, log: list[str]) -> None:
        self._log = log

    def start(self) -> None:
        pass

    def stop(self) -> None:
        self._log.append("connectors")


class RecordingPlayerWithOrder(RecordingPlayer):
    def __init__(self, log: list[str]) -> None:
        super().__init__()
        self._log = log

    def stop(self) -> None:
        self._log.append("silence")
        super().stop()


class RecordingServer(SocketServer):
    def __init__(
        self,
        address: Path,
        handler: Callable[[Request], Response],
        bus: EventBus,
        log: list[str],
    ) -> None:
        super().__init__(address, handler, bus)
        self._log = log

    def stop(self) -> None:
        self._log.append("server")
        super().stop()


def build() -> Daemon:
    return Daemon(
        FakeEngine(),
        RecordingPlayer(),
        profile_for,
        bus=EventBus(),
        channels=ChannelTable(),
    )


def test_a_registered_connector_is_stopped_by_stop_connectors() -> None:
    daemon = build()
    notify = FakeSupervisor()
    follower = FakeSupervisor()
    notify.start()
    daemon.add_connector("notify", notify)
    daemon.add_connector("follower", follower)
    daemon.stop_connectors()
    assert notify.stops == 1 and not notify.running
    assert follower.stops == 1


def test_stop_connectors_with_nothing_registered_is_a_no_op() -> None:
    daemon = build()
    daemon.stop_connectors()


def test_daemon_stop_also_stops_connectors() -> None:
    # A daemon stopped without going through serve() (tests, future
    # embeddings) must still take its readers down.
    daemon = build()
    supervisor = FakeSupervisor()
    supervisor.start()
    daemon.add_connector("notify", supervisor)
    daemon.stop()
    assert supervisor.stops == 1


def test_shutdown_stops_connectors_before_silencing_and_closing_the_server(
    tmp_path: Path,
) -> None:
    """serve()'s documented ordering, pinned: a reader must not enqueue into
    a daemon that is shutting down, and server.stop() must stay after
    silence() so a wedged worker can be freed."""
    from speakd import __main__ as entrypoint

    log: list[str] = []
    daemon = Daemon(
        FakeEngine(),
        RecordingPlayerWithOrder(log),
        profile_for,
        bus=EventBus(),
        channels=ChannelTable(),
    )
    daemon.add_connector("notify", LoggingSupervisor(log))
    server = RecordingServer(tmp_path / "speakd.sock", daemon.handle, daemon.bus, log)
    daemon.start()
    server.start()
    try:
        entrypoint._shutdown(daemon, server, None)
        assert log == ["connectors", "silence", "server"]
    finally:
        daemon.stop()
