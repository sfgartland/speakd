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
from speakd.player import FakeSink, RecordingPlayer, StreamingPlayer
from speakd.protocol import Request, Verb
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
    """A served daemon on the player the shipping daemon actually runs.

    `StreamingPlayer` rather than `RecordingPlayer` so these tests exercise
    the transport verbs the daemon now answers; a fixture whose player cannot
    pause could only ever prove that pause is refused.
    """
    player = StreamingPlayer(FakeSink())
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
def running_recording(tmp_path: Path):  # type: ignore[no-untyped-def]
    """Like `running`, but counting segments rather than chunks.

    `FakeSink.blocks` counts 2048-frame writes, so "two segments were played"
    cannot be said against it without pinning the chunk size as well. Kept on
    `RecordingPlayer` rather than weakened.
    """
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


def test_enqueue_speaks_through_the_daemon(running_recording, capsys) -> None:  # type: ignore[no-untyped-def]
    address, daemon, player = running_recording
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


def test_hush_and_cancel_default_to_the_whole_daemon() -> None:
    """The parser's own default, because the default is the thing that changed.

    A live daemon cannot show this up: it would answer `ok` to a hush that
    silenced a channel called "cli" and left the room talking. `enqueue` keeps
    that default, where speaking as this shell is exactly right.
    """
    from speakd.cli import _build_parser

    parser = _build_parser()
    assert parser.parse_args(["hush"]).source == ""
    assert parser.parse_args(["cancel"]).source == ""
    assert parser.parse_args(["enqueue", "Hello."]).source == "cli"


def test_the_daemon_entry_point_builds_a_pausable_player() -> None:
    """A daemon whose player cannot pause makes the GUI's controls dead."""
    from speakd.__main__ import build_player
    from speakd.player import Pausable

    assert isinstance(build_player(sample_rate=24000, fake=True), Pausable)


def test_pause_works_end_to_end_through_the_socket(running) -> None:  # type: ignore[no-untyped-def]
    address, daemon, _player = running
    assert main(["pause", "--source", "s", "--socket", str(address)]) == 0
    assert main(["resume", "--source", "s", "--socket", str(address)]) == 0


def test_pause_on_a_daemon_that_cannot_pause_prints_why(running_recording, capsys) -> None:  # type: ignore[no-untyped-def]
    """A dead pause button that reports nothing is what this path removes.

    The daemon names the player it got, and the client has to carry that
    through: `RecordingPlayer` says "swap the player", where a bare non-zero
    exit says nothing at all.
    """
    address, _daemon, _player = running_recording
    assert main(["pause", "--source", "s", "--socket", str(address)]) != 0
    err = capsys.readouterr().err
    assert "RecordingPlayer" in err
    assert "pause" in err
    assert "Traceback" not in err


def test_pause_fails_the_same_way_as_hush_when_no_daemon_answers(tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    """A new verb that reports unreachability its own way is a new verb to learn."""
    missing = tmp_path / "absent.sock"
    hush_code = main(["hush", "--source", "s", "--socket", str(missing)])
    hush_err = capsys.readouterr().err
    for verb in ("pause", "resume"):
        code = main([verb, "--source", "s", "--socket", str(missing)])
        assert (code, capsys.readouterr().err) == (hush_code, hush_err)


def test_the_entry_point_runs_the_daemon_on_that_player(monkeypatch, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    """`build_player` is only worth having if `main` is what calls it.

    Nothing else pins the swap: a `build_player` no caller reaches would pass
    every other test here while the shipping daemon went on running the
    blocking player. The sink is built but never started, so no device opens.
    """
    import speakd.__main__ as entry
    import speakd.player
    from speakd.player import Pausable
    from speakd.synth import kokoro_engine

    class StubEngine:
        name = "stub"
        # Deliberately not 24000: a hardcoded rate would still sound right in
        # a test that used Kokoro's own.
        sample_rate = 12345

        def synthesize(self, text: str, voice: str, speed: float, lang: str = "en") -> np.ndarray:
            return np.zeros(0, dtype=np.float32)

    rates: list[int] = []

    class RecordingSink(FakeSink):
        """A `SoundDeviceSink` that records its rate and opens nothing."""

        def __init__(self, sample_rate: int, blocksize: int = 1024) -> None:
            super().__init__()
            rates.append(sample_rate)

    monkeypatch.setattr(kokoro_engine, "KokoroEngine", StubEngine)
    monkeypatch.setattr(speakd.player, "SoundDeviceSink", RecordingSink)
    built: list[Daemon] = []

    def capture(daemon: Daemon, socket_path: Path) -> int:
        built.append(daemon)
        return 0

    monkeypatch.setattr(entry, "serve", capture)
    assert entry.main(["--socket", str(tmp_path / "speakd.sock")]) == 0
    assert built, "the entry point never built a daemon"
    assert isinstance(built[0].player, Pausable), "the shipping daemon cannot pause"
    assert rates == [12345], "the sink did not take the engine's rate"


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


def test_label_names_the_channel(running) -> None:  # type: ignore[no-untyped-def]
    address, daemon, _player = running
    assert main(["label", "PhD session", "--source", "s", "--socket", str(address)]) == 0
    channel = daemon.channels.get("s")
    assert channel is not None and channel.label == "PhD session"


def test_a_label_reaches_status_where_a_gui_would_read_it(running, capsys) -> None:  # type: ignore[no-untyped-def]
    """The whole point of the verb: a listing of names, not of UUIDs."""
    address, _daemon, _player = running
    assert main(["label", "PhD session", "--source", "s", "--socket", str(address)]) == 0
    assert main(["status", "--socket", str(address)]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert any(c["source_id"] == "s" and c["label"] == "PhD session" for c in payload["channels"])


def test_a_blank_label_fails_the_same_way_as_a_bad_role(tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    """Checked in the client, for the reason `role` and `priority` are.

    The socket named here does not exist: were the check left to the daemon,
    this would print "no daemon" rather than what was wrong with the
    argument, and argparse would have exited the process instead of
    returning a code to whoever called `main`.
    """
    missing = tmp_path / "absent.sock"
    assert main(["label", "   ", "--source", "s", "--socket", str(missing)]) == 2
    err = capsys.readouterr().err
    assert "label" in err
    assert "no daemon" not in err


def test_status_prints_the_channels_as_json(running, capsys) -> None:  # type: ignore[no-untyped-def]
    address, _daemon, _player = running
    main(["priority", "3", "--source", "a", "--socket", str(address)])
    assert main(["status", "--socket", str(address)]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert any(c["source_id"] == "a" and c["priority"] == 3 for c in payload["channels"])


def test_mute_and_unmute_reach_the_daemon(running) -> None:  # type: ignore[no-untyped-def]
    address, daemon, _player = running
    assert main(["mute", "--socket", str(address)]) == 0
    assert daemon.muted is True
    assert main(["unmute", "--socket", str(address)]) == 0
    assert daemon.muted is False


def test_mute_without_a_source_means_the_whole_daemon(running) -> None:  # type: ignore[no-untyped-def]
    """`speakctl mute` means stop talking, not stop talking to this shell.

    Every other verb here defaults `--source` to `cli`, which for a mute
    would silence a channel nothing speaks on and leave the room as loud as
    it was -- and leave a phantom `cli` channel behind to show for it.
    """
    address, daemon, _player = running
    assert main(["mute", "--socket", str(address)]) == 0
    assert daemon.muted is True
    assert daemon.channels.get("cli") is None


def test_mute_can_name_one_channel(running) -> None:  # type: ignore[no-untyped-def]
    address, daemon, _player = running
    assert main(["mute", "--source", "s", "--socket", str(address)]) == 0
    channel = daemon.channels.get("s")
    assert channel is not None and channel.muted is True
    assert daemon.muted is False, "a channel mute silenced the whole daemon"


def test_status_prints_the_two_off_switches(running, capsys) -> None:  # type: ignore[no-untyped-def]
    """What a control surface reads to draw them."""
    address, _daemon, _player = running
    assert main(["status", "--socket", str(address)]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["muted"] is False
    assert payload["engine"] == {
        "loaded": True,
        "loading": False,
        "name": "fake",
        "piper": {"available": False, "voices": []},
    }


def test_disable_on_a_daemon_whose_engine_cannot_be_unloaded_names_it(running, capsys) -> None:  # type: ignore[no-untyped-def]
    """The same answer `pause` gives a player that cannot pause."""
    address, _daemon, _player = running
    assert main(["disable", "--socket", str(address)]) != 0
    err = capsys.readouterr().err
    assert "FakeEngine" in err
    assert "Traceback" not in err


def test_enable_and_disable_drive_a_lazy_engine(tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    """End to end over the socket, on the engine the shipping daemon now runs."""
    from speakd.synth.lazy import LazyEngine

    engine = LazyEngine(FakeEngine, sample_rate=24000)
    daemon = Daemon(engine, RecordingPlayer(), profile_for, bus=EventBus(), channels=ChannelTable())
    daemon.start()
    server = SocketServer(tmp_path / "speakd.sock", daemon.handle, daemon.bus)
    server.start()
    try:
        assert main(["enable", "--socket", str(server.address)]) == 0
        assert _until(lambda: engine.loaded), "enable never loaded the model"
        # An on switch that stays quiet for half a minute reads as one that
        # did nothing, so it says what it is doing.
        assert "loading" in capsys.readouterr().err
        assert main(["disable", "--socket", str(server.address)]) == 0
        assert not engine.loaded
    finally:
        server.stop()
        daemon.stop()


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


def test_notify_off_and_on_take_only_the_socket() -> None:
    """The off switch is global, so there is deliberately no --source."""
    from speakd.cli import _build_parser

    parser = _build_parser()
    args = parser.parse_args(["notify", "off"])
    assert not hasattr(args, "source")
    assert args.socket is not None


def test_notify_off_and_on_through_the_socket(running) -> None:  # type: ignore[no-untyped-def]
    address, daemon, _player = running

    class Stub:  # a real daemon in this suite has no connectors (conftest)
        def start(self) -> None:
            pass

        def stop(self) -> None:
            pass

    daemon.add_connector("notify", Stub())
    assert main(["notify", "off", "--socket", str(address)]) == 0
    assert daemon.notify_enabled is False
    status = daemon.handle(Request(verb=Verb.STATUS, source_id=""))
    assert status.data["notify"] == {"enabled": False}
    assert main(["notify", "on", "--socket", str(address)]) == 0
    assert daemon.notify_enabled is True


def test_notify_on_is_refused_when_no_reader_is_registered(running, capsys) -> None:  # type: ignore[no-untyped-def]
    address, _daemon, _player = running
    assert main(["notify", "on", "--socket", str(address)]) == 1
    assert "SPEAKD_NO_NOTIFY" in capsys.readouterr().err


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


def test_a_bare_hush_still_means_the_whole_daemon(blocking, capsys) -> None:  # type: ignore[no-untyped-def]
    """`--source` narrows hush now, so its default must not be this shell's channel.

    `speakctl hush` typed by hand means stop talking. A default of "cli" would
    send it to a channel nobody speaks on: exit code 0, nothing printed, and
    the room still talking.
    """
    address, _daemon, player = blocking
    assert main(["enqueue", "First.", "--source", "s", "--socket", str(address)]) == 0
    assert _until(player.started.is_set), "the worker never reached the player"
    assert main(["enqueue", "Second.", "--source", "s", "--socket", str(address)]) == 0
    capsys.readouterr()

    assert main(["hush", "--socket", str(address)]) == 0
    assert "discarded 1" in capsys.readouterr().err


def test_hush_can_name_one_channel(blocking, capsys) -> None:  # type: ignore[no-untyped-def]
    """`--source` reaches the daemon as the scope, leaving every other channel alone."""
    address, _daemon, player = blocking
    assert main(["enqueue", "First.", "--source", "s", "--socket", str(address)]) == 0
    assert _until(player.started.is_set), "the worker never reached the player"
    assert main(["enqueue", "Second.", "--source", "s", "--socket", str(address)]) == 0
    assert main(["enqueue", "Third.", "--source", "t", "--socket", str(address)]) == 0
    capsys.readouterr()

    assert main(["hush", "--source", "s", "--socket", str(address)]) == 0
    assert "discarded 1" in capsys.readouterr().err


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


def note(step):
    with marker.open("a") as handle:
        print(step, file=handle)


daemon = Daemon(
    FakeEngine(), RecordingPlayer(), profile_for, bus=EventBus(), channels=ChannelTable()
)

if mode == "refuse":

    def refuse():
        return Response(ok=False, error="a previous speech worker has not finished yet")

    daemon.start = refuse

if mode.startswith("close"):
    # A player that owns a device handle, and says when it gives it back.
    from speakd.player import FakeSink, StreamingPlayer

    class NotingSink(FakeSink):
        def stop(self):
            note("stop")
            super().stop()

        def write(self, frames):
            if mode == "close-wedged":
                # A real device's write blocks for the whole chunk, and an
                # abort that does not land leaves the worker in it. Nothing
                # here ever releases it, which is what makes the join time out.
                note("write")
                time.sleep(60)
                return
            super().write(frames)

        def close(self):
            note("close")
            if mode == "close-raises":
                raise RuntimeError("PortAudioError: device busy")
            super().close()

    daemon.player = StreamingPlayer(NotingSink())

if mode == "order":
    # Records the shutdown order Ctrl-C actually takes.
    class NotingPlayer(RecordingPlayer):
        def stop(self):
            note("silence")

    class NotingServer(entry.SocketServer):
        def stop(self):
            note("server")
            super().stop()

    daemon.player = NotingPlayer()
    entry.SocketServer = NotingServer

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


def test_ctrl_c_stops_the_audio_before_it_stops_the_server(tmp_path: Path) -> None:
    """Ctrl-C must go quiet at once, without giving up the unwedging.

    server.stop()'s shutdown() is precisely what frees a speech worker wedged
    writing to a subscriber, so it has to keep coming before daemon.stop()'s
    5s join. That leaves the audio sounding for the length of the server's
    shutdown unless something silences the daemon first.
    """
    process, socket_path, marker = _spawn(tmp_path, "order")
    try:
        assert _until(socket_path.exists, timeout=20.0), "the daemon never listened"
        process.send_signal(signal.SIGINT)
        assert process.wait(timeout=20.0) == 0
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate()
    steps = marker.read_text().split()
    # Silence, then the server, then the daemon's own teardown (which
    # silences again on its way down).
    assert steps == ["silence", "server", "silence"], steps


def test_the_entry_point_releases_the_audio_device_on_the_way_out(tmp_path: Path) -> None:
    """The daemon now owns a device handle, and nothing else will give it back.

    `SoundDevicePlayer` held no resources, so shutdown had nothing to release.
    A `StreamingPlayer`'s sink keeps the device claimed for the daemon's whole
    life -- on a laptop, for as long as speakd runs -- which other
    applications notice.
    """
    process, socket_path, marker = _spawn(tmp_path, "close")
    try:
        assert _until(socket_path.exists, timeout=20.0), "the daemon never listened"
        process.send_signal(signal.SIGINT)
        assert process.wait(timeout=20.0) == 0
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate()
    steps = marker.read_text().split()
    assert "close" in steps, "the sink was never closed: the device stays claimed"
    # Last, and after every stop. close() is terminal by contract, so the
    # speech worker has to be gone before it -- a worker still inside play()
    # would meet a closed sink on its next chunk.
    assert steps[-1] == "close", steps
    assert set(steps[:-1]) == {"stop"}, steps


def test_a_sink_that_will_not_close_does_not_break_the_shutdown(tmp_path: Path) -> None:
    """A clean exit must not become a traceback because the device refused."""
    process, socket_path, marker = _spawn(tmp_path, "close-raises")
    try:
        assert _until(socket_path.exists, timeout=20.0), "the daemon never listened"
        process.send_signal(signal.SIGINT)
        code = process.wait(timeout=20.0)
        _out, err = process.communicate(timeout=20.0)
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate()
    assert code == 0, "a sink that would not close took the shutdown down with it"
    assert "close" in marker.read_text().split()
    # Recorded, not swallowed: a device that will not release is exactly why
    # the next start finds it busy.
    assert "device busy" in err
    assert "Traceback" not in err


def test_a_worker_that_outlived_the_join_keeps_the_device_unclosed(tmp_path: Path) -> None:
    """Closing under a live worker is a segfault, not an exception.

    `Daemon.stop()` joins for 5s and returns either way -- `daemon.py` keeps
    `_worker` pointing at a worker that outlived the join, deliberately. So
    "the worker has left" cannot be assumed at the close; it has to be asked.
    A worker still inside the sink's write meets `Pa_CloseStream` freeing the
    ALSA handle underneath it, which is undefined behaviour no `except` can
    catch and no fake can reproduce -- only the decision to skip the close is
    testable, so that is what this pins.
    """
    process, socket_path, marker = _spawn(tmp_path, "close-wedged")
    try:
        assert _until(socket_path.exists, timeout=20.0), "the daemon never listened"
        say = ["enqueue", "One two three.", "--source", "s", "--socket", str(socket_path)]
        assert main(say) == 0

        # The marker does not exist until the first note lands.
        def reached_the_sink() -> bool:
            return marker.exists() and "write" in marker.read_text()

        assert _until(reached_the_sink, timeout=20.0), "the worker never reached the sink"
        process.send_signal(signal.SIGINT)
        # The join is 5s; the wedged writer outlives it by design.
        code = process.wait(timeout=40.0)
        _out, err = process.communicate(timeout=20.0)
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate()
    assert code == 0, "a wedged worker must not take the shutdown down with it"
    steps = marker.read_text().split()
    assert "write" in steps
    assert "close" not in steps, (
        "the device was closed under a worker still inside it: that is the segfault"
    )
    # Never silently: the handle outliving the process by microseconds is
    # fine, but it has to be said.
    assert "device" in err
    assert "Traceback" not in err


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


def test_a_bad_role_and_a_bad_priority_fail_the_same_way(tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    """One shape for both, or `main()`'s callers cannot handle either.

    `role` returned 2 from its own check; `priority` let argparse's `type=int`
    raise SystemExit. A shell sees 2 either way, but a caller of `main()` --
    the tests here included -- sees a return value in one case and an
    exception in the other, from two neighbouring subcommands.
    """
    missing = tmp_path / "absent.sock"
    role_code = main(["role", "loud", "--source", "s", "--socket", str(missing)])
    role_err = capsys.readouterr().err
    priority_code = main(["priority", "loud", "--source", "s", "--socket", str(missing)])
    priority_err = capsys.readouterr().err
    assert role_code == priority_code == 2
    assert "loud" in role_err
    assert "loud" in priority_err
    # Neither needs a daemon to name what was wrong with the argument.
    assert "no daemon" not in priority_err
