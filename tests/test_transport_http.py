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


def _declare(http: HttpServer, source_id: str, raws: list[dict[str, object]]) -> None:
    status, _, body = call(
        http,
        "POST",
        "/v1/declare_settings",
        {"source_id": source_id, "payload": {"settings": raws}},
    )
    assert status == 200
    assert json.loads(body)["ok"] is True


def test_zotero_over_http_can_set_its_own_key(server) -> None:  # type: ignore[no-untyped-def]
    http, _ = server
    _declare(
        http,
        "zotero:K",
        [{"name": "x", "type": "string", "default": "", "label": "l", "help": "h"}],
    )
    status, _, body = call(
        http,
        "POST",
        "/v1/set_setting",
        {"source_id": "zotero:K", "payload": {"key": "zotero.x", "value": "hi"}},
    )
    answer = json.loads(body)
    assert status == 200
    assert answer["ok"] is True
    assert answer["data"]["value"] == "hi"


def test_zotero_over_http_setting_speech_default_language_is_refused(server) -> None:  # type: ignore[no-untyped-def]
    http, _ = server
    status, _, body = call(
        http,
        "POST",
        "/v1/set_setting",
        {"source_id": "zotero:K", "payload": {"key": "speech.default_language", "value": "fr"}},
    )
    answer = json.loads(body)
    assert status == 200
    assert answer["ok"] is False


def test_zotero_over_http_declaring_a_setting_named_for_another_owner_is_refused(
    server: tuple[HttpServer, Daemon],
) -> None:
    """`zotero:K` tries to declare `claude-code.y`. A declaration's owner is
    always the source id's own, and a name carries no dots or dashes, so the
    declaration is refused: nothing comes into existence as `claude-code.y`,
    and nothing is smuggled in as `zotero.y` either. The verb itself still
    answers ok -- one bad declaration is skipped, not a hard error, so a
    client with nine good settings and a tenth of these loses only the tenth.
    """
    http, _ = server
    raw = {"name": "claude-code.y", "type": "bool", "default": True, "label": "l", "help": "h"}
    status, _, body = call(
        http,
        "POST",
        "/v1/declare_settings",
        {"source_id": "zotero:K", "payload": {"settings": [raw]}},
    )
    answer = json.loads(body)
    assert status == 200
    assert answer["ok"] is True
    assert answer["data"]["declared"] == 0
    status, _, body = call(
        http,
        "POST",
        "/v1/settings",
        {"source_id": "zotero:K", "payload": {"owner": "zotero"}},
    )
    assert json.loads(body)["data"]["values"] == {}


def test_settings_over_http_with_no_owner_answers_only_the_clients_own_and_speech(
    server: tuple[HttpServer, Daemon],
) -> None:
    http, _ = server
    _declare(
        http,
        "zotero:K",
        [{"name": "x", "type": "string", "default": "", "label": "l", "help": "h"}],
    )
    status, _, body = call(http, "POST", "/v1/settings", {"source_id": "zotero:K", "payload": {}})
    assert status == 200
    values = json.loads(body)["data"]["values"]
    assert "zotero.x" in values
    assert "speech.default_language" in values


def test_settings_over_http_naming_a_different_owner_is_narrowed_to_own_and_speech(
    server: tuple[HttpServer, Daemon],
) -> None:
    http, _ = server
    _declare(
        http,
        "zotero:K",
        [{"name": "x", "type": "string", "default": "", "label": "l", "help": "h"}],
    )
    _declare(
        http,
        "other:K",
        [{"name": "z", "type": "string", "default": "", "label": "l", "help": "h"}],
    )
    status, _, body = call(
        http,
        "POST",
        "/v1/settings",
        {"source_id": "zotero:K", "payload": {"owner": "other"}},
    )
    assert status == 200
    values = json.loads(body)["data"]["values"]
    assert "other.z" not in values
    assert "zotero.x" in values
    assert "speech.default_language" in values


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


def test_a_negative_content_length_is_refused(server) -> None:  # type: ignore[no-untyped-def]
    """`int("-1")` passes the `> _MAX_BODY` check, and `rfile.read(-1)` reads
    until the connection closes -- a negative length must be rejected before
    either happens."""
    http, _ = server
    with socket.create_connection(("127.0.0.1", http.port), timeout=5) as raw:
        raw.sendall(
            f"POST /v1/status HTTP/1.1\r\nHost: 127.0.0.1:{http.port}\r\n"
            f"Authorization: Bearer {TOKEN}\r\nContent-Length: -1\r\n\r\n".encode()
        )
        assert raw.recv(64).split(b" ")[1] == b"400"


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


def test_a_body_nested_too_deep_to_parse_is_a_400(server) -> None:  # type: ignore[no-untyped-def]
    http, _ = server
    assert call(http, "POST", "/v1/status", raw=b"[" * 100_000)[0] == 400
    assert call(http, "POST", "/v1/status", {})[0] == 200


def test_a_client_that_sends_headers_slowly_is_eventually_dropped(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """No `Content-Length`, so nothing blocks the server thread -- except the
    read timeout: a client that trickles a request line and never finishes it
    would otherwise hold a server thread forever."""
    import speakd.transport_http as transport_http

    monkeypatch.setattr(transport_http, "_READ_TIMEOUT", 0.5)
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
        with socket.create_connection(("127.0.0.1", http.port), timeout=15) as raw:
            raw.sendall(b"GET /v1/hea")  # never finishes the request line
            assert raw.recv(64) == b"", "the server must close a stalled connection"
    finally:
        http.stop()
        daemon.stop()


# --- render: a much larger body cap, and `out` scoped to home ----------


def _render_body(out: str) -> dict[str, object]:
    return {
        "source_id": "zotero:K",
        "payload": {
            "parts": [{"title": "Whole", "text": "One. Two."}],
            "out": out,
            "format": "mp3",
        },
    }


def test_render_accepts_a_body_over_1mb_while_other_verbs_stay_capped(  # type: ignore[no-untyped-def]
    server, tmp_path, monkeypatch
) -> None:
    http, daemon = server
    monkeypatch.setenv("HOME", str(tmp_path))
    big_text = "a" * (2 * 1024 * 1024)  # over the 1 MB cap every other verb keeps
    body = _render_body(str(tmp_path / "out.mp3"))
    body["payload"]["parts"] = [{"title": "Whole", "text": big_text}]  # type: ignore[index]
    status, _, response_body = call(http, "POST", "/v1/render", body)
    assert status == 200
    answer = json.loads(response_body)
    assert answer["ok"] is True
    assert isinstance(answer["data"]["job"], str)
    daemon.render_queue.cancel(answer["data"]["job"])

    # The same size body on an ordinary verb is still refused at 1 MB.
    enqueue_body = {"source_id": "x", "payload": {"text": big_text}}
    assert call(http, "POST", "/v1/enqueue", enqueue_body)[0] == 413


def test_render_out_must_be_absolute(server, tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    http, _ = server
    monkeypatch.setenv("HOME", str(tmp_path))
    status, _, body = call(http, "POST", "/v1/render", _render_body("relative/out.mp3"))
    answer = json.loads(body)
    assert status == 200
    assert answer["ok"] is False
    assert "absolute" in answer["error"]


def test_render_out_inside_home_is_accepted(server, tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    http, daemon = server
    monkeypatch.setenv("HOME", str(tmp_path))
    status, _, body = call(http, "POST", "/v1/render", _render_body(str(tmp_path / "out.mp3")))
    answer = json.loads(body)
    assert status == 200
    assert answer["ok"] is True
    daemon.render_queue.cancel(answer["data"]["job"])


def test_render_out_outside_home_is_refused(server, tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    http, _ = server
    home = tmp_path / "home"
    home.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    monkeypatch.setenv("HOME", str(home))
    status, _, body = call(http, "POST", "/v1/render", _render_body(str(outside / "out.mp3")))
    answer = json.loads(body)
    assert status == 200
    assert answer["ok"] is False
    assert "home" in answer["error"].lower()


def test_render_out_through_a_symlink_escaping_home_is_refused(  # type: ignore[no-untyped-def]
    server, tmp_path, monkeypatch
) -> None:
    http, _ = server
    home = tmp_path / "home"
    home.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    monkeypatch.setenv("HOME", str(home))
    escape = home / "escape"
    escape.symlink_to(outside)
    status, _, body = call(http, "POST", "/v1/render", _render_body(str(escape / "out.mp3")))
    answer = json.loads(body)
    assert status == 200
    assert answer["ok"] is False
    assert "home" in answer["error"].lower()


def test_render_cancel_is_served_over_http(server, tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    http, daemon = server
    monkeypatch.setenv("HOME", str(tmp_path))
    status, _, body = call(http, "POST", "/v1/render", _render_body(str(tmp_path / "out.mp3")))
    job_id = json.loads(body)["data"]["job"]
    status, _, body = call(
        http,
        "POST",
        "/v1/render_cancel",
        {"source_id": "zotero:K", "payload": {"job": job_id}},
    )
    assert status == 200
    answer = json.loads(body)
    assert answer["ok"] is True
    assert isinstance(daemon, Daemon)


def test_a_handler_that_raises_is_answered_not_dropped() -> None:
    """The socket transport answers a failing handler; so must this one."""
    from speakd.protocol import Response

    def explode(request: Request) -> Response:
        raise OSError("state directory is read-only")

    http = HttpServer(0, explode, EventBus(), TOKEN)
    http.start()
    try:
        status, _, body = call(http, "POST", "/v1/set_speed", {"payload": {"speed": 1.2}})
        answer = json.loads(body)
        assert status == 200
        assert answer["ok"] is False
        assert "read-only" in answer["error"]
        assert call(http, "GET", "/v1/health")[0] == 200
    finally:
        http.stop()
