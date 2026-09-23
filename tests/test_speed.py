"""Tests for the listener's speed switch."""

import numpy as np
import pytest

from speakd import state
from speakd.channels import ChannelTable
from speakd.daemon import Daemon, ProfileView
from speakd.events import Event, EventBus
from speakd.player import RecordingPlayer
from speakd.protocol import Request, Response, Verb
from speakd.synth.fake import FakeEngine


def profile_for(name: str) -> ProfileView:
    return ProfileView(
        voice="af_heart", speed=1.1, interrupt_on=(), prepare=lambda p: (list(p), [])
    )


class SpeedEngine(FakeEngine):
    def __init__(self) -> None:
        super().__init__()
        self.speeds: list[float] = []

    def synthesize(self, text: str, voice: str, speed: float) -> np.ndarray:
        self.speeds.append(speed)
        return super().synthesize(text, voice, speed)


def build(engine: FakeEngine | None = None) -> Daemon:
    return Daemon(
        engine or FakeEngine(),
        RecordingPlayer(),
        profile_for,
        bus=EventBus(),
        channels=ChannelTable(),
    )


def set_speed(d: Daemon, value: object) -> Response:
    return d.handle(Request(verb=Verb.SET_SPEED, source_id="", payload={"speed": value}))


def test_set_speed_clamps_and_answers_what_it_applied() -> None:
    d = build()
    assert set_speed(d, 9).data == {"speed": 1.6}
    assert set_speed(d, 1.23).data == {"speed": 1.25}


@pytest.mark.parametrize("bad", [None, "fast", True, 0, -1, float("nan")])
def test_set_speed_refuses_what_is_not_a_positive_number(bad: object) -> None:
    assert not set_speed(build(), bad).ok


def test_speed_is_announced_reported_and_kept() -> None:
    d = build()
    seen: list[Event] = []
    d.bus.subscribe(seen.append)
    set_speed(d, 1.3)
    assert [e.data for e in seen if e.kind == "speed"] == [{"speed": 1.3}]
    assert d.handle(Request(verb=Verb.STATUS, source_id="")).data["speed"] == 1.3
    assert build().tempo.value == 1.3


def test_setting_speed_leaves_the_mute_flag_alone() -> None:
    d = build()
    d.handle(Request(verb=Verb.MUTE, source_id="", payload={"muted": True}))
    set_speed(d, 1.2)
    assert state.load().muted is True


def test_speech_is_synthesised_at_the_multiplied_speed() -> None:
    engine = SpeedEngine()
    d = build(engine)
    set_speed(d, 1.5)
    d.start()
    try:
        d.handle(
            Request(verb=Verb.ENQUEUE, source_id="s", payload={"text": "One.", "kind": "response"})
        )
        assert d.wait_idle(timeout=5.0)
    finally:
        d.stop()
    assert engine.speeds == [pytest.approx(1.65)]
