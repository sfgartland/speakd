"""What a new prompt does to the reading in progress: `speech.on_prompt`."""

import threading
from collections.abc import Iterator

import numpy as np
import pytest

from speakd.channels import ChannelTable
from speakd.daemon import Daemon, ProfileView
from speakd.events import Event, EventBus
from speakd.player import RecordingPlayer
from speakd.protocol import Request, Response, Verb
from speakd.synth.fake import FakeEngine


class HoldingPlayer(RecordingPlayer):
    """Blocks in its first play() until released, signalling when it gets there."""

    def __init__(self, reached: threading.Event, release: threading.Event) -> None:
        super().__init__()
        self.reached = reached
        self.release = release

    def play(self, audio: np.ndarray, sample_rate: int) -> None:
        super().play(audio, sample_rate)
        if len(self.played) == 1:
            self.reached.set()
            assert self.release.wait(timeout=5), "test never released play()"


class Rig:
    def __init__(self) -> None:
        self.reached = threading.Event()
        self.release = threading.Event()
        self.player = HoldingPlayer(self.reached, self.release)
        self.seen: list[Event] = []
        bus = EventBus()
        bus.subscribe(self.seen.append)
        self.d = Daemon(
            FakeEngine(),
            self.player,
            lambda name: ProfileView(
                voice="v", speed=1.0, interrupt_on=(), prepare=lambda p: (list(p), [])
            ),
            bus=bus,
            channels=ChannelTable(),
        )
        self.d.start()

    def req(self, verb: Verb, source: str = "s", **payload: object) -> Response:
        return self.d.handle(Request(verb=verb, source_id=source, payload=dict(payload)))

    def speak_and_queue(self) -> None:
        """One utterance playing (held), one queued behind it, on channel `s`."""
        assert self.req(Verb.ENQUEUE, text="One. Two.", kind="response").ok
        assert self.reached.wait(timeout=5.0), "the worker never started playing"
        assert self.req(Verb.ENQUEUE, text="Queued.", kind="response").ok

    def queued(self) -> int:
        return len(self.d._pending_jobs())

    def channel_status(self) -> dict[str, object]:
        data = self.req(Verb.STATUS, source="").data["channels"]
        assert isinstance(data, list)
        row = data[0]
        assert isinstance(row, dict)
        return row


@pytest.fixture
def rig() -> Iterator[Rig]:
    r = Rig()
    try:
        yield r
    finally:
        r.release.set()
        r.d.stop()


def set_global(r: Rig, value: str) -> None:
    r.d.settings.set("speech.on_prompt", value)


def test_new_turn_hush_lets_the_reading_finish_by_default(rig: Rig) -> None:
    rig.speak_and_queue()
    rig.req(Verb.SET_CAPABILITIES, briefs=True)
    rig.d.channels.mark_briefed("s", True)
    response = rig.req(Verb.HUSH, new_turn=True)
    assert response.data == {"discarded": 0, "scope": "channel", "held": True}
    # Nothing dropped, and the briefed flag is still reset for the new turn.
    assert rig.queued() == 1
    assert rig.d.channels.get("s").briefed_this_turn is False  # type: ignore[union-attr]
    rig.release.set()
    assert rig.d.wait_idle(timeout=5.0)
    assert not [e for e in rig.seen if e.kind == "discarded"]
    assert len(rig.player.played) >= 2


def test_new_turn_hush_cuts_off_under_the_global_hush_policy(rig: Rig) -> None:
    set_global(rig, "hush")
    rig.speak_and_queue()
    response = rig.req(Verb.HUSH, new_turn=True)
    assert response.data["discarded"] == 1
    assert "held" not in response.data
    assert rig.queued() == 0


def test_a_channel_override_beats_the_global_setting(rig: Rig) -> None:
    set_global(rig, "finish")
    rig.speak_and_queue()
    assert rig.req(Verb.SET_ON_PROMPT, on_prompt="hush").ok
    assert rig.req(Verb.HUSH, new_turn=True).data["discarded"] == 1


def test_default_clears_the_override(rig: Rig) -> None:
    set_global(rig, "hush")
    rig.speak_and_queue()
    rig.req(Verb.SET_ON_PROMPT, on_prompt="finish")
    rig.req(Verb.SET_ON_PROMPT, on_prompt="default")
    assert rig.channel_status()["on_prompt_override"] is None
    assert rig.req(Verb.HUSH, new_turn=True).data["discarded"] == 1


def test_a_hush_that_is_not_a_new_turn_always_stops(rig: Rig) -> None:
    rig.speak_and_queue()
    assert rig.req(Verb.HUSH).data["discarded"] == 1


def test_set_on_prompt_refusals(rig: Rig) -> None:
    rig.req(Verb.ENQUEUE, text="Hi.", kind="response")
    assert not rig.req(Verb.SET_ON_PROMPT, on_prompt="sometimes").ok
    assert not rig.req(Verb.SET_ON_PROMPT, source="", on_prompt="hush").ok
    typo = rig.req(Verb.SET_ON_PROMPT, source="typo", on_prompt="hush")
    assert not typo.ok and "no channel" in typo.error
    assert rig.d.channels.get("typo") is None


def test_set_on_prompt_is_announced_and_in_status(rig: Rig) -> None:
    rig.req(Verb.ENQUEUE, text="Hi.", kind="response")
    answer = rig.req(Verb.SET_ON_PROMPT, on_prompt="hush")
    assert answer.data == {"on_prompt": "hush", "override": "hush"}
    event = [e for e in rig.seen if e.kind == "on_prompt"][-1]
    assert (event.source_id, event.data) == ("s", {"on_prompt": "hush", "override": "hush"})
    row = rig.channel_status()
    assert (row["on_prompt"], row["on_prompt_override"]) == ("hush", "hush")
    rig.req(Verb.SET_ON_PROMPT, on_prompt="default")
    row = rig.channel_status()
    assert (row["on_prompt"], row["on_prompt_override"]) == ("finish", None)
