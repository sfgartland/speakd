"""Flags that outlive one run of the daemon.

A leaf, like `paths`: `__main__` reads `disabled` *before* it constructs the
engine, because a daemon that starts disabled must not load three gigabytes for
the sole purpose of freeing them. Importing anything heavy here would put that
cost back.

Every failure resolves to the default state rather than an exception. A daemon
that will not start because a JSON file was truncated is a worse failure than
one that starts audible.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass(frozen=True)
class DaemonState:
    muted: bool = False
    disabled: bool = False


def state_path() -> Path:
    xdg = os.environ.get("XDG_STATE_HOME")
    base = Path(xdg) if xdg else Path.home() / ".local" / "state"
    return base / "speakd" / "state.json"


def load() -> DaemonState:
    try:
        body = json.loads(state_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return DaemonState()
    if not isinstance(body, dict):
        return DaemonState()
    return DaemonState(
        muted=bool(body.get("muted", False)),
        disabled=bool(body.get("disabled", False)),
    )


def save(state: DaemonState) -> None:
    """Write the flags, or give up quietly.

    Quietly because the caller is a control verb: failing `mute` because the
    state directory is read-only would refuse an action that did in fact take
    effect in memory.
    """
    path = state_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(state)), encoding="utf-8")
    except OSError:
        pass
