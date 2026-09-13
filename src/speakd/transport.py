"""Unix socket transport: line-delimited JSON in, line-delimited JSON out.

One accept loop, one thread per connection. A connection that sends `subscribe`
stops being a request/response channel and becomes an event stream until the
client goes away.

A malformed line is answered with an error response rather than a dropped
connection: a client that sent nonsense deserves to be told what was wrong. A
client that vanishes is dropped from the bus without disturbing anything else,
because a GUI closing its window must never silence speech.
"""

from __future__ import annotations

import json
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
# How long a client waits for a response to a request before giving up. Only
# ever applied around the request/response read -- a subscriber's event
# stream is otherwise unbounded, since an idle stream is not a stuck one.
_REQUEST_TIMEOUT_SECONDS = 5.0


def _event_line(event: Event) -> bytes:
    body: dict[str, object] = {
        "event": event.kind,
        "source_id": event.source_id,
        "data": event.data,
    }
    return (json.dumps(body) + "\n").encode("utf-8")


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
        self.address.parent.mkdir(parents=True, exist_ok=True)
        # A daemon that died without cleaning up must not make the next one
        # unstartable.
        if self.address.exists():
            self.address.unlink()
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(self.address))
        server.listen(16)
        server.settimeout(_ACCEPT_POLL_SECONDS)
        self._socket = server
        self._running.set()
        self._accept_thread = threading.Thread(
            target=self._accept_loop, name="speakd-accept", daemon=True
        )
        self._accept_thread.start()

    def stop(self) -> None:
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
        if self.address.exists():
            self.address.unlink()

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
        # Serializes every write to this connection: the subscribe ack, any
        # error/handler response, and every event the bus pushes. Without it,
        # two threads (this one, and whichever thread calls bus.publish())
        # can both be mid-sendall on the same socket, corrupting the framing.
        write_lock = threading.Lock()
        acked = False
        pending: list[bytes] = []

        def write_locked(payload: bytes) -> None:
            with write_lock:
                connection.sendall(payload)

        def emit_event(event: Event) -> None:
            line = _event_line(event)
            with write_lock:
                if acked:
                    connection.sendall(line)
                else:
                    # A publish can land between registering with the bus and
                    # sending the ack (even, in the extreme case, from the
                    # very same thread if something publishes synchronously
                    # from inside bus.subscribe()). Queue it rather than
                    # write it: the ack must always be the first thing this
                    # client sees after asking to subscribe.
                    pending.append(line)

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
                        # acked must be reset here: a re-subscribe reopens the
                        # same ack-then-flush window as the first one. Left at
                        # True (its value from the first subscribe's ack), an
                        # event published between this registration and the
                        # second ack below would take the "already acked"
                        # branch in emit_event and be written ahead of the
                        # second ack -- the same desync the pending-buffer
                        # exists to prevent, just reopened by reuse.
                        with write_lock:
                            acked = False
                        # self._bus.subscribe() (like subscription.dispose()
                        # above) must stay OUTSIDE `with write_lock`: the bus
                        # can invoke emit_event synchronously, on this same
                        # thread, from inside subscribe() -- that is exactly
                        # how the regression tests force this race. Were this
                        # call inside the lock, that nested same-thread
                        # callback would deadlock re-acquiring a lock this
                        # thread already holds. Do not "tidy" this into one
                        # critical section.
                        subscription = self._bus.subscribe(emit_event, kinds=kinds)
                        with write_lock:
                            connection.sendall(encode(Response(ok=True)))
                            acked = True
                            for buffered in pending:
                                connection.sendall(buffered)
                            pending.clear()
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
    """A connection to the daemon."""

    def __init__(self, connection: socket.socket) -> None:
        self._socket = connection
        self._reader = connection.makefile("rb")

    def send(self, request: Request) -> Response:
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
