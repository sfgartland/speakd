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
from speakd.transport import SocketServer


def _profile_for(name: str) -> ProfileView:
    # Until the plugin host is wired in, every profile speaks plainly.
    def prepare(pieces: Sequence[Piece]) -> tuple[list[Piece], list[str]]:
        return list(pieces), []

    return ProfileView(voice="af_heart", speed=1.1, interrupt_on=("error",), prepare=prepare)


def _unlink_quietly(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


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
    server.stop()
    daemon.stop()
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

    from speakd.player import SoundDevicePlayer
    from speakd.synth.kokoro_engine import KokoroEngine

    try:
        engine = KokoroEngine()
    except ModuleNotFoundError:
        # Constructing the engine is what imports kokoro; without the extra
        # this would otherwise arrive as an uncaught traceback.
        sys.stderr.write("speakd: the kokoro extra is not installed (uv sync --extra kokoro)\n")
        return 2

    daemon = Daemon(
        engine, SoundDevicePlayer(), _profile_for, bus=EventBus(), channels=ChannelTable()
    )
    return serve(daemon, args.socket)


if __name__ == "__main__":
    raise SystemExit(main())
