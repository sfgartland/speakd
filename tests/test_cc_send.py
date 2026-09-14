"""Tests for the client's one conversation with the daemon."""

import socket as socketlib
import threading
import time
from collections.abc import Sequence
from pathlib import Path

import pytest

from speakd import transport
from speakd.channels import ChannelTable
from speakd.clients.claude_code.send import enqueue, hush, send
from speakd.daemon import Daemon, ProfileView
from speakd.events import EventBus
from speakd.model import Piece
from speakd.player import RecordingPlayer
from speakd.protocol import Request, Verb
from speakd.synth.fake import FakeEngine
from speakd.transport import SocketClient, SocketServer


def passthrough(pieces: Sequence[Piece]) -> tuple[list[Piece], list[str]]:
    return list(pieces), []


def profile_for(name: str) -> ProfileView:
    return ProfileView(voice="af_heart", speed=1.1, interrupt_on=("error",), prepare=passthrough)


@pytest.fixture
def running(tmp_path: Path):  # type: ignore[no-untyped-def]
    player = RecordingPlayer()
    daemon = Daemon(FakeEngine(), player, profile_for, bus=EventBus(), channels=ChannelTable())
    daemon.start()
    server = SocketServer(tmp_path / "speakd.sock", daemon.handle, daemon.bus)
    server.start()
    try:
        yield Path(str(server.address)), daemon, player
    finally:
        server.stop()
        daemon.stop()


def test_enqueue_reaches_the_daemon(running) -> None:  # type: ignore[no-untyped-def]
    socket, daemon, player = running
    assert enqueue("cc:s1", "One. Two.", socket=socket) is None
    assert daemon.wait_idle(timeout=5.0)
    assert len(player.played) == 2


def test_hush_reaches_the_daemon(running) -> None:  # type: ignore[no-untyped-def]
    socket, _daemon, _player = running
    assert hush("cc:s1", socket=socket) is None


def test_no_daemon_returns_a_reason_rather_than_raising(tmp_path: Path) -> None:
    absent = tmp_path / "nothing.sock"
    reason = send(Request(verb=Verb.HUSH, source_id="cc:s1"), socket=absent)
    assert reason is not None
    assert str(absent) in reason
    assert "\n" not in reason


def test_a_refused_verb_returns_the_daemon_error(running) -> None:  # type: ignore[no-untyped-def]
    socket, _daemon, _player = running
    reason = send(
        Request(verb=Verb.ENQUEUE, source_id="cc:s1", payload={"text": ""}),
        socket=socket,
    )
    assert reason is None or "\n" not in reason


def test_a_refusal_is_reported_with_the_daemon_s_reason(running) -> None:  # type: ignore[no-untyped-def]
    """The test above passes if the refusal branch is replaced by a bare `None`.

    It is written permissively on purpose — whether a given verb is refused
    is the daemon's business, not this module's. But *this* verb is refused,
    reliably, and a client that turned "refused" into "fine" would lose every
    diagnostic the log exists to carry. So the same call is asserted twice.
    """
    socket, _daemon, _player = running
    reason = send(
        Request(verb=Verb.ENQUEUE, source_id="cc:s1", payload={"text": ""}),
        socket=socket,
    )
    assert reason is not None, "a refused request must not read as a success"
    assert "enqueue refused" in reason
    assert "non-empty text" in reason
    assert "\n" not in reason


def test_enqueueing_nothing_never_opens_a_connection(tmp_path: Path) -> None:
    # No socket exists, so a connection attempt would fail loudly.
    assert enqueue("cc:s1", "   ", socket=tmp_path / "nothing.sock") is None
    assert enqueue("cc:s1", "", socket=tmp_path / "nothing.sock") is None


@pytest.fixture
def wedged(tmp_path: Path):  # type: ignore[no-untyped-def]
    """A listener that accepts a connection and then never answers.

    Not the same failure as a dead daemon: the connect succeeds, so only the
    timeout gets the caller back out.
    """
    address = tmp_path / "wedged.sock"
    listener = socketlib.socket(socketlib.AF_UNIX, socketlib.SOCK_STREAM)
    listener.bind(str(address))
    listener.listen(4)
    held: list[socketlib.socket] = []
    stop = threading.Event()

    def accept_and_ignore() -> None:
        listener.settimeout(0.1)
        while not stop.is_set():
            try:
                connection, _ = listener.accept()
            except OSError:
                continue
            held.append(connection)

    thread = threading.Thread(target=accept_and_ignore, daemon=True)
    thread.start()
    try:
        yield address
    finally:
        stop.set()
        thread.join(timeout=2.0)
        for connection in held:
            connection.close()
        listener.close()


def test_a_wedged_daemon_gives_up_inside_the_timeout(wedged: Path) -> None:
    started = time.monotonic()
    reason = send(Request(verb=Verb.HUSH, source_id="cc:s1"), socket=wedged, timeout=0.05)
    elapsed = time.monotonic() - started
    assert reason is not None
    assert "hush failed" in reason
    assert "\n" not in reason
    # The transport's own default is 5s. Anything near that means the timeout
    # argument never reached select(), which is the whole point of passing it:
    # a hook has three seconds before Claude Code fails the turn for it.
    assert elapsed < 1.0, f"gave up only after {elapsed:.2f}s"


def test_the_connection_is_closed_even_when_the_request_fails(  # type: ignore[no-untyped-def]
    running,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    socket, _daemon, _player = running
    closed: list[str] = []
    real_connect = transport.connect

    def tracking(path: Path, *, timeout: float | None = None) -> object:
        client = real_connect(path, timeout=timeout)

        def explode(request: Request, *, timeout: float | None = None) -> None:
            raise ConnectionResetError("the daemon went away mid-request")

        def close() -> None:
            closed.append("closed")
            SocketClient.close(client)

        monkeypatch.setattr(client, "send", explode)
        monkeypatch.setattr(client, "close", close)
        return client

    monkeypatch.setattr(transport, "connect", tracking)
    reason = send(Request(verb=Verb.HUSH, source_id="cc:s1"), socket=socket)
    assert reason is not None and "went away mid-request" in reason
    # A hook process is short-lived, so a leaked descriptor is cheap; a hook
    # that leaves the daemon holding a half-open connection per tool call is
    # not, and only the `finally` prevents it.
    assert closed == ["closed"]


def test_repeated_failures_stay_bounded_and_never_raise(tmp_path: Path) -> None:
    """The same call, many times over, must keep terminating the same way.

    The layer below this one shipped a bug that only existed across
    successive calls. This layer holds no state, which is exactly the claim
    worth checking rather than assuming: twenty sends against an absent
    socket must give twenty identical bounded answers, not a descriptor leak
    or a wait that grows.
    """
    absent = tmp_path / "nothing.sock"
    reasons: list[str | None] = []
    started = time.monotonic()
    for _ in range(20):
        reasons.append(hush("cc:s1", socket=absent, timeout=0.05))
    elapsed = time.monotonic() - started
    assert all(reason is not None for reason in reasons)
    assert len(set(reasons)) == 1, f"the reason drifted between calls: {set(reasons)}"
    assert elapsed < 2.0, f"twenty failed sends took {elapsed:.2f}s"


@pytest.fixture
def deaf(tmp_path: Path):  # type: ignore[no-untyped-def]
    """A socket that listens and never accepts.

    Different from `wedged`, which accepts and then ignores you: here the
    connect itself is what eventually blocks, once the listen backlog fills.
    A daemon whose accept loop has died looks exactly like this.
    """
    address = tmp_path / "deaf.sock"
    listener = socketlib.socket(socketlib.AF_UNIX, socketlib.SOCK_STREAM)
    listener.bind(str(address))
    listener.listen(1)
    try:
        yield address
    finally:
        listener.close()


def test_a_socket_that_never_accepts_cannot_wedge_the_hook(deaf: Path) -> None:
    """`connect` has to be bounded too, not just the reply.

    The first calls succeed into the backlog and come back on the read
    timeout. Once the backlog is full the connect itself blocks, and with no
    timeout on it the hook hangs until Claude Code kills it — which is a
    failed turn, the one outcome this module exists to prevent.

    Run on a daemon thread so that a regression fails this test in ten
    seconds instead of hanging the suite forever.
    """
    results: list[str | None] = []
    done = threading.Event()

    def probe() -> None:
        for _ in range(8):
            results.append(hush("cc:s1", socket=deaf, timeout=0.1))
        done.set()

    threading.Thread(target=probe, daemon=True).start()
    assert done.wait(10.0), (
        f"blocked after {len(results)} of 8 calls: connect is not bounded by the timeout"
    )
    assert all(reason is not None for reason in results)
    assert all("\n" not in str(reason) for reason in results)


def test_connecting_and_replying_share_one_budget(  # type: ignore[no-untyped-def]
    running,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`timeout` is the cost of the call, not the cost of each half of it.

    Spent once on the connect and again on the reply, a hush costs up to
    2x what it was given, and UserPromptSubmit sends two of them inside a
    3s window. What the reply gets has to be what is left.
    """
    socket, _daemon, _player = running
    real_connect = transport.connect
    handed: list[float | None] = []
    spent_connecting = 0.25

    def slow_connect(path: Path, *, timeout: float | None = None) -> object:
        time.sleep(spent_connecting)
        client = real_connect(path, timeout=timeout)
        original = client.send

        def recording(request: Request, *, timeout: float | None = None) -> object:
            handed.append(timeout)
            return original(request, timeout=timeout)

        monkeypatch.setattr(client, "send", recording)
        return client

    monkeypatch.setattr(transport, "connect", slow_connect)
    budget = 0.6
    started = time.monotonic()
    assert send(Request(verb=Verb.HUSH, source_id="cc:s1"), socket=socket, timeout=budget) is None
    elapsed = time.monotonic() - started

    assert handed and handed[0] is not None
    assert handed[0] <= budget - spent_connecting + 0.05, (
        f"the reply was handed {handed[0]}s of a {budget}s budget after "
        f"{spent_connecting}s had already gone on connecting"
    )
    assert elapsed < budget + 0.2, f"the whole call took {elapsed:.2f}s of a {budget}s budget"


def test_a_call_never_costs_more_than_its_timeout(deaf: Path) -> None:
    """Measured end to end, against the socket that makes both halves wait."""
    budget = 0.3
    worst = 0.0
    results: list[str | None] = []
    done = threading.Event()

    def probe() -> None:
        nonlocal worst
        for _ in range(8):
            started = time.monotonic()
            results.append(hush("cc:s1", socket=deaf, timeout=budget))
            worst = max(worst, time.monotonic() - started)
        done.set()

    threading.Thread(target=probe, daemon=True).start()
    assert done.wait(15.0), "a call never came back at all"
    assert all(reason is not None for reason in results)
    # Generous on absolute overhead, strict on the multiple: the failure this
    # guards is a second full budget, not a slow machine.
    assert worst < budget * 1.6, f"worst call took {worst:.2f}s of a {budget}s budget"


def test_a_connect_that_eats_the_whole_budget_gives_up_cleanly(  # type: ignore[no-untyped-def]
    running,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Nothing left for the reply is a reason, not a negative timeout."""
    socket, _daemon, _player = running
    real_connect = transport.connect

    def crawling_connect(path: Path, *, timeout: float | None = None) -> object:
        time.sleep(0.3)
        return real_connect(path, timeout=timeout)

    monkeypatch.setattr(transport, "connect", crawling_connect)
    started = time.monotonic()
    reason = send(Request(verb=Verb.HUSH, source_id="cc:s1"), socket=socket, timeout=0.1)
    elapsed = time.monotonic() - started
    assert reason is not None
    assert "\n" not in reason
    assert "went on connecting" in reason, reason
    # Still bounded by what the connect cost, not by a second budget on top.
    assert elapsed < 1.0, f"took {elapsed:.2f}s"
