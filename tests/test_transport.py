"""Tests for the Unix socket transport."""

import threading
from pathlib import Path

import pytest

from speakd.events import Event, EventBus
from speakd.protocol import Request, Response, Verb
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
    client.close()
    del stream
    # Publishing to a dead subscriber must not raise, and the server must still serve.
    bus.publish(Event(kind="position", source_id="s", data={}))
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


def test_stop_removes_the_socket_file(tmp_path: Path) -> None:
    srv = SocketServer(tmp_path / "speakd.sock", echo_handler, EventBus())
    srv.start()
    assert srv.address.exists()
    srv.stop()
    assert not srv.address.exists()


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
