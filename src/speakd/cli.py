"""speakctl — the lowest common denominator client.

Any agent that can run a shell command can drive speech through this.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING

from speakd.model import Piece, Role, Span
from speakd.pipeline import speak
from speakd.protocol import ProtocolError, Request, Response, Verb
from speakd.segmenter import DEFAULT_MAX_CHARS

if TYPE_CHECKING:  # pragma: no cover - typing only
    from speakd.transport import SocketClient


def default_socket_path() -> Path:
    """Where the daemon listens, following XDG with a sensible fallback."""
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    base = Path(runtime) if runtime else Path(os.environ.get("TMPDIR", "/tmp"))
    return base / "speakd" / "speakd.sock"


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="speakctl")
    sub = parser.add_subparsers(dest="command")

    say = sub.add_parser("say", help="speak text")
    say.add_argument("text", nargs="?", help="text to speak; reads stdin when omitted")
    say.add_argument("--voice", default="af_heart")
    say.add_argument("--speed", type=float, default=1.1)
    say.add_argument("--max-chars", type=int, default=DEFAULT_MAX_CHARS)
    say.add_argument(
        "--dry-run",
        action="store_true",
        help="use the fake engine and print the timeline instead of playing",
    )

    # Every daemon-facing verb names a channel and a socket the same way.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--source", default="cli", help="the channel this speaks on")
    common.add_argument(
        "--socket",
        type=Path,
        default=default_socket_path(),
        help="where the daemon listens (default: %(default)s)",
    )

    enqueue = sub.add_parser("enqueue", parents=[common], help="speak text through the daemon")
    enqueue.add_argument("text")
    enqueue.add_argument("--kind", default="response", help="what sort of utterance this is")
    enqueue.add_argument("--profile", help="override the channel's profile for this utterance")

    # These two are not synonyms, and the difference costs a queue.
    sub.add_parser(
        "hush",
        parents=[common],
        help="stop talking: cancel what is being said and clear the queue",
    )
    sub.add_parser(
        "cancel",
        parents=[common],
        help="skip this one: cancel what is being said, the queue continues",
    )

    role = sub.add_parser("role", parents=[common], help="set a channel's role")
    # Validated in `_role` rather than by argparse `choices`, which exits the
    # process instead of returning a code to whoever called `main`.
    role.add_argument("role", metavar="{foreground,background}", help="foreground or background")

    priority = sub.add_parser("priority", parents=[common], help="set a channel's priority")
    priority.add_argument("priority", type=int, help="higher is heard first")

    sub.add_parser("subscribe", parents=[common], help="stream events as JSON lines")
    sub.add_parser("status", parents=[common], help="print the daemon's channels as JSON")
    return parser


def _validate_say_args(args: argparse.Namespace) -> str | None:
    """Check `say` arguments that would otherwise reach broken code paths.

    Pure and side-effect free — it touches nothing but `args`, never the
    segmenter or an engine. `speakd.segmenter._split_words` loops forever
    when max_chars < 1 (the computed end index never advances past start for
    a non-space character), and `FakeEngine.synthesize` divides by `speed`,
    so zero or negative values misbehave there too. Both are rejected here,
    at the CLI boundary, before either code path is ever reached.

    Returns an error message, or `None` if `args` is valid.
    """
    if args.max_chars < 1:
        return f"--max-chars must be at least 1, got {args.max_chars}"
    if args.speed <= 0:
        return f"--speed must be greater than 0, got {args.speed}"
    return None


def _say(args: argparse.Namespace) -> int:
    error = _validate_say_args(args)
    if error is not None:
        print(f"speakctl: {error}", file=sys.stderr)
        return 2

    text = args.text if args.text is not None else sys.stdin.read()

    if args.dry_run:
        from speakd.player import RecordingPlayer
        from speakd.synth.fake import FakeEngine

        engine: object = FakeEngine()
        player: object = RecordingPlayer()
    else:
        from speakd.player import SoundDevicePlayer
        from speakd.synth.kokoro_engine import KokoroEngine

        try:
            engine = KokoroEngine()
        except ModuleNotFoundError:
            # Constructing the engine is what imports kokoro, so without the
            # extra this arrived as an uncaught traceback.
            print(
                "speakctl: the kokoro extra is not installed (uv sync --extra kokoro)",
                file=sys.stderr,
            )
            return 2
        player = SoundDevicePlayer()

    pieces = [Piece(span=Span(0, len(text)), spoken=text)]
    result = speak(
        pieces,
        engine,  # type: ignore[arg-type]
        player,  # type: ignore[arg-type]
        voice=args.voice,
        speed=args.speed,
        max_chars=args.max_chars,
    )

    if args.dry_run:
        print(
            json.dumps(
                {
                    "duration": result.timeline.duration,
                    "errors": result.errors,
                    "segments": [
                        {
                            "text": s.text,
                            "span": [s.span.start, s.span.end],
                            "audio_offset": s.audio_offset,
                            "duration": s.duration,
                        }
                        for s in result.timeline.segments
                    ],
                },
                indent=2,
            )
        )
    for message in result.errors:
        print(f"speakctl: {message}", file=sys.stderr)
    # 0 spoke cleanly, 1 spoke but something failed, 2 never got started.
    # An agent driving this needs to tell a partial failure from a clean run,
    # and a silently dropped segment is exactly what it must not miss.
    return 1 if result.errors else 0


# Exit codes across every subcommand: 0 it worked, 1 the daemon was reached
# and said no, 2 it never got started (bad arguments, or no daemon there).
_UNREACHABLE = 2
_REFUSED = 1


def _connect(socket_path: Path) -> SocketClient | None:
    """Open a connection, or print one clear line and return None.

    A ConnectionRefusedError traceback tells an agent nothing it can act on,
    and agents are the primary users of this tool.
    """
    from speakd.transport import connect

    try:
        return connect(socket_path)
    except OSError:
        # FileNotFoundError (no socket file), ConnectionRefusedError (a stale
        # one left by a daemon that died) and the rest all mean the same
        # thing to whoever ran this.
        print(
            f"speakctl: no daemon at {socket_path} (start it with: speakd)",
            file=sys.stderr,
        )
        return None


def _close_quietly(client: SocketClient) -> None:
    try:
        client.close()
    except OSError:
        # Closing a socket the peer already tore down must not become the
        # error the user sees instead of the real one.
        pass


def _call(socket_path: Path, request: Request) -> Response | None:
    """Send one request, or print one clear line and return None."""
    client = _connect(socket_path)
    if client is None:
        return None
    try:
        return client.send(request)
    except (OSError, ProtocolError) as exc:
        # The daemon was there and then was not: a different fact from never
        # having been there, and worth saying so.
        print(f"speakctl: lost the daemon at {socket_path}: {exc}", file=sys.stderr)
        return None
    finally:
        _close_quietly(client)


def _refused(response: Response) -> int:
    print(f"speakctl: {response.error}", file=sys.stderr)
    return _REFUSED


def _enqueue(args: argparse.Namespace) -> int:
    payload: dict[str, object] = {"text": args.text, "kind": args.kind}
    if args.profile is not None:
        payload["profile"] = args.profile
    response = _call(
        args.socket, Request(verb=Verb.ENQUEUE, source_id=args.source, payload=payload)
    )
    if response is None:
        return _UNREACHABLE
    if not response.ok:
        return _refused(response)
    if response.data.get("spoken") is False:
        # The arbitration rules declined it. Accepted, so not an error — but
        # never silent: speech that is not going to happen has to be visible
        # to whoever asked for it.
        reason = response.data.get("reason", "no reason given")
        print(f"speakctl: not spoken ({reason})", file=sys.stderr)
    return 0


def _silence(args: argparse.Namespace, verb: Verb) -> int:
    response = _call(args.socket, Request(verb=verb, source_id=args.source))
    if response is None:
        return _UNREACHABLE
    if not response.ok:
        return _refused(response)
    discarded = response.data.get("discarded", 0)
    if isinstance(discarded, int) and not isinstance(discarded, bool) and discarded > 0:
        # `hush` empties the queue. What it threw away is reported, because a
        # queue that vanishes without a word is the failure this tool exists
        # to avoid. `cancel` discards nothing, so it says nothing.
        plural = "" if discarded == 1 else "s"
        print(f"speakctl: discarded {discarded} queued utterance{plural}", file=sys.stderr)
    return 0


def _role(args: argparse.Namespace) -> int:
    try:
        role = Role(args.role)
    except ValueError:
        print(
            f"speakctl: unknown role {args.role!r} (expected foreground or background)",
            file=sys.stderr,
        )
        return _UNREACHABLE
    response = _call(
        args.socket,
        Request(verb=Verb.SET_ROLE, source_id=args.source, payload={"role": role.value}),
    )
    if response is None:
        return _UNREACHABLE
    return 0 if response.ok else _refused(response)


def _priority(args: argparse.Namespace) -> int:
    response = _call(
        args.socket,
        Request(
            verb=Verb.SET_PRIORITY,
            source_id=args.source,
            payload={"priority": args.priority},
        ),
    )
    if response is None:
        return _UNREACHABLE
    return 0 if response.ok else _refused(response)


def _status(args: argparse.Namespace) -> int:
    response = _call(args.socket, Request(verb=Verb.STATUS, source_id=args.source))
    if response is None:
        return _UNREACHABLE
    if not response.ok:
        return _refused(response)
    print(json.dumps(response.data, indent=2))
    return 0


def _subscribe(args: argparse.Namespace) -> int:
    client = _connect(args.socket)
    if client is None:
        return _UNREACHABLE
    try:
        for event in client.subscribe():
            # Flushed per line: a consumer piping this is watching speech as
            # it happens, and a buffered stream is not a stream.
            print(
                json.dumps({"event": event.kind, "source_id": event.source_id, "data": event.data}),
                flush=True,
            )
    except KeyboardInterrupt:
        return 0
    except (OSError, ProtocolError) as exc:
        print(f"speakctl: lost the daemon at {args.socket}: {exc}", file=sys.stderr)
        return _REFUSED
    finally:
        _close_quietly(client)
    # The stream ended because the daemon closed it, which is an ordinary way
    # for a subscription to finish.
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.command == "say":
        return _say(args)
    if args.command == "enqueue":
        return _enqueue(args)
    if args.command == "hush":
        return _silence(args, Verb.HUSH)
    if args.command == "cancel":
        return _silence(args, Verb.CANCEL)
    if args.command == "role":
        return _role(args)
    if args.command == "priority":
        return _priority(args)
    if args.command == "status":
        return _status(args)
    if args.command == "subscribe":
        return _subscribe(args)
    parser.print_usage(sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
