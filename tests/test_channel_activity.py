"""Tests for when each channel last tried to speak."""

import time

from speakd.channels import ChannelTable
from speakd.daemon import Daemon, ProfileView
from speakd.events import Event, EventBus
from speakd.player import RecordingPlayer
from speakd.protocol import Request, Verb
from speakd.synth.fake import FakeEngine


def build() -> Daemon:
    d = Daemon(
        FakeEngine(),
        RecordingPlayer(),
        lambda name: ProfileView(
            voice="v", speed=1.0, interrupt_on=(), prepare=lambda p: (list(p), [])
        ),
        bus=EventBus(),
        channels=ChannelTable(),
    )
    d.start()
    return d


def say(d: Daemon, source: str) -> None:
    d.handle(
        Request(verb=Verb.ENQUEUE, source_id=source, payload={"text": "Hi.", "kind": "response"})
    )


def channel(d: Daemon, source: str) -> dict[str, object]:
    channels = d.handle(Request(verb=Verb.STATUS, source_id="")).data["channels"]
    assert isinstance(channels, list)
    return next(c for c in channels if c["source_id"] == source)


def test_speaking_records_when() -> None:
    d = build()
    try:
        before = time.time()
        say(d, "a")
        assert before <= channel(d, "a")["last_output"] <= time.time()  # type: ignore[operator]
    finally:
        d.stop()


def test_a_muted_attempt_still_counts() -> None:
    d = build()
    try:
        d.handle(Request(verb=Verb.MUTE, source_id="a", payload={"muted": True}))
        say(d, "a")
        assert channel(d, "a")["last_output"] > 0  # type: ignore[operator]
    finally:
        d.stop()


def test_a_channel_that_never_spoke_reports_zero() -> None:
    d = build()
    try:
        d.handle(Request(verb=Verb.SET_LABEL, source_id="quiet", payload={"label": "Quiet"}))
        assert channel(d, "quiet")["last_output"] == 0.0
    finally:
        d.stop()


def test_events_carry_the_time() -> None:
    d = build()
    seen: list[Event] = []
    d.bus.subscribe(seen.append)
    try:
        say(d, "a")
        d.handle(Request(verb=Verb.MUTE, source_id="b", payload={"muted": True}))
        say(d, "b")
        assert d.wait_idle(timeout=5.0)
        stamped = [e for e in seen if e.kind in ("queued", "declined")]
        assert {e.kind for e in stamped} == {"queued", "declined"}
        assert all(isinstance(e.data.get("at"), float) for e in stamped)
    finally:
        d.stop()
