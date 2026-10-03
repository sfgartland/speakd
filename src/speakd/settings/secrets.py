"""Where `secret` settings live: one private file each, never in settings.toml.

An API key is a setting a person wants to type into the window like any
other, and nothing like any other once it is there. Three things follow, and
this module is the first of them:

- **Stored apart.** `settings.toml` is the file people copy between machines,
  keep in dotfile repositories and paste into bug reports. A secret goes in
  `<config>/speakd/secrets/<key>` instead: the directory is 0700, the file
  0600, created private rather than written and then narrowed, so there is no
  moment at which another user could read it.
- **Never read back** (`registry.py`). Every verb, event and status reports a
  secret only as set or not set. The one way to the value is
  `Settings.secret()`, in-process, for the code that sends it to its service.
- **Never on a command line** (`cli.py`). `speakctl set` asks for it without
  echo, since an argument is visible to every user through `ps` and stays in
  shell history.

What this does not do is encrypt: anything running as this user can read the
file, as it could read a keyring unlocked for the session. The boundary is
other users, and other programs being *handed* the key -- not this user's own
processes.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path


class SecretStore:
    """One 0600 file per secret, in one 0700 directory."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory

    def _path(self, key: str) -> Path:
        # Keys are `owner.name`, both already restricted to [a-z0-9_-] by the
        # declaration rules, so a key can never name a path outside here.
        return self.directory / key

    def has(self, key: str) -> bool:
        return self.read(key) is not None

    def read(self, key: str) -> str | None:
        try:
            text = self._path(key).read_text(encoding="utf-8").strip()
        except OSError:
            return None
        return text or None

    def write(self, key: str, value: str) -> None:
        """Replace the secret atomically, private from the first byte."""
        self.directory.mkdir(parents=True, exist_ok=True)
        if stat.S_IMODE(self.directory.stat().st_mode) != 0o700:
            self.directory.chmod(0o700)
        path = self._path(key)
        tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
        try:
            descriptor = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(value + "\n")
            os.replace(tmp, path)
        finally:
            tmp.unlink(missing_ok=True)

    def delete(self, key: str) -> None:
        self._path(key).unlink(missing_ok=True)
