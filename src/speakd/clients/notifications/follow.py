"""Speak the desktop notifications you asked to hear.

The loop is four steps and deliberately dull: take what the bus says, ask the
rules whether it is wanted, record the answer either way, and hand the text to
the daemon. Everything difficult lives on one side or the other of it --
`monitor` knows about D-Bus and nothing about you; `rules` knows about you and
nothing about D-Bus.

A child of the daemon, like the Claude Code follower and for the same reason:
a second service would need a unit file for systemd, launchd and Windows
alike, where a child is written once.

Nothing raises out of the loop. One notification that cannot be handled must
not end the connector for every notification after it.
"""

from __future__ import annotations

import argparse
import signal
import threading
import time
from collections.abc import Callable, Iterable, Sequence
from contextlib import closing
from types import FrameType

from speakd.clients.log import logger_for
from speakd.clients.notifications import history, rules
from speakd.clients.notifications.monitor import (
    Monitor,
    Notification,
    busctl_owner,
    stream,
)
from speakd.clients.send import send as send_request
from speakd.paths import client_state_dir, default_notifications_path
from speakd.protocol import Request, Verb

_log = logger_for(lambda: client_state_dir("notifications"), "notify.log")

# Above Claude Code's default of zero. Priority orders the queue without
# cutting off what is already speaking, so a message from a person is said
# before the rest of an AI monologue and never over the top of it.
DEFAULT_PRIORITY = 10

Send = Callable[[str, str, dict[str, object]], str | None]


def _default_send(verb: str, source: str, payload: dict[str, object]) -> str | None:
    return send_request(Request(verb=Verb(verb), source_id=source, payload=payload))


class Narrator:
    """One notification in, at most one utterance out."""

    def __init__(
        self,
        ruleset: rules.Ruleset,
        *,
        send: Send | None = None,
        priority: int = DEFAULT_PRIORITY,
        now: Callable[[], float] = time.time,
        log: Callable[[str], None] | None = None,
    ) -> None:
        self._rules = ruleset
        self._send: Send = send if send is not None else _default_send
        self._limit = rules.RateLimit(ruleset.max_per_minute, now=now)
        self._priority = priority
        self._now = now
        self._log = log if log is not None else _log
        # Channels whose label and priority have been set. Sending them again
        # for every notification would be a round trip per message saying
        # what the daemon already knows.
        self._announced: set[str] = set()

    def handle(self, note: Notification) -> None:
        verdict = rules.decide(note, self._rules)
        if not verdict.speak:
            self._remember(note, verdict, spoken=False, reason=verdict.reason)
            return
        if not self._limit.allow():
            # Dropped rather than queued. A group chat waking up produces
            # forty notifications in a minute, and a queue of forty is not
            # something anyone sits through -- they would reach for the
            # daemon's off switch, which costs them Claude Code as well.
            reason = f"over the limit of {self._rules.max_per_minute} a minute"
            self._remember(note, verdict, spoken=False, reason=reason)
            self._log(f"dropped a {note.app} notification: {reason}")
            return
        self._announce(verdict)
        failure = self._send(
            "enqueue",
            verdict.channel,
            # `kind` so that a channel someone has put into the background
            # can be told to interrupt for notifications and nothing else.
            {"text": verdict.text, "profile": verdict.profile, "kind": "notification"},
        )
        if failure is not None:
            self._log(f"could not speak a {note.app} notification: {failure}")
        self._remember(note, verdict, spoken=failure is None, reason=failure or "")

    def run(self, notes: Iterable[Notification]) -> None:
        """Handle notifications until the source runs out.

        An iterable rather than a monitor, so the loop knows nothing about
        D-Bus or subprocesses and a test can drive it with a list. `main`
        passes `stream(...)`, which is the only place a process is spawned.
        """
        for note in notes:
            try:
                self.handle(note)
            except Exception as exc:
                # One notification that cannot be handled must not end the
                # connector for every notification after it.
                self._log(f"failed on a {note.app} notification: {exc!r}")

    def _announce(self, verdict: rules.Verdict) -> None:
        """Name the channel and set its standing, once."""
        if verdict.channel in self._announced:
            return
        # Both are best-effort: a daemon that refuses a label is still a
        # daemon that will speak, and the utterance is what matters. Recorded
        # as announced either way, so a daemon that is down does not turn
        # every notification into three failed round trips.
        self._announced.add(verdict.channel)
        if verdict.label:
            self._send("set_label", verdict.channel, {"label": verdict.label})
        self._send("set_priority", verdict.channel, {"priority": self._priority})

    def _remember(
        self,
        note: Notification,
        verdict: rules.Verdict,
        *,
        spoken: bool,
        reason: str,
    ) -> None:
        history.record(
            history.Entry(
                when=note.when or self._now(),
                app=note.app,
                summary=note.summary,
                body=note.body,
                spoken=spoken,
                rule=verdict.rule,
                reason=reason,
            )
        )


def load_ruleset(log: Callable[[str], None] | None = None) -> rules.Ruleset | None:
    """The rules, or `None` if the file is there but unreadable.

    A malformed file speaks nothing, where `build_profiles` falls back to its
    default. The asymmetry is deliberate: falling back on a profile costs you
    its transforms, while falling back here would read aloud the very app you
    wrote a rule to silence. Silence is the safe direction, and the log says
    why it went quiet.
    """
    report = log if log is not None else _log
    path = default_notifications_path()
    try:
        return rules.load_rules(path)
    except ValueError as exc:
        report(f"speaking nothing: {path} could not be read: {exc}")
        return None


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="speakd-notify-follow", description=__doc__)
    parser.add_argument(
        "--priority",
        type=int,
        default=DEFAULT_PRIORITY,
        help="queue standing for notification channels (default: %(default)s)",
    )
    args = parser.parse_args(argv)

    ruleset = load_ruleset()
    if ruleset is None:
        return 1

    stop = threading.Event()

    def on_signal(_signum: int, _frame: FrameType | None) -> None:
        stop.set()

    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGTERM, on_signal)

    try:
        # `closing`, not a bare `for`: `stream` is a generator whose `finally`
        # reaps busctl, and that only runs when it is exhausted, closed or
        # collected. An exception leaving `run` would otherwise leave the
        # generator suspended and a busctl reading the bus for nobody until
        # the garbage collector happened to notice.
        with closing(stream(Monitor(owner=busctl_owner), stop)) as notes:
            Narrator(ruleset, priority=args.priority).run(notes)
    except FileNotFoundError:
        # `busctl` is how this connector hears anything at all. Reported and
        # left to the supervisor's backoff rather than retried here, so that
        # installing systemd-free is a line in a log rather than a silence
        # nobody can account for.
        _log("busctl is not installed; no notification can be heard without it")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
