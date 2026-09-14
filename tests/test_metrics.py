"""Tests for the metrics event: the four numbers the pinned monitor shows.

The numbers themselves are checked against hand-built state, where they are
arithmetic. The event is checked against a running daemon, because when it
fires and when it stops firing are what a monitor depends on, and they are
the parts a timer gets wrong.
"""

import os
import threading
import time
from collections.abc import Callable, Sequence
from pathlib import Path

import numpy as np
import pytest

from speakd.channels import ChannelTable
from speakd.daemon import _METRICS_THREAD_NAME, Daemon, ProfileView
from speakd.events import Event, EventBus
from speakd.metrics import _WINDOW_SEGMENTS, SynthesisWindow, resident_bytes
from speakd.model import Piece
from speakd.player import Player, RecordingPlayer
from speakd.protocol import Request, Verb
from speakd.synth import Synthesizer
from speakd.synth.fake import FakeEngine

# Short enough that a test is not mostly sleep, long enough that a tick is
# not lost in scheduling noise. The shipped interval is one second, which is
# pinned by its own test rather than by waiting out three of them here.
TICK = 0.02


def passthrough(pieces: Sequence[Piece]) -> tuple[list[Piece], list[str]]:
    return list(pieces), []


def profile_for(name: str) -> ProfileView:
    return ProfileView(voice="af_heart", speed=1.1, interrupt_on=(), prepare=passthrough)


def enqueue(source: str, text: str) -> Request:
    return Request(verb=Verb.ENQUEUE, source_id=source, payload={"text": text})


def until(predicate: Callable[[], bool], timeout: float = 5.0) -> bool:
    """Poll rather than sleep a fixed amount, so a slow machine is not a failure."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.002)
    return predicate()


class ClockPlayer(RecordingPlayer):
    """A sink that takes as long as the audio it is handed, plus a fixed stall.

    `RecordingPlayer` returns at once, which makes every utterance look
    infinitely early and drift meaningless. Real playback is the pipeline's
    clock; `extra` is the stall that drift exists to report.
    """

    def __init__(self, extra: float = 0.0) -> None:
        super().__init__()
        self.extra = extra

    def play(self, audio: np.ndarray, sample_rate: int) -> None:
        super().play(audio, sample_rate)
        time.sleep(len(audio) / sample_rate + self.extra)


class HoldingPlayer(RecordingPlayer):
    """Holds inside its first play() until released, signalling `reached` first."""

    def __init__(self, reached: threading.Event, release: threading.Event) -> None:
        super().__init__()
        self.reached = reached
        self.release = release

    def play(self, audio: np.ndarray, sample_rate: int) -> None:
        super().play(audio, sample_rate)
        if len(self.played) == 1:
            self.reached.set()
            assert self.release.wait(timeout=5), "test never released play()"


def tickers() -> list[threading.Thread]:
    return [t for t in threading.enumerate() if t.name == _METRICS_THREAD_NAME]


@pytest.fixture
def running_daemon():  # type: ignore[no-untyped-def]
    """A started daemon on a fast tick, with its bus, built per test."""
    built: list[Daemon] = []

    def build(
        player: Player | None = None,
        engine: Synthesizer | None = None,
        interval: float = TICK,
    ) -> tuple[Daemon, EventBus, list[Event]]:
        bus = EventBus()
        d = Daemon(
            engine if engine is not None else FakeEngine(),
            player if player is not None else RecordingPlayer(),
            profile_for,
            bus=bus,
            channels=ChannelTable(),
            metrics_interval=interval,
        )
        seen: list[Event] = []
        bus.subscribe(seen.append, kinds=["metrics"])
        d.start()
        built.append(d)
        return d, bus, seen

    try:
        yield build
    finally:
        for d in built:
            d.stop()


# --- the numbers ---------------------------------------------------------


def test_rtf_weighs_by_audio_rather_than_averaging_the_segments() -> None:
    window = SynthesisWindow()
    window.record(1.0, 1.0)  # a short segment, paying the fixed per-call cost
    window.record(0.5, 4.0)  # a long one, where that cost is spread thin
    # 1.5s of synthesis for 5s of audio. Averaging the two ratios would say
    # 0.56, which is one short segment counting for as much as four seconds
    # of speech.
    assert window.rtf() == pytest.approx(0.3)


def test_the_window_forgets_what_the_machine_was_doing_before() -> None:
    window = SynthesisWindow()
    for _ in range(_WINDOW_SEGMENTS):
        window.record(2.0, 1.0)
    for _ in range(_WINDOW_SEGMENTS):
        window.record(0.5, 1.0)
    # Since boot this is 1.25. The window is what the machine is doing now.
    assert window.rtf() == pytest.approx(0.5)


def test_rtf_is_unknown_until_something_has_been_synthesised() -> None:
    assert SynthesisWindow().rtf() is None


def test_a_call_that_produced_no_audio_does_not_divide_by_zero() -> None:
    window = SynthesisWindow()
    window.record(0.9, 0.0)
    assert window.rtf() is None


def test_resident_bytes_is_the_second_field_of_statm_in_pages(tmp_path: Path) -> None:
    statm = tmp_path / "statm"
    # size, resident, shared, text, lib, data, dirty -- resident is the second.
    statm.write_text("4096 250 180 1 0 300 0\n", encoding="utf-8")
    assert resident_bytes(statm) == 250 * os.sysconf("SC_PAGE_SIZE")


def test_resident_bytes_is_unknown_rather_than_a_guess_without_proc(tmp_path: Path) -> None:
    assert resident_bytes(tmp_path / "no-such-file") is None


def test_a_statm_that_is_not_linuxs_is_unknown_rather_than_an_exception(
    tmp_path: Path,
) -> None:
    statm = tmp_path / "statm"
    statm.write_text("not a statm at all\n", encoding="utf-8")
    assert resident_bytes(statm) is None


def test_this_process_reports_its_own_resident_set() -> None:
    resident = resident_bytes()
    assert resident is not None
    # numpy alone is more than this; the assertion is that it is a real
    # measurement rather than zero or a page count left unconverted.
    assert resident > 8 * 1024 * 1024


# --- the event -----------------------------------------------------------


def test_the_event_carries_the_four_numbers_the_monitor_shows(running_daemon) -> None:  # type: ignore[no-untyped-def]
    d, _bus, seen = running_daemon(player=ClockPlayer())
    d.handle(enqueue("s", "One. Two. Three."))
    assert d.wait_idle(timeout=5.0)
    assert until(lambda: bool(seen) and seen[-1].data["queue"] == 0)

    event = seen[-1]
    assert event.kind == "metrics"
    assert event.source_id == ""
    assert set(event.data) == {"rtf", "drift", "mem_bytes", "queue"}
    assert isinstance(event.data["rtf"], float)
    assert isinstance(event.data["drift"], float)
    assert isinstance(event.data["mem_bytes"], int)
    assert isinstance(event.data["queue"], int)


def test_the_queue_reported_is_the_work_the_daemon_has_not_finished(running_daemon) -> None:  # type: ignore[no-untyped-def]
    reached, release = threading.Event(), threading.Event()
    d, _bus, seen = running_daemon(player=HoldingPlayer(reached, release))
    d.handle(enqueue("s", "One. Two."))
    assert reached.wait(timeout=5)
    d.handle(enqueue("s", "Three."))
    assert until(lambda: any(e.data["queue"] == 2 for e in seen))
    release.set()
    assert d.wait_idle(timeout=5.0)


def test_the_ticker_does_not_wait_on_a_worker_stuck_in_play(running_daemon) -> None:  # type: ignore[no-untyped-def]
    """The hard constraint: a wedged worker must not take the numbers with it.

    The worker is held inside play(), which is where this project has already
    shipped an hour-long stall. Every number in the event has to be reachable
    from the thread that emits, so the ticks go on.
    """
    reached, release = threading.Event(), threading.Event()
    d, _bus, seen = running_daemon(player=HoldingPlayer(reached, release))
    d.handle(enqueue("s", "One. Two. Three."))
    assert reached.wait(timeout=5)
    assert until(lambda: len(seen) >= 3), f"only {len(seen)} ticks while the worker was stuck"
    assert all(e.data["queue"] >= 1 for e in seen), "the utterance was not counted as pending"
    release.set()
    assert d.wait_idle(timeout=5.0)


def test_nothing_goes_out_while_the_daemon_has_nothing_to_say(running_daemon) -> None:  # type: ignore[no-untyped-def]
    _d, _bus, seen = running_daemon()
    time.sleep(TICK * 8)
    assert seen == [], f"{len(seen)} events from an idle daemon"


def test_the_ticker_settles_the_monitor_once_and_then_stops(running_daemon) -> None:  # type: ignore[no-untyped-def]
    d, _bus, seen = running_daemon(player=ClockPlayer())
    d.handle(enqueue("s", "One. Two. Three."))
    assert d.wait_idle(timeout=5.0)
    assert until(lambda: bool(seen) and seen[-1].data["queue"] == 0)

    settled = len(seen)
    time.sleep(TICK * 8)
    assert len(seen) == settled, f"{len(seen) - settled} more events after going idle"


def test_an_utterance_shorter_than_a_tick_still_settles_the_monitor(running_daemon) -> None:  # type: ignore[no-untyped-def]
    """A "Done." is over before the first tick, and still has to be shown."""
    interval = 0.3
    d, _bus, seen = running_daemon(interval=interval)
    d.handle(enqueue("s", "Done."))
    assert d.wait_idle(timeout=5.0)

    assert until(lambda: len(seen) == 1, timeout=3.0), f"{len(seen)} events, expected one"
    assert seen[0].data["queue"] == 0
    time.sleep(interval * 2.5)
    assert len(seen) == 1, f"{len(seen)} events, expected the one that settled it"


def test_mem_bytes_is_left_out_rather_than_guessed(  # type: ignore[no-untyped-def]
    running_daemon,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("speakd.daemon.resident_bytes", lambda: None)
    d, _bus, seen = running_daemon(player=ClockPlayer())
    d.handle(enqueue("s", "One. Two."))
    assert d.wait_idle(timeout=5.0)
    assert until(lambda: bool(seen) and seen[-1].data["queue"] == 0)

    assert set(seen[-1].data) == {"rtf", "drift", "queue"}


def test_the_drift_reported_is_the_stall_in_the_utterance_being_spoken(running_daemon) -> None:  # type: ignore[no-untyped-def]
    """Three segments, each played 0.15s slower than its own audio.

    Drift is measured elapsed minus nominal elapsed between the first segment
    and the last, so two stalls have accumulated by the time the third one
    starts: 0.30s, not the 0.78s of wall clock the utterance took.
    """
    d, _bus, seen = running_daemon(player=ClockPlayer(extra=0.15))
    d.handle(enqueue("s", "One. Two. Three."))
    assert d.wait_idle(timeout=5.0)
    assert until(lambda: bool(seen) and seen[-1].data["queue"] == 0)

    drift = seen[-1].data["drift"]
    assert isinstance(drift, float)
    assert drift == pytest.approx(0.30, abs=0.12), f"drift was {drift:.3f}s"


def test_the_rtf_reported_is_what_synthesis_cost_against_the_audio_it_made(  # type: ignore[no-untyped-def]
    running_daemon,
) -> None:
    """0.1s per call against segments of roughly 0.24s to 0.36s of audio."""
    d, _bus, seen = running_daemon(engine=FakeEngine(synthesis_cost=0.1))
    d.handle(enqueue("s", "One. Two. Three."))
    assert d.wait_idle(timeout=5.0)
    assert until(lambda: bool(seen) and seen[-1].data["queue"] == 0)

    rtf = seen[-1].data["rtf"]
    assert isinstance(rtf, float)
    assert rtf == pytest.approx(0.35, abs=0.12), f"rtf was {rtf:.3f}"


def test_the_ticker_does_not_outlive_the_daemon() -> None:
    before = len(tickers())
    bus = EventBus()
    seen: list[Event] = []
    bus.subscribe(seen.append, kinds=["metrics"])
    d = Daemon(
        FakeEngine(),
        RecordingPlayer(),
        profile_for,
        bus=bus,
        channels=ChannelTable(),
        metrics_interval=TICK,
    )
    d.start()
    assert len(tickers()) == before + 1, "no ticker thread while the daemon runs"
    d.handle(enqueue("s", "One. Two."))
    assert d.wait_idle(timeout=5.0)
    assert until(lambda: bool(seen))

    assert d.stop() is True
    assert until(lambda: len(tickers()) == before), "the ticker outlived the daemon"
    after_stop = len(seen)
    time.sleep(TICK * 8)
    assert len(seen) == after_stop, "the ticker kept publishing after stop()"


def test_a_restarted_daemon_ticks_once_not_twice() -> None:
    before = len(tickers())
    bus = EventBus()
    seen: list[Event] = []
    bus.subscribe(seen.append, kinds=["metrics"])
    d = Daemon(
        FakeEngine(),
        RecordingPlayer(),
        profile_for,
        bus=bus,
        channels=ChannelTable(),
        metrics_interval=TICK,
    )
    d.start()
    assert d.stop() is True
    d.start()
    try:
        assert len(tickers()) == before + 1, "the stopped daemon's ticker is still running"
        d.handle(enqueue("s", "Done."))
        assert d.wait_idle(timeout=5.0)
        assert until(lambda: len(seen) == 1)
        time.sleep(TICK * 8)
        assert len(seen) == 1, f"{len(seen)} events for one settled utterance"
    finally:
        d.stop()


def test_the_shipped_interval_is_one_second() -> None:
    """The contract's cadence, which no test above waits out."""
    d = Daemon(FakeEngine(), RecordingPlayer(), profile_for)
    assert d._metrics_interval == 1.0


def test_a_ticker_the_join_gave_up_on_does_not_publish_for_its_successor() -> None:
    """stop() bounds its join, so a slow subscriber can outlive it.

    The Event alone cannot retire that ticker: the next start() clears it.
    Identity is what does, which is only visible when a ticker is still inside
    a publish while the daemon it belonged to is stopped and started again.
    """
    release = threading.Event()
    held = threading.Event()
    seen: list[Event] = []

    def hold(event: Event) -> None:
        seen.append(event)
        if len(seen) == 1:
            held.set()
            assert release.wait(timeout=10), "the test never released the subscriber"

    bus = EventBus()
    bus.subscribe(hold, kinds=["metrics"])
    d = Daemon(
        FakeEngine(),
        RecordingPlayer(),
        profile_for,
        bus=bus,
        channels=ChannelTable(),
        metrics_interval=TICK,
    )
    d.start()
    d.handle(enqueue("s", "Done."))
    assert d.wait_idle(timeout=5.0)
    assert held.wait(timeout=5.0), "the ticker published nothing to hold"

    assert d.stop() is True
    d.start()
    try:
        d.handle(enqueue("s", "Again."))
        assert d.wait_idle(timeout=5.0)
        assert until(lambda: len(seen) == 2), f"the new ticker never settled: {len(seen)} events"
        release.set()
        time.sleep(TICK * 10)
        assert len(seen) == 2, f"the retired ticker published {len(seen) - 2} more"
    finally:
        release.set()
        d.stop()


def test_a_speech_worker_that_will_not_start_takes_the_ticker_with_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ticker comes up first, so the refused half has to retire it.

    Left running, it would be a thread ticking for speech that can never
    happen, in a daemon whose start() reported failure. On an interval this
    long, only being woken retires it inside the test: clearing the daemon's
    handle on it alone would leave it asleep for half a minute.
    """
    real_start = threading.Thread.start

    def refuse_the_worker(self: threading.Thread) -> None:
        if self.name == "speakd-speech":
            raise RuntimeError("can't start new thread")
        real_start(self)

    before = len(tickers())
    monkeypatch.setattr(threading.Thread, "start", refuse_the_worker)
    d = Daemon(
        FakeEngine(),
        RecordingPlayer(),
        profile_for,
        bus=EventBus(),
        channels=ChannelTable(),
        metrics_interval=30.0,
    )
    with pytest.raises(RuntimeError):
        d.start()
    monkeypatch.undo()

    assert until(lambda: len(tickers()) == before), "the ticker outlived a refused start()"
    assert d.handle(enqueue("s", "One.")).ok is False


def test_stopping_does_not_wait_out_a_tick() -> None:
    """A shutdown must not sit behind a sleeping monitor.

    The shipped interval is a second, and stop() is already on the path a
    supervisor waits on. Retiring the ticker by identity alone would leave it
    asleep until its interval ran out -- invisible at the intervals the tests
    above use, which is why this one sets an interval no test would wait for.
    """
    before = len(tickers())
    d = Daemon(
        FakeEngine(),
        RecordingPlayer(),
        profile_for,
        bus=EventBus(),
        channels=ChannelTable(),
        metrics_interval=30.0,
    )
    d.start()
    assert len(tickers()) == before + 1, "no ticker thread while the daemon runs"

    started = time.monotonic()
    assert d.stop() is True
    elapsed = time.monotonic() - started
    assert len(tickers()) == before, "stop() returned with the ticker still asleep"
    assert elapsed < 5.0, f"stop() took {elapsed:.2f}s waiting on the ticker"
