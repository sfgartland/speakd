"""Unix socket transport: line-delimited JSON in, line-delimited JSON out.

One accept loop, one thread per connection. A connection that sends `subscribe`
stops being a request/response channel and becomes an event stream until the
client goes away.

A malformed line is answered with an error response rather than a dropped
connection: a client that sent nonsense deserves to be told what was wrong. A
client that vanishes is dropped from the bus without disturbing anything else,
because a GUI closing its window must never silence speech. Neither must a GUI
that stops reading: events go out on a per-subscriber writer thread through a
bounded queue, so no publish ever waits on a socket.
"""

from __future__ import annotations

import json
import queue
import select
import socket
import threading
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path

from speakd.events import Event, EventBus, Subscription
from speakd.protocol import (
    ProtocolError,
    Request,
    Response,
    Verb,
    decode_request,
    decode_response,
    encode,
)

Handler = Callable[[Request], Response]

# How often the accept loop wakes up to check whether it should keep running.
_ACCEPT_POLL_SECONDS = 0.2
# How long stop() waits for a thread to notice it should exit before giving up.
_JOIN_TIMEOUT_SECONDS = 2.0
# How long a liveness probe waits to find out whether anyone is on an existing
# socket file before that file is treated as a dead daemon's litter.
_PROBE_TIMEOUT_SECONDS = 0.5
# How long a client waits for a response to a request before giving up. Only
# ever applied around the request/response read -- a subscriber's event
# stream is otherwise unbounded, since an idle stream is not a stuck one.
_REQUEST_TIMEOUT_SECONDS = 5.0
# How many events may be waiting to go out to one subscriber before it is
# dropped. A subscriber that stops reading fills the socket's own buffer first
# (about 46 KB, some 280 position events, measured on Linux); this bounds what
# the daemon is willing to hold on top of that before giving up on it. Large
# enough that an ordinary GUI stall rides through, small enough that a wedged
# one is noticed while the memory it costs is still trivial.
_SUBSCRIBER_BACKLOG = 256


class _SubscriberUnreachable(Exception):
    """Raised out of a bus callback so the bus drops that subscriber.

    `EventBus` drops a subscriber that raises; it has no answer for one that
    blocks. This turns the second into the first.
    """


def _is_live(address: Path) -> bool:
    """Is something serving on this socket, or is the file a dead daemon's litter?

    Asked by connecting: there is no other way to tell a stale socket file
    from a live one, and the difference decides whether unlinking it is
    housekeeping or theft.
    """
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        probe.settimeout(_PROBE_TIMEOUT_SECONDS)
        probe.connect(str(address))
    except OSError:
        # ECONNREFUSED (a file no listener is behind), ENOENT (it went away
        # in between), ENOTSOCK (not a socket at all): nothing serves here.
        return False
    finally:
        probe.close()
    return True


def _event_line(event: Event) -> bytes:
    body: dict[str, object] = {
        "event": event.kind,
        "source_id": event.source_id,
        "data": event.data,
    }
    return (json.dumps(body) + "\n").encode("utf-8")


def _halt(outbox: queue.Queue[bytes | None]) -> None:
    """Leave a writer thread its sentinel, making room for it if need be."""
    while True:
        try:
            outbox.get_nowait()
        except queue.Empty:
            break
    try:
        outbox.put_nowait(None)
    except queue.Full:  # pragma: no cover - nothing publishes here any more
        # The subscription is disposed before this runs, so only an event
        # already in flight could refill the queue. The writer still leaves:
        # the shutdown() above it breaks any send it is sitting in.
        pass


class SocketServer:
    """Serves the control verbs and the event stream over a Unix socket."""

    def __init__(self, path: Path, handler: Handler, bus: EventBus) -> None:
        self.address = Path(path)
        self._handler = handler
        self._bus = bus
        self._socket: socket.socket | None = None
        self._accept_thread: threading.Thread | None = None
        self._running = threading.Event()
        self._connections: list[socket.socket] = []
        self._conn_threads: list[threading.Thread] = []
        self._lock = threading.Lock()

    def start(self) -> None:
        with self._lock:
            if self._running.is_set():
                # A second start() would overwrite `_socket` and
                # `_accept_thread`, leaving the first socket bound and its
                # accept loop running with nothing left holding either.
                raise RuntimeError(f"already serving on {self.address}: stop() it first")
            # Claimed before the bind, so two concurrent start()s cannot both
            # get past this and leak a listener between them.
            self._running.set()
        try:
            self.address.parent.mkdir(parents=True, exist_ok=True)
            if self.address.exists():
                # A daemon that died without cleaning up must not make the
                # next one unstartable. A daemon that is alive must not be
                # robbed either: it still holds the audio device, so a second
                # speakd taking its address leaves every client talking to a
                # daemon that cannot speak. Which of the two this is can only
                # be settled by asking.
                if _is_live(self.address):
                    raise RuntimeError(f"another speakd is already listening on {self.address}")
                self.address.unlink()
            server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self._socket = server
            server.bind(str(self.address))
            server.listen(16)
            server.settimeout(_ACCEPT_POLL_SECONDS)
            self._accept_thread = threading.Thread(
                target=self._accept_loop, name="speakd-accept", daemon=True
            )
            self._accept_thread.start()
        except BaseException:  # noqa: B036 - rolled back, then re-raised
            # A start that failed must leave a server that can be started
            # again, not one that claims an address it never bound.
            self._running.clear()
            if self._socket is not None:
                self._socket.close()
                self._socket = None
            raise

    def stop(self) -> None:
        with self._lock:
            if not self._running.is_set():
                # Never started, or already stopped. Going on would unlink
                # `address` -- and pointed at a live daemon's socket, which
                # is the ordinary case since every server is built with the
                # same default path, that deletes the one thing clients find
                # it by. Claimed under the lock that start() claims under,
                # so that two concurrent stop()s cannot both get past here
                # and tear the same connections down twice.
                return
            self._running.clear()
        if self._accept_thread is not None:
            self._accept_thread.join(timeout=_JOIN_TIMEOUT_SECONDS)
            self._accept_thread = None
        if self._socket is not None:
            self._socket.close()
            self._socket = None
        with self._lock:
            connections = list(self._connections)
            threads = list(self._conn_threads)
        # Three separate passes: shutdown every connection, THEN wait for
        # every connection thread to actually notice and exit, and only THEN
        # close the sockets. Interleaving close() into the same pass as
        # shutdown() risks a thread still in-flight inside a blocked read on
        # a file descriptor number the process has, by then, already reused
        # for something unrelated.
        for connection in connections:
            # A bare close() from this (the stopping) thread is not
            # guaranteed to wake a connection thread blocked in a read on the
            # same socket. shutdown() reliably does: the peer's blocked read
            # returns immediately with EOF.
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        for thread in threads:
            thread.join(timeout=_JOIN_TIMEOUT_SECONDS)
        for connection in connections:
            try:
                connection.close()
            except OSError:
                pass
        with self._lock:
            self._connections.clear()
            self._conn_threads.clear()
        try:
            self.address.unlink()
        except OSError:
            # exists() then unlink() was two calls with a race between them:
            # whoever else cleans up this path wins it, and a shutdown that
            # had otherwise finished threw FileNotFoundError. Nothing here
            # needs the file gone by its own hand, only gone.
            pass

    def _accept_loop(self) -> None:
        assert self._socket is not None
        while self._running.is_set():
            try:
                connection, _ = self._socket.accept()
            except TimeoutError:
                continue
            except OSError:
                break
            thread = threading.Thread(
                target=self._serve, args=(connection,), name="speakd-conn", daemon=True
            )
            with self._lock:
                self._connections.append(connection)
                self._conn_threads.append(thread)
                # Started while still holding the lock: a concurrent stop()
                # can only ever observe this thread in _conn_threads once it
                # has actually been started, so its later thread.join() can
                # never race an unstarted Thread (which raises RuntimeError
                # and would abort stop() before the socket file is unlinked).
                thread.start()

    def _serve(self, connection: socket.socket) -> None:
        subscription: Subscription | None = None
        subscriber_id = ""
        # Serializes every write to this connection: the subscribe ack, any
        # error/handler response, and every event the bus pushes. Without it,
        # two threads (this one, and this connection's writer) can both be
        # mid-sendall on the same socket, corrupting the framing. A Condition
        # rather than a plain Lock because the writer must also be held off
        # while an ack is owed -- see `ack_owed`.
        write = threading.Condition()
        # True between registering a subscription and that subscription's ack
        # actually going out. A publish can land in between (even, in the
        # extreme case, synchronously on this very thread from inside
        # bus.subscribe()), and the ack must always be the first thing a
        # client sees after asking to subscribe. A re-subscribe on the same
        # connection reopens exactly the same window, which is why this is
        # raised again each time rather than only once.
        ack_owed = False
        # Events are never written on the publishing thread -- that is the
        # speech worker's own thread, and an accepted socket has no send
        # timeout, so a subscriber that stopped reading would wedge it inside
        # sendall forever: no further speech, wait_idle never returning, and
        # a daemon that can never be started again. They go into this bounded
        # queue instead, and out through `writer`; past the bound the
        # subscriber is dropped rather than waited for.
        outbox: queue.Queue[bytes | None] = queue.Queue(maxsize=_SUBSCRIBER_BACKLOG)
        writer: threading.Thread | None = None
        # Set once the peer has proved unreachable, so the next publish drops
        # the subscription instead of filling the queue for nobody.
        gone = threading.Event()
        drop_lock = threading.Lock()
        dropped = False

        def write_locked(payload: bytes) -> None:
            with write:
                connection.sendall(payload)

        def claim_drop() -> bool:
            """Take responsibility for announcing this drop, exactly once."""
            nonlocal dropped
            with drop_lock:
                if dropped:
                    return False
                dropped = True
                return True

        def emit_event(event: Event) -> None:
            # Runs on the publishing thread, which is the speech worker's.
            # Nothing here may block, so the line is only ever offered to the
            # queue: a subscriber that cannot keep up is dropped, never
            # waited for.
            if gone.is_set() or dropped:
                raise _SubscriberUnreachable(subscriber_id)
            try:
                outbox.put_nowait(_event_line(event))
            except queue.Full:
                if claim_drop():
                    # Announced, because a GUI that silently stopped
                    # receiving is indistinguishable, to whoever is watching
                    # it, from a daemon with nothing to say. Published before
                    # the raise: this callback is already marked dropped, so
                    # the nested publish skips it and cannot recurse.
                    self._bus.publish(
                        Event(
                            kind="error",
                            source_id=subscriber_id,
                            data={
                                "message": (
                                    f"dropped subscriber {subscriber_id!r}: it stopped "
                                    f"reading and {_SUBSCRIBER_BACKLOG} events backed up"
                                ),
                                "subscriber": subscriber_id,
                            },
                        )
                    )
                    # Wakes this connection's own threads: the reader, so the
                    # connection is cleaned up rather than left behind, and
                    # the writer, wedged in a send to a peer that stopped
                    # reading. shutdown() does not block.
                    try:
                        connection.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
                raise _SubscriberUnreachable(subscriber_id) from None

        def writer_loop() -> None:
            """Drain the outbox onto the socket. The only thread that may block."""
            try:
                while True:
                    line = outbox.get()
                    if line is None:
                        return
                    try:
                        with write:
                            write.wait_for(lambda: not ack_owed)
                            connection.sendall(line)
                    except OSError:
                        # The peer vanished, or this connection was shut
                        # down. Left to the next publish to drop from the
                        # bus, and to the finally below to clean up.
                        return
            finally:
                gone.set()

        try:
            with connection.makefile("rb") as reader:
                for line in reader:
                    if not line.strip():
                        continue
                    try:
                        request = decode_request(line)
                    except Exception as exc:
                        # Not just ProtocolError: a client can send something
                        # that blows up json.loads in a way that is not a
                        # ProtocolError (e.g. RecursionError on deeply nested
                        # JSON). Reported to the caller either way, never a
                        # traceback and never a closed connection.
                        write_locked(encode(Response(ok=False, error=str(exc))))
                        continue
                    if request.verb is Verb.SUBSCRIBE:
                        raw_kinds = request.payload.get("kinds")
                        kinds: Sequence[str] | None = (
                            [str(item) for item in raw_kinds]
                            if isinstance(raw_kinds, list)
                            else None
                        )
                        if subscription is not None:
                            # A second subscribe on one connection must not
                            # leak the first: otherwise events arrive twice.
                            subscription.dispose()
                        subscriber_id = request.source_id
                        # Raised before registering, so that whatever the bus
                        # pushes from here on waits in the outbox until the
                        # ack below has actually gone out.
                        with write:
                            ack_owed = True
                        subscription = self._bus.subscribe(emit_event, kinds=kinds)
                        with write:
                            connection.sendall(encode(Response(ok=True)))
                            ack_owed = False
                            write.notify_all()
                        if writer is None:
                            # One writer per connection, started on its first
                            # subscribe: a connection that only ever sends
                            # requests never pays for a thread. Bound only
                            # once it is actually running -- bound before,
                            # a "can't start new thread" leaves the finally
                            # below joining a thread that never started,
                            # which throws from inside the cleanup and skips
                            # the deregistration and close under it.
                            started = threading.Thread(
                                target=writer_loop, name="speakd-writer", daemon=True
                            )
                            started.start()
                            writer = started
                        continue
                    try:
                        response = self._handler(request)
                    except Exception as exc:
                        # Reported to the caller, never swallowed and never
                        # left to surface as an unrelated traceback.
                        response = Response(ok=False, error=str(exc))
                    write_locked(encode(response))
        except OSError:
            # The peer vanished (write/read on a closed or reset socket).
            # Nothing else needs to know; the finally block below cleans up.
            pass
        finally:
            if subscription is not None:
                subscription.dispose()
            if writer is not None:
                # shutdown() first: a writer wedged in a send to a peer that
                # stopped reading is only freed by it, and joining before
                # that would burn the timeout for nothing.
                try:
                    connection.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                with write:
                    # An ack whose send raised would otherwise leave the
                    # writer waiting on `ack_owed` for one that never comes.
                    ack_owed = False
                    write.notify_all()
                _halt(outbox)
                writer.join(timeout=_JOIN_TIMEOUT_SECONDS)
            with self._lock:
                if connection in self._connections:
                    self._connections.remove(connection)
                current = threading.current_thread()
                if current in self._conn_threads:
                    self._conn_threads.remove(current)
            try:
                connection.close()
            except OSError:
                pass


class SocketClient:
    """A connection to the daemon.

    One connection, one purpose. A subscriber that stops draining its events
    is dropped and its connection is shut down with it, requests and all, so
    a client that wants both should open two — which is what `speakctl
    subscribe` does.
    """

    def __init__(self, connection: socket.socket) -> None:
        self._socket = connection
        self._reader = connection.makefile("rb")

    def send(self, request: Request) -> Response:
        """Send one request and read the next line back as its response.

        Strictly synchronous today: there is no correlation id on the wire, so
        the reply is whatever arrives next on this connection, and a second
        request must not be sent before the first has been answered. A
        pipelining client would need the protocol to grow correlation first.
        """
        return self.send_raw(encode(request))

    def send_raw(self, line: bytes) -> Response:
        self._socket.sendall(line)
        # Bounded via select(), not socket.settimeout(). self._reader wraps a
        # socket.SocketIO whose readinto() latches _timeout_occurred the
        # first time a socket-level timeout actually fires; every later read
        # on that same file object then raises "cannot read from timed out
        # object" forever after, with no way to reset it -- including reads
        # for a live subscription sharing this same reader (_iter_events
        # below). select() bounds the wait without ever touching the
        # socket's own timeout state, so a request that times out cannot
        # poison anything read afterward. A subscriber's event stream stays
        # untimed, as it always has, since this wait is local to this call.
        readable, _, _ = select.select([self._socket], [], [], _REQUEST_TIMEOUT_SECONDS)
        if not readable:
            raise TimeoutError("timed out waiting for a response")
        reply = self._reader.readline()
        if not reply:
            raise ConnectionError("daemon closed the connection")
        return decode_response(reply)

    def subscribe(self, kinds: Sequence[str] | None = None) -> Iterator[Event]:
        payload: dict[str, object] = {}
        if kinds is not None:
            payload["kinds"] = list(kinds)
        # Sent and acknowledged eagerly: by the time this call returns, the
        # subscription is already registered on the bus. Only the pulling of
        # event lines off the wire is deferred to the returned iterator.
        response = self.send(Request(verb=Verb.SUBSCRIBE, source_id="subscriber", payload=payload))
        if not response.ok:
            raise ConnectionError(f"subscribe was refused: {response.error}")
        return self._iter_events()

    def _iter_events(self) -> Iterator[Event]:
        for line in self._reader:
            try:
                parsed = json.loads(line.decode("utf-8"))
                if not isinstance(parsed, dict):
                    raise ProtocolError("event line must be a JSON object")
                data = parsed.get("data", {})
                if not isinstance(data, dict):
                    # dict(data) would otherwise raise a raw TypeError or
                    # ValueError for a list/number/etc, escaping the very
                    # wrapper this branch exists to provide.
                    raise ProtocolError("event data must be a JSON object")
                event = Event(
                    kind=str(parsed["event"]),
                    source_id=str(parsed["source_id"]),
                    data=data,
                )
            except ProtocolError:
                raise
            except (UnicodeDecodeError, json.JSONDecodeError, KeyError) as exc:
                raise ProtocolError(f"malformed event line: {exc}") from exc
            yield event

    def close(self) -> None:
        try:
            self._reader.close()
        finally:
            self._socket.close()


def connect(path: Path) -> SocketClient:
    connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    connection.connect(str(path))
    return SocketClient(connection)
