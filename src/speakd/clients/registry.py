"""Which agent sessions are live, and where their transcripts are.

Shared by the agent clients: the Claude Code hook and the OpenCode plugin
each write one small file per session; the Claude follower and `speakd-mcp`'s
session resolver read them. A file rather than a control verb because the
hook already writes to this directory for the watermark, and a registration
verb would be a second source of truth for one fact -- with the added cost
of a socket round trip on the one hook that is still inside the user's
critical path.

Nothing here raises. A hook that fails is a hook that breaks someone's
editor, and a follower that dies on one corrupt file stops speaking for
every session.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass
from pathlib import Path

from speakd.paths import client_state_dir

# Half an hour of no prompt is a session someone has walked away from. Long
# enough that a lunch break does not cost the follower the session; short
# enough that yesterday's twenty windows are not polled today.
DEFAULT_MAX_IDLE_SECONDS = 1800.0

_SAFE = re.compile(r"[^A-Za-z0-9_.-]")


@dataclass(frozen=True)
class Registration:
    client: str
    session_id: str
    transcript: Path
    cwd: str
    touched: float
    # The agent process the session runs in, so an MCP server it started can
    # find which session it serves. None for registrations written before
    # this was recorded.
    agent_pid: int | None = None


def _slug(session_id: str) -> str:
    """A filename that cannot leave the state directory.

    Session ids come from a hook payload or a plugin event. Sanitising is not
    paranoia about the agents; it is that a path-shaped id would otherwise
    write state wherever it pointed.
    """
    safe = _SAFE.sub("_", session_id)[:80]
    digest = hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:12]
    return f"{safe}-{digest}"


def state_dir(client: str) -> Path:
    """The directory holding one client's registration files."""
    return client_state_dir(client)


def _registration_path(client: str, session_id: str) -> Path:
    directory = state_dir(client)
    directory.mkdir(parents=True, exist_ok=True)
    return directory / f"{_slug(session_id)}.session.json"


def register(
    session_id: str,
    transcript: Path,
    cwd: str,
    *,
    client: str,
    agent_pid: int | None = None,
) -> None:
    """Record that this session is live. Never raises."""
    try:
        _registration_path(client, session_id).write_text(
            json.dumps(
                {
                    "session_id": session_id,
                    "transcript": str(transcript) if transcript != Path() else "",
                    "cwd": cwd,
                    "touched": time.time(),
                    "client": client,
                    "agent_pid": agent_pid,
                }
            ),
            encoding="utf-8",
        )
    except OSError:
        pass


def live(max_idle_seconds: float = DEFAULT_MAX_IDLE_SECONDS) -> list[Registration]:
    """Every session of every client registered recently enough to be polled."""
    base = client_state_dir("")
    try:
        directories = [path for path in base.iterdir() if path.is_dir()]
    except OSError:
        return []
    now = time.time()
    found: list[Registration] = []
    for directory in sorted(directories):
        try:
            candidates = sorted(directory.glob("*.session.json"))
        except OSError:
            continue
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
            client = body.get("client")
            if not isinstance(touched, int | float) or not isinstance(transcript, str):
                continue
            if not isinstance(session_id, str) or not isinstance(client, str):
                continue
            if now - touched > max_idle_seconds:
                continue
            found.append(
                Registration(
                    client=client,
                    session_id=session_id,
                    transcript=Path(transcript),
                    cwd=str(body.get("cwd") or ""),
                    touched=float(touched),
                    agent_pid=pid if isinstance(pid := body.get("agent_pid"), int) else None,
                )
            )
    return found
