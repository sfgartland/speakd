"""Tests for forgetting channels nobody has used in a long time."""

import time

from speakd.channels import ChannelTable
from speakd.daemon import Daemon, ProfileView
from speakd.events import EventBus
from speakd.player import FakeSink, Player, RecordingPlayer, StreamingPlayer
from speakd.protocol import Request, Verb
from speakd.synth.fake import FakeEngine

HOUR = 3600.0


def build(player: Player | None = None) -> Daemon:
    return Daemon(
        FakeEngine(),
        player or RecordingPlayer(),
        lambda n: ProfileView(
            voice="v", speed=1.0, interrupt_on=(), prepare=lambda p: (list(p), [])
        ),
        bus=EventBus(),
        channels=ChannelTable(),
    )


def listed(d: Daemon) -> list[str]:
    return [
        c["source_id"] for c in d.handle(Request(verb=Verb.STATUS, source_id="")).data["channels"]
    ]


def age(d: Daemon, source: str, hours: float) -> None:
    channel = d.channels.get(source)
    assert channel is not None
    channel.last_used = time.time() - hours * HOUR


def test_a_channel_unused_for_half_a_day_is_forgotten() -> None:
    d = build()
    d.handle(Request(verb=Verb.SET_LABEL, source_id="old", payload={"label": "Old"}))
    d.handle(Request(verb=Verb.SET_LABEL, source_id="new", payload={"label": "New"}))
    age(d, "old", 13)
    age(d, "new", 11)
    assert listed(d) == ["new"]


def test_using_a_channel_keeps_it() -> None:
    d = build()
    d.handle(Request(verb=Verb.SET_LABEL, source_id="kept", payload={"label": "Kept"}))
    age(d, "kept", 13)
    d.handle(Request(verb=Verb.MUTE, source_id="kept", payload={"muted": True}))
    assert "kept" in listed(d)


def test_a_channel_with_speech_waiting_is_never_forgotten() -> None:
    # Paused, so the utterance is still in flight when the list is read.
    d = build(StreamingPlayer(FakeSink()))
    d.start()
    try:
        d.handle(Request(verb=Verb.PAUSE, source_id=""))
        d.handle(
            Request(
                verb=Verb.ENQUEUE,
                source_id="busy",
                payload={"text": "One. Two.", "kind": "response"},
            )
        )
        age(d, "busy", 13)
        assert "busy" in listed(d)
    finally:
        d.handle(Request(verb=Verb.RESUME, source_id=""))
        d.stop()
