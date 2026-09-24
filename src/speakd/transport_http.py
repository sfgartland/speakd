"""Loopback HTTP and server-sent events, for clients that cannot open a Unix socket.

The Zotero plugin is the first: it runs in Zotero's plugin sandbox, where
`fetch` is available and Unix sockets are not, inside a flatpak that cannot
see the daemon's socket anyway. This is an adapter and nothing more -- the
same `Request`s reach the same handler the socket serves, and the event
stream carries the same JSON the socket does, one `data:` line per event.

Loopback is not the socket's trust boundary, so three rules hold on every
request (see `speakd.http_token` for why):

- `Host` must name this server as 127.0.0.1 or localhost (421 otherwise),
  which defeats DNS rebinding -- a page on another name resolving here.
- `Authorization: Bearer <token>` must match (401 otherwise), which defeats
  every other page: a site can send a request here, but not the token.
- No CORS header is ever sent, so no page can read an answer by accident.

Only the verbs a document reader needs are served. `set_capabilities` above
all is not: nothing on this path briefs, and a page that could declare a
briefer could silence a session's responses.

Like the socket transport, a slow event reader never holds up speech: each
stream gets a bounded queue, and one that overflows is dropped.
"""

from __future__ import annotations

import hmac
import json
import queue
import threading
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from speakd.events import Event, EventBus
from speakd.protocol import Request, Response, Verb

Handler = Callable[[Request], Response]

VERBS = frozenset(
    {
        "enqueue",
        "cancel",
        "hush",
        "pause",
        "resume",
        "seek",
        "replay",
        "set_label",
        "set_speed",
        "status",
    }
)

# A request body is a control message; a paper's page of text is a few
# kilobytes. Anything past this is not something a client meant to send.
_MAX_BODY = 1024 * 1024

# Events a stream may fall behind by before it is dropped, as the socket
# transport bounds its outboxes.
_QUEUE_EVENTS = 1024

# How often an idle stream says it is alive, so a client can tell a quiet
# daemon from a dead connection, and a closed client is noticed.
_PING_SECONDS = 15.0

# A write that has not gone through in this long is to a client that has
# stopped reading; the thread is given back rather than parked on it.
_WRITE_TIMEOUT = 30.0


class _Dropped(Exception):
    """Raised into the bus by a stream that has fallen too far behind."""


def _event_json(event: Event) -> str:
    return json.dumps({"event": event.kind, "source_id": event.source_id, "data": event.data})


class HttpServer:
    def __init__(
        self, port: int, handler: Handler, bus: EventBus, token: str, host: str = "127.0.0.1"
    ) -> None:
        self._handler = handler
        self._bus = bus
        self._token = token
        self._stopping = threading.Event()
        self._server = ThreadingHTTPServer((host, port), self._request_class())
        self._server.daemon_threads = True
        self._thread: threading.Thread | None = None

    @property
    def port(self) -> int:
        return int(self._server.server_address[1])

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._server.serve_forever, name="speakd-http", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stopping.set()
        self._server.shutdown()
        self._server.server_close()

    def _request_class(self) -> type[BaseHTTPRequestHandler]:
        outer = self

        class RequestHandler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            server_version = "speakd"

            def log_message(self, format: str, *args: object) -> None:  # noqa: A002
                # Every request would otherwise be a line in the journal.
                pass

            # ---- answers ----

            def _answer(self, status: int, body: dict[str, object]) -> None:
                data = json.dumps(body).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(data)
                self.close_connection = True

            def _refuse(self, status: int, error: str) -> None:
                self._answer(status, {"ok": False, "data": {}, "error": error})

            def _allowed(self) -> bool:
                """The Host and token checks every request passes first."""
                port = outer.port
                if self.headers.get("Host", "") not in (f"127.0.0.1:{port}", f"localhost:{port}"):
                    self._refuse(421, "this server answers to 127.0.0.1 and localhost only")
                    return False
                given = self.headers.get("Authorization", "")
                wanted = f"Bearer {outer._token}"
                if not hmac.compare_digest(given.encode(), wanted.encode()):
                    self._refuse(401, "missing or wrong token (speakctl http-token)")
                    return False
                return True

            # ---- methods ----

            def do_GET(self) -> None:  # noqa: N802 - http.server's naming
                if not self._allowed():
                    return
                if self.path == "/v1/health":
                    self._answer(200, {"ok": True})
                elif self.path == "/v1/events":
                    self._stream()
                elif self.path.removeprefix("/v1/") in VERBS:
                    self._refuse(405, "verbs are POSTed")
                else:
                    self._refuse(404, f"no such endpoint: {self.path}")

            def do_POST(self) -> None:  # noqa: N802
                if not self._allowed():
                    return
                verb = self.path.removeprefix("/v1/")
                if not self.path.startswith("/v1/") or verb not in VERBS:
                    self._refuse(404, f"no such verb: {verb}")
                    return
                length = self.headers.get("Content-Length")
                if length is None:
                    self._refuse(411, "a Content-Length is required")
                    return
                try:
                    size = int(length)
                except ValueError:
                    self._refuse(400, "Content-Length is not a number")
                    return
                if size > _MAX_BODY:
                    self._refuse(413, f"a request body is at most {_MAX_BODY} bytes")
                    return
                try:
                    body = json.loads(self.rfile.read(size) or b"{}")
                except (ValueError, RecursionError):
                    # ValueError covers undecodable bytes as well as bad JSON;
                    # RecursionError is a body nested deeper than the parser
                    # will follow. Either way the answer is the client's fault.
                    self._refuse(400, "the body is not JSON")
                    return
                if not isinstance(body, dict):
                    self._refuse(400, "the body must be a JSON object")
                    return
                source_id = body.get("source_id", "")
                payload = body.get("payload", {})
                if not isinstance(source_id, str) or not isinstance(payload, dict):
                    self._refuse(400, "source_id must be a string and payload an object")
                    return
                try:
                    response = outer._handler(Request(Verb(verb), source_id, payload))
                except Exception as exc:
                    # Answered, as the socket transport answers it. Unanswered,
                    # a client cannot tell a verb that failed from a daemon that
                    # is not there, and says the daemon is not running.
                    response = Response(ok=False, error=f"{verb} failed: {exc!r}")
                self._answer(
                    200, {"ok": response.ok, "data": response.data, "error": response.error}
                )

            def _other(self) -> None:
                # OPTIONS included: a preflight gets no CORS headers, so the
                # browser refuses the cross-origin request it was asking about.
                self._refuse(405, "GET or POST only")

            do_PUT = do_DELETE = do_PATCH = do_OPTIONS = do_HEAD = _other  # noqa: N815

            # ---- the event stream ----

            def _stream(self) -> None:
                outbox: queue.Queue[str] = queue.Queue(maxsize=_QUEUE_EVENTS)
                dropped = threading.Event()

                def offer(event: Event) -> None:
                    # On the publishing thread, which may be the speech
                    # worker's: never blocks. A full queue drops this stream,
                    # and raising is how the bus learns to forget it.
                    if dropped.is_set():
                        raise _Dropped
                    try:
                        outbox.put_nowait(_event_json(event))
                    except queue.Full:
                        dropped.set()
                        raise _Dropped from None

                subscription = outer._bus.subscribe(offer)
                try:
                    self.connection.settimeout(_WRITE_TIMEOUT)
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Cache-Control", "no-cache")
                    self.send_header("Connection", "close")
                    self.end_headers()
                    self.wfile.write(b": speakd events\n\n")
                    self.wfile.flush()
                    while not (dropped.is_set() or outer._stopping.is_set()):
                        try:
                            line = outbox.get(timeout=_PING_SECONDS)
                        except queue.Empty:
                            self.wfile.write(b": ping\n\n")
                        else:
                            self.wfile.write(f"data: {line}\n\n".encode())
                        self.wfile.flush()
                except OSError:
                    pass  # the client went away, or stopped reading
                finally:
                    subscription.dispose()
                    self.close_connection = True

        return RequestHandler
