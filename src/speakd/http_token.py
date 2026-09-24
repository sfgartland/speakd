"""The bearer token the loopback HTTP transport requires.

Loopback is not the Unix socket's trust boundary. Any web page the user
opens can reach 127.0.0.1, and a plain-text POST needs no CORS preflight, so
without a secret any site could make the machine speak, hush it, or -- by
DNS rebinding -- read the event stream, which carries the text of every
agent session. The token is that secret: made once, readable by this user
alone, and pasted into whatever client (the Zotero plugin) needs it.

A leaf module: stdlib only, and nothing raises past `ensure` but a real
inability to write the file.
"""

from __future__ import annotations

import os
import secrets
import stat
from pathlib import Path


def token_path() -> Path:
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else Path.home() / ".config"
    return base / "speakd" / "http-token"


def read(path: Path | None = None) -> str | None:
    """The token, or None when there is none yet."""
    try:
        token = (path or token_path()).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return token or None


# How many times `ensure` retries after losing a creation race before giving
# up. One retry handles the ordinary case (another `ensure` call won); a
# handful more cover a writer that has not finished yet, without looping
# forever on a file that will never gain content.
_CREATE_ATTEMPTS = 5


def ensure(path: Path | None = None) -> str:
    """The token, made if missing or empty, and kept readable by this user alone."""
    path = path or token_path()
    token = read(path)
    if token is None:
        path.parent.mkdir(parents=True, exist_ok=True)
        for _ in range(_CREATE_ATTEMPTS):
            candidate = secrets.token_urlsafe(32)
            try:
                # O_EXCL rather than O_TRUNC: a daemon's first start and a
                # concurrent `speakctl http-token` can both see no token here
                # and both reach this line. O_EXCL makes exactly one of them
                # create the file; created private rather than written and
                # then narrowed, so there is no moment at which another user
                # could read it.
                descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                # Lost the race: someone else created the file between our
                # read above and this attempt. Their file is the one to use,
                # not one we would overwrite it with -- re-read it rather
                # than assuming we won.
                token = read(path)
                if token is not None:
                    break
                # Still empty: not a race in progress -- nothing here writes
                # an empty file and fills it in later -- but a stale
                # leftover, from a create that was interrupted before it
                # could write. Remove it and try again rather than looping
                # on a file that will never gain content.
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass
                continue
            else:
                with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                    handle.write(candidate + "\n")
                token = candidate
                break
        else:
            raise OSError(f"could not create {path}: kept losing the race to create it")
    if stat.S_IMODE(path.stat().st_mode) != 0o600:
        path.chmod(0o600)
    return token
