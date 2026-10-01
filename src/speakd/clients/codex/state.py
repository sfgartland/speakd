"""A cursor shared by the prompt hook and follower, protected by a bounded lock."""

from __future__ import annotations

import fcntl
import json
import os
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path

from speakd.clients.registry import _slug
from speakd.paths import client_state_dir


@dataclass(frozen=True)
class Cursor:
    path: str
    offset: int
    active: bool = True


def state_dir() -> Path:
    return client_state_dir("codex")


def _path(session_id: str, suffix: str) -> Path:
    directory = state_dir()
    directory.mkdir(parents=True, exist_ok=True)
    return directory / f"{_slug(session_id)}{suffix}"


def load(session_id: str) -> Cursor | None:
    try:
        body = json.loads(_path(session_id, ".cursor.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(body, dict):
        return None
    path, offset, active = body.get("path"), body.get("offset"), body.get("active")
    if not isinstance(path, str) or type(offset) is not int or offset < 0:
        return None
    if not isinstance(active, bool):
        return None
    return Cursor(path, offset, active)


def save(session_id: str, cursor: Cursor) -> None:
    """Caller holds locked() across read, send and save."""
    target = _path(session_id, ".cursor.json")
    temporary = target.with_suffix(".tmp")
    temporary.write_text(json.dumps(asdict(cursor)), encoding="utf-8")
    os.replace(temporary, target)


@contextmanager
def locked(session_id: str, *, timeout: float = 0.0) -> Iterator[None]:
    descriptor = os.open(_path(session_id, ".lock"), os.O_CREAT | os.O_RDWR, 0o600)
    deadline = time.monotonic() + timeout
    try:
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError("Codex speech cursor is busy") from None
                time.sleep(0.01)
        yield
    finally:
        os.close(descriptor)
