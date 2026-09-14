"""Run the daemon: `python -m speakd`."""

from __future__ import annotations

import argparse
import os
import signal
import sys
import threading
from collections.abc import Sequence
from pathlib import Path
from types import FrameType

from speakd.channels import ChannelTable
from speakd.cli import default_socket_path
from speakd.daemon import Daemon, ProfileView
from speakd.events import EventBus
from speakd.model import Piece
from speakd.player import Closeable, Player
from speakd.transport import SocketServer


def _profile_for(name: str) -> ProfileView:
    # Until the plugin host is wired in, every profile speaks plainly.
    def prepare(pieces: Sequence[Piece]) -> tuple[list[Piece], list[str]]:
        return list(pieces), []

    return ProfileView(voice="af_heart", speed=1.1, interrupt_on=("error",), prepare=prepare)


def build_player(*, sample_rate: int, fake: bool = False) -> Player:
    """The player the daemon runs with.

    `StreamingPlayer` rather than `SoundDevicePlayer` because a hush that
    waits for the current sentence to end is not a hush. `fake=True` is for
    tests, which must never open an audio device.

    Imported inside the function, like every other optional-dependency use
    here: `SoundDeviceSink` opens no device until `start()`, but the module
    it needs is only present with the kokoro extra.
    """
    if fake:
        from speakd.player import FakeSink, StreamingPlayer

        return StreamingPlayer(FakeSink())
    from speakd.player import SoundDeviceSink, StreamingPlayer

    return StreamingPlayer(SoundDeviceSink(sample_rate))


def _unlink_quietly(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


def _close_player(player: Player) -> None:
    """Give the audio device back, reporting rather than raising if it refuses.

    Guarded like every other player call in this project: a raise here would
    turn a clean shutdown into a traceback for something that is already over.
    Reported on stderr rather than on the bus, because by this point the
    server is stopped and there is no subscriber left to hear it -- and never
    swallowed, since a device that would not release is exactly why the next
    start finds it busy.

    A player that holds nothing is not asked to release it; `isinstance` is
    the probe, as with `Pausable`.
    """
    if not isinstance(player, Closeable):
        return
    try:
        player.close()
    except Exception as exc:
        sys.stderr.write(f"speakd: could not release the audio device: {exc!r}\n")


def serve(daemon: Daemon, socket_path: Path) -> int:
    """Serve `daemon` on `socket_path` until a signal says to stop."""
    stop = threading.Event()
    server = SocketServer(socket_path, daemon.handle, daemon.bus)

    def on_signal(signum: int, _frame: FrameType | None) -> None:
        if stop.is_set():
            # A graceful stop is already under way, and it can take up to one
            # utterance: Daemon.stop() joins its speech worker for 5s, and the
            # worker cannot leave mid-`play()`. Someone who signals again has
            # waited long enough, so leave at once rather than finish the
            # sentence. The socket file goes first — a listener that is about
            # to stop existing must not be advertised, and `speakctl` should
            # get its "no daemon" line rather than a connection to nobody.
            _unlink_quietly(server.address)
            sys.stderr.write("speakd: leaving now\n")
            sys.stderr.flush()
            # _exit, not sys.exit: the point is not to run anything else,
            # including the shutdown this is interrupting.
            os._exit(128 + signum)
        sys.stderr.write("speakd: stopping; signal again to leave without finishing\n")
        sys.stderr.flush()
        stop.set()

    # Installed before anything starts. A signal arriving mid-startup then
    # sets the flag instead of raising KeyboardInterrupt out of a half-built
    # daemon, and the socket file's existence is a reliable sign, to whoever
    # is watching, that the handlers are in place.
    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGTERM, on_signal)

    started = daemon.start()
    if not started.ok:
        # Refused, not started: carrying on would leave a socket accepting
        # speech that nothing is going to say.
        sys.stderr.write(f"speakd: {started.error}\n")
        return 1

    server.start()
    stop.wait()
    # Silenced first, so Ctrl-C goes quiet at once. The two calls below keep
    # their order: server.stop()'s shutdown() is what frees a speech worker
    # wedged writing to a subscriber that stopped reading, and putting
    # daemon.stop() first would make every Ctrl-C wait out a 5s join before
    # anything could unwedge it.
    daemon.silence()
    server.stop()
    # Last, and never above `daemon.stop()`: `close()` is terminal by contract
    # -- the sink refuses every later write and start -- so the speech worker
    # has to be gone before it runs. And asked, not assumed: `stop()` joins
    # for 5s and returns either way, so a worker can outlive it and still be
    # inside the sink's write. Freeing the device under one is a segfault in
    # the sound library rather than an exception -- it was, on real hardware,
    # in libasound -- and no in-process counter can guard it, because the
    # thread that faults is not one this process owns. The handle then
    # outlives the daemon by the microseconds until the process exits, which
    # is what happened before any of this existed and never crashed.
    # Nothing may use the player after this line.
    if daemon.stop():
        _close_player(daemon.player)
    else:
        sys.stderr.write(
            "speakd: the speech worker did not stop in time; "
            "leaving the audio device for the OS to reclaim\n"
        )
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="speakd", description=__doc__)
    parser.add_argument(
        "--socket",
        type=Path,
        default=default_socket_path(),
        help="where to listen (default: %(default)s)",
    )
    args = parser.parse_args(argv)

    from speakd.synth.kokoro_engine import KokoroEngine

    try:
        engine = KokoroEngine()
    except ModuleNotFoundError:
        # Constructing the engine is what imports kokoro; without the extra
        # this would otherwise arrive as an uncaught traceback.
        sys.stderr.write("speakd: the kokoro extra is not installed (uv sync --extra kokoro)\n")
        return 2

    daemon = Daemon(
        engine,
        # The engine's own rate, not a literal: a sink opened at the wrong
        # rate plays the right samples at the wrong speed, which sounds like
        # a broken voice rather than like a misconfiguration.
        build_player(sample_rate=engine.sample_rate),
        _profile_for,
        bus=EventBus(),
        channels=ChannelTable(),
    )
    return serve(daemon, args.socket)


if __name__ == "__main__":
    raise SystemExit(main())
