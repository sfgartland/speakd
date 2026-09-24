"""speakctl — the lowest common denominator client.

Any agent that can run a shell command can drive speech through this.
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING

from speakd.clients.notifications import history, rules
from speakd.clients.notifications.starter import STARTER_TOML
from speakd.model import Piece, Role, Span
from speakd.paths import default_notifications_path, default_profiles_path, default_socket_path
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

    # Verbs that mean the whole daemon unless a channel is named, which is the
    # one place `--source cli` would be wrong: `speakctl hush` means stop
    # talking, not stop talking to the shell that just asked. Since hush and
    # cancel started honouring their source, that default would have silenced
    # a channel nobody speaks on and left the room talking -- exit code 0,
    # nothing printed. Its own parent rather than a per-subcommand override,
    # so the six cannot drift apart.
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

    enqueue = sub.add_parser("enqueue", parents=[common], help="speak text through the daemon")
    enqueue.add_argument("text")
    enqueue.add_argument("--kind", default="response", help="what sort of utterance this is")
    enqueue.add_argument("--profile", help="override the channel's profile for this utterance")

    # These two are not synonyms, and the difference costs a queue.
    sub.add_parser(
        "hush",
        parents=[wide],
        help="stop talking: cancel what is being said and clear the queue",
    )
    sub.add_parser(
        "cancel",
        parents=[wide],
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

    speed = sub.add_parser("speed", parents=[common], help="print or set the speaking speed")
    # A string, checked in `_speed`, for the reason `priority` gives above.
    speed.add_argument("value", nargs="?", help="a multiplier, 0.7 to 1.6; omit to print it")
    speed.add_argument("--ramp", type=float, default=None, help="seconds to glide there over")
    mode = sub.add_parser("mode", parents=[common], help="brief or full narration for a channel")
    # Checked in `_mode`, not by argparse `choices`, for the reason `role` gives.
    mode.add_argument("mode", metavar="{brief,full}", help="brief: what the agent chooses to say")
    seek = sub.add_parser("seek", parents=[common], help="move within what is being spoken")
    where = seek.add_mutually_exclusive_group(required=True)
    where.add_argument("--by", type=int, help="sentences forward (negative: back)")
    where.add_argument("--index", type=int, help="the sentence to go to, counting from 0")

    sub.add_parser(
        "http-token", help="print the token clients of the HTTP transport need (the Zotero plugin)"
    )
    sub.add_parser("subscribe", parents=[common], help="stream events as JSON lines")
    sub.add_parser("status", parents=[common], help="print the daemon's channels as JSON")

    settings_cmd = sub.add_parser(
        "settings", parents=[common], help="print settings and their values as a table"
    )
    settings_cmd.add_argument("owner", nargs="?", default=None, help="only this owner's settings")

    set_cmd = sub.add_parser(
        "set", parents=[common], help="set one setting; the value is parsed by its declared type"
    )
    set_cmd.add_argument("key", help="owner.name, e.g. speech.default_language")
    set_cmd.add_argument("value", help="parsed according to the setting's declared type")

    notify = sub.add_parser("notify", help="the desktop notification connector")
    notify_sub = notify.add_subparsers(dest="notify_command")
    recent = notify_sub.add_parser("recent", help="what arrived, and what was decided about it")
    # Untyped and checked in the handler, like `priority` above: argparse's
    # `type=int` exits the process where every other failure here returns a code.
    recent.add_argument("--limit", metavar="N", default="20", help="how many to show")
    recent.add_argument("--clear", action="store_true", help="forget everything recorded so far")
    notify_sub.add_parser(
        "tap",
        help="watch notifications arrive and say what would happen, speaking nothing",
    )
    init = notify_sub.add_parser("init", help="write a starter rules file")
    init.add_argument("--force", action="store_true", help="overwrite an existing file")
    test = notify_sub.add_parser("test", help="ask the rules what they would do with one")
    test.add_argument("app")
    test.add_argument("summary", nargs="?", default="")
    test.add_argument("body", nargs="?", default="")
    test.add_argument("--urgency", default="normal", help="low, normal or critical")
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


def _speed(args: argparse.Namespace) -> int:
    """Print the speed, or set it and print what the daemon applied.

    Printed either way, because the daemon clamps: asking for 3 and being
    told 1.6 is the answer, and a silent success would hide it.
    """
    if args.value is None:
        request = Request(verb=Verb.STATUS, source_id=args.source)
    else:
        try:
            value = float(args.value)
        except ValueError:
            print(f"speakctl: speed must be a number, got {args.value!r}", file=sys.stderr)
            return _UNREACHABLE
        payload: dict[str, object] = {"speed": value}
        if args.ramp is not None:
            payload["ramp"] = args.ramp
        request = Request(verb=Verb.SET_SPEED, source_id=args.source, payload=payload)
    response = _call(args.socket, request)
    if response is None:
        return _UNREACHABLE
    if not response.ok:
        return _refused(response)
    print(response.data.get("speed"))
    return 0


def _mode(args: argparse.Namespace) -> int:
    """Switch a channel between brief and full. Silent on success, like mute."""
    if args.mode not in ("brief", "full"):
        print(f"speakctl: unknown mode {args.mode!r} (expected brief or full)", file=sys.stderr)
        return _UNREACHABLE
    response = _call(
        args.socket,
        Request(verb=Verb.SET_MODE, source_id=args.source, payload={"mode": args.mode}),
    )
    if response is None:
        return _UNREACHABLE
    return 0 if response.ok else _refused(response)


def _seek(args: argparse.Namespace) -> int:
    payload = {"by": args.by} if args.by is not None else {"index": args.index}
    response = _call(args.socket, Request(verb=Verb.SEEK, source_id=args.source, payload=payload))
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


_URGENCIES = {"low": 0, "normal": 1, "critical": 2}


def _notify(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    if args.notify_command == "recent":
        return _notify_recent(args)
    if args.notify_command == "tap":
        return _notify_tap()
    if args.notify_command == "init":
        return _notify_init(args)
    if args.notify_command == "test":
        return _notify_test(args)
    parser.print_usage(sys.stderr)
    return 2


def _load_rules_for_cli() -> rules.Ruleset | None:
    """The rules as the connector would see them, or a message and None."""
    path = default_notifications_path()
    try:
        return rules.load_rules(path)
    except ValueError as exc:
        sys.stderr.write(f"speakctl: {path}: {exc}\n")
        return None


def _describe(entry: history.Entry) -> str:
    stamp = time.strftime("%H:%M:%S", time.localtime(entry.when))
    outcome = "spoken " if entry.spoken else "skipped"
    note = entry.rule or entry.reason or "no rule matched"
    said = " ".join(f"{entry.summary} {entry.body}".split())
    return f"{stamp}  {outcome}  {entry.app:<18.18}  {note:<22.22}  {said[:60]}"


def _notify_recent(args: argparse.Namespace) -> int:
    if args.clear:
        history.clear()
        sys.stdout.write("forgotten\n")
        return 0
    try:
        limit = int(args.limit)
    except ValueError:
        sys.stderr.write(f"speakctl: --limit must be an integer, got {args.limit!r}\n")
        return 2
    entries = history.recent(limit=limit)
    if not entries:
        # The likeliest reason by far, and the one nobody guesses: the
        # connector has never run, so nothing has ever been recorded.
        sys.stdout.write(
            "nothing recorded yet -- is the daemon running with the notification "
            "connector enabled?\n"
        )
        return 0
    for entry in entries:
        sys.stdout.write(_describe(entry) + "\n")
    return 0


def _notify_tap() -> int:
    """Watch the bus and report, speaking nothing.

    Speaking nothing is the point: this is what you run while you send
    yourself a message from another device, to find out what the app that
    sent it calls itself before writing a rule about it.
    """
    from speakd.clients.notifications.monitor import Monitor, busctl_owner, stream

    ruleset = _load_rules_for_cli()
    if ruleset is None:
        return 2
    stop = threading.Event()
    sys.stdout.write("watching for notifications; nothing here is spoken. Ctrl-C to stop.\n")
    sys.stdout.flush()
    try:
        for note in stream(Monitor(owner=busctl_owner), stop):
            verdict = rules.decide(note, ruleset)
            sys.stdout.write(
                json.dumps(
                    {
                        "app": note.app,
                        "summary": note.summary,
                        "body": note.body,
                        "urgency": note.urgency,
                        "desktop_entry": note.desktop_entry,
                        "would_speak": verdict.speak,
                        "rule": verdict.rule,
                        "reason": verdict.reason,
                        "text": verdict.text,
                    }
                )
                + "\n"
            )
            # Flushed per notification: this is watched live, and a tap that
            # shows nothing until its buffer fills is a tap that looks broken.
            sys.stdout.flush()
    except FileNotFoundError:
        sys.stderr.write("speakctl: busctl is not installed; notifications cannot be read\n")
        return 1
    except KeyboardInterrupt:
        stop.set()
    return 0


def _notify_init(args: argparse.Namespace) -> int:
    path = default_notifications_path()
    if path.exists() and not args.force:
        sys.stderr.write(f"speakctl: {path} already exists; pass --force to overwrite it\n")
        return 1
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(STARTER_TOML, encoding="utf-8")
    except OSError as exc:
        sys.stderr.write(f"speakctl: could not write {path}: {exc}\n")
        return 1
    sys.stdout.write(f"{path}\n")
    return 0


def _notify_test(args: argparse.Namespace) -> int:
    """Answer what the rules would do, without waiting for a real message."""
    from speakd.clients.notifications.monitor import Notification

    urgency = _URGENCIES.get(args.urgency)
    if urgency is None:
        wanted = ", ".join(sorted(_URGENCIES))
        sys.stderr.write(f"speakctl: --urgency must be one of {wanted}, got {args.urgency!r}\n")
        return 2
    ruleset = _load_rules_for_cli()
    if ruleset is None:
        return 2
    note = Notification(app=args.app, summary=args.summary, body=args.body, urgency=urgency)
    verdict = rules.decide(note, ruleset)
    sys.stdout.write(
        json.dumps(
            {
                "speak": verdict.speak,
                "rule": verdict.rule,
                "reason": verdict.reason,
                "channel": verdict.channel,
                "label": verdict.label,
                "profile": verdict.profile,
                "text": verdict.text,
            },
            indent=2,
        )
        + "\n"
    )
    return 0


def _settings_table(args: argparse.Namespace) -> int:
    """`speakctl settings [owner]`: a readable table of key, value, default, type."""
    payload: dict[str, object] = {"owner": args.owner} if args.owner else {}
    response = _call(
        args.socket, Request(verb=Verb.SETTINGS, source_id=args.source, payload=payload)
    )
    if response is None:
        return _UNREACHABLE
    if not response.ok:
        return _refused(response)
    schema = response.data.get("schema", [])
    values = response.data.get("values", {})
    if not isinstance(schema, list) or not schema:
        print("no settings declared" + (f" for {args.owner!r}" if args.owner else ""))
        return 0
    if not isinstance(values, dict):
        values = {}
    header = ("key", "value", "default", "type")
    rows = [
        (
            str(decl["key"]),
            str(values.get(decl["key"], decl["default"])),
            str(decl["default"]),
            str(decl["type"]),
        )
        for decl in schema
    ]
    widths = [max(len(row[i]) for row in (header, *rows)) for i in range(4)]
    for row in (header, *rows):
        print("  ".join(cell.ljust(width) for cell, width in zip(row, widths, strict=True)))
    return 0


def _parse_setting_value(setting_type: str, raw: str) -> object:
    """`raw` as `setting_type` would validate it, or raise `ValueError`.

    Mirrors, in miniature, the same reading a person typing `true` or `off`
    at a shell expects -- `speakd.settings.types.validate` is stricter than
    this on purpose (a real `bool` from JSON, never the string `"true"`),
    since it also has to guard against a program passing the wrong Python
    type by accident, which is not a risk a string typed at a terminal has.
    """
    if setting_type == "bool":
        lowered = raw.strip().lower()
        if lowered in ("true", "on", "1"):
            return True
        if lowered in ("false", "off", "0"):
            return False
        raise ValueError(f"not a boolean (true/false, on/off, 1/0): {raw!r}")
    if setting_type == "int":
        try:
            return int(raw)
        except ValueError:
            raise ValueError(f"not an integer: {raw!r}") from None
    if setting_type == "float":
        try:
            return float(raw)
        except ValueError:
            raise ValueError(f"not a number: {raw!r}") from None
    if setting_type == "voice_map":
        result: dict[str, str] = {}
        for pair in raw.split(","):
            pair = pair.strip()
            if not pair:
                continue
            lang, sep, voice = pair.partition("=")
            if not sep:
                raise ValueError(f"expected lang=voice, got {pair!r}")
            result[lang.strip()] = voice.strip()
        return result
    # string, choice, voice: the string as given, and the daemon's own
    # validation says whether it is one of the right ones.
    return raw


def _set_setting(args: argparse.Namespace) -> int:
    """`speakctl set <key> <value>`.

    The daemon does not tell a client what type a value should parse as --
    `set_setting` takes an already-typed JSON value -- so this asks
    `settings` first, for the one declaration it needs, and parses the
    command-line string against that before sending it on.
    """
    owner = args.key.split(".", 1)[0]
    lookup = _call(
        args.socket, Request(verb=Verb.SETTINGS, source_id=args.source, payload={"owner": owner})
    )
    if lookup is None:
        return _UNREACHABLE
    if not lookup.ok:
        return _refused(lookup)
    schema = lookup.data.get("schema", [])
    decls = schema if isinstance(schema, list) else []
    decl = next((d for d in decls if isinstance(d, dict) and d.get("key") == args.key), None)
    if decl is None:
        print(f"speakctl: no such setting {args.key!r}", file=sys.stderr)
        return _UNREACHABLE
    try:
        value = _parse_setting_value(str(decl["type"]), args.value)
    except ValueError as exc:
        print(f"speakctl: {exc}", file=sys.stderr)
        return _UNREACHABLE
    response = _call(
        args.socket,
        Request(
            verb=Verb.SET_SETTING,
            source_id=args.source,
            payload={"key": args.key, "value": value},
        ),
    )
    if response is None:
        return _UNREACHABLE
    if not response.ok:
        return _refused(response)
    print(response.data.get("value"))
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
    if args.command == "speed":
        return _speed(args)
    if args.command == "seek":
        return _seek(args)
    if args.command == "mode":
        return _mode(args)
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
    if args.command == "settings":
        return _settings_table(args)
    if args.command == "set":
        return _set_setting(args)
    if args.command == "http-token":
        from speakd import http_token

        # Made here as well as by the daemon, so it can be pasted into a
        # client before the daemon has ever run.
        try:
            print(http_token.ensure())
        except OSError as exc:
            # An unwritable config directory (a read-only home, a full disk)
            # must reach the user as one line, not the traceback `ensure`
            # would otherwise raise straight out of `main`.
            print(f"speakctl: could not create the http token: {exc}", file=sys.stderr)
            return 2
        return 0
    if args.command == "subscribe":
        return _subscribe(args)
    if args.command == "notify":
        return _notify(args, parser)
    parser.print_usage(sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
