"""Where things live on disk.

Deliberately a leaf: this module imports `os` and `pathlib` and must never
import anything from `speakd`. It exists because `default_socket_path` used to
live in `cli.py`, which imports the synthesis pipeline -- so the Claude Code
hook loaded numpy on every tool call to build a socket path. Anything added
here inherits that obligation.
"""

from __future__ import annotations

import os
from pathlib import Path


def default_socket_path() -> Path:
    """Where the daemon listens, following XDG with a sensible fallback."""
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    base = Path(runtime) if runtime else Path(os.environ.get("TMPDIR", "/tmp"))
    return base / "speakd" / "speakd.sock"


def default_profiles_path() -> Path:
    """Where profiles live when `--profiles-file` is not given.

    Here rather than in `cli.py` for the same reason as the socket path: the
    daemon needs it too, and reaching into `cli` for it would drag the
    synthesis pipeline into anything that only wanted a filename.
    """
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else Path.home() / ".config"
    return base / "speakd" / "profiles.toml"


def client_state_dir(name: str) -> Path:
    """The directory holding one client's state files.

    `SPEAKD_STATE_DIR` exists so tests never touch the real one, and is
    honoured first. Resolved on every call rather than cached: the test suite
    sets it per test, and a cached value would pin whichever test ran first.

    Shared by every client, so that the two of them cannot drift on where
    state lives or on which environment variable overrides it.
    """
    override = os.environ.get("SPEAKD_STATE_DIR")
    if override:
        return Path(override) / name
    xdg = os.environ.get("XDG_STATE_HOME")
    base = Path(xdg) if xdg else Path.home() / ".local" / "state"
    return base / "speakd" / name


def default_notifications_path() -> Path:
    """Where the notification filter rules live."""
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else Path.home() / ".config"
    return base / "speakd" / "notifications.toml"
