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
