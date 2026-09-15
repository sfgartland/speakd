"""Desktop notifications, off the session bus and into a record.

`busctl --user monitor --json=short` is the source because it is the only one
measured to survive a real message: `dbus-monitor` prints string arguments
raw, so a two-line WhatsApp body cannot be delimited back out of it, and a
D-Bus binding would be a new dependency in a virtualenv whose hand-installed
torch does not survive a resolver pass.

Every notification crosses this bus twice -- the app to the notification
daemon, that daemon onward to the shell -- so read naively the stream says
everything aloud twice. Which copy is which is decided here, in `feed`,
against the current owner of `org.freedesktop.Notifications`. `stream` holds
the pipe and nothing else. That split is what lets the rule be exercised with
strings rather than a live desktop, and it is why the owner arrives as a
callable.

Nothing in `feed` raises. A line that is not JSON, or is JSON in a shape no
busctl has emitted, costs one notification; an exception out of it costs every
notification for the rest of the day.
"""

from __future__ import annotations

import json
import subprocess
import threading
import time
from collections.abc import Callable, Generator, Sequence
from dataclasses import dataclass

# No `stdbuf`. busctl was measured flushing per message -- 40 ms from
# `notify-send` to a parsed record -- and wrapping it would only hide the day
# that stops being true.
DEFAULT_ARGV: tuple[str, ...] = (
    "busctl",
    "--user",
    "monitor",
    "--json=short",
    "org.freedesktop.Notifications",
)

_OWNER_ARGV: tuple[str, ...] = (
    "busctl",
    "--user",
    "call",
    "org.freedesktop.DBus",
    "/org/freedesktop/DBus",
    "org.freedesktop.DBus",
    "GetNameOwner",
    "s",
    "org.freedesktop.Notifications",
)

_NOTIFY_INTERFACE = "org.freedesktop.Notifications"

# Notify's D-Bus signature: app_name, replaces_id, app_icon, summary, body,
# actions, hints, expire_timeout. Checked before the arguments are touched --
# `member == "Notify"` is a name anyone may put on the bus, the signature is
# what says the arguments are the ones this module knows how to read.
_NOTIFY_SIGNATURE = "susssasa{sv}i"
_NOTIFY_ARGUMENTS = 8

# Enough distinct notifications to cover any plausible burst inside one dedup
# window, and small enough that a machine left running for a week is holding
# kilobytes. The table is a suppression aid, not a record: §4's history is
# what remembers what arrived.
DEDUP_MAX_ENTRIES = 256

# Short on purpose: this runs on the path of a notification waiting to be
# spoken, and an unanswered lookup is survivable -- dedup covers what the
# owner rule then cannot.
_OWNER_TIMEOUT_SECONDS = 1.0

_TERMINATE_TIMEOUT_SECONDS = 5.0

# How long the thread watching for `stop` sleeps between checks that the
# stream it is watching is still running. A poll, for the reason `supervise`
# polls: there is no portable wait on two events at once, and the alternative
# -- a watcher that only ever waits on `stop` -- outlives every stream that
# ends for any other reason.
_STOP_POLL_SECONDS = 0.1


@dataclass(frozen=True)
class Notification:
    app: str
    summary: str
    body: str
    urgency: int = 1  # 0 low, 1 normal, 2 critical
    desktop_entry: str = ""  # the "desktop-entry" hint, "" if absent
    when: float = 0.0


class Monitor:
    """The double-delivery rule and the dedup window, and nothing else.

    Pure given its clock and its owner callable, which is the point: the two
    rules that decide whether a notification is heard once, twice or not at
    all are the ones hardest to get right and the ones a live bus is worst at
    demonstrating.
    """

    def __init__(
        self,
        *,
        owner: Callable[[], str | None],
        dedup_seconds: float = 2.0,
        reresolve_seconds: float = 5.0,
        now: Callable[[], float] = time.time,
    ) -> None:
        self._owner_of = owner
        self._dedup_seconds = dedup_seconds
        self._reresolve_seconds = reresolve_seconds
        self._now = now
        self._owner: str | None = None
        # Distinct from `_owner is None`, which is also what an unanswerable
        # lookup leaves behind. Without it there would be no way to tell the
        # first line of the day from a name nobody owns.
        self._resolved = False
        self._resolved_at = 0.0
        self._seen: dict[tuple[str, str, str], float] = {}

    def feed(self, line: str) -> Notification | None:
        """One line of busctl JSON in; a notification worth considering out."""
        record = _notify_call(line)
        if record is None:
            return None
        arguments = _arguments(record)
        if arguments is None:
            return None
        now = self._now()
        if not self._addressed_to_the_owner(record, now):
            return None
        hints = arguments[6] if isinstance(arguments[6], dict) else {}
        note = Notification(
            app=_text(arguments[0]),
            summary=_text(arguments[3]),
            body=_text(arguments[4]),
            urgency=_urgency(hints),
            desktop_entry=_desktop_entry(hints),
            when=now,
        )
        if self._is_repeat(note, now):
            return None
        return note

    def _addressed_to_the_owner(self, record: dict[str, object], now: float) -> bool:
        """§2's rule: a notification is a Notify addressed *to* the owner."""
        sender = record.get("sender")
        destination = record.get("destination")
        if not self._resolved:
            self._resolve(now)
        verdict = self._verdict(sender, destination)
        if verdict is not None:
            return verdict
        # Neither end is the owner as last known, so the name may have moved:
        # a shell restart or an extension reload hands it to a new unique
        # name. Resolving costs a subprocess, so it happens at most once per
        # `reresolve_seconds` no matter how much traffic arrives -- a chatty
        # group chat must not turn into a lookup per message.
        if now - self._resolved_at >= self._reresolve_seconds:
            self._resolve(now)
            verdict = self._verdict(sender, destination)
            if verdict is not None:
                return verdict
        # Still unattributable. Accepted, not dropped: the cost of accepting
        # is hearing a relay that the dedup window behind this takes anyway,
        # and the cost of dropping is silence with nothing to explain it.
        return True

    def _verdict(self, sender: object, destination: object) -> bool | None:
        """True to accept, False to drop, None if the owner cannot say."""
        owner = self._owner
        if owner is None:
            # Nobody answered GetNameOwner. Not an answer of its own: say
            # nothing here and let the caller re-resolve and then accept.
            return None
        if destination == owner:
            # Whatever sent it -- including xdg-desktop-portal relaying a
            # sandboxed app, which is how a Flatpak browser appears.
            return True
        if sender == owner:
            # The daemon's own relay outward. Deliberately no re-resolve:
            # this is the one case that is already fully explained.
            return False
        return None

    def _resolve(self, now: float) -> None:
        self._resolved = True
        # Stamped whether or not the answer is useful, so a bus that keeps
        # refusing is asked on the same slow cadence as one that has simply
        # not changed hands.
        self._resolved_at = now
        try:
            self._owner = self._owner_of()
        except Exception:
            # The resolver shells out. A busctl that is missing or a bus that
            # is briefly unreachable reads the same as an unowned name here,
            # and neither may end the stream.
            self._owner = None

    def _is_repeat(self, note: Notification, now: float) -> bool:
        """The §2 backstop: the same text twice within `dedup_seconds`.

        Not redundant beside the owner rule. It covers a desktop that relays
        through a third hop, and every case above where the owner could not
        be attributed and the line was let through regardless.
        """
        key = (note.app, note.summary, note.body)
        last = self._seen.get(key)
        if last is not None and now - last < self._dedup_seconds:
            # Not restamped. A chat repeating one line every second would
            # otherwise be suppressed forever by a window that keeps sliding
            # out ahead of it.
            return True
        self._seen[key] = now
        self._bound_the_table(now)
        return False

    def _bound_the_table(self, now: float) -> None:
        """Keep `_seen` from growing for as long as the machine is up."""
        if len(self._seen) <= DEDUP_MAX_ENTRIES:
            return
        # Every key older than the window is dead weight: it can never
        # suppress anything again. Sweeping them in a batch when the table
        # grows keeps the per-line path free of bookkeeping.
        cutoff = now - self._dedup_seconds
        self._seen = {key: seen for key, seen in self._seen.items() if seen > cutoff}
        # A burst inside a single window can still be over the cap, and a
        # clock that went backwards can leave everything looking fresh.
        # Insertion order is age order here, so the oldest go first.
        while len(self._seen) > DEDUP_MAX_ENTRIES:
            self._seen.pop(next(iter(self._seen)))


def _notify_call(line: str) -> dict[str, object] | None:
    """One busctl line, if it is a call asking for a notification to be shown."""
    try:
        parsed = json.loads(line)
    except ValueError:
        # A blank line, a half-written line, a line of something else
        # entirely. One skipped line, never a dead stream.
        return None
    if not isinstance(parsed, dict):
        return None
    if parsed.get("type") != "method_call":
        return None
    if parsed.get("interface") != _NOTIFY_INTERFACE:
        return None
    if parsed.get("member") != "Notify":
        return None
    return parsed


def _arguments(record: dict[str, object]) -> list[object] | None:
    """Notify's eight arguments, or None if this payload is not them."""
    payload = record.get("payload")
    if not isinstance(payload, dict):
        return None
    if payload.get("type") != _NOTIFY_SIGNATURE:
        return None
    data = payload.get("data")
    # The signature has already promised eight arguments. This is the guard
    # for the busctl that one day disagrees with its own type string: the
    # alternative is an IndexError on the streaming path.
    if not isinstance(data, list) or len(data) != _NOTIFY_ARGUMENTS:
        return None
    return data


def _text(value: object) -> str:
    """A D-Bus string argument, or "" for anything that is not one."""
    return value if isinstance(value, str) else ""


def _hint(hints: dict[str, object], name: str) -> object:
    """The value inside a hint's `{"type": ..., "data": ...}` wrapper."""
    wrapper = hints.get(name)
    if not isinstance(wrapper, dict):
        return None
    return wrapper.get("data")


def _urgency(hints: dict[str, object]) -> int:
    raw = _hint(hints, "urgency")
    # Sent as a byte, so it arrives as a JSON number. `bool` is excluded
    # because it is an `int` in Python and `True` is not an urgency.
    if isinstance(raw, bool) or not isinstance(raw, int):
        return 1
    return raw


def _desktop_entry(hints: dict[str, object]) -> str:
    return _text(_hint(hints, "desktop-entry"))


def stream(
    monitor: Monitor,
    stop: threading.Event,
    argv: Sequence[str] | None = None,
) -> Generator[Notification, None, None]:
    """Notifications off the bus until `stop` is set.

    A `Generator` and not an `Iterator`, because `close()` is part of what
    this returns rather than an implementation detail: closing it is what
    reaps the child when a caller leaves the loop early, and a caller holding
    only an `Iterator` cannot do that -- nor wrap it in `contextlib.closing`,
    which is how `follow.py` guarantees it.

    A missing `busctl` raises `FileNotFoundError` out of here on purpose,
    alone among the failures in this module. The caller is a supervised child
    whose death is reported; a connector that is permanently silent with no
    explanation is the worst outcome this design has.
    """
    command = list(argv) if argv is not None else list(DEFAULT_ARGV)
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        # No `stderr=DEVNULL`: it is inherited deliberately, because busctl's
        # own complaint is the only account anyone gets of a bus it could not
        # attach to, and this child's stderr is already going to a log.
        text=True,
        bufsize=1,
        # A body is whatever an application put in it. One undecodable byte
        # costs that character, not the stream.
        errors="replace",
    )
    # The loop below spends nearly all of its life blocked inside a read, and
    # a desktop can be quiet for hours. Checking `stop` between lines is
    # therefore no check at all: the caller is an ordinary `for` loop blocked
    # in `next()`, so it cannot close this generator to break the read, and
    # the follower's SIGTERM handler -- which only sets `stop` -- would stop
    # ending the process. Measured: a stop over a silent bus hung until the
    # process was killed by hand. Terminating the child is what turns the
    # blocking read into EOF, and only another thread can do it from here.
    done = threading.Event()
    watcher = threading.Thread(
        target=_terminate_when_stopped,
        args=(process, stop, done),
        name="speakd-notify-stop",
        daemon=True,
    )
    watcher.start()
    try:
        stdout = process.stdout
        if stdout is None:  # pragma: no cover - stdout is a pipe, above
            return
        for line in stdout:
            # Still checked here, so a stop that lands between two busy lines
            # is honoured without waiting on the watcher's next poll.
            if stop.is_set():
                return
            note = monitor.feed(line)
            if note is not None:
                yield note
    finally:
        # Reached on a normal end, on an exception, and on `close()` of a
        # generator suspended at the yield. Without it a stopped follower
        # leaves a busctl behind, reading the bus for nobody.
        done.set()
        _terminate(process)
        # Bounded, like every join in this codebase. The watcher only ever
        # waits on the same child this thread has just reaped, so a join that
        # times out means something worse than a leaked thread is wrong.
        watcher.join(timeout=_TERMINATE_TIMEOUT_SECONDS)
        # Closed last, and only here: this is the thread that was reading it,
        # and closing a pipe out from under a blocked read is how a clean
        # shutdown turns into an exception in the caller's `for` loop.
        _close(process)


def _terminate_when_stopped(
    process: subprocess.Popen[str],
    stop: threading.Event,
    done: threading.Event,
) -> None:
    """Take the child down when `stop` is set, so the read above sees EOF."""
    while not done.is_set():
        if stop.wait(_STOP_POLL_SECONDS):
            # Racing the `finally`, deliberately: `_terminate` is idempotent
            # and silent, and a double terminate costs nothing next to a
            # stop that arrives one instruction before `done` is set and is
            # then never acted on at all.
            _terminate(process)
            return


def _terminate(process: subprocess.Popen[str]) -> None:
    """Kill the child and reap it. Safe to call twice, and from two threads."""
    try:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=_TERMINATE_TIMEOUT_SECONDS)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=_TERMINATE_TIMEOUT_SECONDS)
    except Exception:
        # Best effort, as in `supervise._terminate`: this runs in a `finally`
        # and anything raised here replaces whatever sent us there. A process
        # already reaped by the other caller arrives here as one of these.
        pass


def _close(process: subprocess.Popen[str]) -> None:
    if process.stdout is None:  # pragma: no cover - stdout is a pipe
        return
    try:
        process.stdout.close()
    except Exception:
        pass


def busctl_owner() -> str | None:
    """Who owns `org.freedesktop.Notifications` right now, if anyone.

    None for every failure alike -- no busctl, no owner, no answer in time --
    because `Monitor` does the same thing with all three: lean on dedup until
    the next lookup is due.
    """
    try:
        result = subprocess.run(
            _OWNER_ARGV,
            capture_output=True,
            text=True,
            timeout=_OWNER_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return _parse_owner(result.stdout)


def _parse_owner(output: str) -> str | None:
    """`s ":1.32"` -> `:1.32`.

    Delimited by the quotes rather than split on whitespace: the `s` is
    busctl's type prefix, and only the quotes say where the name begins and
    ends.
    """
    start = output.find('"')
    end = output.rfind('"')
    if start < 0 or end <= start:
        return None
    return output[start + 1 : end] or None
