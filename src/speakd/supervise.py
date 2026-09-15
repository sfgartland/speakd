"""Keep a child process alive, without letting it take this one down.

The follower is a child of the daemon rather than a second service because
packaging is already the least portable part of this project: a second unit
would have to be written for systemd, launchd and Windows alike, where a child
is written once.

Every failure here is survivable by design. A follower that will not start
means a session is not spoken aloud; it must never mean the daemon stops
answering the socket for everyone else.
"""

from __future__ import annotations

import subprocess
import threading
from collections.abc import Sequence
from typing import Protocol

# Starts short, so restarting a wedged follower is not a visible wait, and
# caps low enough that a daemon left running overnight beside a permanently
# broken follower is not spawning in a tight loop.
DEFAULT_BACKOFF: tuple[float, ...] = (1.0, 2.0, 5.0, 10.0, 30.0)

# How long a dead follower stays dead before anyone notices. There is no
# portable "wait on a child with a deadline that another thread can break",
# so this is a poll; short enough that a crash is invisible next to the
# backoff that follows it, long enough to cost nothing while the child lives.
_POLL_SECONDS = 0.1


class ChildProcess(Protocol):
    """All supervision needs of a child, so that the seam below can be faked.

    `subprocess.Popen` satisfies this as it stands. Naming the three methods
    instead of the class is what lets a test substitute an object with no
    process behind it -- the alternative being tests that launch real
    processes to watch them die.
    """

    def poll(self) -> int | None: ...

    def terminate(self) -> None: ...

    def wait(self, timeout: float | None = None) -> int: ...


class Supervisor:
    def __init__(
        self,
        argv: Sequence[str],
        *,
        backoff: Sequence[float] = DEFAULT_BACKOFF,
    ) -> None:
        self._argv = list(argv)
        # An empty backoff would index out of range on the first failure and
        # kill the supervisor thread, which is the one outcome this class
        # exists to prevent.
        self._backoff = tuple(backoff) or (1.0,)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._child: ChildProcess | None = None
        self._started = 0
        # Guards the pair (`_child`, `_stop` has been set) so that stop() and
        # a spawn in flight cannot interleave into an unowned child; see the
        # handoff in `_run`. It also carries `_started` for `wait_for_children`.
        self._changed = threading.Condition()

    def _spawn(self) -> ChildProcess:
        """The seam tests replace. On POSIX the child gets its own session so
        a Ctrl-C in the daemon's terminal does not reach it directly; the
        daemon terminates it deliberately in `stop`.

        A Windows branch would pass
        `creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP` here. It is
        not written, because `transport.py` is AF_UNIX-only and a branch here
        would pretend the port is closer than it is.
        """
        return subprocess.Popen(self._argv, start_new_session=True)

    def start(self) -> None:
        if self._thread is not None:
            return
        # Cleared here rather than in stop(), so that a stop() before any
        # start() -- the shutdown path of a daemon that never got the
        # follower up -- does not leave the flag set for the next start().
        self._stop.clear()
        thread = threading.Thread(target=self._run, name="speakd-supervisor", daemon=True)
        self._thread = thread
        thread.start()

    def _run(self) -> None:
        attempt = 0
        while not self._stop.is_set():
            try:
                child = self._spawn()
            except OSError:
                # The follower's own absence is reported this way: a missing
                # interpreter, a missing module, a fork that the OS refused.
                # None of them is a reason for this thread to leave, because
                # a thread that has left never notices the cause being fixed.
                delay = self._backoff[min(attempt, len(self._backoff) - 1)]
                attempt += 1
                self._stop.wait(delay)
                continue
            attempt = 0
            with self._changed:
                self._started += 1
                self._changed.notify_all()
                # The handoff. stop() sets the flag and reads `_child` under
                # this same lock, so a child published after it has looked is
                # a child nobody would ever terminate: it would outlive the
                # daemon, keep tailing transcripts and keep talking to a
                # socket that has gone. Taking it down here instead is what
                # makes stop() total rather than merely likely.
                stopped = self._stop.is_set()
                if not stopped:
                    self._child = child
            if stopped:
                _terminate(child)
                return
            while not self._stop.is_set() and child.poll() is None:
                self._stop.wait(_POLL_SECONDS)
            if self._stop.is_set():
                return
            self._stop.wait(self._backoff[0])

    def wait_for_children(self, count: int, timeout: float) -> bool:
        """Block until `count` children have been spawned. Tests use it."""
        with self._changed:
            return self._changed.wait_for(lambda: self._started >= count, timeout)

    def stop(self) -> None:
        with self._changed:
            self._stop.set()
            child = self._child
            # Dropped while still holding it, so a second stop() -- and the
            # shutdown path calls this after the daemon may already have --
            # does not terminate a child a third party has since reaped.
            self._child = None
        # Outside the lock: terminate() and wait() talk to the OS and can
        # block for the full timeout, and the supervisor thread needs the
        # lock to reach the handoff above and retire itself.
        _terminate(child)
        thread = self._thread
        if thread is not None:
            # Bounded, like every other join in this codebase: a shutdown
            # that hangs on supervision is worse than a child that outlives
            # it, which the OS reclaims anyway.
            thread.join(timeout=5.0)
        self._thread = None


def _terminate(child: ChildProcess | None) -> None:
    if child is None or child.poll() is not None:
        return
    try:
        child.terminate()
        child.wait(timeout=5.0)
    except Exception:
        # Best effort: the OS reclaims it when this process exits, and
        # raising here would turn a clean shutdown into a traceback.
        pass
