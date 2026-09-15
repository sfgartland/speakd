"""speakctl — the lowest common denominator client.

Any agent that can run a shell command can drive speech through this.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING

from speakd.model import Piece, Role, Span
from speakd.paths import default_profiles_path, default_socket_path
from speakd.pipeline import speak
from speakd.plugins.builtin import register_builtins
from speakd.plugins.host import PluginHost
from speakd.plugins.registry import ServiceRegistry
from speakd.profiles import load_profiles, resolve_chain
from speakd.protocol import ProtocolError, Request, Response, Verb
from speakd.segmenter import DEFAULT_MAX_CHARS
from speakd.transforms.chain import apply_chain

if TYPE_CHECKING:  # pragma: no cover - typing only
    from speakd.transport import SocketClient

# `default_socket_path` moved to `speakd.paths` so the Claude Code hook could
# build a socket path without importing the synthesis pipeline. It is re-exported
# because `speakctl`, `__main__` and the tests all learned to ask `cli` for it.
__all__ = ["default_socket_path", "main"]


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="speakctl")
    sub = parser.add_subparsers(dest="command")

    say = sub.add_parser("say", help="speak text")
    say.add_argument("text", nargs="?", help="text to speak; reads stdin when omitted")
    # These three default to None rather than a concrete value so `_say` can
    # tell "the user passed nothing" from "the user passed the default" --
    # the former defers to the profile, the latter must win outright.
    say.add_argument("--voice", default=None, help="voice; overrides the profile if given")
    say.add_argument(
        "--speed", type=float, default=None, help="speech rate; overrides the profile if given"
    )
    say.add_argument(
        "--max-chars",
        type=int,
        default=None,
        help=f"max characters per synthesised unit (default: {DEFAULT_MAX_CHARS})",
    )
    say.add_argument(
        "--profile",
        default="default",
        help="name of the transform profile to apply (default: %(default)s)",
    )
    say.add_argument(
        "--profiles-file",
        default=None,
        help=(
            "path to profiles.toml (default: $XDG_CONFIG_HOME/speakd/profiles.toml, "
            "falling back to ~/.config/speakd/profiles.toml)"
        ),
    )
    say.add_argument(
        "--no-transforms",
        action="store_true",
        help="skip the transform chain entirely and speak the raw text",
    )
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
    # Left untyped here and checked in `_priority`, for the same reason as
    # the role above: argparse's `type=int` raises SystemExit, so two
    # neighbouring subcommands failed in two different shapes -- the same
    # exit code to a shell, a return value in one case and an exception in
    # the other to anyone calling `main`.
    priority.add_argument("priority", metavar="INTEGER", help="higher is heard first")

    label = sub.add_parser("label", parents=[common], help="name a channel for display")
    # Emptiness is checked in `_label`, not by an argparse `type=` callable,
    # for the reason the two above give: argparse raises SystemExit, and this
    # subcommand has to fail in the same shape as its neighbours.
    label.add_argument("label", help="what to show for this channel")

    # The off switches are daemon-wide unless told otherwise, which is the
    # one place `--source cli` would be wrong: `speakctl mute` means stop
    # talking, not stop talking to the shell that just asked. Its own parent
    # rather than a per-subcommand override, so the four cannot drift apart.
    wide = argparse.ArgumentParser(add_help=False)
    wide.add_argument(
        "--source",
        default="",
        help="the channel this applies to (default: the whole daemon)",
    )
    wide.add_argument(
        "--socket",
        type=Path,
        default=default_socket_path(),
        help="where the daemon listens (default: %(default)s)",
    )

    # Two levels of off, and the difference is three gigabytes: mute keeps
    # the model loaded and starts speaking again the instant it is cleared,
    # disable gives the memory back and takes tens of seconds to come round.
    sub.add_parser("mute", parents=[wide], help="stop speaking, keeping the model loaded")
    sub.add_parser("unmute", parents=[wide], help="speak again")
    sub.add_parser("disable", parents=[wide], help="unload the model; nothing is spoken")
    sub.add_parser("enable", parents=[wide], help="load the model again")

    # Transport, in the same shape as the verbs above: --source, --socket,
    # and one line back when nothing answers.
    sub.add_parser("pause", parents=[common], help="suspend playback where it is")
    sub.add_parser("resume", parents=[common], help="take playback up again")

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

    `max_chars` and `speed` default to `None` (meaning "use the profile's
    value, or the built-in default") and are only checked when the user
    actually passed one -- a profile's own speed is already validated by
    `load_profiles`, and `None` is never invalid.

    Returns an error message, or `None` if `args` is valid.
    """
    if args.max_chars is not None and args.max_chars < 1:
        return f"--max-chars must be at least 1, got {args.max_chars}"
    if args.speed is not None and args.speed <= 0:
        return f"--speed must be greater than 0, got {args.speed}"
    return None


def _say(args: argparse.Namespace) -> int:
    error = _validate_say_args(args)
    if error is not None:
        print(f"speakctl: {error}", file=sys.stderr)
        return 2

    text = args.text if args.text is not None else sys.stdin.read()

    profiles_path = (
        Path(args.profiles_file) if args.profiles_file is not None else default_profiles_path()
    )
    try:
        profiles = load_profiles(profiles_path)
    except ValueError as exc:
        # A malformed profile is the one failure mode this command must not
        # speak through: which transforms to run and how is exactly what is
        # broken, so there is nothing safe left to fall back to.
        print(f"speakctl: invalid profiles file {profiles_path}: {exc}", file=sys.stderr)
        return 2

    profile = profiles.get(args.profile)
    if profile is None:
        known = ", ".join(sorted(profiles))
        print(f"speakctl: unknown profile {args.profile!r} (known: {known})", file=sys.stderr)
        return 2

    # Precedence: an explicit flag wins, then the profile, then (for a
    # profile that never overrides voice/speed, i.e. the default one) the
    # built-in default already carried on `Profile`.
    voice = args.voice if args.voice is not None else profile.voice
    speed = args.speed if args.speed is not None else profile.speed
    max_chars = args.max_chars if args.max_chars is not None else DEFAULT_MAX_CHARS

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

    # A missing or failing transform never silences the rest of the
    # utterance -- the chain just loses that transform's contribution, and
    # `transformed` below carries on regardless. But an agent driving this
    # reads either the exit code or the --dry-run JSON, and both must tell
    # the same story: `missing_transforms` and `transform_errors` feed both
    # the JSON payload below and, together with `result.errors`, the exit
    # code, so the two surfaces cannot silently drift apart again.
    missing_transforms: list[str] = []
    transform_errors: list[str] = []
    if args.no_transforms:
        transformed = pieces
    else:
        host = PluginHost(ServiceRegistry())
        register_builtins(host)
        chain, missing = resolve_chain(profile, host)
        for name in missing:
            print(
                f"speakctl: profile {profile.name!r}: transform {name!r} "
                "is not provided by any plugin",
                file=sys.stderr,
            )
            missing_transforms.append(name)
        chain_result = apply_chain(pieces, chain)
        for message in chain_result.errors:
            print(f"speakctl: {message}", file=sys.stderr)
        transform_errors.extend(chain_result.errors)
        transformed = chain_result.pieces

    result = speak(
        transformed,
        engine,  # type: ignore[arg-type]
        player,  # type: ignore[arg-type]
        voice=voice,
        speed=speed,
        max_chars=max_chars,
    )

    if args.dry_run:
        print(
            json.dumps(
                {
                    "duration": result.timeline.duration,
                    "errors": result.errors,
                    "missing_transforms": missing_transforms,
                    "transform_errors": transform_errors,
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
    # and a silently dropped segment -- or a silently dropped transform -- is
    # exactly what it must not miss. `missing_transforms` and
    # `transform_errors` also ride along in the --dry-run JSON above, so the
    # exit code and the JSON always agree on why a run exited 1.
    return 1 if (result.errors or missing_transforms or transform_errors) else 0


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


def _transport(args: argparse.Namespace, verb: Verb) -> int:
    """pause and resume. A daemon whose player cannot pause says so, and that
    refusal is printed rather than swallowed: a dead pause button that reports
    nothing is the failure this whole path exists to remove."""
    response = _call(args.socket, Request(verb=verb, source_id=args.source))
    if response is None:
        return _UNREACHABLE
    return 0 if response.ok else _refused(response)


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
    try:
        priority = int(args.priority)
    except ValueError:
        print(
            f"speakctl: priority must be an integer, got {args.priority!r}",
            file=sys.stderr,
        )
        return _UNREACHABLE
    response = _call(
        args.socket,
        Request(
            verb=Verb.SET_PRIORITY,
            source_id=args.source,
            payload={"priority": priority},
        ),
    )
    if response is None:
        return _UNREACHABLE
    return 0 if response.ok else _refused(response)


def _mute(args: argparse.Namespace, muted: bool) -> int:
    """mute and unmute. Silent on success, like every other switch here."""
    response = _call(
        args.socket,
        Request(verb=Verb.MUTE, source_id=args.source, payload={"muted": muted}),
    )
    if response is None:
        return _UNREACHABLE
    return 0 if response.ok else _refused(response)


def _set_engine(args: argparse.Namespace, loaded: bool) -> int:
    """enable and disable.

    `enable` returns while the model is still loading -- tens of seconds of
    it -- so it says so. An off switch may be silent because it took effect
    before the command returned; an on switch that stays quiet for half a
    minute reads as one that did nothing.
    """
    response = _call(
        args.socket,
        Request(verb=Verb.SET_ENGINE, source_id=args.source, payload={"loaded": loaded}),
    )
    if response is None:
        return _UNREACHABLE
    if not response.ok:
        return _refused(response)
    if response.data.get("loading") is True:
        print(
            "speakctl: loading the model; speech starts when it is ready "
            "(watch for the `engine` event on `speakctl subscribe`)",
            file=sys.stderr,
        )
    return 0


def _label(args: argparse.Namespace) -> int:
    if not args.label.strip():
        print("speakctl: a label must not be empty", file=sys.stderr)
        return _UNREACHABLE
    response = _call(
        args.socket,
        Request(verb=Verb.SET_LABEL, source_id=args.source, payload={"label": args.label}),
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
    if args.command == "pause":
        return _transport(args, Verb.PAUSE)
    if args.command == "resume":
        return _transport(args, Verb.RESUME)
    if args.command == "role":
        return _role(args)
    if args.command == "priority":
        return _priority(args)
    if args.command == "label":
        return _label(args)
    if args.command == "mute":
        return _mute(args, True)
    if args.command == "unmute":
        return _mute(args, False)
    if args.command == "disable":
        return _set_engine(args, False)
    if args.command == "enable":
        return _set_engine(args, True)
    if args.command == "status":
        return _status(args)
    if args.command == "subscribe":
        return _subscribe(args)
    parser.print_usage(sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
