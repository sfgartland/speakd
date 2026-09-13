"""Transcript plus watermark in, speech plus next watermark out."""

from __future__ import annotations

from pathlib import Path

from speakd.clients.claude_code.transcript import Record, parse, speakable
from speakd.clients.claude_code.watermark import Watermark


def _from_start(records: list[Record]) -> list[Record]:
    """Records belonging to the current turn only.

    With no usable watermark the alternative is reading the whole session
    aloud, which is what a resumed session would otherwise do on its first
    Stop hook.
    """
    for index in range(len(records) - 1, -1, -1):
        if records[index].kind == "user":
            return records[index + 1 :]
    return records


def new_text(transcript: Path, mark: Watermark | None) -> tuple[str, Watermark | None]:
    """What this session has not spoken yet, and where to resume.

    Returns `("", None)` when there is nothing new: the caller then writes
    no state at all.
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
    if not resumable:
        records = _from_start(records)
    if not records:
        return "", None

    return speakable(records), Watermark(
        path=str(transcript),
        offset=start + consumed,
        uuid=records[-1].uuid,
    )
