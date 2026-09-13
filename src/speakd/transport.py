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
        for connection in connections:
            # A bare close() from this (the stopping) thread is not
            # guaranteed to wake a connection thread blocked in a read on the
            # same socket. shutdown() reliably does: the peer's blocked read
            # returns immediately with EOF.
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                connection.close()
            except OSError:
                pass
        for thread in threads:
            thread.join(timeout=_JOIN_TIMEOUT_SECONDS)
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
            thread.start()

    def _serve(self, connection: socket.socket) -> None:
        subscription: Subscription | None = None
        try:
            with connection.makefile("rb") as reader:
                for line in reader:
                    if not line.strip():
                        continue
                    try:
                        request = decode_request(line)
                    except ProtocolError as exc:
                        connection.sendall(encode(Response(ok=False, error=str(exc))))
                        continue
                    if request.verb is Verb.SUBSCRIBE:
                        raw_kinds = request.payload.get("kinds")
                        kinds: Sequence[str] | None = (
                            [str(item) for item in raw_kinds]
                            if isinstance(raw_kinds, list)
                            else None
                        )
                        subscription = self._bus.subscribe(
                            lambda event: connection.sendall(_event_line(event)),
                            kinds=kinds,
                        )
                        connection.sendall(encode(Response(ok=True)))
                        continue
                    try:
                        response = self._handler(request)
                    except Exception as exc:
                        # Reported to the caller, never swallowed and never
                        # left to surface as an unrelated traceback.
                        response = Response(ok=False, error=str(exc))
                    connection.sendall(encode(response))
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
        self.send(Request(verb=Verb.SUBSCRIBE, source_id="subscriber", payload=payload))
        return self._iter_events()

    def _iter_events(self) -> Iterator[Event]:
        for line in self._reader:
            body = json.loads(line.decode("utf-8"))
            yield Event(
                kind=str(body["event"]),
                source_id=str(body["source_id"]),
                data=dict(body.get("data", {})),
            )

    def close(self) -> None:
        try:
            self._reader.close()
        finally:
            self._socket.close()


def connect(path: Path) -> SocketClient:
    connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    connection.connect(str(path))
    return SocketClient(connection)
