"""A subscriber that stops reading must be dropped, not tolerated.

`emit_event` writes to a subscriber's socket synchronously, on the speech
worker's own thread. An accepted AF_UNIX connection has no send timeout, so a
subscriber that connects and then stops reading fills the socket buffer (about
46 KB here, measured) and wedges that `sendall` forever: no further speech
plays, `wait_idle` never returns, and `Daemon.stop()`'s join times out, leaving
`_worker` set so every later `start()` refuses. A dead or slow GUI must never
silence speech, so the bus has to drop such a subscriber -- and say so.
"""

from __future__ import annotations

import socket
import threading
import time
from collections.abc import Sequence
from pathlib import Path

from speakd.channels import ChannelTable
from speakd.daemon import Daemon, ProfileView
from speakd.events import Event, EventBus
from speakd.model import Piece
from speakd.player import RecordingPlayer
from speakd.protocol import Request, Response, Verb, encode
from speakd.synth.fake import FakeEngine
from speakd.transport import SocketServer

# Well past the socket buffer (about 280 events of this size on Linux), so the
# unfixed code is certain to wedge rather than merely be lucky.
_SENTENCES = 500


def passthrough(pieces: Sequence[Piece]) -> tuple[list[Piece], list[str]]:
    return list(pieces), []


def profile_for(name: str) -> ProfileView:
    return ProfileView(voice="af_heart", speed=1.1, interrupt_on=(), prepare=passthrough)


def _no_op_handler(request: Request) -> Response:
    return Response(ok=True)


def _subscribe_and_stop_reading(address: Path, bus: EventBus, source_id: str) -> socket.socket:
    """Connect, subscribe, read the ack -- and then never read again.

    The real thing: `speakctl subscribe | less`, or a GUI that stops draining
    its socket. An in-process callback that merely sleeps would not exercise
    `sendall` at all.
    """
    connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    connection.connect(str(address))
    connection.sendall(encode(Request(verb=Verb.SUBSCRIBE, source_id=source_id, payload={})))
    ack = b""
    while not ack.endswith(b"\n"):
        chunk = connection.recv(1)
        assert chunk, "the daemon closed the connection instead of acking the subscribe"
        ack += chunk
    assert bus.subscriber_count() == 1, "the subscription never registered"
    return connection


def test_a_subscriber_that_stops_reading_does_not_wedge_the_speech_worker(
    tmp_path: Path,
) -> None:
    player = RecordingPlayer()
    daemon = Daemon(FakeEngine(), player, profile_for, bus=EventBus(), channels=ChannelTable())
    daemon.start()
    server = SocketServer(tmp_path / "speakd.sock", daemon.handle, daemon.bus)
    server.start()
    deaf = None
    try:
        deaf = _subscribe_and_stop_reading(server.address, daemon.bus, "gui")

        # One long utterance publishes a position event per sentence, which is
        # what fills the deaf subscriber's socket buffer, and a short one
        # behind it proves speech carried on afterwards.
        first = " ".join(["One."] * _SENTENCES)
        assert (
            daemon.handle(Request(verb=Verb.ENQUEUE, source_id="s", payload={"text": first})).ok
            is True
        )
        assert (
            daemon.handle(Request(verb=Verb.ENQUEUE, source_id="s", payload={"text": "Two."})).ok
            is True
        )

        assert daemon.wait_idle(timeout=20.0), "the speech worker never came back to idle"
        assert len(player.played) == _SENTENCES + 1
    finally:
        if deaf is not None:
            deaf.close()
        server.stop()
        daemon.stop()


def test_a_dropped_subscriber_is_announced_on_the_bus(tmp_path: Path) -> None:
    """A GUI that silently stopped receiving is a quiet daemon, to its user.

    Dropping is right; dropping without a word is the silent-loss failure this
    daemon exists to prevent, so the drop goes out as an `error` event naming
    the subscriber that caused it.
    """
    bus = EventBus()
    server = SocketServer(tmp_path / "speakd.sock", _no_op_handler, bus)
    server.start()
    deaf = None
    try:
        deaf = _subscribe_and_stop_reading(server.address, bus, "gui")
        seen: list[Event] = []
        bus.subscribe(seen.append, kinds=["error"])

        def publish_many() -> None:
            for n in range(4000):
                bus.publish(Event(kind="position", source_id="s", data={"n": n}))

        published = threading.Thread(target=publish_many, daemon=True)
        published.start()
        published.join(timeout=20.0)
        assert not published.is_alive(), "publishing wedged on the subscriber that stopped reading"

        assert bus.subscriber_count() == 1, "the deaf subscriber was not dropped"
        assert seen, "the drop was never announced"
        message = str(seen[0].data.get("message", ""))
        assert "subscriber" in message
        assert "gui" in message or seen[0].source_id == "gui"
    finally:
        if deaf is not None:
            deaf.close()
        server.stop()


def test_a_dropped_subscriber_is_disconnected_not_left_behind(tmp_path: Path) -> None:
    """Dropping it from the bus is half the job; the connection goes too.

    Its reader thread is blocked on a client that will never send again and
    its writer is wedged in a send that will never complete, so without the
    shutdown that wakes both, a dropped subscriber costs two threads, a
    socket and two list entries for the life of the daemon.
    """
    bus = EventBus()
    server = SocketServer(tmp_path / "speakd.sock", _no_op_handler, bus)
    server.start()
    deaf = None
    try:
        deaf = _subscribe_and_stop_reading(server.address, bus, "gui")
        for n in range(4000):
            bus.publish(Event(kind="position", source_id="s", data={"n": n}))
            if bus.subscriber_count() == 0:
                break
        assert bus.subscriber_count() == 0, "the deaf subscriber was never dropped"
        deadline = time.monotonic() + 5.0
        while server._connections and time.monotonic() < deadline:
            threading.Event().wait(0.01)
        assert server._connections == [], "the dropped subscriber's connection was left behind"
        assert server._conn_threads == [], "the dropped subscriber's thread was left behind"
    finally:
        if deaf is not None:
            deaf.close()
        server.stop()


def test_a_peer_that_stops_receiving_is_dropped_on_the_next_publish(tmp_path: Path) -> None:
    """A peer can stop receiving without going away.

    shutdown(SHUT_RD) on a Unix socket breaks the daemon's sends while the
    client's own requests still arrive, so the writer thread dies and the
    connection thread stays blocked in its read. Without the `gone` check the
    next publishes pile into the outbox for a writer that is not there -- 256
    of them before the daemon notices, and then announced as a subscriber
    that stopped reading, which is a different fault with a different cure.
    """
    bus = EventBus()
    server = SocketServer(tmp_path / "speakd.sock", _no_op_handler, bus)
    server.start()
    deaf = None
    try:
        deaf = _subscribe_and_stop_reading(server.address, bus, "gui")
        seen: list[Event] = []
        bus.subscribe(seen.append, kinds=["error"])
        deaf.shutdown(socket.SHUT_RD)
        for _ in range(10):
            bus.publish(Event(kind="position", source_id="s", data={}))
            time.sleep(0.02)
        # Only the in-process watcher above is left.
        assert bus.subscriber_count() == 1, "a peer that stopped receiving was kept on the bus"
        assert seen == [], "a vanished peer was announced as a subscriber that could not keep up"
    finally:
        if deaf is not None:
            deaf.close()
        server.stop()
