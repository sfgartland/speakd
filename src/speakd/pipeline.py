"""Streaming synthesis: play segment N while segment N+1 is being made.

A queue of depth one is deliberate. Deeper buffering would synthesise far
ahead of playback, which wastes work when speech is cancelled — and speech
is cancelled often, because the user typing is a cancel.
"""

from __future__ import annotations

import queue
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

import numpy as np

from speakd.metrics import SynthesisWindow
from speakd.model import Piece, Segment
from speakd.player import Player
from speakd.segmenter import DEFAULT_MAX_CHARS, segment
from speakd.synth import Synthesizer
from speakd.timeline import Timeline


@dataclass
class SpeechResult:
    timeline: Timeline
    cancelled: bool = False
    aborted: bool = False
    errors: list[str] = field(default_factory=list)


_Item = tuple[Piece, np.ndarray] | str | None

# How long `speak()` waits, once playback is over, for the reports it has
# already handed over to reach the callback. Long enough that a subscriber
# briefly behind still gets every position out before the caller says the
# utterance finished; short enough that one that has stopped returning
# altogether delays the next utterance by a second rather than by itself.
_DELIVERY_SECONDS = 1.0


class _Announcer:
    """Tells a caller which segment is playing, off the speech thread.

    The callback belongs to whoever called `speak()`. In the daemon it
    publishes on the event bus, and a bus subscriber is arbitrary code that
    someone else wrote. Two liveness bugs in this project were arbitrary code
    on the speech worker's path that stopped returning -- an unbounded audio
    write, and a subscriber that stopped reading its socket -- so calling the
    callback inline, between stamping a segment and playing it, would make a
    third. The speech thread only ever puts; this thread does the calling.

    The queue is unbounded on purpose. A bound would have to choose between
    blocking the speech thread and dropping a report, and dropping one breaks
    the promise that nothing is spoken without being reported. What bounds it
    in practice is the utterance: a few dozen segments, each a sentence.
    """

    def __init__(self, callback: Callable[[Segment], None]) -> None:
        self._callback = callback
        self._pending: queue.Queue[Segment | None] = queue.Queue()
        self._errors: list[str] = []
        self._thread = threading.Thread(target=self._deliver, daemon=True)
        self._thread.start()

    def announce(self, segment: Segment) -> None:
        """Hand over one segment. Never blocks, never raises."""
        self._pending.put(segment)

    def _deliver(self) -> None:
        while True:
            segment = self._pending.get()
            if segment is None:
                return
            try:
                self._callback(segment)
            except BaseException as exc:  # noqa: B036 - re-raising would end delivery
                # Recorded like a failed synthesis rather than raised, and
                # delivery carries on: a callback that trips over one
                # segment must not cost every later segment its report.
                # `BaseException` for the same reason -- a callback raising
                # something outside `Exception` would otherwise kill this
                # thread and take the rest of the utterance's reports with
                # it, silently, which is the one thing this daemon must not do.
                self._errors.append(f"position callback for {segment.text[:40]!r}: {exc!r}")

    def close(self, timeout: float) -> list[str]:
        """Deliver what is queued, then leave, saying what went wrong.

        Bounded, because the thing being waited for is the callback. When it
        does not return in time the wait is abandoned rather than extended:
        the thread is a daemon thread holding nothing but its own queue, it
        goes on delivering in the background if the callback ever comes back,
        and a caller told about it can say so.
        """
        self._pending.put(None)
        self._thread.join(timeout)
        # Copied rather than handed over: on the timeout path this thread is
        # still running and may append again, and the caller's list must not
        # grow under it after `speak()` has returned.
        errors = list(self._errors)
        if self._thread.is_alive():
            errors.append(
                f"position reports are still pending after {timeout:.1f}s: "
                "the callback has not returned"
            )
        return errors


def speak(
    pieces: Sequence[Piece],
    engine: Synthesizer,
    player: Player,
    *,
    voice: str = "af_heart",
    speed: float = 1.1,
    cancel: threading.Event | None = None,
    max_chars: int = DEFAULT_MAX_CHARS,
    window: SynthesisWindow | None = None,
    timeline: Timeline | None = None,
    on_playing: Callable[[Segment], None] | None = None,
) -> SpeechResult:
    """Speak `pieces`, returning the timeline of what was actually played.

    `cancel` is read, never written. A caller's Event is theirs: mutating it
    would make a torn-down utterance look cancelled to whoever owns it, and
    the next `speak()` reusing that Event would play nothing at all.
    Internal teardown goes through `stop` instead.

    `window` and `timeline` are for a caller that has to watch this from
    another thread. Timing belongs to the pipeline -- engines synthesise one
    unit and nothing else -- so this is where a synthesize() call can be timed
    against the audio it produced, and `timeline` is the same map that comes
    back in the result, handed in so it can be read while it is still being
    built rather than only once the utterance is over. A supplied timeline
    should be empty: this starts its audio clock at zero. Neither argument is
    required -- without them nothing is recorded and nobody is watching.

    `on_playing` is called with each segment as that segment starts playing,
    which is the only moment at which a report of it is still news: `play()`
    blocks for the length of its audio, so anything sent after it describes a
    sentence already spoken. It is called once per segment, in playback
    order, with the same `Segment` object the timeline gets, and it is called
    from a thread of this function's own -- so a callback that raises is
    recorded in `errors` rather than raised, and one that never returns costs
    the utterance nothing but the bounded wait at the end. A segment whose
    `play()` then fails has already been announced; see `_Announcer` and the
    note at the call below for why that is the right way round.
    """
    cancel = cancel or threading.Event()
    # Only when somebody is listening: a caller that passes no callback pays
    # neither a thread nor a queue for a report nobody asked for.
    announcer = _Announcer(on_playing) if on_playing is not None else None
    # Internal teardown flag. Distinct from `cancel` so the caller's Event
    # stays untouched and `speak()` is a pure function of its arguments.
    stop = threading.Event()
    units = segment(pieces, max_chars)
    work: queue.Queue[_Item] = queue.Queue(maxsize=1)

    def produce() -> None:
        try:
            for unit in units:
                if cancel.is_set() or stop.is_set():
                    break
                started = time.monotonic()
                try:
                    audio = engine.synthesize(unit.spoken, voice, speed)
                except Exception as exc:  # speech must not vanish on one bad segment
                    work.put(f"{unit.spoken[:40]!r}: {exc}")
                    continue
                if window is not None:
                    # Recorded here rather than by the consumer below, so the
                    # real-time factor moves as soon as synthesis is done --
                    # it is the leading indicator, and waiting for playback to
                    # reach the segment would delay it by a segment. A call
                    # that raised is not recorded: it produced no audio, and
                    # seconds per second of nothing is not a measurement. The
                    # window's own lock is held for an append and no more, so
                    # this cannot stall the producer.
                    window.record(time.monotonic() - started, len(audio) / engine.sample_rate)
                work.put((unit, audio))
        finally:
            work.put(None)

    worker = threading.Thread(target=produce, daemon=True)
    worker.start()

    timeline = timeline if timeline is not None else Timeline()
    errors: list[str] = []
    delivery: list[str] = []
    offset = 0.0
    exhausted = False
    aborted = False
    try:
        while True:
            item = work.get()
            if item is None:
                exhausted = True
                break
            if isinstance(item, str):
                errors.append(item)
                continue
            if cancel.is_set():
                player.stop()
                break
            unit, audio = item
            duration = len(audio) / engine.sample_rate
            # Stamped here, not after play() returns: this is when playback
            # of the segment actually starts, which is what subscribers of
            # `played_at` need. `audio_offset` above stays nominal instead —
            # accumulated from durations alone — because seek and resume
            # describe the audio, not the wall clock. The two diverge exactly
            # when synthesis stalls playback, and that divergence is the
            # point: collapsing them into one measured number would hide it.
            started = time.monotonic()
            played = Segment(
                span=unit.span,
                text=unit.spoken,
                audio_offset=offset,
                duration=duration,
                played_at=started,
            )
            if announcer is not None:
                # Here, and not after `play()` returns: `play()` blocks for
                # the length of the audio, so a report sent after it reaches
                # a monitor a whole sentence late -- which is the bug this
                # argument exists to fix. The cost of being early is that a
                # segment whose `play()` is about to raise has already been
                # announced, where building the reports out of the finished
                # timeline left it unmentioned. That is the better half of
                # the trade: a sink that fails mid-write has already put part
                # of the segment through the speakers, and nothing spoken may
                # go unreported. The timeline below is unaffected.
                announcer.announce(played)
            try:
                player.play(audio, engine.sample_rate)
            except Exception as exc:
                # A sink vanishing mid-utterance is routine for a daemon: a
                # headset walking out of range, say. Recording the failure
                # rather than raising it keeps the position map built so
                # far — discarding it would defeat resume in exactly the
                # case where resume matters most.
                errors.append(f"player: {exc}")
                aborted = True
                break
            # Appended only once playback has actually succeeded, so a
            # segment whose play() call failed is never added: the timeline
            # returned on the aborted path is exactly what was played.
            timeline.append(played)
            offset += duration
    finally:
        if not exhausted:
            # Reached by cancellation discovered above, or by a player
            # failure recorded and broken out of above. Either way the
            # producer may already be past its own cancel check for the next
            # unit — or simply mid-synthesis, unaware anything has gone
            # wrong — and about to put once more. With a depth-one queue and
            # no reader left, that put (including its unconditional final
            # put(None)) would block forever, leaking the thread. Setting
            # `stop` makes the producer stop at its next check; draining to
            # the sentinel guarantees it always has a reader until it
            # actually exits.
            #
            # This delays speak()'s return by at most one in-flight
            # synthesize() call. Audio itself has already stopped on the
            # cancellation path, since player.stop() precedes this. An error
            # string queued after this point is discarded here rather than
            # reaching SpeechResult.errors — the one place a synthesis
            # failure is allowed to go unreported, because the utterance is
            # already being torn down.
            stop.set()
            while work.get() is not None:
                pass
        if announcer is not None:
            # In the `finally`, so that a `BaseException` on its way out of
            # play() does not leave the notifier thread parked on a queue
            # nobody will ever close.
            delivery = announcer.close(_DELIVERY_SECONDS)

    errors.extend(delivery)
    worker.join(timeout=1.0)
    if worker.is_alive():
        # The drain above should make this unreachable. Report it rather than
        # returning quietly: in a long-lived daemon a regression here
        # accumulates stuck threads, one per utterance, with nothing to see.
        errors.append("synthesis thread did not exit within 1.0s")
    return SpeechResult(
        timeline=timeline, cancelled=cancel.is_set(), aborted=aborted, errors=errors
    )
