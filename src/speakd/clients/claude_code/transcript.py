"""Reading Claude Code's transcript JSONL.

Pure: bytes in, records out. Every filesystem concern lives in `watermark`
and every network concern in `send`, so the rules about what counts as
speech can be tested without either.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class Record:
    """One transcript line, reduced to what matters for speech."""

    uuid: str
    kind: str
    is_sidechain: bool
    text: str
    # Only an `ai-title` record carries one, and it is not speech: it is the
    # name the GUI shows for the session.
    title: str | None = None


def _text_of(message: object) -> str:
    """Join the `text` blocks of one assistant message.

    `thinking` blocks are not for speaking and `tool_use` blocks are JSON;
    both are dropped here rather than anywhere downstream.
    """
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    if not isinstance(content, list):
        return ""
    blocks: list[str] = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            text = block.get("text")
            if isinstance(text, str) and text.strip():
                blocks.append(text)
    return "\n\n".join(blocks)


def _record_of(body: dict[str, object]) -> Record | None:
    kind = body.get("type")
    if kind == "ai-title":
        title = body.get("aiTitle")
        if isinstance(title, str) and title.strip():
            # No uuid on these records, and no need of one: nothing walks
            # back to a title, and `speakable` skips every kind but
            # "assistant" anyway. Loosening the uuid check below instead
            # would let every other uuid-less record through -- summaries
            # among them, whose empty uuid would become a saved watermark
            # that fails to load and re-speaks the turn.
            return Record(uuid="", kind="ai-title", is_sidechain=False, text="", title=title)
        return None
    uuid = body.get("uuid")
    if not isinstance(uuid, str) or not isinstance(kind, str):
        return None
    text = _text_of(body.get("message")) if kind == "assistant" else ""
    return Record(
        uuid=uuid,
        kind=kind,
        is_sidechain=bool(body.get("isSidechain")),
        text=text,
    )


def parse(chunk: bytes) -> tuple[list[Record], int]:
    """Parse whole lines from `chunk`; return the records and bytes consumed.

    The transcript is appended to while we read it, so a trailing partial
    line is normal and must not be consumed — the caller resumes at the
    returned offset and sees that line whole next time. A line that is
    complete but unparseable is skipped *and* counted: stopping forever on
    one bad byte loses every later sentence, which is the worse failure.

    The split is strictly on `b"\n"`, never `splitlines`, which also breaks
    on a bare `\r`. A stray CR inside one corrupt line would otherwise yield
    a fragment that is not `\n`-terminated, read as the trailing partial
    line, and everything after it in the chunk would be discarded
    *unconsumed* -- the offset would never move past that byte and the
    session would fall silent for good. A CR left at the end of a line by a
    CRLF writer is JSON whitespace and parses fine.
    """
    records: list[Record] = []
    consumed = 0
    lines = chunk.split(b"\n")
    # Every element but the last was terminated by the newline we split on;
    # the last is whatever follows the final newline, complete or not.
    for raw in lines[:-1]:
        consumed += len(raw) + 1
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        if not isinstance(body, dict):
            continue
        record = _record_of(body)
        if record is not None:
            records.append(record)
    return records, consumed


def speakable(records: Sequence[Record]) -> str:
    """The text a listener should hear, from records in transcript order."""
    return "\n\n".join(
        record.text
        for record in records
        if record.kind == "assistant" and not record.is_sidechain and record.text
    )


def ai_title(records: Sequence[Record]) -> str | None:
    """The session's latest human-readable name, if it has one yet.

    Claude Code rewrites this as a session develops, so the last one wins.
    """
    for record in reversed(records):
        if record.kind == "ai-title" and record.title:
            return record.title
    return None
