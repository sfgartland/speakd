"""Tests for the Unix socket transport."""

import select
import threading
import time
import warnings
from collections.abc import Callable, Sequence
from pathlib import Path

import pytest

import speakd.transport as transport
from speakd.events import Event, EventBus, Subscription
from speakd.protocol import ProtocolError, Request, Response, Verb, decode_response
from speakd.transport import SocketServer, connect


def echo_handler(request: Request) -> Response:
    return Response(ok=True, data={"verb": request.verb.value, "source": request.source_id})


@pytest.fixture
def server(tmp_path: Path):  # type: ignore[no-untyped-def]
    bus = EventBus()
    srv = SocketServer(tmp_path / "speakd.sock", echo_handler, bus)
    srv.start()
    try:
        yield srv, bus
    finally:
        srv.stop()


def test_a_request_gets_its_response(server) -> None:  # type: ignore[no-untyped-def]
    srv, _bus = server
    client = connect(srv.address)
    try:
        response = client.send(Request(verb=Verb.HUSH, source_id="s", payload={}))
        assert response.ok is True
        assert response.data == {"verb": "hush", "source": "s"}
    finally:
        client.close()


def test_several_requests_share_one_connection(server) -> None:  # type: ignore[no-untyped-def]
    srv, _bus = server
    client = connect(srv.address)
    try:
        for verb in (Verb.HUSH, Verb.PAUSE, Verb.RESUME):
            assert client.send(Request(verb=verb, source_id="s", payload={})).ok is True
    finally:
        client.close()


def test_a_malformed_line_gets_an_error_response_not_a_closed_socket(server) -> None:  # type: ignore[no-untyped-def]
    srv, _bus = server
    client = connect(srv.address)
    try:
        response = client.send_raw(b"{not json\n")
        assert response.ok is False
        assert "JSON" in response.error
        # The connection must still work afterwards.
        assert client.send(Request(verb=Verb.HUSH, source_id="s", payload={})).ok is True
    finally:
        client.close()


def test_a_subscriber_receives_published_events(server) -> None:  # type: ignore[no-untyped-def]
    srv, bus = server
    client = connect(srv.address)
    received: list[Event] = []
    ready = threading.Event()

    def reader() -> None:
        for event in client.subscribe():
            received.append(event)
            ready.set()
            break

    thread = threading.Thread(target=reader, daemon=True)
    thread.start()
    # Give the subscription a moment to register, then publish.
    for _ in range(100):
        if bus.subscriber_count() > 0:
            break
        threading.Event().wait(0.01)
    bus.publish(Event(kind="position", source_id="s", data={"offset": 3}))
    assert ready.wait(timeout=5.0)
    thread.join(timeout=5.0)
    client.close()
    assert received[0].kind == "position"
    assert received[0].data == {"offset": 3}


def test_a_vanished_subscriber_is_dropped_without_disturbing_the_server(server) -> None:  # type: ignore[no-untyped-def]
    srv, bus = server
    client = connect(srv.address)
    stream = client.subscribe()
    for _ in range(100):
        if bus.subscriber_count() > 0:
            break
        threading.Event().wait(0.01)
    # The subscription must actually have registered -- otherwise everything
    # below holds trivially with zero subscribers and proves nothing.
    assert bus.subscriber_count() == 1
    client.close()
    del stream
    # Publishing to a dead subscriber must not raise, and the subscriber must
    # actually be dropped -- the server must still serve everyone else.
    for _ in range(100):
        bus.publish(Event(kind="position", source_id="s", data={}))
        if bus.subscriber_count() == 0:
            break
        threading.Event().wait(0.01)
    assert bus.subscriber_count() == 0
    other = connect(srv.address)
    try:
        assert other.send(Request(verb=Verb.HUSH, source_id="s", payload={})).ok is True
    finally:
        other.close()


def test_a_stale_socket_file_does_not_block_startup(tmp_path: Path) -> None:
    path = tmp_path / "speakd.sock"
    path.write_bytes(b"")  # a leftover from a daemon that died badly
    srv = SocketServer(path, echo_handler, EventBus())
    srv.start()
    try:
        client = connect(srv.address)
        assert client.send(Request(verb=Verb.HUSH, source_id="s", payload={})).ok is True
        client.close()
    finally:
        srv.stop()


def test_a_timed_out_request_does_not_poison_later_reads_on_the_connection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A request timeout must be local to that one wait, not the connection.

    self._reader wraps a socket.SocketIO whose readinto() latches
    _timeout_occurred the first time a real socket-level timeout fires, with
    no way to reset it -- every later read on that file object then raises
    "cannot read from timed out object" forever after. That would silence a
    live subscription sharing the same reader the moment any other request
    on the connection happened to be slow: precisely "a GUI must never
    silence speech", inverted. The fix (select() instead of
    socket.settimeout()) must mean a timeout here is just a timeout, not a
    fuse.
    """
    monkeypatch.setattr(transport, "_REQUEST_TIMEOUT_SECONDS", 0.2)
    release = threading.Event()

    def slow_then_ok(request: Request) -> Response:
        release.wait(timeout=5.0)
        return Response(ok=True, data={"verb": request.verb.value})

    srv = SocketServer(tmp_path / "speakd.sock", slow_then_ok, EventBus())
    srv.start()
    try:
        client = connect(srv.address)
        try:
            with pytest.raises(TimeoutError):
                client.send(Request(verb=Verb.HUSH, source_id="s", payload={}))

            # Let the slow handler finish and explicitly drain its now-late
            # response, so the next read below is unambiguously the fresh
            # request's own response and not that stale one -- this test is
            # about whether the earlier timeout poisoned the reader, not
            # about request/response correlation on a pipelined connection.
            release.set()
            readable, _, _ = select.select([client._socket], [], [], 5.0)
            assert readable, "the late response never arrived"
            late = decode_response(client._reader.readline())
            assert late.data == {"verb": "hush"}

            # A fresh, ordinary request over the same connection/reader must
            # still work: the earlier timeout must not have latched the
            # reader into permanently raising "cannot read from timed out
            # object".
            response = client.send(Request(verb=Verb.PAUSE, source_id="s", payload={}))
            assert response.ok is True
            assert response.data == {"verb": "pause"}
        finally:
            client.close()
    finally:
        srv.stop()


def test_a_malformed_event_data_value_becomes_a_protocol_error(server) -> None:  # type: ignore[no-untyped-def]
    srv, bus = server
    client = connect(srv.address)
    try:
        stream = client.subscribe()
        for _ in range(100):
            if bus.subscriber_count() > 0:
                break
            threading.Event().wait(0.01)
        assert bus.subscriber_count() == 1
        # Event.data's type hint promises dict[str, object], but nothing
        # enforces that on a misbehaving publisher -- a JSON value that
        # isn't an object must not leak a raw TypeError to the caller.
        bus.publish(Event(kind="position", source_id="s", data=["not", "a", "dict"]))  # type: ignore[arg-type]
        with pytest.raises(ProtocolError):
            next(stream)
    finally:
        client.close()


def test_stop_removes_the_socket_file(tmp_path: Path) -> None:
    srv = SocketServer(tmp_path / "speakd.sock", echo_handler, EventBus())
    srv.start()
    assert srv.address.exists()
    srv.stop()
    assert not srv.address.exists()


def test_stop_terminates_a_live_unclosed_connection_within_a_bound(tmp_path: Path) -> None:
    srv = SocketServer(tmp_path / "speakd.sock", echo_handler, EventBus())
    srv.start()
    client = connect(srv.address)
    try:
        # connect() returns once the connection is queued, not once the
        # server's accept loop has actually accepted it and spawned its
        # thread -- wait for that to happen so stop() has a real connection
        # thread blocked in a read to contend with, bounded so a regression
        # here fails fast instead of hanging. Bound to this server's own
        # thread list rather than a process-global thread-name search: a
        # leftover "speakd-conn" thread from any other test in the same
        # process would satisfy the pre-wait trivially and could fail the
        # final assertion spuriously even though this server's own
        # connection thread terminated correctly.
        for _ in range(200):
            if srv._conn_threads:
                break
            threading.Event().wait(0.01)
        conn_threads = list(srv._conn_threads)
        assert len(conn_threads) == 1

        started = time.monotonic()
        srv.stop()
        elapsed = time.monotonic() - started

        # With a correct shutdown()-before-close(), this should complete in
        # well under a second; a connection thread stuck on a bare read would
        # only be freed once the join() timeout itself expires (2s).
        assert elapsed < 1.0
        assert not conn_threads[0].is_alive()
    finally:
        client.close()


def test_the_ack_always_precedes_an_event_forced_during_registration(tmp_path: Path) -> None:
    """Reproduces the ack/event ordering race deterministically.

    Rather than hoping a scheduler quirk wins the race, force a publish to
    happen synchronously from inside bus.subscribe() itself, before it
    returns to the caller -- the same technique used to demonstrate the bug.
    A correct server must still deliver the ack before the event no matter
    which thread, or which call frame, the publish happens on.
    """
    bus = EventBus()
    original_subscribe = bus.subscribe

    def racing_subscribe(
        callback: Callable[[Event], None], kinds: Sequence[str] | None = None
    ) -> Subscription:
        subscription = original_subscribe(callback, kinds=kinds)
        bus.publish(Event(kind="position", source_id="s", data={"forced": True}))
        return subscription

    bus.subscribe = racing_subscribe  # type: ignore[method-assign]

    srv = SocketServer(tmp_path / "speakd.sock", echo_handler, bus)
    srv.start()
    try:
        client = connect(srv.address)
        try:
            stream = client.subscribe()  # must not raise: the ack must win the race
            event = next(stream)
            assert event.kind == "position"
            assert event.data == {"forced": True}
        finally:
            client.close()
    finally:
        srv.stop()


def test_the_ack_precedes_an_event_forced_during_a_second_subscribe(tmp_path: Path) -> None:
    """Reproduces the same race, but on a re-subscribe over one connection.

    The first subscribe must complete cleanly (nothing forced): it exists
    only to leave the connection with a writer thread already running and an
    ack already behind it, which is the stale state a second subscribe on the
    same connection must not inherit. The forced publish happens only on the
    second subscribe's registration.

    The pause after that publish is what makes this a reproduction rather
    than a hope. Events no longer go out on the publishing thread: the forced
    one lands in the outbox, where the connection thread would otherwise win
    the race to the ack simply by being quicker. Held here, the writer has
    every opportunity to send it first, and only `ack_owed` stops it. Without
    that wait the client reads an event line where the subscribe's response
    should be, and `decode_response` rejects it.
    """
    bus = EventBus()
    original_subscribe = bus.subscribe
    call_count = 0

    def racing_subscribe(
        callback: Callable[[Event], None], kinds: Sequence[str] | None = None
    ) -> Subscription:
        nonlocal call_count
        call_count += 1
        subscription = original_subscribe(callback, kinds=kinds)
        if call_count == 2:
            bus.publish(Event(kind="position", source_id="s", data={"forced": "second"}))
            time.sleep(0.3)
        return subscription

    bus.subscribe = racing_subscribe  # type: ignore[method-assign]

    srv = SocketServer(tmp_path / "speakd.sock", echo_handler, bus)
    srv.start()
    try:
        client = connect(srv.address)
        try:
            client.subscribe()  # first subscribe: ack only, nothing forced
            # Second subscribe on the same connection: must not raise even
            # though a publish is forced inside this exact registration.
            second_stream = client.subscribe()
            event = next(second_stream)
            assert event.kind == "position"
            assert event.data == {"forced": "second"}
        finally:
            client.close()
    finally:
        srv.stop()


def test_a_handler_that_raises_becomes_an_error_response(tmp_path: Path) -> None:
    def boom(request: Request) -> Response:
        raise RuntimeError("handler exploded")

    srv = SocketServer(tmp_path / "speakd.sock", boom, EventBus())
    srv.start()
    try:
        client = connect(srv.address)
        response = client.send(Request(verb=Verb.HUSH, source_id="s", payload={}))
        assert response.ok is False
        assert "exploded" in response.error
        client.close()
    finally:
        srv.stop()


# --- Whole-branch review: the lifecycle guards ---


def test_starting_a_server_twice_is_refused(tmp_path: Path) -> None:
    """A second start() overwrote `_socket` and `_accept_thread`.

    The first socket stayed bound and its accept thread stayed running, with
    nothing left holding either: a leaked listener nobody can close, and two
    accept loops on one address.
    """
    srv = SocketServer(tmp_path / "speakd.sock", echo_handler, EventBus())
    srv.start()
    try:
        first_socket = srv._socket
        first_thread = srv._accept_thread
        with pytest.raises(RuntimeError):
            srv.start()
        assert srv._socket is first_socket, "the second start() replaced the listening socket"
        assert srv._accept_thread is first_thread, "the second start() replaced the accept thread"
        client = connect(srv.address)
        try:
            assert client.send(Request(verb=Verb.HUSH, source_id="s", payload={})).ok is True
        finally:
            client.close()
    finally:
        srv.stop()


def test_stop_without_start_leaves_the_socket_file_alone(tmp_path: Path) -> None:
    """stop() unlinked `address` unconditionally.

    Pointed at a live daemon's socket -- which is the ordinary case, since
    every SocketServer is constructed with the same default path -- that
    deletes the one thing clients use to find it.
    """
    live = SocketServer(tmp_path / "speakd.sock", echo_handler, EventBus())
    live.start()
    try:
        never_started = SocketServer(live.address, echo_handler, EventBus())
        never_started.stop()
        assert live.address.exists(), "stop() deleted a running daemon's socket"
        client = connect(live.address)
        try:
            assert client.send(Request(verb=Verb.HUSH, source_id="s", payload={})).ok is True
        finally:
            client.close()
    finally:
        live.stop()


def test_a_second_server_refuses_to_steal_a_live_address(tmp_path: Path) -> None:
    """start() unlinked any existing socket file with no liveness probe.

    A second speakd then silently took the address from a first that is still
    running -- and still holding the audio device, so everything routed to the
    new one goes nowhere.
    """
    first = SocketServer(tmp_path / "speakd.sock", echo_handler, EventBus())
    first.start()
    try:
        second = SocketServer(first.address, echo_handler, EventBus())
        with pytest.raises(RuntimeError) as refusal:
            second.start()
        assert "listening" in str(refusal.value)
        assert str(first.address) in str(refusal.value)
        # The first is untouched: same socket file, still answering.
        client = connect(first.address)
        try:
            assert client.send(Request(verb=Verb.HUSH, source_id="s", payload={})).ok is True
        finally:
            client.close()
    finally:
        first.stop()


def test_a_closed_connection_is_forgotten_by_the_server(tmp_path: Path) -> None:
    """The deregistration in `_serve`'s finally is a real unbounded leak.

    `_connections` and `_conn_threads` grow by one entry per `speakctl`
    invocation otherwise -- one socket object and one dead Thread each, held
    for the life of a daemon that is meant to run for weeks, with nothing
    watching either list.
    """
    srv = SocketServer(tmp_path / "speakd.sock", echo_handler, EventBus())
    srv.start()
    try:
        for _ in range(5):
            client = connect(srv.address)
            try:
                assert client.send(Request(verb=Verb.HUSH, source_id="s", payload={})).ok is True
            finally:
                client.close()
        deadline = time.monotonic() + 5.0
        while srv._connections and time.monotonic() < deadline:
            threading.Event().wait(0.01)
        assert srv._connections == [], "closed connections were never forgotten"
        assert srv._conn_threads == [], "finished connection threads were never forgotten"
    finally:
        srv.stop()


# --- Re-review: the same defect class, reintroduced by the rewrite ---


def test_a_writer_thread_that_will_not_start_leaves_nothing_behind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`writer` must not be bound until the thread is actually running.

    Bound before `start()`, a "can't start new thread" leaves the finally
    calling join() on a thread that was never started -- RuntimeError, thrown
    from inside the cleanup, which skips the deregistration and the close
    below it. One leaked socket and one leaked entry in each list, for the
    life of the daemon.
    """
    real_start = threading.Thread.start

    def refuse_writers(self: threading.Thread) -> None:
        if self.name == "speakd-writer":
            raise RuntimeError("can't start new thread")
        real_start(self)

    monkeypatch.setattr(threading.Thread, "start", refuse_writers)
    srv = SocketServer(tmp_path / "speakd.sock", echo_handler, EventBus())
    srv.start()
    try:
        client = connect(srv.address)
        try:
            # The ack goes out before the writer is started, so this still
            # succeeds; what follows it on the server is what matters.
            client.subscribe()
        finally:
            client.close()
        deadline = time.monotonic() + 5.0
        while srv._connections and time.monotonic() < deadline:
            threading.Event().wait(0.01)
        assert srv._connections == [], "the connection was never deregistered"
        assert srv._conn_threads == [], "the connection thread was never deregistered"
    finally:
        srv.stop()


def test_stop_survives_a_socket_file_that_vanishes_under_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """exists() then unlink() is two calls, and the file can go between them.

    Whoever else cleans up that path -- a second stop(), an impatient signal
    handler, a tmpfiles sweep -- wins the race, and stop() throws
    FileNotFoundError out of a shutdown that had otherwise finished.
    """
    srv = SocketServer(tmp_path / "speakd.sock", echo_handler, EventBus())
    srv.start()
    real_unlink = Path.unlink

    def vanished(self: Path, missing_ok: bool = False) -> None:
        if self == srv.address:
            raise FileNotFoundError(2, "No such file or directory", str(self))
        real_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", vanished)
    srv.stop()
    monkeypatch.undo()


def test_a_connect_timeout_does_not_leave_the_event_stream_timed(server) -> None:  # type: ignore[no-untyped-def]
    """A bounded connect must not bound everything read afterwards.

    `settimeout` puts the socket in timeout mode for every later operation,
    and `socket.SocketIO` latches `_timeout_occurred` the first time one
    fires — after which every read on that file object raises forever, with
    no way to reset it. An event stream is idle most of the time by nature,
    so a socket left in timeout mode turns a quiet subscriber into a dead
    one. `connect` clears the timeout before the reader is built over it;
    this is what says so.
    """
    srv, bus = server
    client = connect(srv.address, timeout=0.1)
    received: list[Event] = []
    ready = threading.Event()

    def reader() -> None:
        for event in client.subscribe():
            received.append(event)
            ready.set()
            break

    thread = threading.Thread(target=reader, daemon=True)
    thread.start()
    for _ in range(100):
        if bus.subscriber_count() > 0:
            break
        threading.Event().wait(0.01)
    # Idle for several multiples of the connect timeout before anything is
    # published. This is the wait a real subscriber spends between events.
    threading.Event().wait(0.5)
    bus.publish(Event(kind="position", source_id="s", data={"offset": 7}))
    assert ready.wait(timeout=5.0), "the idle stream timed out and never recovered"
    thread.join(timeout=5.0)
    client.close()
    assert received[0].data == {"offset": 7}


def test_a_connect_that_fails_closes_its_socket(tmp_path: Path) -> None:
    """A caller retrying in a loop must not leave one socket per attempt.

    Counting descriptors cannot show this: CPython's refcounting reclaims an
    unreferenced socket as soon as `connect` returns, so the count stays flat
    whether or not the close is there. What it does not do is stay quiet —
    it closes the socket *for* us and says so — so the warning is the
    observable, and the only thing that distinguishes a socket closed on
    purpose from one collected after the fact.
    """
    absent = tmp_path / "nothing.sock"
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        for _ in range(5):
            with pytest.raises(OSError):
                connect(absent)
    unclosed = [w for w in caught if issubclass(w.category, ResourceWarning)]
    assert unclosed == [], f"a failed connect left its socket open: {unclosed[0].message}"
