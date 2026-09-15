"""What arrived, and what was done about it.

"It did not read my WhatsApp" is unanswerable without this. The notification
either never reached the bus, or reached it under an app name no rule matches,
or matched a rule that declined it -- and those have three different fixes.
By the time anyone asks, the notification itself is long gone: nothing on a
Linux desktop keeps one after it has been shown.

So every notification the connector sees is recorded with the decision taken
and the rule that took it, whether or not it was spoken. A hundred of them,
which is a busy morning, and `speakctl notify recent` prints them.

Bodies are stored as they were read aloud. That is not a new exposure -- the
text was already being spoken into the room -- but it is a file on disk, so
`clear()` exists and `speakctl notify recent --clear` reaches it.

Nothing here raises. A history that cannot be written must not stop a
notification being spoken; the record is the diagnostic, not the product.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from speakd.paths import client_state_dir

# A hundred entries is a busy morning, and the file stays under ~40 KB. The
# whole file is rewritten on every notification, which is affordable only
# because of that cap -- raising it far would put a growing write on the path
# to speech.
HISTORY_LIMIT = 100


@dataclass(frozen=True)
class Entry:
    when: float
    app: str
    summary: str
    body: str
    spoken: bool
    rule: str = ""
    reason: str = ""


def state_dir() -> Path:
    return client_state_dir("notifications")


def path() -> Path:
    return state_dir() / "history.jsonl"


def record(entry: Entry) -> None:
    """Add one entry, dropping the oldest past the cap. Never raises."""
    try:
        entries = recent(limit=HISTORY_LIMIT - 1)
        entries.append(entry)
        directory = state_dir()
        directory.mkdir(parents=True, exist_ok=True)
        lines = "".join(json.dumps(asdict(e)) + "\n" for e in entries)
        # Written whole rather than appended-and-trimmed: an append plus a
        # separate truncation has a window in which the file holds more than
        # the cap, and a reader that arrives inside it gets a file this module
        # promises cannot exist. At a hundred short lines the rewrite costs
        # less than the socket round trip that follows it.
        temporary = directory / "history.jsonl.tmp"
        temporary.write_text(lines, encoding="utf-8")
        temporary.replace(path())
    except OSError:
        pass


def recent(limit: int = HISTORY_LIMIT) -> list[Entry]:
    """The most recent entries, oldest first. Never raises."""
    try:
        raw = path().read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    entries: list[Entry] = []
    for line in raw.splitlines():
        if not line.strip():
            continue
        try:
            body = json.loads(line)
        except json.JSONDecodeError:
            # One bad line is one lost notification, not a lost history. This
            # file is only ever read to answer a question about a notification
            # that has already happened.
            continue
        if not isinstance(body, dict):
            continue
        try:
            entries.append(
                Entry(
                    when=float(body.get("when", 0.0)),
                    app=str(body.get("app", "")),
                    summary=str(body.get("summary", "")),
                    body=str(body.get("body", "")),
                    spoken=bool(body.get("spoken", False)),
                    rule=str(body.get("rule", "")),
                    reason=str(body.get("reason", "")),
                )
            )
        except (TypeError, ValueError):
            continue
    return entries[-limit:] if limit > 0 else []


def clear() -> None:
    """Forget everything recorded so far. Never raises."""
    try:
        path().unlink()
    except OSError:
        pass


def now() -> float:
    return time.time()
