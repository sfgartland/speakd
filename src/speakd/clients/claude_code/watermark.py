"""Where we stopped reading each session's transcript.

Correctness for the whole client reduces to one property — a transcript
record is spoken at most once — and this module is where that property
lives. Two hooks can run at the same moment (Claude Code fires PostToolUse
concurrently for parallel tool calls, and Stop can overlap one), so the
lock is held across read-enqueue-write by the caller, not merely around the
write here.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path

_SAFE = re.compile(r"[^A-Za-z0-9_.-]")


@dataclass(frozen=True)
class Watermark:
    """The transcript we were reading, and how far into it we got."""

    path: str
    offset: int
    uuid: str


def state_dir() -> Path:
    """The directory holding one state file per session.

    `SPEAKD_STATE_DIR` exists so tests never touch the real one.
    """
    override = os.environ.get("SPEAKD_STATE_DIR")
    if override:
        return Path(override) / "claude-code"
    xdg = os.environ.get("XDG_STATE_HOME")
    base = Path(xdg) if xdg else Path.home() / ".local" / "state"
    return base / "speakd" / "claude-code"


def _slug(session_id: str) -> str:
    """A filename that cannot leave the state directory.

    Session ids come from a hook payload. Sanitising is not paranoia about
    Claude Code; it is that a path-shaped id would otherwise write state
    wherever it pointed.
    """
    safe = _SAFE.sub("_", session_id)[:80]
    digest = hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:12]
    return f"{safe}-{digest}"


def _path_for(session_id: str, suffix: str) -> Path:
    directory = state_dir()
    directory.mkdir(parents=True, exist_ok=True)
    return directory / f"{_slug(session_id)}{suffix}"


def load(session_id: str) -> Watermark | None:
    """The stored watermark, or `None` if there is none we can trust."""
    try:
        body = json.loads(_path_for(session_id, ".json").read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(body, dict):
        return None
    path, offset, uuid = body.get("path"), body.get("offset"), body.get("uuid")
    if not isinstance(path, str) or not isinstance(offset, int) or not isinstance(uuid, str):
        return None
    return Watermark(path=path, offset=offset, uuid=uuid)


def save(session_id: str, mark: Watermark) -> None:
    """Store `mark`, atomically.

    A half-written watermark read by the next hook loses the offset and
    re-speaks the whole turn, so this writes beside the target and replaces.
    """
    target = _path_for(session_id, ".json")
    temporary = target.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(asdict(mark)), encoding="utf-8")
    os.replace(temporary, target)


@contextmanager
def locked(session_id: str) -> Iterator[None]:
    """Hold this session's lock for the body of the `with`.

    The lock file is separate from the state file: `flock` follows the open
    descriptor, and `save`'s atomic replace swaps the inode underneath a
    lock taken on the state file itself.
    """
    lock_path = _path_for(session_id, ".lock")
    handle = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield
    finally:
        os.close(handle)
