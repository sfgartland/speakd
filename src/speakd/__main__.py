"""Run the daemon: `python -m speakd`."""

from __future__ import annotations

import argparse
import os
import signal
import sys
import threading
from collections.abc import Callable, Sequence
from pathlib import Path
from types import FrameType

from speakd.channels import ChannelTable
from speakd.daemon import Daemon, ProfileView
from speakd.events import EventBus
from speakd.model import Piece
from speakd.paths import default_profiles_path, default_socket_path
from speakd.player import Closeable, Player
from speakd.plugins.builtin import register_builtins
from speakd.plugins.host import PluginHost
from speakd.plugins.registry import ServiceRegistry
from speakd.profiles import Profile, load_profiles, resolve_chain
from speakd.supervise import Supervisor
from speakd.transforms.chain import apply_chain
from speakd.transport import SocketServer
from speakd.transport_http import HttpServer


def _view_of(profile: Profile, host: PluginHost) -> ProfileView:
    """Bind one profile to a resolved transform chain.

    The chain is resolved here, once, rather than inside `prepare`: prepare
    runs on the path to first audio, and re-resolving names against the host
    for every utterance would put work there that cannot change between them.

    Names nobody provides are reported on every utterance rather than dropped
    at startup. A profile asking for a transform that is not installed should
    be visible to whoever is listening to that profile -- the daemon has no
    other way to say so, and a chain that silently does less than it was asked
    is an afternoon spent wondering why.
    """
    chain, missing = resolve_chain(profile, host)
    absent = [
        f"profile {profile.name!r} wants {name!r} -- no plugin provides it" for name in missing
    ]

    def prepare(pieces: Sequence[Piece]) -> tuple[list[Piece], list[str]]:
        result = apply_chain(pieces, chain)
        return list(result.pieces), absent + result.errors

    return ProfileView(
        voice=profile.voice,
        speed=profile.speed,
        interrupt_on=profile.interrupt_on,
        prepare=prepare,
    )


def build_profiles() -> Callable[[str], ProfileView]:
    """Every profile the daemon will speak with, resolved once at startup.

    Until this existed the daemon handed every job a `prepare` that returned
    its input untouched, so everything it spoke was raw markdown -- asterisks,
    backticks and table pipes read aloud. `cli.py` wired the host for
    `speakctl say`, which is exactly why that path sounded correct and hid it.

    A malformed profiles file is reported and then ignored. Refusing to start
    would leave someone with a typo in a TOML file and no speech at all, where
    the built-in default profile is a perfectly good thing to fall back to.
    """
    host = PluginHost(ServiceRegistry())
    register_builtins(host)
    path = default_profiles_path()
    try:
        profiles = load_profiles(path)
    except ValueError as exc:
        sys.stderr.write(f"speakd: ignoring {path}: {exc}\n")
        profiles = load_profiles(Path(os.devnull))
    views = {name: _view_of(profile, host) for name, profile in profiles.items()}
    default = views["default"]

    def profile_for(name: str) -> ProfileView:
        # An unknown name falls back rather than failing: the profile is
        # chosen by whoever sent the utterance, and a typo there should cost
        # them the profile's transforms, not the sentence.
        return views.get(name, default)

    return profile_for


def _claude_code_defaults(source_id: str) -> dict[str, object]:
    """How a Claude Code session's channel starts: able to brief, and muted.

    Only the session's main channel -- `claude-code:<id>`, one colon. Several
    sessions run at once and hearing all of them is noise, so each is silent
    until chosen; and the plugin ships the MCP server, so each can brief.
    """
    if source_id.startswith("claude-code:") and source_id.count(":") == 1:
        return {"briefs": True, "muted": True}
    return {}


def build_channels() -> ChannelTable:
    """The channel table, with Claude Code sessions starting brief and muted.

    A session is silent until it is chosen -- unmuted in the window's channel
    list or with `speakctl unmute --source` -- and then speaks the briefings
    its agent chooses to give, not every response, until switched to full.
    Everything else, the paste box and notifications included, starts audible
    as before. Held for the daemon's lifetime: after a restart every session
    starts this way again.
    """
    return ChannelTable(defaults=_claude_code_defaults)


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


# One per client connector: the environment variable that suppresses it, and
# the module to run. Data rather than a branch each, because the next client
# should be a line here and nothing else.
CONNECTORS: tuple[tuple[str, str], ...] = (
    ("SPEAKD_NO_FOLLOWER", "speakd.clients.claude_code.follow"),
    ("SPEAKD_NO_NOTIFY", "speakd.clients.notifications.follow"),
)


def _start_children() -> list[Supervisor]:
    """Start the client connectors under supervision.

    Called after the socket exists, never before: a child's first act is to
    connect to it, and one that starts into a closed socket spends its backoff
    on an absence we created.

    Each is suppressible on its own. The only callers of those variables are
    the test suite -- which must never spawn a child that outlives its
    daemon's socket and starts tailing the developer's real transcripts or
    reading their real notifications aloud -- and someone debugging one
    connector by hand.
    """
    started: list[Supervisor] = []
    for variable, module in CONNECTORS:
        if os.environ.get(variable):
            continue
        child = Supervisor([sys.executable, "-m", module])
        child.start()
        started.append(child)
    return started


# The HTTP transport's default port. `SPEAKD_HTTP_PORT=0` turns it off.
DEFAULT_HTTP_PORT = 8642


def http_port() -> int | None:
    """The port to serve HTTP on, or None when it is off or misconfigured."""
    raw = os.environ.get("SPEAKD_HTTP_PORT")
    if raw is None:
        return DEFAULT_HTTP_PORT
    try:
        port = int(raw)
    except ValueError:
        sys.stderr.write(f"speakd: SPEAKD_HTTP_PORT={raw!r} is not a port; HTTP is off\n")
        return None
    if not 0 < port < 65536:
        return None
    return port


def start_http(daemon: Daemon) -> HttpServer | None:
    """Serve the daemon over loopback HTTP too, or say why not and carry on.

    Never fatal: the socket is how everything else reaches the daemon, and a
    port some other program holds must not cost the user their agents' voice.
    """
    port = http_port()
    if port is None:
        return None
    from speakd import http_token

    try:
        token = http_token.ensure()
        server = HttpServer(port, daemon.handle, daemon.bus, token)
    except OSError as exc:
        sys.stderr.write(f"speakd: no HTTP transport on 127.0.0.1:{port}: {exc}\n")
        return None
    server.start()
    return server


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
    http = start_http(daemon)
    children = _start_children()

    stop.wait()
    # Before the daemon goes quiet, so a client cannot enqueue into a daemon
    # that is shutting down and have it discarded as unspoken.
    for child in children:
        child.stop()
    # Silenced first, so Ctrl-C goes quiet at once. The two calls below keep
    # their order: server.stop()'s shutdown() is what frees a speech worker
    # wedged writing to a subscriber that stopped reading, and putting
    # daemon.stop() first would make every Ctrl-C wait out a 5s join before
    # anything could unwedge it.
    daemon.silence()
    server.stop()
    if http is not None:
        http.stop()
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

    from speakd import state
    from speakd.synth.kokoro_engine import KokoroEngine
    from speakd.synth.lazy import LazyEngine

    # The class attributes, not an instance: `build_player` below needs the
    # rate to open its sink, and asking an instance for it would mean loading
    # the model this whole path exists to be able to not load.
    engine = LazyEngine(KokoroEngine, name=KokoroEngine.name, sample_rate=KokoroEngine.sample_rate)

    def load() -> None:
        try:
            engine.load()
        except ModuleNotFoundError:
            # Constructing KokoroEngine is what imports kokoro, and that no
            # longer happens on the main thread, so without this the missing
            # extra would arrive as a traceback on a thread nobody is
            # watching. The daemon stays up and says nothing: `status` shows
            # the engine unloaded, and `speakctl enable` retries once the
            # extra is installed.
            sys.stderr.write("speakd: the kokoro extra is not installed (uv sync --extra kokoro)\n")

    if not state.load().disabled:
        # On a thread, so the socket appears at once rather than thirty
        # seconds later. The README already tells users that the socket
        # appearing is the readiness signal; this is the first release in
        # which that is true.
        threading.Thread(target=load, name="speakd-engine-load", daemon=True).start()

    daemon = Daemon(
        engine,
        # The engine's own rate, not a literal: a sink opened at the wrong
        # rate plays the right samples at the wrong speed, which sounds like
        # a broken voice rather than like a misconfiguration.
        build_player(sample_rate=engine.sample_rate),
        build_profiles(),
        bus=EventBus(),
        channels=build_channels(),
    )
    return serve(daemon, args.socket)


if __name__ == "__main__":
    raise SystemExit(main())
