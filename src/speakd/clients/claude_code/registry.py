"""Which Claude Code sessions are live, and where their transcripts are.

The hook writes one small file per session; the follower reads them. A file
rather than a control verb because the hook already writes to this directory
for the watermark, and a registration verb would be a second source of truth
for one fact -- with the added cost of a socket round trip on the one hook
that is still inside the user's critical path.

Nothing here raises. A hook that fails is a hook that breaks someone's editor,
and a follower that dies on one corrupt file stops speaking for every session.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path

from speakd.clients.claude_code.watermark import _path_for, state_dir

# Half an hour of no prompt is a session someone has walked away from. Long
# enough that a lunch break does not cost the follower the session; short
# enough that yesterday's twenty windows are not polled today.
DEFAULT_MAX_IDLE_SECONDS = 1800.0


@dataclass(frozen=True)
class Registration:
    session_id: str
    transcript: Path
    cwd: str
    touched: float


def _registration_path(session_id: str) -> Path:
    """Where this session's registration lives.

    Through `watermark._path_for`, which slugs the id, rather than
    interpolating it: session ids arrive in a hook payload, and a path-shaped
    one would otherwise write a registration wherever it pointed -- silently,
    since `register` swallows the OSError that a bad path would raise. The
    watermark beside it has been sanitised since it was written; the two files
    in this directory must not disagree about that.

    `live()` reads `session_id` out of the file body and never off the
    filename, so slugging is invisible to every caller.
    """
    return _path_for(session_id, ".session.json")


def register(session_id: str, transcript: Path, cwd: str) -> None:
    """Record that this session is live. Never raises."""
    try:
        _registration_path(session_id).write_text(
            json.dumps(
                {
                    "session_id": session_id,
                    "transcript": str(transcript),
                    "cwd": cwd,
                    "touched": time.time(),
                }
            ),
            encoding="utf-8",
        )
    except OSError:
        pass


def live(max_idle_seconds: float = DEFAULT_MAX_IDLE_SECONDS) -> list[Registration]:
    """Every session registered recently enough to be worth polling."""
    directory = state_dir()
    try:
        candidates = sorted(directory.glob("*.session.json"))
    except OSError:
        return []
    now = time.time()
    found: list[Registration] = []
    for path in candidates:
        try:
            body = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            continue
        if not isinstance(body, dict):
            continue
        touched = body.get("touched")
        transcript = body.get("transcript")
        session_id = body.get("session_id")
        if not isinstance(touched, int | float) or not isinstance(transcript, str):
            continue
        if not isinstance(session_id, str) or now - touched > max_idle_seconds:
            continue
        found.append(
            Registration(
                session_id=session_id,
                transcript=Path(transcript),
                cwd=str(body.get("cwd") or ""),
                touched=float(touched),
            )
        )
    return found
