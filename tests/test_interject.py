"""Short notes interject into a long reading, which then resumes: `speech.interject`."""

import threading
import time
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from typing import cast

import numpy as np
import pytest

from speakd.channels import ChannelTable
from speakd.daemon import Daemon, ProfileView
from speakd.events import Event, EventBus
from speakd.model import Piece
from speakd.player import RecordingPlayer
from speakd.protocol import Request, Response, Verb
from speakd.synth.fake import FakeEngine

TEXT = "Zero. One. Two. Three."


class GatePlayer(RecordingPlayer):
    """Lets `budget` plays through, then holds each one until let go or stopped.

    A held play() returns when the daemon stops the player, as a real one
    does, so an interruption is visible as the voice moving on while the gate
    is still shut. `on_interrupted` runs on the worker's thread just after a
    held play was cut short -- the window between an interjection's cancel
    and `_speak` re-queuing the rest -- so a test can land a hush or a seek
    exactly there.
    """

    def __init__(self) -> None:
        super().__init__()
        self.cond = threading.Condition()
        self.budget = 0
        self.count = 0
        self._interrupt = False
        self.on_interrupted: Callable[[], None] | None = None
        self._paused = False

    def play(self, audio: np.ndarray, sample_rate: int) -> None:
        super().play(audio, sample_rate)
        with self.cond:
            self.count += 1
            mine = self.count
            self.cond.notify_all()
            self.cond.wait_for(lambda: self.budget >= mine or self._interrupt, timeout=10)
            interrupted = self._interrupt
            self._interrupt = False
        hook = self.on_interrupted
        if interrupted and hook is not None:
            self.on_interrupted = None
            hook()

    def stop(self) -> None:
        super().stop()
        with self.cond:
            self._interrupt = True
            self.cond.notify_all()

    def clear_interrupt(self) -> None:
        with self.cond:
            self._interrupt = False

    def let_go(self, n: int = 1_000_000) -> None:
        with self.cond:
            self.budget += n
            self.cond.notify_all()

    # Pausable, so PAUSE is accepted. The gate does the holding.
    def pause(self) -> None:
        self._paused = True

    def resume(self) -> None:
        self._paused = False

    @property
    def paused(self) -> bool:
        return self._paused


def until(predicate: Callable[[], bool], timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.002)
    return bool(predicate())


@dataclass
class Run:
    """One `started` and what followed it up to its `finished`."""

    source: str
    text: str
    positions: list[int] = field(default_factory=list)
    finished: dict[str, object] | None = None


class Rig:
    def __init__(self) -> None:
        self.player = GatePlayer()
        self.seen: list[Event] = []
        self._lock = threading.Lock()
        self.hold_prepare = threading.Event()
        self.hold_prepare.set()
        self.preparing = threading.Event()

        def prepare(pieces: Sequence[Piece]) -> tuple[list[Piece], list[str]]:
            if any("Zero" in p.spoken for p in pieces):
                self.preparing.set()
                assert self.hold_prepare.wait(timeout=10)
            return list(pieces), []

        bus = EventBus()
        bus.subscribe(self._record)
        self.d = Daemon(
            FakeEngine(),
            self.player,
            lambda name: ProfileView(voice="v", speed=1.0, interrupt_on=(), prepare=prepare),
            bus=bus,
            channels=ChannelTable(),
        )
        self.d.start()
        # `b` is a session in brief mode, so its briefings are spoken.
        self.req(Verb.SET_CAPABILITIES, source="b", briefs=True)
        self.req(Verb.SET_MODE, source="b", mode="brief")

    def _record(self, event: Event) -> None:
        with self._lock:
            self.seen.append(event)

    def events(self) -> list[Event]:
        with self._lock:
            return list(self.seen)

    def req(self, verb: Verb, source: str = "s", **payload: object) -> Response:
        return self.d.handle(Request(verb=verb, source_id=source, payload=dict(payload)))

    def say(self, kind: str, text: str, source: str = "s") -> None:
        response = self.req(Verb.ENQUEUE, source=source, text=text, kind=kind)
        assert response.ok and response.data["spoken"] is True, response

    def runs(self) -> list[Run]:
        out: list[Run] = []
        for e in self.events():
            if e.kind == "started":
                out.append(Run(e.source_id, cast(str, e.data["text"])))
            elif e.kind == "position" and out:
                out[-1].positions.append(cast(int, e.data["index"]))
            elif e.kind == "finished" and out:
                out[-1].finished = dict(e.data)
        return out

    def started(self) -> list[str]:
        return [r.source for r in self.runs()]

    def reading_held_at_sentence_one(self) -> None:
        """The response on `s` has played sentence 0 and is held in sentence 1."""
        self.player.let_go(1)
        self.say("response", TEXT)
        assert until(lambda: self.player.count == 2 and self.runs()[-1].positions == [0, 1])

    def finish(self) -> None:
        self.player.let_go()
        assert self.d.wait_idle(timeout=10.0)
        assert self.d._pending == 0


@pytest.fixture
def rig() -> Iterator[Rig]:
    r = Rig()
    try:
        yield r
    finally:
        r.hold_prepare.set()
        r.player.let_go()
        r.d.stop()


def test_a_briefing_interjects_and_the_reading_resumes_at_its_sentence(rig: Rig) -> None:
    rig.reading_held_at_sentence_one()
    rig.say("response", "Queued reading.")
    rig.say("brief", "Build passed.", source="b")
    # Spoken while the gate is still shut: the reading was cut off for it.
    assert until(lambda: rig.started() == ["s", "b"])
    rig.finish()
    runs = rig.runs()
    assert [r.source for r in runs] == ["s", "b", "s", "s"]
    first, brief, resumed, queued = runs
    assert first.finished == {"cancelled": False, "aborted": False, "interjected": True}
    assert "Build passed." in brief.text
    # The same reading, again from the sentence that was cut off.
    assert resumed.text == TEXT
    assert resumed.positions == [1, 2, 3]
    assert resumed.finished == {"cancelled": False, "aborted": False}
    assert queued.text == "Queued reading."
    resumed_queued = [e for e in rig.events() if e.kind == "queued" and e.data.get("resumed")]
    assert len(resumed_queued) == 1 and resumed_queued[0].source_id == "s"


def test_an_attention_call_interjects_too(rig: Rig) -> None:
    rig.reading_held_at_sentence_one()
    rig.say("attention", "OpenCode needs your permission.", source="n")
    assert until(lambda: rig.started() == ["s", "n"])
    rig.finish()
    assert rig.started() == ["s", "n", "s"]
    assert rig.runs()[2].positions == [1, 2, 3]


def test_under_attention_only_a_briefing_waits_its_turn(rig: Rig) -> None:
    rig.d.settings.set("speech.interject", "attention")
    rig.reading_held_at_sentence_one()
    rig.say("response", "Queued reading.")
    rig.say("brief", "Build passed.", source="b")
    # Plain first come, first served: no jump, no cut.
    assert [j.source_id for j in rig.d._pending_jobs()] == ["s", "b"]
    time.sleep(0.1)
    assert rig.started() == ["s"]
    rig.finish()
    assert rig.started() == ["s", "s", "b"]
    assert all(not (r.finished or {}).get("interjected") for r in rig.runs())


def test_under_attention_an_attention_call_still_interjects(rig: Rig) -> None:
    rig.d.settings.set("speech.interject", "attention")
    rig.reading_held_at_sentence_one()
    rig.say("attention", "Finished.", source="n")
    assert until(lambda: rig.started() == ["s", "n"])
    rig.finish()
    assert rig.started() == ["s", "n", "s"]


def test_off_means_nothing_interjects(rig: Rig) -> None:
    rig.d.settings.set("speech.interject", "off")
    rig.reading_held_at_sentence_one()
    rig.say("response", "Queued reading.")
    rig.say("attention", "Finished.", source="n")
    assert [j.source_id for j in rig.d._pending_jobs()] == ["s", "n"]
    time.sleep(0.1)
    assert rig.started() == ["s"]
    rig.finish()
    assert rig.started() == ["s", "s", "n"]
    assert rig.runs()[0].positions == [0, 1, 2, 3]


def test_an_interjection_does_not_cut_into_another(rig: Rig) -> None:
    rig.say("brief", "First note.", source="b")
    assert until(lambda: rig.started() == ["b"] and rig.player.count == 1)
    rig.say("response", TEXT)
    rig.say("attention", "Second note.", source="n")
    # Next, but not now.
    assert [j.source_id for j in rig.d._pending_jobs()] == ["n", "s"]
    time.sleep(0.1)
    assert rig.started() == ["b"]
    rig.finish()
    assert rig.started() == ["b", "n", "s"]
    assert all(not (r.finished or {}).get("interjected") for r in rig.runs())


def test_interjections_take_turns_among_themselves(rig: Rig) -> None:
    rig.reading_held_at_sentence_one()
    rig.say("response", "Queued reading.")
    rig.say("attention", "First note.", source="n")
    rig.say("brief", "Second note.", source="b")
    assert until(lambda: rig.started() == ["s", "n"])
    rig.finish()
    assert rig.started() == ["s", "n", "b", "s", "s"]
    runs = rig.runs()
    assert runs[3].text == TEXT and runs[3].positions == [1, 2, 3]
    assert runs[4].text == "Queued reading."


def test_a_resumed_reading_can_be_interjected_again(rig: Rig) -> None:
    rig.reading_held_at_sentence_one()
    rig.say("attention", "First note.", source="n")
    assert until(lambda: rig.started() == ["s", "n"])
    # Through the note and the resumed sentence 1, held in sentence 2.
    rig.player.let_go(3)
    assert until(lambda: rig.runs()[-1].positions == [1, 2] and rig.player.count == 5)
    rig.say("attention", "Second note.", source="n")
    assert until(lambda: rig.started() == ["s", "n", "s", "n"])
    rig.finish()
    assert rig.started() == ["s", "n", "s", "n", "s"]
    assert rig.runs()[4].positions == [2, 3]


def test_a_hush_after_the_interjection_discards_the_resumed_reading(rig: Rig) -> None:
    rig.reading_held_at_sentence_one()
    rig.say("response", "Queued reading.")
    rig.say("brief", "Build passed.", source="b")
    assert until(lambda: rig.started() == ["s", "b"])
    # The rest of the reading is already back in the queue, behind the note.
    assert [j.source_id for j in rig.d._pending_jobs()] == ["s", "s"]
    assert rig.req(Verb.HUSH, source="").data["discarded"] == 2
    rig.finish()
    assert rig.started() == ["s", "b"]


def test_a_hush_between_the_cut_and_the_requeue_wins(rig: Rig) -> None:
    """The window `_stop_speaking` clears `interjected` for."""
    rig.reading_held_at_sentence_one()
    rig.say("response", "Queued reading.")
    done = threading.Event()

    def land() -> None:
        rig.req(Verb.HUSH, source="")
        done.set()

    rig.player.on_interrupted = land
    rig.say("brief", "Build passed.", source="b")
    assert done.wait(timeout=5)
    rig.finish()
    runs = rig.runs()
    assert [r.source for r in runs] == ["s"]
    assert runs[0].finished == {"cancelled": True, "aborted": False}


def test_a_seek_between_the_cut_and_the_requeue_wins(rig: Rig) -> None:
    rig.reading_held_at_sentence_one()
    done = threading.Event()

    def land() -> None:
        rig.req(Verb.SEEK, source="", index=3)
        done.set()

    rig.player.on_interrupted = land
    rig.say("brief", "Build passed.", source="b")
    assert done.wait(timeout=5)
    rig.finish()
    runs = rig.runs()
    # The reading went where the listener sent it; the note waited for it.
    assert [r.source for r in runs] == ["s", "b"]
    assert runs[0].positions == [0, 1, 3]


def test_paused_speech_is_not_cut_but_the_note_goes_next(rig: Rig) -> None:
    rig.reading_held_at_sentence_one()
    assert rig.req(Verb.PAUSE).ok
    rig.say("response", "Queued reading.")
    rig.say("brief", "Build passed.", source="b")
    assert [j.source_id for j in rig.d._pending_jobs()] == ["b", "s"]
    time.sleep(0.1)
    assert rig.started() == ["s"]
    assert rig.req(Verb.RESUME).ok
    rig.finish()
    assert rig.started() == ["s", "b", "s"]
    assert rig.runs()[0].positions == [0, 1, 2, 3]


def test_an_interjection_during_preparation_cuts_in_before_a_word(rig: Rig) -> None:
    rig.player.let_go()
    rig.hold_prepare.clear()
    rig.say("response", TEXT)
    assert rig.preparing.wait(timeout=5)
    rig.say("brief", "Build passed.", source="b")
    rig.hold_prepare.set()
    rig.finish()
    runs = rig.runs()
    assert [r.source for r in runs] == ["s", "b", "s"]
    assert runs[0].positions == []
    assert runs[0].finished == {"cancelled": False, "aborted": False, "interjected": True}
    assert runs[2].positions == [0, 1, 2, 3]


def test_status_and_queued_say_which_jobs_interject(rig: Rig) -> None:
    rig.d.settings.set("speech.interject", "off")
    rig.reading_held_at_sentence_one()
    rig.say("response", "Queued reading.")
    rig.d.settings.set("speech.interject", "brief")
    rig.req(Verb.PAUSE)
    rig.say("brief", "Build passed.", source="b")
    queue = cast(list[dict[str, object]], rig.req(Verb.STATUS, source="").data["queue"])
    assert [(e["source_id"], e.get("interjects", False)) for e in queue] == [
        ("b", True),
        ("s", False),
    ]
    queued = [e for e in rig.events() if e.kind == "queued"]
    assert queued[-1].data.get("interjects") is True
    assert "interjects" not in queued[-2].data
    rig.req(Verb.RESUME)
    rig.finish()


def test_stop_during_an_interjection_does_not_hang(rig: Rig) -> None:
    rig.reading_held_at_sentence_one()
    rig.say("response", "Queued reading.")
    rig.say("brief", "Build passed.", source="b")
    assert until(lambda: rig.started() == ["s", "b"])
    began = time.monotonic()
    assert rig.d.stop() is True
    assert time.monotonic() - began < 3.0
    assert rig.d.wait_idle(timeout=1.0)
    assert rig.d._pending == 0
