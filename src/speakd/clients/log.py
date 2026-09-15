"""One capped, append-only log per client, which never fails its caller.

Both clients report the same way and for the same reason: a hook that raises
fails the user's turn, and a follower that raises stops speaking for every
session. What is left, when nothing may raise and stderr goes nowhere anyone
reads, is a line in a file -- and a file nobody rotates needs a cap of its own.

Extracted when the second client arrived. Until then this lived in the Claude
Code hook, which is where a notification connector would have had to reach to
report that it could not start.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path

# Past this, the log is emptied and started again. A follower polling ten
# times a second with the daemon off writes a line per poll; the hooks alone
# reached 1.8 MB per twenty thousand entries, measured. Not rotation: one
# file, one cap, and the newest failures are the ones a person tailing it
# needs. A quarter of a megabyte is some 2,500 lines, far more history than
# any diagnosis of this uses.
LOG_CAP_BYTES = 256 * 1024


def logger_for(directory: Callable[[], Path], filename: str) -> Callable[[str], None]:
    """Build a `log(message)` appending to `filename` under `directory()`.

    `directory` is a callable resolved on every call, not a path captured
    once: `state_dir()` reads its override out of the environment each time it
    runs, and a path bound at import would pin whichever value was set when
    the module first loaded -- in the test suite, whatever the first test to
    import it happened to have.
    """

    def log(message: str) -> None:
        """Append one line, or give up quietly.

        Quietly is deliberate: if the log is unwritable there is nowhere left
        to report to, and failing the caller to announce it is the worse trade.
        """
        try:
            target = directory()
            target.mkdir(parents=True, exist_ok=True)
            stamp = time.strftime("%Y-%m-%dT%H:%M:%S")
            path = target / filename
            try:
                overgrown = path.stat().st_size > LOG_CAP_BYTES
            except OSError:
                overgrown = False
            mode = "w" if overgrown else "a"
            with path.open(mode, encoding="utf-8") as handle:
                if overgrown:
                    handle.write(
                        f"{stamp} (earlier entries dropped: the log passed its size cap)\n"
                    )
                handle.write(f"{stamp} {message}\n")
        except Exception:
            pass

    return log
