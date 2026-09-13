"""speakctl — the lowest common denominator client.

Any agent that can run a shell command can drive speech through this.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence

from speakd.model import Piece, Span
from speakd.pipeline import speak
from speakd.segmenter import DEFAULT_MAX_CHARS


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

        engine = KokoroEngine()
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
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.command == "say":
        return _say(args)
    parser.print_usage(sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
