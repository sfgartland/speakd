"""Tests for the bus half of the notification connector.

Strings and a fake clock throughout: no test spawns a process or touches the
real bus, which is the whole reason `feed` carries the judgement and `stream`
carries nothing but a pipe.

The three captured lines below are verbatim `busctl --user monitor
--json=short` output from this machine -- one `notify-send` crossing the bus
twice, plus the `Hello` that precedes it -- because a hand-written sample
would only ever prove the parser agrees with the person who wrote it.
"""

from __future__ import annotations

import io
import json
import subprocess
import sys
import threading
import time
from typing import Any

import pytest

from speakd.clients.notifications.monitor import (
    DEDUP_MAX_ENTRIES,
    Monitor,
    Notification,
    _parse_owner,
    busctl_owner,
    stream,
)

OWNER = ":1.32"
APP = ":1.1137"
SHELL = ":1.19"

BODY = 'line one\nline two with "quotes" and a  backslash'

FIRST_HOP = r'{"type":"method_call","endian":"l","flags":0,"version":1,"cookie":9,"timestamp-realtime":1789474326094064,"sender":":1.1137","destination":":1.32","path":"/org/freedesktop/Notifications","interface":"org.freedesktop.Notifications","member":"Notify","payload":{"type":"susssasa{sv}i","data":["Google Chrome",0,"","Mamma","line one\nline two with \"quotes\" and a  backslash",[],{"urgency":{"type":"y","data":1},"sender-pid":{"type":"x","data":1813045}},-1]}}'  # noqa: E501 - verbatim capture; wrapping it would stop being the capture

RELAY = r'{"type":"method_call","endian":"l","flags":0,"version":1,"cookie":2291,"timestamp-realtime":1789474326099242,"sender":":1.32","destination":":1.19","path":"/org/freedesktop/Notifications","interface":"org.freedesktop.Notifications","member":"Notify","payload":{"type":"susssasa{sv}i","data":["Google Chrome",0,"","Mamma","line one\nline two with \"quotes\" and a  backslash",[],{"urgency":{"type":"y","data":1},"sender-pid":{"type":"x","data":1813045},"x-shell-sender-pid":{"type":"u","data":1813045},"x-shell-sender":{"type":"s","data":":1.1137"}},-1]}}'  # noqa: E501 - verbatim capture; wrapping it would stop being the capture

HELLO = r'{"type":"method_call","endian":"l","flags":0,"version":1,"cookie":1,"timestamp-realtime":1789474326077740,"sender":":1.1137","destination":"org.freedesktop.DBus","path":"/org/freedesktop/DBus","interface":"org.freedesktop.DBus","member":"Hello","payload":{"type":"","data":[]}}'  # noqa: E501 - verbatim capture; wrapping it would stop being the capture


class Clock:
    def __init__(self, start: float = 1000.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


class Owner:
    """A fake name resolver that counts how often it was asked."""

    def __init__(self, name: str | None = OWNER) -> None:
        self.name = name
        self.calls = 0

    def __call__(self) -> str | None:
        self.calls += 1
        return self.name


def monitor(owner: Owner, clock: Clock, **kwargs: float) -> Monitor:
    return Monitor(owner=owner, now=clock, **kwargs)


def notify_line(
    *,
    sender: str = APP,
    destination: str = OWNER,
    app: str = "Google Chrome",
    summary: str = "Mamma",
    body: str = "hello",
    hints: dict[str, Any] | None = None,
    signature: str = "susssasa{sv}i",
    member: str = "Notify",
    interface: str = "org.freedesktop.Notifications",
    kind: str = "method_call",
) -> str:
    if hints is None:
        hints = {"urgency": {"type": "y", "data": 1}}
    return json.dumps(
        {
            "type": kind,
            "sender": sender,
            "destination": destination,
            "path": "/org/freedesktop/Notifications",
            "interface": interface,
            "member": member,
            "payload": {
                "type": signature,
                "data": [app, 0, "", summary, body, [], hints, -1],
            },
        }
    )


def test_the_first_hop_is_accepted_and_every_field_extracted() -> None:
    clock = Clock()
    note = monitor(Owner(), clock).feed(FIRST_HOP)
    assert note == Notification(
        app="Google Chrome",
        summary="Mamma",
        body=BODY,
        urgency=1,
        desktop_entry="",
        when=clock.t,
    )
    # The body is why busctl was chosen over dbus-monitor: a newline and a
    # pair of quotes survive the round trip intact.
    assert note is not None
    assert note.body.splitlines() == ["line one", 'line two with "quotes" and a  backslash']


def test_the_relay_is_dropped_because_its_sender_owns_the_name() -> None:
    # The dedup window has long expired, so anything dropped here is dropped
    # by the owner rule alone.
    clock = Clock()
    owner = Owner()
    m = monitor(owner, clock)
    assert m.feed(FIRST_HOP) is not None
    clock.advance(600.0)
    assert m.feed(RELAY) is None
    # Dropping the daemon's own relay must not cost a re-resolve, however
    # long the monitor has been running.
    assert owner.calls == 1


def test_the_relay_is_dropped_by_a_monitor_that_never_saw_the_first_hop() -> None:
    # An empty dedup table: there is nothing for dedup to match against, so
    # this can only be the sender rule.
    assert monitor(Owner(), Clock()).feed(RELAY) is None


def test_a_repeat_is_dropped_inside_the_dedup_window_and_allowed_after() -> None:
    clock = Clock()
    m = monitor(Owner(), clock, dedup_seconds=2.0)
    assert m.feed(notify_line()) is not None
    clock.advance(1.9)
    assert m.feed(notify_line()) is None
    clock.advance(0.1)
    # Exactly the window: the second delivery of a genuine repeat is over.
    assert m.feed(notify_line()) is not None


def test_dedup_keys_on_app_summary_and_body_together() -> None:
    clock = Clock()
    m = monitor(Owner(), clock)
    assert m.feed(notify_line(summary="one")) is not None
    assert m.feed(notify_line(summary="two")) is not None
    assert m.feed(notify_line(body="other")) is not None
    assert m.feed(notify_line(app="Thunderbird")) is not None
    assert m.feed(notify_line(summary="one")) is None


def test_the_dedup_table_stays_bounded() -> None:
    clock = Clock()
    m = monitor(Owner(), clock)
    for index in range(2000):
        assert m.feed(notify_line(summary=f"message {index}")) is not None
        clock.advance(0.25)
    assert len(m._seen) <= DEDUP_MAX_ENTRIES


def test_the_dedup_table_still_suppresses_after_it_has_been_pruned() -> None:
    clock = Clock()
    m = monitor(Owner(), clock)
    for index in range(2000):
        m.feed(notify_line(summary=f"message {index}"))
        clock.advance(0.25)
    assert m.feed(notify_line(summary="fresh")) is not None
    assert m.feed(notify_line(summary="fresh")) is None


def test_traffic_that_is_not_a_notification_is_ignored() -> None:
    m = monitor(Owner(), Clock())
    assert m.feed(HELLO) is None
    assert m.feed(notify_line(member="CloseNotification")) is None
    assert m.feed(notify_line(interface="org.freedesktop.DBus.Properties")) is None
    assert m.feed(notify_line(kind="method_return")) is None


def test_malformed_input_returns_none_rather_than_raising() -> None:
    m = monitor(Owner(), Clock())
    assert m.feed("") is None
    assert m.feed("\n") is None
    assert m.feed("   ") is None
    assert m.feed("not json at all") is None
    assert m.feed('{"type":"method_call",') is None
    assert m.feed("[1, 2, 3]") is None
    assert m.feed("null") is None


def test_a_payload_of_the_wrong_signature_is_never_indexed() -> None:
    m = monitor(Owner(), Clock())
    # A `Notify` whose payload does not have Notify's shape: the arguments
    # below would raise IndexError under a parser that trusted the member.
    assert m.feed(notify_line(signature="s")) is None
    assert m.feed(json.dumps(_notify_record(payload={"type": "susssasa{sv}i"}))) is None
    assert m.feed(json.dumps(_notify_record(payload={"type": "sus", "data": ["one"]}))) is None
    assert m.feed(json.dumps(_notify_record(payload=["not", "a", "dict"]))) is None
    assert m.feed(json.dumps(_notify_record(payload=None))) is None
    short: dict[str, Any] = {"type": "susssasa{sv}i", "data": ["Chrome", 0, ""]}
    assert m.feed(json.dumps(_notify_record(payload=short))) is None


def _notify_record(*, payload: Any) -> dict[str, Any]:
    return {
        "type": "method_call",
        "sender": APP,
        "destination": OWNER,
        "interface": "org.freedesktop.Notifications",
        "member": "Notify",
        "payload": payload,
    }


def test_missing_hints_default_to_normal_urgency_and_no_desktop_entry() -> None:
    m = monitor(Owner(), Clock())
    note = m.feed(notify_line(hints={}))
    assert note is not None
    assert note.urgency == 1
    assert note.desktop_entry == ""


def test_hints_are_unwrapped_from_their_type_and_data() -> None:
    m = monitor(Owner(), Clock())
    note = m.feed(
        notify_line(
            hints={
                "urgency": {"type": "y", "data": 2},
                "desktop-entry": {"type": "s", "data": "google-chrome"},
            }
        )
    )
    assert note is not None
    assert note.urgency == 2
    assert note.desktop_entry == "google-chrome"


def test_hints_of_the_wrong_shape_fall_back_to_the_defaults() -> None:
    m = monitor(Owner(), Clock())
    note = m.feed(notify_line(hints={"urgency": 2, "desktop-entry": {"type": "s", "data": 7}}))
    assert note is not None
    assert note.urgency == 1
    assert note.desktop_entry == ""


def test_an_owner_change_is_re_resolved_once_and_then_accepted() -> None:
    clock = Clock()
    owner = Owner()
    m = monitor(owner, clock, reresolve_seconds=5.0)
    assert m.feed(notify_line(summary="before")) is not None
    assert owner.calls == 1

    owner.name = ":1.50"  # the shell restarted; a new unique name owns it
    clock.advance(5.0)
    note = m.feed(notify_line(destination=":1.50", summary="after"))
    assert note is not None
    assert note.summary == "after"
    assert owner.calls == 2

    # The new owner is cached: its relay is dropped without asking again.
    assert m.feed(notify_line(sender=":1.50", destination=SHELL, summary="relayed")) is None
    assert owner.calls == 2


def test_re_resolution_is_rate_limited() -> None:
    clock = Clock()
    owner = Owner()
    m = monitor(owner, clock, reresolve_seconds=5.0)
    # Ten seconds of traffic that matches neither end, in steps that are
    # exact in binary so the window boundary is not a rounding question.
    for index in range(40):
        m.feed(notify_line(sender=":1.98", destination=":1.99", summary=f"n{index}"))
        clock.advance(0.25)
    # One resolve to learn the owner, one to check whether it had changed --
    # not one per line.
    assert owner.calls == 2


def test_a_line_matching_neither_end_is_still_spoken() -> None:
    # Whoever owns the name, a notification nobody can attribute is better
    # heard once than lost; the dedup window covers the doubling.
    clock = Clock()
    owner = Owner()
    m = monitor(owner, clock)
    assert m.feed(notify_line(sender=":1.98", destination=":1.99")) is not None


def test_an_unknown_owner_falls_back_to_dedup_alone() -> None:
    clock = Clock()
    owner = Owner(None)
    m = monitor(owner, clock)
    assert m.feed(FIRST_HOP) is not None
    # Same notification, so dedup takes it even though the sender rule cannot.
    assert m.feed(RELAY) is None
    clock.advance(600.0)
    assert m.feed(RELAY) is not None


def test_an_owner_lookup_that_raises_is_not_fatal() -> None:
    def boom() -> str | None:
        raise OSError("bus is gone")

    m = Monitor(owner=boom, now=Clock())
    assert m.feed(FIRST_HOP) is not None


def test_parsing_the_owner_out_of_busctl_call_output() -> None:
    assert _parse_owner('s ":1.32"\n') == ":1.32"
    assert _parse_owner('s ":1.32"') == ":1.32"
    assert _parse_owner('s ""\n') is None
    assert _parse_owner("") is None
    assert _parse_owner("s :1.32") is None


def test_busctl_owner_reports_none_on_every_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    def missing(*args: Any, **kwargs: Any) -> None:
        raise FileNotFoundError("busctl")

    monkeypatch.setattr(subprocess, "run", missing)
    assert busctl_owner() is None

    def timed_out(*args: Any, **kwargs: Any) -> None:
        raise subprocess.TimeoutExpired(cmd="busctl", timeout=1.0)

    monkeypatch.setattr(subprocess, "run", timed_out)
    assert busctl_owner() is None

    def no_owner(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="no owner")

    monkeypatch.setattr(subprocess, "run", no_owner)
    assert busctl_owner() is None


def test_busctl_owner_returns_the_unique_name(monkeypatch: pytest.MonkeyPatch) -> None:
    def answered(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args=[], returncode=0, stdout='s ":1.32"\n', stderr="")

    monkeypatch.setattr(subprocess, "run", answered)
    assert busctl_owner() == ":1.32"


class FakePopen:
    """Enough of `Popen` to stream lines and be terminated. Spawns nothing."""

    lines: list[str] = []
    last: FakePopen | None = None

    def __init__(self, argv: list[str], **kwargs: Any) -> None:
        self.argv = argv
        # A real file object rather than a list iterator: `stream` closes the
        # pipe it was handed, and a fake that cannot be closed would hide it.
        self.stdout = io.StringIO("".join(FakePopen.lines))
        self.terminated = False
        self.killed = False
        self.alive = True
        FakePopen.last = self

    def poll(self) -> int | None:
        return None if self.alive else 0

    def terminate(self) -> None:
        self.terminated = True
        self.alive = False

    def kill(self) -> None:
        self.killed = True
        self.alive = False

    def wait(self, timeout: float | None = None) -> int:
        return 0


def _fake_bus(monkeypatch: pytest.MonkeyPatch, lines: list[str]) -> None:
    FakePopen.lines = lines
    FakePopen.last = None
    monkeypatch.setattr(subprocess, "Popen", FakePopen)


def test_stream_yields_what_feed_accepts_and_reaps_the_child(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fake_bus(monkeypatch, [HELLO + "\n", FIRST_HOP + "\n", RELAY + "\n"])
    m = monitor(Owner(), Clock())
    notes = list(stream(m, threading.Event()))
    assert [note.summary for note in notes] == ["Mamma"]
    assert FakePopen.last is not None
    assert FakePopen.last.terminated


def test_stream_terminates_the_child_when_the_generator_is_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fake_bus(monkeypatch, [FIRST_HOP + "\n"] * 3)
    notes = stream(monitor(Owner(), Clock()), threading.Event())
    next(notes)
    notes.close()
    assert FakePopen.last is not None
    assert FakePopen.last.terminated


def test_stream_terminates_the_child_when_feed_is_interrupted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fake_bus(monkeypatch, [FIRST_HOP + "\n"])

    class Exploding(Monitor):
        def feed(self, line: str) -> Notification | None:
            raise RuntimeError("never mind why")

    with pytest.raises(RuntimeError):
        list(stream(Exploding(owner=Owner()), threading.Event()))
    assert FakePopen.last is not None
    assert FakePopen.last.terminated


def test_stream_stops_when_asked(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_bus(monkeypatch, [FIRST_HOP + "\n", notify_line(summary="second") + "\n"])
    stop = threading.Event()
    stop.set()
    assert list(stream(monitor(Owner(), Clock()), stop)) == []


def test_stream_passes_the_default_argv(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_bus(monkeypatch, [])
    list(stream(monitor(Owner(), Clock()), threading.Event()))
    assert FakePopen.last is not None
    assert FakePopen.last.argv[0] == "busctl"
    assert "org.freedesktop.Notifications" in FakePopen.last.argv
    assert "--json=short" in FakePopen.last.argv
    assert not any("stdbuf" in part for part in FakePopen.last.argv)


def _recording_popen(monkeypatch: pytest.MonkeyPatch) -> list[subprocess.Popen[str]]:
    """Spawn for real, but keep the handle, so a leak can be proved."""
    real = subprocess.Popen
    created: list[subprocess.Popen[str]] = []

    def spy(*args: Any, **kwargs: Any) -> subprocess.Popen[str]:
        process: subprocess.Popen[str] = real(*args, **kwargs)
        created.append(process)
        return process

    monkeypatch.setattr(subprocess, "Popen", spy)
    return created


def test_stream_returns_when_stopped_while_blocked_on_a_silent_child(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The one test here that spawns a process, because it must block.

    A child that never writes a line leaves the reader inside a blocking
    read, where every fake above hands it EOF instead. `stop` has to reach
    through that read: `follow.py`'s SIGTERM handler only sets it, so a
    `stream` that waits for one more line means SIGTERM no longer ends the
    follower -- the supervisor times out, swallows it, and an orphaned busctl
    and an orphaned follower go on reading someone's mail aloud into a socket
    that has gone. Measured before the fix: still running at 25 s.
    """
    created = _recording_popen(monkeypatch)
    silent = [sys.executable, "-c", "import time; time.sleep(30)"]
    stop = threading.Event()
    notes = stream(monitor(Owner(), Clock()), stop, argv=silent)
    drained = threading.Event()

    def drain() -> None:
        list(notes)
        drained.set()

    worker = threading.Thread(target=drain, daemon=True)
    worker.start()
    try:
        # Let the child exist before asking for it to go away.
        deadline = time.monotonic() + 5.0
        while not created and time.monotonic() < deadline:
            time.sleep(0.01)
        assert len(created) == 1
        stop.set()
        worker.join(timeout=5.0)
        assert drained.is_set(), "stream did not return when stop was set"
        # A stream that returns while leaving busctl running has moved the
        # leak, not closed it.
        assert created[0].poll() is not None
    finally:
        for process in created:
            process.kill()
            process.wait(timeout=5.0)


def test_a_missing_busctl_is_reported_rather_than_swallowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def missing(*args: Any, **kwargs: Any) -> None:
        raise FileNotFoundError("busctl")

    monkeypatch.setattr(subprocess, "Popen", missing)
    with pytest.raises(FileNotFoundError):
        list(stream(monitor(Owner(), Clock()), threading.Event()))
