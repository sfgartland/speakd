"""Tests for the daemon-facing speakctl verbs and the `python -m speakd` entry point."""

import json
import signal
import socket
import subprocess
import sys
import textwrap
import threading
import time
from collections.abc import Iterator, Sequence
from pathlib import Path

import numpy as np
import pytest

from speakd.channels import ChannelTable
from speakd.cli import default_socket_path, main
from speakd.daemon import Daemon, ProfileView
from speakd.events import EventBus
from speakd.model import Piece
from speakd.player import RecordingPlayer
from speakd.synth.fake import FakeEngine
from speakd.transport import SocketServer


def passthrough(pieces: Sequence[Piece]) -> tuple[list[Piece], list[str]]:
    return list(pieces), []


def profile_for(name: str) -> ProfileView:
    return ProfileView(voice="af_heart", speed=1.1, interrupt_on=("error",), prepare=passthrough)


def _until(predicate, timeout: float = 5.0) -> bool:  # type: ignore[no-untyped-def]
    """Poll until `predicate` holds. Returns whether it ever did."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return bool(predicate())


class BlockingPlayer:
    """Blocks inside play() until the test releases it."""

    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()
        self.played: list[np.ndarray] = []

    def play(self, audio: np.ndarray, sample_rate: int) -> None:
        self.started.set()
        self.release.wait(timeout=10.0)
        self.played.append(audio)

    def stop(self) -> None:
        # Deliberately does not release: the drain must be observable while
        # the worker is still held inside the utterance being hushed.
        pass


@pytest.fixture
def running(tmp_path: Path):  # type: ignore[no-untyped-def]
    player = RecordingPlayer()
    daemon = Daemon(FakeEngine(), player, profile_for, bus=EventBus(), channels=ChannelTable())
    daemon.start()
    server = SocketServer(tmp_path / "speakd.sock", daemon.handle, daemon.bus)
    server.start()
    try:
        yield server.address, daemon, player
    finally:
        server.stop()
        daemon.stop()


@pytest.fixture
def served(tmp_path: Path) -> Iterator[tuple[SocketServer, Daemon]]:
    """Like `running`, but hands back the server so a test can close it."""
    daemon = Daemon(
        FakeEngine(), RecordingPlayer(), profile_for, bus=EventBus(), channels=ChannelTable()
    )
    daemon.start()
    server = SocketServer(tmp_path / "speakd.sock", daemon.handle, daemon.bus)
    server.start()
    try:
        yield server, daemon
    finally:
        server.stop()
        daemon.stop()


@pytest.fixture
def blocking(tmp_path: Path):  # type: ignore[no-untyped-def]
    """A daemon whose player stays inside one utterance until released."""
    player = BlockingPlayer()
    daemon = Daemon(FakeEngine(), player, profile_for, bus=EventBus(), channels=ChannelTable())
    daemon.start()
    server = SocketServer(tmp_path / "speakd.sock", daemon.handle, daemon.bus)
    server.start()
    try:
        yield server.address, daemon, player
    finally:
        player.release.set()
        server.stop()
        daemon.stop()


# ---------------------------------------------------------------------------
# The verbs, over a live daemon
# ---------------------------------------------------------------------------


def test_enqueue_speaks_through_the_daemon(running, capsys) -> None:  # type: ignore[no-untyped-def]
    address, daemon, player = running
    code = main(["enqueue", "One. Two.", "--source", "s", "--socket", str(address)])
    assert code == 0
    assert daemon.wait_idle(timeout=5.0)
    assert len(player.played) == 2


def test_hush_is_accepted(running) -> None:  # type: ignore[no-untyped-def]
    address, _daemon, _player = running
    assert main(["hush", "--source", "s", "--socket", str(address)]) == 0


def test_cancel_is_accepted(running) -> None:  # type: ignore[no-untyped-def]
    address, _daemon, _player = running
    assert main(["cancel", "--source", "s", "--socket", str(address)]) == 0


def test_role_sets_the_channel_role(running) -> None:  # type: ignore[no-untyped-def]
    address, daemon, _player = running
    assert main(["role", "background", "--source", "s", "--socket", str(address)]) == 0
    channel = daemon.channels.get("s")
    assert channel is not None and channel.role.value == "background"


def test_an_unknown_role_exits_non_zero(running, capsys) -> None:  # type: ignore[no-untyped-def]
    address, _daemon, _player = running
    assert main(["role", "loud", "--source", "s", "--socket", str(address)]) != 0
    assert "role" in capsys.readouterr().err


def test_an_unknown_role_is_rejected_without_reaching_the_daemon(tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    """The client knows the two roles, so a typo does not need a daemon to name it.

    The socket here does not exist: were the check left entirely to the
    daemon, this would print "no daemon" instead of naming the bad role.
    """
    missing = tmp_path / "absent.sock"
    assert main(["role", "loud", "--source", "s", "--socket", str(missing)]) != 0
    err = capsys.readouterr().err
    assert "loud" in err
    assert "no daemon" not in err


def test_priority_sets_the_channel_priority(running) -> None:  # type: ignore[no-untyped-def]
    address, daemon, _player = running
    assert main(["priority", "7", "--source", "s", "--socket", str(address)]) == 0
    channel = daemon.channels.get("s")
    assert channel is not None and channel.priority == 7


def test_status_prints_the_channels_as_json(running, capsys) -> None:  # type: ignore[no-untyped-def]
    address, _daemon, _player = running
    main(["priority", "3", "--source", "a", "--socket", str(address)])
    assert main(["status", "--socket", str(address)]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert any(c["source_id"] == "a" and c["priority"] == 3 for c in payload["channels"])


def test_status_does_not_open_a_channel_for_itself(running, capsys) -> None:  # type: ignore[no-untyped-def]
    """Asking what the channels are must not add one."""
    address, daemon, _player = running
    assert main(["status", "--socket", str(address)]) == 0
    assert json.loads(capsys.readouterr().out)["channels"] == []
    assert daemon.channels.get("cli") is None


def test_a_refused_verb_prints_the_daemons_error_and_exits_non_zero(running, capsys) -> None:  # type: ignore[no-untyped-def]
    address, _daemon, _player = running
    assert main(["enqueue", "   ", "--source", "s", "--socket", str(address)]) != 0
    assert "non-empty text" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Reaching (and failing to reach) the daemon
# ---------------------------------------------------------------------------


def test_no_daemon_gives_one_clear_line_not_a_traceback(tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    missing = tmp_path / "absent.sock"
    code = main(["hush", "--source", "s", "--socket", str(missing)])
    assert code != 0
    err = capsys.readouterr().err
    assert "no daemon" in err
    assert "Traceback" not in err


def test_the_no_daemon_line_names_the_socket_and_how_to_start_one(tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    missing = tmp_path / "absent.sock"
    main(["hush", "--source", "s", "--socket", str(missing)])
    err = capsys.readouterr().err.strip()
    assert err == f"speakctl: no daemon at {missing} (start it with: speakd)"


def test_a_daemon_that_drops_the_connection_is_reported_not_traced(tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    """A socket that accepts and then hangs up is not a traceback either."""
    path = tmp_path / "rude.sock"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(path))
    listener.listen(1)

    def accept_then_hang_up() -> None:
        try:
            connection, _ = listener.accept()
        except OSError:
            return
        connection.close()

    greeter = threading.Thread(target=accept_then_hang_up, daemon=True)
    greeter.start()
    try:
        code = main(["hush", "--source", "s", "--socket", str(path)])
    finally:
        greeter.join(timeout=5.0)
        listener.close()
    assert code != 0
    err = capsys.readouterr().err
    assert "Traceback" not in err
    assert str(path) in err


def test_say_still_works_without_a_daemon(capsys) -> None:  # type: ignore[no-untyped-def]
    assert main(["say", "One. Two.", "--dry-run"]) == 0
    assert json.loads(capsys.readouterr().out)["segments"]


def test_default_socket_path_is_under_the_runtime_dir(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/1234")
    assert default_socket_path() == Path("/run/user/1234/speakd/speakd.sock")


def test_default_socket_path_falls_back_when_the_runtime_dir_is_unset(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    assert default_socket_path().name == "speakd.sock"


def test_default_socket_path_uses_tmpdir_when_the_runtime_dir_is_unset(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    monkeypatch.setenv("TMPDIR", "/var/tmp")
    assert default_socket_path() == Path("/var/tmp/speakd/speakd.sock")


# ---------------------------------------------------------------------------
# hush and cancel are different verbs, and nothing is dropped in silence
# ---------------------------------------------------------------------------


def test_help_distinguishes_hush_from_cancel(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """A user who picks the wrong one loses a queue they wanted.

    `hush` clears the queue and `cancel` does not, so the one-line help for
    each has to carry that difference rather than both saying "stop".
    """
    from speakd.cli import _build_parser

    # Wide enough that argparse puts each subcommand's help on one line.
    monkeypatch.setenv("COLUMNS", "200")
    lines = _build_parser().format_help().splitlines()
    hush = next(line for line in lines if line.strip().startswith("hush "))
    cancel = next(line for line in lines if line.strip().startswith("cancel "))
    assert "queue" in hush and "queue" in cancel
    assert "clear" in hush
    assert "continue" in cancel


def test_hush_reports_what_it_discarded(blocking, capsys) -> None:  # type: ignore[no-untyped-def]
    """A discarded utterance is surfaced, never swallowed."""
    address, _daemon, player = blocking
    assert main(["enqueue", "First.", "--source", "s", "--socket", str(address)]) == 0
    assert _until(player.started.is_set), "the worker never reached the player"
    assert main(["enqueue", "Second.", "--source", "s", "--socket", str(address)]) == 0
    assert main(["enqueue", "Third.", "--source", "s", "--socket", str(address)]) == 0
    capsys.readouterr()

    assert main(["hush", "--source", "s", "--socket", str(address)]) == 0
    assert "discarded 2" in capsys.readouterr().err


def test_cancel_does_not_report_a_discard_it_did_not_make(blocking, capsys) -> None:  # type: ignore[no-untyped-def]
    """`cancel` leaves the queue alone, so there is nothing to report."""
    address, _daemon, player = blocking
    assert main(["enqueue", "First.", "--source", "s", "--socket", str(address)]) == 0
    assert _until(player.started.is_set), "the worker never reached the player"
    assert main(["enqueue", "Second.", "--source", "s", "--socket", str(address)]) == 0
    capsys.readouterr()

    assert main(["cancel", "--source", "s", "--socket", str(address)]) == 0
    assert "discarded" not in capsys.readouterr().err


def test_enqueue_says_when_the_rules_refused_to_speak(running, capsys) -> None:  # type: ignore[no-untyped-def]
    """A background channel silently dropping speech is the failure to avoid."""
    address, _daemon, _player = running
    assert main(["role", "background", "--source", "s", "--socket", str(address)]) == 0
    capsys.readouterr()
    assert main(["enqueue", "Quiet one.", "--source", "s", "--socket", str(address)]) == 0
    assert "background channel does not interrupt" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# subscribe
# ---------------------------------------------------------------------------


def test_subscribe_streams_events_as_json_lines(served, capsys) -> None:  # type: ignore[no-untyped-def]
    server, daemon = served
    address = str(server.address)
    codes: list[int] = []

    def stream() -> None:
        codes.append(main(["subscribe", "--socket", address]))

    watcher = threading.Thread(target=stream, daemon=True)
    watcher.start()
    try:
        assert _until(lambda: daemon.bus.subscriber_count() > 0), "subscribe never registered"
        assert main(["enqueue", "One. Two.", "--source", "s", "--socket", address]) == 0
        assert daemon.wait_idle(timeout=5.0)
    finally:
        # Closing the server ends the stream, which is how the loop exits.
        server.stop()
        watcher.join(timeout=5.0)

    assert not watcher.is_alive()
    assert codes == [0]
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.strip()]
    assert "started" in [event["event"] for event in events]
    positions = [event for event in events if event["event"] == "position"]
    assert len(positions) == 2
    assert positions[0]["source_id"] == "s"
    assert positions[0]["data"]["text"] == "One."
    assert positions[0]["data"]["span_start"] == 0


# ---------------------------------------------------------------------------
# `python -m speakd`
# ---------------------------------------------------------------------------

_DRIVER = """
import sys
import time
from pathlib import Path

import speakd.__main__ as entry
from speakd.channels import ChannelTable
from speakd.daemon import Daemon, ProfileView
from speakd.events import EventBus
from speakd.player import RecordingPlayer
from speakd.protocol import Response
from speakd.synth.fake import FakeEngine

mode = sys.argv[1]
socket_path = Path(sys.argv[2])
marker = Path(sys.argv[3])


def prepare(pieces):
    return list(pieces), []


def profile_for(name):
    return ProfileView(voice="af_heart", speed=1.1, interrupt_on=("error",), prepare=prepare)


daemon = Daemon(
    FakeEngine(), RecordingPlayer(), profile_for, bus=EventBus(), channels=ChannelTable()
)

if mode == "refuse":

    def refuse():
        return Response(ok=False, error="a previous speech worker has not finished yet")

    daemon.start = refuse

if mode == "slow-server":

    class SlowServer(entry.SocketServer):
        def stop(self):
            marker.write_text("stopping")
            time.sleep(30)

    entry.SocketServer = SlowServer

raise SystemExit(entry.serve(daemon, socket_path))
"""


def _spawn(tmp_path: Path, mode: str) -> tuple["subprocess.Popen[str]", Path, Path]:
    driver = tmp_path / "driver.py"
    driver.write_text(textwrap.dedent(_DRIVER))
    socket_path = tmp_path / "run" / "speakd.sock"
    marker = tmp_path / "marker"
    process = subprocess.Popen(
        [sys.executable, str(driver), mode, str(socket_path), str(marker)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return process, socket_path, marker


def test_the_entry_point_serves_and_stops_on_a_signal(tmp_path: Path) -> None:
    process, socket_path, _marker = _spawn(tmp_path, "normal")
    try:
        assert _until(socket_path.exists, timeout=20.0), "the daemon never listened"
        process.send_signal(signal.SIGINT)
        assert process.wait(timeout=20.0) == 0
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate()
    assert not socket_path.exists(), "the socket file outlived the daemon"


def test_a_second_signal_does_not_wait_for_the_first_stop(tmp_path: Path) -> None:
    """stop() joins its worker for up to 5s; a user who hits Ctrl-C again means it.

    The server here takes 30s to shut down, so a run that waited for the
    graceful path could not finish inside this test's timeout.
    """
    process, socket_path, marker = _spawn(tmp_path, "slow-server")
    try:
        assert _until(socket_path.exists, timeout=20.0), "the daemon never listened"
        process.send_signal(signal.SIGINT)
        assert _until(marker.exists, timeout=20.0), "the graceful stop never began"
        assert socket_path.exists(), "the slow server was expected to still hold the socket"
        process.send_signal(signal.SIGINT)
        assert process.wait(timeout=10.0) == 128 + signal.SIGINT
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate()
    assert not socket_path.exists(), "the impatient exit left a stale socket behind"


def test_a_refused_start_is_reported_and_never_listens(tmp_path: Path) -> None:
    process, socket_path, _marker = _spawn(tmp_path, "refuse")
    try:
        code = process.wait(timeout=20.0)
        _out, err = process.communicate(timeout=20.0)
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate()
    assert code != 0
    assert "not finished yet" in err
    assert "Traceback" not in err
    assert not socket_path.exists(), "a daemon that refused to start still listened"


def test_the_client_and_the_entry_point_import_without_kokoro() -> None:
    """Neither module may pull the optional extra in at import time."""
    probe = (
        "import sys, speakd.cli, speakd.__main__\n"
        "assert 'kokoro' not in sys.modules, 'kokoro imported'\n"
        "assert 'torch' not in sys.modules, 'torch imported'\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, timeout=60
    )
    assert result.returncode == 0, result.stderr
