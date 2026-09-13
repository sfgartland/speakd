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

from speakd.model import Piece, Span
from speakd.pipeline import speak
from speakd.plugins.builtin import register_builtins
from speakd.plugins.host import PluginHost
from speakd.plugins.registry import ServiceRegistry
from speakd.profiles import load_profiles, resolve_chain
from speakd.segmenter import DEFAULT_MAX_CHARS
from speakd.transforms.chain import apply_chain


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
    return parser


def _default_profiles_path() -> Path:
    """Where profiles live when `--profiles-file` is not given."""
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else Path.home() / ".config"
    return base / "speakd" / "profiles.toml"


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
        Path(args.profiles_file) if args.profiles_file is not None else _default_profiles_path()
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

    if args.no_transforms:
        transformed = pieces
    else:
        host = PluginHost(ServiceRegistry())
        register_builtins(host)
        chain, missing = resolve_chain(profile, host)
        for name in missing:
            # A profile naming a transform nobody provides must not silence
            # the rest of the utterance -- it just loses that transform's
            # contribution, so this is a warning, not a reason to stop.
            print(
                f"speakctl: profile {profile.name!r}: transform {name!r} "
                "is not provided by any plugin",
                file=sys.stderr,
            )
        chain_result = apply_chain(pieces, chain)
        for message in chain_result.errors:
            print(f"speakctl: {message}", file=sys.stderr)
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


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.command == "say":
        return _say(args)
    parser.print_usage(sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
