"""Tests for the loopback HTTP + server-sent-events transport."""

import json
import socket
import threading
import time
from collections.abc import Iterator
from http.client import HTTPConnection

import pytest

from speakd.channels import ChannelTable
from speakd.daemon import Daemon, ProfileView
from speakd.events import EventBus
from speakd.player import FakeSink, StreamingPlayer
from speakd.protocol import Request, Verb
from speakd.synth.fake import FakeEngine
from speakd.transport_http import HttpServer

TOKEN = "t" * 43


@pytest.fixture
def server() -> Iterator[tuple[HttpServer, Daemon]]:
    daemon = Daemon(
        FakeEngine(),
        StreamingPlayer(FakeSink()),
        lambda n: ProfileView(
            voice="v", speed=1.0, interrupt_on=(), prepare=lambda p: (list(p), [])
        ),
        bus=EventBus(),
        channels=ChannelTable(),
    )
    daemon.start()
    http = HttpServer(0, daemon.handle, daemon.bus, TOKEN)
    http.start()
    try:
        yield http, daemon
    finally:
        http.stop()
        daemon.stop()


def call(
    http: HttpServer,
    method: str,
    path: str,
    body: object = None,
    *,
    token: str | None = TOKEN,
    host: str | None = None,
    raw: bytes | None = None,
) -> tuple[int, dict[str, str], bytes]:
    connection = HTTPConnection("127.0.0.1", http.port, timeout=5)
    headers = {"Host": host or f"127.0.0.1:{http.port}"}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    if data is not None:
        headers["Content-Type"] = "application/json"
    connection.request(method, path, body=data, headers=headers)
    response = connection.getresponse()
    result = (response.status, {k.lower(): v for k, v in response.getheaders()}, response.read())
    connection.close()
    return result


def test_a_request_without_the_token_is_refused(server) -> None:  # type: ignore[no-untyped-def]
    http, _ = server
    assert call(http, "POST", "/v1/status", {}, token=None)[0] == 401
    assert call(http, "POST", "/v1/status", {}, token="wrong")[0] == 401


def test_a_foreign_host_is_refused_even_with_the_token(server) -> None:  # type: ignore[no-untyped-def]
    """DNS rebinding: a page on evil.example resolving to 127.0.0.1."""
    http, _ = server
    assert call(http, "POST", "/v1/status", {}, host=f"evil.example:{http.port}")[0] == 421
    assert call(http, "POST", "/v1/status", {}, host=f"localhost:{http.port}")[0] == 200


def test_status_and_enqueue_reach_the_daemon(server) -> None:  # type: ignore[no-untyped-def]
    http, daemon = server
    status, _, body = call(
        http,
        "POST",
        "/v1/enqueue",
        {"source_id": "zotero:K", "payload": {"text": "One.", "kind": "response"}},
    )
    assert status == 200
    assert json.loads(body)["ok"] is True
    assert daemon.wait_idle(timeout=5.0)
    listed = json.loads(call(http, "POST", "/v1/status", {})[2])["data"]["channels"]
    assert [c["source_id"] for c in listed] == ["zotero:K"]


def test_only_the_readers_verbs_are_served(server) -> None:  # type: ignore[no-untyped-def]
    http, _ = server
    assert (
        call(http, "POST", "/v1/set_capabilities", {"source_id": "x", "payload": {"briefs": True}})[
            0
        ]
        == 404
    )
    assert call(http, "POST", "/v1/nonsense", {})[0] == 404
    assert call(http, "GET", "/v1/status")[0] == 405


def test_bad_bodies_are_answered_and_the_server_carries_on(server) -> None:  # type: ignore[no-untyped-def]
    http, _ = server
    assert call(http, "POST", "/v1/status", raw=b"{not json")[0] == 400
    assert call(http, "POST", "/v1/status", raw=b"[1, 2]")[0] == 400
    # Refused on the declared length, before a byte of the body is read.
    with socket.create_connection(("127.0.0.1", http.port), timeout=5) as raw:
        raw.sendall(
            f"POST /v1/status HTTP/1.1\r\nHost: 127.0.0.1:{http.port}\r\n"
            f"Authorization: Bearer {TOKEN}\r\nContent-Length: {1024 * 1024 + 1}\r\n\r\n".encode()
        )
        assert raw.recv(64).split(b" ")[1] == b"413"
    assert call(http, "POST", "/v1/status", {})[0] == 200


def test_a_post_without_a_length_is_refused(server) -> None:  # type: ignore[no-untyped-def]
    http, _ = server
    with socket.create_connection(("127.0.0.1", http.port), timeout=5) as raw:
        raw.sendall(
            f"POST /v1/status HTTP/1.1\r\nHost: 127.0.0.1:{http.port}\r\n"
            f"Authorization: Bearer {TOKEN}\r\n\r\n".encode()
        )
        assert raw.recv(64).split(b" ")[1] == b"411"


def test_health_answers(server) -> None:  # type: ignore[no-untyped-def]
    http, _ = server
    status, _, body = call(http, "GET", "/v1/health")
    assert (status, json.loads(body)) == (200, {"ok": True})


def test_no_response_ever_allows_another_origin(server) -> None:  # type: ignore[no-untyped-def]
    http, _ = server
    for method, path in (("GET", "/v1/health"), ("POST", "/v1/status"), ("OPTIONS", "/v1/status")):
        _, headers, _ = call(http, method, path, {} if method == "POST" else None)
        assert not any(h.startswith("access-control-") for h in headers)


def open_events(http: HttpServer, token: str = TOKEN) -> socket.socket:
    raw = socket.create_connection(("127.0.0.1", http.port), timeout=5)
    raw.sendall(
        f"GET /v1/events HTTP/1.1\r\nHost: 127.0.0.1:{http.port}\r\n"
        f"Authorization: Bearer {token}\r\nAccept: text/event-stream\r\n\r\n".encode()
    )
    return raw


def read_until(raw: socket.socket, marker: bytes, timeout: float = 5.0) -> bytes:
    raw.settimeout(timeout)
    seen = b""
    while marker not in seen:
        chunk = raw.recv(4096)
        if not chunk:
            break
        seen += chunk
    return seen


def test_events_stream_as_server_sent_events(server) -> None:  # type: ignore[no-untyped-def]
    http, daemon = server
    raw = open_events(http)
    try:
        head = read_until(raw, b"\r\n\r\n")
        assert b" 200 " in head.split(b"\r\n")[0]
        assert b"text/event-stream" in head
        until = time.monotonic() + 2
        while daemon.bus.subscriber_count() == 0 and time.monotonic() < until:
            time.sleep(0.01)
        daemon.handle(
            Request(
                verb=Verb.ENQUEUE,
                source_id="zotero:K",
                payload={"text": "One.", "kind": "response"},
            )
        )
        stream = read_until(raw, b'"finished"')
        events = [
            json.loads(line[len(b"data: ") :])
            for line in stream.split(b"\n")
            if line.startswith(b"data: ")
        ]
        kinds = [e["event"] for e in events]
        assert "started" in kinds and "position" in kinds and "finished" in kinds
        started = next(e for e in events if e["event"] == "started")
        assert started["source_id"] == "zotero:K"
        assert started["data"]["segments"][0]["text"] == "One."
    finally:
        raw.close()


def test_an_event_stream_needs_the_token_too(server) -> None:  # type: ignore[no-untyped-def]
    http, _ = server
    raw = open_events(http, token="wrong")
    try:
        assert b" 401 " in read_until(raw, b"\r\n").split(b"\r\n")[0]
    finally:
        raw.close()


def test_a_client_that_leaves_is_forgotten(server) -> None:  # type: ignore[no-untyped-def]
    http, daemon = server
    raw = open_events(http)
    read_until(raw, b"\r\n\r\n")
    until = time.monotonic() + 2
    while daemon.bus.subscriber_count() == 0 and time.monotonic() < until:
        time.sleep(0.01)
    assert daemon.bus.subscriber_count() == 1
    raw.close()
    until = time.monotonic() + 5
    while daemon.bus.subscriber_count() and time.monotonic() < until:
        # A publish is what discovers the closed connection.
        daemon.handle(Request(verb=Verb.SET_LABEL, source_id="x", payload={"label": "x"}))
        daemon.bus.publish(__import__("speakd.events", fromlist=["Event"]).Event("metrics", "", {}))
        time.sleep(0.05)
    assert daemon.bus.subscriber_count() == 0


def test_a_client_that_never_reads_cannot_stall_the_daemon(server) -> None:  # type: ignore[no-untyped-def]
    from speakd.events import Event

    http, daemon = server
    raw = open_events(http)
    read_until(raw, b"\r\n\r\n")
    raw.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1024)
    done = threading.Event()

    def flood() -> None:
        for i in range(20000):
            daemon.bus.publish(Event("metrics", "", {"n": i, "pad": "x" * 200}))
        done.set()

    threading.Thread(target=flood, daemon=True).start()
    try:
        assert done.wait(timeout=10), "publishing blocked on a client that does not read"
    finally:
        raw.close()
