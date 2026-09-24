"""Tests for the streaming pipeline."""

import threading
import time

import numpy as np
import pytest

from speakd.metrics import SynthesisWindow
from speakd.model import Piece, Segment, Span
from speakd.pipeline import AudioCache, SpeechResult, speak
from speakd.player import FakeSink, RecordingPlayer, StreamingPlayer
from speakd.synth.fake import FakeEngine
from speakd.tempo import Tempo
from speakd.timeline import Timeline


def piece(text: str) -> Piece:
    return Piece(span=Span(0, len(text)), spoken=text)


class SleepingPlayer(RecordingPlayer):
    """A sink that takes real time, so overlap can be measured.

    Records when each play() finished as well as when it started, so overlap
    can be shown by ordering two observed events rather than by comparing
    total runtime against a constant.
    """

    def __init__(self, cost: float) -> None:
        super().__init__()
        self.cost = cost
        self.finished: list[float] = []

    def play(self, audio: np.ndarray, sample_rate: int) -> None:
        super().play(audio, sample_rate)
        time.sleep(self.cost)
        self.finished.append(time.monotonic())


class TimedEngine(FakeEngine):
    """Records when each synthesize() call began."""

    def __init__(self, synthesis_cost: float) -> None:
        super().__init__(synthesis_cost=synthesis_cost)
        self.started: list[float] = []

    def synthesize(self, text: str, voice: str, speed: float, lang: str = "en") -> np.ndarray:
        self.started.append(time.monotonic())
        return super().synthesize(text, voice, speed, lang)


class ExplodingEngine(FakeEngine):
    """Fails on one specific text and works for everything else."""

    def __init__(self, bad: str) -> None:
        super().__init__()
        self.bad = bad

    def synthesize(self, text: str, voice: str, speed: float, lang: str = "en") -> np.ndarray:
        if text == self.bad:
            raise RuntimeError("engine exploded")
        return super().synthesize(text, voice, speed, lang)


class HoldingEngine(FakeEngine):
    """Blocks its third synthesize() call until released.

    Signals `reached` on entry, so a driver thread can order events around
    it: by the time `reached` fires, this call's own cancel check already
    passed, and both earlier units are already enqueued (the first has
    necessarily been dequeued, since a depth-one queue could not otherwise
    have held the second for this call to be reached at all).
    """

    def __init__(self, reached: threading.Event, release: threading.Event) -> None:
        super().__init__()
        self.calls = 0
        self.reached = reached
        self.release = release

    def synthesize(self, text: str, voice: str, speed: float, lang: str = "en") -> np.ndarray:
        idx = self.calls
        self.calls += 1
        if idx == 2:
            self.reached.set()
            assert self.release.wait(timeout=5), "driver never released synthesize()"
        return super().synthesize(text, voice, speed, lang)


class HoldingPlayer(RecordingPlayer):
    """Holds after its first play() call until released, signalling `reached` first."""

    def __init__(self, reached: threading.Event, release: threading.Event) -> None:
        super().__init__()
        self.reached = reached
        self.release = release

    def play(self, audio: np.ndarray, sample_rate: int) -> None:
        super().play(audio, sample_rate)
        if len(self.played) == 1:
            self.reached.set()
            assert self.release.wait(timeout=5), "driver never released play()"


class RaisingPlayer(RecordingPlayer):
    """Raises on one specific play() call, simulating a lost audio device.

    `fail_on_call` counts play() invocations from 1, so the default raises on
    the very first call.
    """

    def __init__(self, fail_on_call: int = 1) -> None:
        super().__init__()
        self.fail_on_call = fail_on_call
        self.calls = 0

    def play(self, audio: np.ndarray, sample_rate: int) -> None:
        self.calls += 1
        if self.calls == self.fail_on_call:
            raise RuntimeError("device disappeared")
        super().play(audio, sample_rate)


def test_plays_every_segment_in_order() -> None:
    player = RecordingPlayer()
    result = speak([piece("One. Two. Three.")], FakeEngine(), player)
    assert len(player.played) == 3
    assert len(result.timeline) == 3
    assert result.cancelled is False
    assert result.errors == []


def test_timeline_offsets_accumulate() -> None:
    player = RecordingPlayer()
    result = speak(
        [piece("One. Two.")], FakeEngine(sample_rate=1000, chars_per_second=10.0), player
    )
    assert result.timeline.span_at(0.0) is not None
    assert result.timeline.duration > 0.0


def test_synthesis_overlaps_playback() -> None:
    # Four segments, each costing 0.05s to synthesise and 0.05s to play.
    player = SleepingPlayer(cost=0.05)
    engine = TimedEngine(synthesis_cost=0.05)
    start = time.monotonic()
    speak([piece("One. Two. Three. Four.")], engine, player)
    elapsed = time.monotonic() - start

    assert len(player.played) == 4
    assert len(engine.started) == 4

    # The proof: the second unit's synthesis began before the first unit had
    # finished playing. Nothing serial can do that. It orders two measured
    # events rather than testing either against a constant, so a contended
    # runner slows both and the comparison holds.
    assert engine.started[1] < player.finished[0], (
        f"synthesis did not run ahead of playback: "
        f"unit 1 began {engine.started[1] - player.finished[0]:.3f}s after unit 0 finished"
    )

    # Coarse backstop only, now that the assertion above carries the proof:
    # serial would be ~0.40s and overlapped measures ~0.25s.
    assert elapsed < 0.40, f"no overlap: {elapsed:.2f}s"


def test_cancel_before_start_plays_nothing() -> None:
    # Cancel is already set when speak() is called, so the producer breaks on
    # its first check and the consumer takes the sentinel branch: player.stop()
    # is never reached. Mid-stream cancellation, which does reach it, is
    # covered by test_cancellation_mid_stream_does_not_leak_the_producer_thread.
    cancel = threading.Event()
    cancel.set()
    player = RecordingPlayer()
    result = speak([piece("One. Two. Three.")], FakeEngine(), player, cancel=cancel)
    assert result.cancelled is True
    assert player.played == []


def test_a_failing_segment_does_not_lose_the_others() -> None:
    player = RecordingPlayer()
    result = speak([piece("One. Two. Three.")], ExplodingEngine(bad="Two."), player)
    assert len(player.played) == 2
    assert len(result.errors) == 1
    assert "exploded" in result.errors[0]


def test_an_unsupported_language_stops_the_pass_and_names_it() -> None:
    # Simulates the first pipeline for a language failing to build (the
    # daemon's own retry, with a different lang, is Task 5's concern -- this
    # only proves speak() surfaces it distinctly from an ordinary error).
    engine = FakeEngine(raise_for=["ja"])
    player = RecordingPlayer()
    result = speak([piece("One. Two. Three.")], engine, player, lang="ja")
    assert result.unsupported_language == "ja"
    assert player.played == []
    assert result.errors == []
    assert result.aborted is False
    assert result.cancelled is False


def test_lang_is_passed_through_to_the_engine() -> None:
    engine = FakeEngine()
    speak([piece("Bonjour.")], engine, RecordingPlayer(), lang="fr")
    assert engine.synthesized_langs == ["fr"]


def test_cancellation_mid_stream_does_not_leak_the_producer_thread() -> None:
    # Three segments. The producer is held right as it enters synthesizing
    # the third (having already enqueued the second, past its own cancel
    # check for it), and the consumer is held right after playing the
    # first. Only once both are confirmed parked do we set cancel and
    # release both — forcing exactly the interleaving where the producer
    # commits one more item after the consumer has already discovered
    # cancellation and stopped reading. Without draining on that path, the
    # producer's later put (and its unconditional final put(None)) blocks
    # forever on the depth-one queue: this test fails against the brief's
    # verbatim implementation and passes against the drain fix.
    baseline = threading.active_count()

    cancel = threading.Event()
    engine_reached = threading.Event()
    engine_release = threading.Event()
    player_reached = threading.Event()
    player_release = threading.Event()

    engine = HoldingEngine(engine_reached, engine_release)
    player = HoldingPlayer(player_reached, player_release)

    def driver() -> None:
        assert engine_reached.wait(timeout=5), "producer never reached the third segment"
        assert player_reached.wait(timeout=5), "consumer never finished playing the first"
        cancel.set()
        player_release.set()
        engine_release.set()

    driver_thread = threading.Thread(target=driver, daemon=True)
    driver_thread.start()

    result = speak([piece("One. Two. Three.")], engine, player, cancel=cancel)
    driver_thread.join(timeout=5)

    assert player.stopped is True
    assert result.cancelled is True
    assert len(player.played) == 1
    assert not driver_thread.is_alive()
    assert threading.active_count() == baseline


def test_a_raising_player_does_not_leak_the_producer_thread() -> None:
    # The producer will have a second unit ready (or in flight) when
    # play() raises on the first. Without draining on the recorded-failure
    # path, the producer's later put blocks forever on the depth-one queue
    # with no reader left: this test fails against an implementation that
    # skips the drain and passes against the shared drain-in-finally fix.
    before = set(threading.enumerate())
    player = RaisingPlayer()

    result = speak([piece("One. Two.")], FakeEngine(), player)

    assert result.aborted is True
    new_threads = set(threading.enumerate()) - before
    for new_thread in new_threads:
        new_thread.join(timeout=2.0)
    assert not any(new_thread.is_alive() for new_thread in new_threads)


def test_a_raising_player_does_not_set_a_caller_supplied_cancel_event() -> None:
    # Teardown after a recorded player failure used to run through the
    # caller's own Event. The caller then saw cancelled=True for an
    # utterance nobody cancelled, and the next speak() reusing that Event
    # played nothing, reported no error and returned an empty timeline — a
    # silent failure.
    cancel = threading.Event()
    player = RaisingPlayer()

    result = speak([piece("One. Two.")], FakeEngine(), player, cancel=cancel)

    assert result.aborted is True
    assert not cancel.is_set(), "speak() mutated the caller's Event"

    reused = RecordingPlayer()
    result = speak([piece("One. Two.")], FakeEngine(), reused, cancel=cancel)
    assert len(reused.played) == 2
    assert len(result.timeline) == 2
    assert result.cancelled is False
    assert result.errors == []


def test_a_cancelled_utterance_leaves_the_caller_event_as_the_caller_set_it() -> None:
    # The other half of the same property: speak() reports cancellation, it
    # does not author it. An Event the caller never set stays unset.
    cancel = threading.Event()
    player = RecordingPlayer()
    result = speak([piece("One. Two. Three.")], FakeEngine(), player, cancel=cancel)
    assert not cancel.is_set()
    assert result.cancelled is False
    assert len(player.played) == 3


def test_segments_record_when_playback_started() -> None:
    player = RecordingPlayer()
    result = speak([piece("One. Two.")], FakeEngine(), player)
    stamps = [s.played_at for s in result.timeline.segments]
    assert all(s is not None for s in stamps)
    assert stamps == sorted(s for s in stamps if s is not None)


def test_audio_offset_stays_nominal_while_played_at_is_real() -> None:
    # A slow engine stalls playback: the nominal offsets stay tight while the
    # wall-clock stamps spread out. That gap is the thing subscribers must see.
    player = RecordingPlayer()
    engine = FakeEngine(sample_rate=1000, chars_per_second=1000.0, synthesis_cost=0.05)
    result = speak([piece("One. Two. Three.")], engine, player)
    segments = result.timeline.segments
    nominal = segments[-1].audio_offset - segments[0].audio_offset
    assert segments[0].played_at is not None and segments[-1].played_at is not None
    real = segments[-1].played_at - segments[0].played_at
    assert real > nominal


def test_a_raising_player_is_recorded_not_raised() -> None:
    player = RaisingPlayer()
    result = speak([piece("One. Two. Three.")], FakeEngine(), player)
    assert result.aborted is True
    assert len(result.errors) == 1
    assert "player" in result.errors[0].lower() or "device" in result.errors[0].lower()


def test_a_raising_player_keeps_the_timeline_built_so_far() -> None:
    player = RaisingPlayer(fail_on_call=2)
    result = speak([piece("One. Two. Three.")], FakeEngine(), player)
    assert len(result.timeline) == 1
    assert result.timeline.segments[0].text == "One."
    assert result.aborted is True


def test_a_clean_run_is_not_aborted() -> None:
    result = speak([piece("One.")], FakeEngine(), RecordingPlayer())
    assert result.aborted is False


def test_speak_records_what_each_synthesis_cost_against_the_audio_it_made() -> None:
    window = SynthesisWindow()
    speak([piece("One. Two.")], FakeEngine(synthesis_cost=0.02), RecordingPlayer(), window=window)
    rtf = window.rtf()
    assert len(window) == 2
    assert rtf is not None
    # 0.02s a call against roughly 0.24s of audio a segment.
    assert 0.02 < rtf < 0.3, f"rtf was {rtf:.3f}"


def test_a_segment_that_failed_to_synthesise_is_not_counted_as_audio() -> None:
    window = SynthesisWindow()
    speak([piece("One. Two.")], ExplodingEngine("One."), RecordingPlayer(), window=window)
    assert len(window) == 1, "a call that produced nothing was recorded as if it had"


def test_speak_fills_a_timeline_the_caller_supplied() -> None:
    timeline = Timeline()
    result = speak([piece("One. Two.")], FakeEngine(), RecordingPlayer(), timeline=timeline)
    assert result.timeline is timeline
    assert len(timeline) == 2


def test_a_supplied_timeline_can_be_read_while_it_is_still_being_built() -> None:
    """The daemon reads drift off this from another thread mid-utterance."""
    timeline = Timeline()
    player = SleepingPlayer(cost=0.1)
    driver = threading.Thread(
        target=speak,
        args=([piece("One. Two. Three.")], FakeEngine(), player),
        kwargs={"timeline": timeline},
    )
    driver.start()
    try:
        deadline = time.monotonic() + 5.0
        while len(timeline) < 1 and time.monotonic() < deadline:
            time.sleep(0.002)
        assert len(timeline) >= 1, "nothing was readable until speak() returned"
        assert driver.is_alive(), "the utterance was over before the timeline was read"
    finally:
        driver.join(timeout=5.0)
    assert not driver.is_alive()


# --- Reporting each segment as it begins playing ---


def test_a_segment_is_reported_while_it_is_still_being_spoken() -> None:
    """The report has to reach the caller during the sentence, not after it.

    Reporting off the finished timeline passes every test that only asks
    whether positions came out: they all do, in one burst, once playback is
    over. The property that was missing is this one -- the first segment is in
    the caller's hands while `play()` of that same segment has not returned.
    An implementation that reports after a successful `play()` instead fails
    here, which is the point: it would still report every segment, a whole
    sentence late.
    """
    reached = threading.Event()
    release = threading.Event()
    player = HoldingPlayer(reached, release)
    seen: list[Segment] = []
    arrived = threading.Event()

    def note(segment: Segment) -> None:
        seen.append(segment)
        arrived.set()

    driver = threading.Thread(
        target=speak,
        args=([piece("One. Two. Three.")], FakeEngine(), player),
        kwargs={"on_playing": note},
        daemon=True,
    )
    driver.start()
    try:
        assert reached.wait(timeout=5.0), "playback of the first segment never began"
        assert arrived.wait(timeout=5.0), "the first segment was not reported while it played"
        assert [segment.text for segment in seen] == ["One."]
    finally:
        release.set()
        driver.join(timeout=5.0)
    assert not driver.is_alive()


def test_every_segment_played_is_reported_exactly_once_and_in_order() -> None:
    """One report per segment, and the same segment the timeline gets.

    Same object, not merely equal fields: the daemon turns each report into a
    `position` event, and the timeline is what seek and resume read. Building
    the two separately is how they drift.
    """
    seen: list[Segment] = []
    result = speak(
        [piece("One. Two. Three.")], FakeEngine(), RecordingPlayer(), on_playing=seen.append
    )
    assert [segment.text for segment in seen] == ["One.", "Two.", "Three."]
    assert seen == list(result.timeline.segments), "the reports and the timeline disagree"


def test_a_raising_callback_does_not_cost_the_utterance() -> None:
    """A caller's broken callback must not silence what is left to say.

    Recorded like a failed synthesis rather than raised, and delivery carries
    on: a callback that trips over one segment's text still gets the rest.
    """

    def explode(segment: Segment) -> None:
        raise RuntimeError("monitor blew up")

    player = RecordingPlayer()
    result = speak([piece("One. Two. Three.")], FakeEngine(), player, on_playing=explode)
    assert len(player.played) == 3, "a broken callback silenced the rest of the utterance"
    assert len(result.timeline) == 3
    assert result.aborted is False
    assert len(result.errors) == 3, f"one report per failure, got {result.errors}"
    assert all("monitor blew up" in message for message in result.errors)


def test_a_callback_that_never_returns_does_not_hold_up_playback() -> None:
    """The third liveness bug this project must not ship.

    Two are already fixed: an unbounded audio write that wedged the daemon for
    an hour, and an event subscriber that stopped reading its socket and
    wedged it permanently. Both were arbitrary code on the speech worker's
    path that stopped returning. A position callback is arbitrary code too --
    in the daemon it ends inside a bus subscriber -- so the speech thread must
    never be the thread that calls it. Here the callback never returns at all:
    every segment must still play, and `speak()` must still come back.
    """
    stuck = threading.Event()
    release = threading.Event()

    def wedge(segment: Segment) -> None:
        stuck.set()
        assert release.wait(timeout=30.0), "the test never released the callback"

    player = RecordingPlayer()
    results: list[SpeechResult] = []

    def run() -> None:
        results.append(speak([piece("One. Two. Three.")], FakeEngine(), player, on_playing=wedge))

    driver = threading.Thread(target=run, daemon=True)
    driver.start()
    try:
        assert stuck.wait(timeout=5.0), "the callback was never called"
        driver.join(timeout=10.0)
        assert not driver.is_alive(), "speak() waited out a callback that never returned"
        assert len(player.played) == 3, "playback stopped short while the callback was stuck"
        assert len(results[0].timeline) == 3
        assert any("callback" in message for message in results[0].errors), (
            f"a wedged callback went unreported: {results[0].errors}"
        )
    finally:
        release.set()


def test_a_segment_whose_playback_failed_is_reported_but_not_in_the_timeline() -> None:
    """The one behaviour this change moves, and the reason it has to move.

    The report goes out before `play()`, because after it the sentence has
    already been spoken. Nothing can know at that moment that `play()` is
    about to raise, so a segment whose playback fails is now reported --
    where reporting off the timeline stayed silent about it, the timeline
    only ever receiving a segment that played. That silence was the wrong
    half of the trade: a sink that fails mid-write has already put part of
    that segment through the speakers, and this daemon's promise is that
    nothing is spoken without being reported.

    The timeline itself is untouched, which is what seek, resume and the
    span-to-time map read.
    """
    seen: list[Segment] = []
    player = RaisingPlayer(fail_on_call=2)
    result = speak([piece("One. Two. Three.")], FakeEngine(), player, on_playing=seen.append)
    assert [segment.text for segment in seen] == ["One.", "Two."]
    assert [segment.text for segment in result.timeline.segments] == ["One."]
    assert result.aborted is True


def test_reporting_leaves_no_thread_behind() -> None:
    """One notifier thread per utterance, and it leaves with the utterance."""
    baseline = threading.active_count()
    speak([piece("One. Two.")], FakeEngine(), RecordingPlayer(), on_playing=lambda _s: None)
    assert threading.active_count() == baseline


def three_sentences() -> list[Piece]:
    text = "One thing. Two things. Three things."
    return [Piece(span=Span(0, len(text)), spoken=text)]


def test_the_result_carries_the_whole_segmentation() -> None:
    # Seek and rewind both mean "speak this again from unit k", and the
    # caller cannot index units it was never given. Returning them is what
    # lets the daemon re-speak without re-segmenting -- and without having to
    # assume segmentation is deterministic, which is the assumption the GUI
    # milestone doc flagged as the alternative.
    result = speak(three_sentences(), FakeEngine(), RecordingPlayer())
    assert [u.spoken for u in result.units] == ["One thing.", "Two things.", "Three things."]


def test_start_index_skips_the_units_before_it() -> None:
    engine, player = FakeEngine(), RecordingPlayer()
    result = speak(three_sentences(), engine, player, start_index=1)
    assert [s.text for s in result.timeline.segments] == ["Two things.", "Three things."]


def test_start_index_still_reports_the_whole_segmentation() -> None:
    # Otherwise seeking once would make every later seek target the wrong
    # unit, each one compounding the last.
    result = speak(three_sentences(), FakeEngine(), RecordingPlayer(), start_index=2)
    assert len(result.units) == 3


def test_a_start_index_past_the_end_speaks_nothing() -> None:
    player = RecordingPlayer()
    result = speak(three_sentences(), FakeEngine(), player, start_index=99)
    assert not result.timeline.segments
    assert player.played == []


def test_a_negative_start_index_starts_at_the_beginning() -> None:
    # Clamped rather than passed to the slice: Python would read -1 as "the
    # last unit", so a caller that computed an index one step below zero
    # would hear the end of the utterance instead of the start of it, with
    # nothing raised to say so.
    result = speak(three_sentences(), FakeEngine(), RecordingPlayer(), start_index=-1)
    assert [s.text for s in result.timeline.segments] == [
        "One thing.",
        "Two things.",
        "Three things.",
    ]


def test_start_offset_continues_the_audio_clock() -> None:
    # A seek is a position within one utterance, so the offsets it reports
    # have to stay comparable with the ones reported before it. Restarting at
    # zero would make a progress display jump backwards.
    result = speak(
        three_sentences(), FakeEngine(), RecordingPlayer(), start_index=1, start_offset=10.0
    )
    assert result.timeline.segments[0].audio_offset == 10.0
    assert result.timeline.segments[1].audio_offset > 10.0


def test_start_offset_defaults_to_zero() -> None:
    result = speak(three_sentences(), FakeEngine(), RecordingPlayer())
    assert result.timeline.segments[0].audio_offset == 0.0


class CountingEngine(FakeEngine):
    def __init__(self) -> None:
        super().__init__()
        self.calls: list[tuple[str, float]] = []

    def synthesize(self, text: str, voice: str, speed: float, lang: str = "en") -> np.ndarray:
        self.calls.append((text, speed))
        return super().synthesize(text, voice, speed, lang)


def test_given_units_are_spoken_as_given() -> None:
    units = [Piece(span=Span(0, 4), spoken="One."), Piece(span=Span(5, 9), spoken="Two.")]
    player = RecordingPlayer()
    result = speak([], FakeEngine(), player, units=units)
    assert result.units == tuple(units)
    assert len(player.played) == 2


def test_segments_carry_their_absolute_index() -> None:
    seen: list[int] = []
    speak(
        [piece("One. Two. Six.")],
        FakeEngine(),
        RecordingPlayer(),
        start_index=1,
        on_playing=lambda seg: seen.append(seg.index),
    )
    assert seen == [1, 2]


def test_a_cached_unit_is_not_synthesised_again() -> None:
    engine = CountingEngine()
    cache = AudioCache()
    pieces = [piece("One. Two.")]
    speak(pieces, engine, RecordingPlayer(), cache=cache)
    speak(pieces, engine, RecordingPlayer(), cache=cache)
    assert len(engine.calls) == 2


def test_new_audio_is_made_at_the_tempo() -> None:
    engine = CountingEngine()
    speak([piece("One.")], engine, RecordingPlayer(), speed=1.1, tempo=Tempo(1.5))
    [(text, speed)] = engine.calls
    assert text == "One."
    assert speed == pytest.approx(1.65)


def test_a_stretchable_player_is_held_to_the_tempo() -> None:
    sink = FakeSink()
    result = speak(
        [piece("One.")],
        FakeEngine(),
        StreamingPlayer(sink),
        speed=1.0,
        tempo=Tempo(1.0),
    )
    assert result.timeline.segments[0].duration == sink.frames_written / 24000


def test_the_cache_evicts_furthest_from_where_playback_is() -> None:
    cache = AudioCache(max_bytes=3 * 400)
    for i in range(4):
        cache.near = i
        cache.put(i, np.zeros(100, np.float32), 1.0)
    assert cache.get(0) is None
    assert cache.get(3) is not None


def test_waiting_for_synthesis_is_announced_before_the_segment_plays() -> None:
    events: list[tuple[str, int]] = []
    speak(
        [piece("One. Two.")],
        FakeEngine(synthesis_cost=0.05),
        RecordingPlayer(),
        on_playing=lambda seg: events.append(("playing", seg.index)),
        on_waiting=lambda index: events.append(("waiting", index)),
    )
    assert events[:2] == [("waiting", 0), ("playing", 0)]
    assert ("playing", 1) in events
    # Never announced for a segment past the end, however the last one ended.
    assert all(i < 2 for kind, i in events if kind == "waiting")


def test_audio_already_made_is_not_waited_for() -> None:
    cache = AudioCache()
    pieces = [piece("One.")]
    speak(pieces, FakeEngine(), RecordingPlayer(), cache=cache)
    waits: list[int] = []
    speak(
        pieces,
        FakeEngine(synthesis_cost=0.05),
        SleepingPlayer(0.05),
        cache=cache,
        on_playing=lambda seg: None,
        on_waiting=waits.append,
    )
    assert waits == []
