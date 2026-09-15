"""Transcript plus watermark in, speech plus next watermark out."""

from __future__ import annotations

from pathlib import Path

from speakd.clients.claude_code.transcript import parse, speakable
from speakd.clients.claude_code.watermark import Watermark


def new_text(transcript: Path, mark: Watermark | None) -> tuple[str, Watermark | None]:
    """What this session has not spoken yet, and where to resume.

    Returns `("", None)` when there is nothing new: the caller then writes
    no state at all.

    With no usable watermark this reads from the top of the file. It used to
    guess the current turn instead, by scanning back for the last record of
    type "user" -- but a tool result is itself a "user" record, so the guess
    threw away every prose block before the last tool call, which on a turn
    that used any tools at all is most of what was said. There is no guess
    that survives that, so there is none. A caller that must not hear a
    session's history writes its own watermark at end of file first, which is
    what the follower does the moment a session registers; here, "no usable
    watermark" means "start here".
    """
    try:
        size = transcript.stat().st_size
    except OSError:
        return "", None

    resumable = mark is not None and mark.path == str(transcript) and 0 <= mark.offset <= size
    start = mark.offset if resumable and mark is not None else 0

    try:
        with transcript.open("rb") as handle:
            handle.seek(start)
            chunk = handle.read()
    except OSError:
        return "", None

    records, consumed = parse(chunk)
    if not records:
        return "", None

    return speakable(records), Watermark(
        path=str(transcript),
        offset=start + consumed,
        uuid=records[-1].uuid,
    )
