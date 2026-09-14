"""Playback sinks.

`play` blocks until the audio has finished, which is what lets the pipeline
use playback as its clock while a producer thread runs ahead.
"""

from __future__ import annotations

import sys
import threading
import time
from typing import TYPE_CHECKING, NamedTuple, Protocol, runtime_checkable

import numpy as np

if TYPE_CHECKING:
    import sounddevice as sd


class Player(Protocol):
    def play(self, audio: np.ndarray, sample_rate: int) -> None: ...

    def stop(self) -> None: ...


@runtime_checkable
class Pausable(Protocol):
    """A player whose playback can be suspended and taken up again.

    Deliberately separate from `Player`: the pipeline never needs to pause,
    and folding these into `Player` would make every test double implement
    two methods and a property it has no use for. The daemon asks with
    `isinstance` and refuses the verb, naming the player, when the answer is
    no — "this daemon's player cannot pause" is something a caller can act
    on, where "not implemented" is not.

    `runtime_checkable` checks only that the attributes exist, not their
    signatures. That is what is wanted here: the question is whether this
    player can pause at all.
    """

    def pause(self) -> None: ...

    def resume(self) -> None: ...

    @property
    def paused(self) -> bool: ...


@runtime_checkable
class Closeable(Protocol):
    """A player holding a resource that outlives every utterance it plays.

    Separate from `Player` for the same reason as `Pausable`: nothing in the
    speech path ever closes anything, and folding this in would make every
    test double implement a method it has no use for. The entry point asks
    with `isinstance` on its way down, and only there.
    """

    def close(self) -> None: ...


class RecordingPlayer:
    """Records what it was asked to play. For tests."""

    def __init__(self) -> None:
        self.played: list[np.ndarray] = []
        self.timestamps: list[float] = []
        self.stopped = False

    def play(self, audio: np.ndarray, sample_rate: int) -> None:
        self.played.append(audio)
        self.timestamps.append(time.monotonic())

    def stop(self) -> None:
        self.stopped = True


class SoundDevicePlayer:
    """Plays through the system audio device.

    Superseded by `StreamingPlayer` and feature-frozen. It is still the
    shipping path only because the interruptible player is not yet wired into
    the CLI; new playback behaviour goes into `StreamingPlayer`, so that the
    two cannot drift while both exist.
    """

    def play(self, audio: np.ndarray, sample_rate: int) -> None:
        import sounddevice as sd

        sd.play(audio, samplerate=sample_rate)
        sd.wait()

    def stop(self) -> None:
        import sounddevice as sd

        sd.stop()


def _dropped(frames: int, reason: str) -> str:
    """The one wording both sinks use, so the fake cannot drift from the real."""
    return f"dropped {frames} frames: {reason}"


# Enough to show a pattern — a repeated message is the diagnosis, and thirty-two
# of them is plenty to see one — and small enough that the whole log stays
# readable when something dumps it.
_MAX_ERRORS = 32


class _ErrorLog(list[str]):
    """A bounded record of what went wrong.

    A device that has begun failing fails repeatedly, and this daemon runs for
    days, so an unbounded list is a slow leak that answers "what went wrong"
    ten thousand times over. The first `_MAX_ERRORS` messages are kept — the
    first ones, because they are the ones that say how the trouble started —
    and everything after them becomes a single running tally at the end, so
    nothing disappears without saying that it did.

    A list subclass rather than a helper on each sink: one implementation is
    one fewer thing for the two sinks to drift apart on.

    It carries its own lock rather than relying on the caller's. The playback
    thread records a write it lost and the control thread records a teardown
    that failed, and no single caller-held lock covers both: `SoundDeviceSink`
    records the mid-write drop *after* releasing `_lock`, and `FakeSink` has no
    lock at all. Two threads crossing the cap together would otherwise both
    append the tally — permanently, since later drops overwrite the last entry
    and never reach the duplicate beneath it — or overwrite a real message with
    it. A bounded log that depends on its callers to hold the right lock is a
    contract nobody will keep.
    """

    def __init__(self) -> None:
        super().__init__()
        self.dropped = 0
        self._lock = threading.Lock()

    def append(self, message: str) -> None:
        with self._lock:
            if len(self) < _MAX_ERRORS:
                super().append(message)
                return
            self.dropped += 1
            tally = f"... and {self.dropped} further errors not recorded"
            if self.dropped == 1:
                super().append(tally)
            else:
                self[-1] = tally


class AudioDeviceWedged(RuntimeError):
    """A single write stayed inside the device far longer than its own audio.

    Deliberately not the recorded drop a hush produces, and raised rather than
    recorded for that reason. A hush is expected, and turning one into a
    crashed utterance would be worse than the hush; a device that has stopped
    taking audio is not expected, every remaining chunk of the utterance would
    cost another full timeout against it, and a recorded drop reaches
    `sink.errors` and stops there. Raising is what carries the failure up
    through `speak()` to the event bus, which is where a user can see it.
    """


def _wedged(frames: int, seconds: float) -> str:
    """The one wording for a bounded-out write, so the log and the raise agree."""
    return (
        f"audio device wedged: a {frames}-frame write did not return within "
        f"{seconds:.1f}s; the stream was aborted to release the speech thread"
    )


def _to_journal(message: str) -> None:
    """Say it where the operator of a systemd unit will find it.

    Straight to stderr, like every other operator-facing line in this project:
    journald captures a unit's stderr and the daemon configures no logging. A
    device that wedges and then recovers in silence is barely better than one
    that stays wedged, and the event bus alone is not enough — a daemon whose
    speech worker is stuck may have no subscriber listening at all.

    Guarded, because this runs on the watchdog thread: a stderr closed
    underneath it, which is what the end of a test looks like, must not be the
    thing that kills the watchdog.
    """
    try:
        sys.stderr.write(f"speakd: {message}\n")
        sys.stderr.flush()
    except Exception:
        pass


# How long one blocking write may stay inside the device before the watchdog
# aborts the stream out from under it.
#
# A blocking `OutputStream.write()` comes back in about the time its own audio
# takes to play: 2048 frames at 24 kHz is 85 ms. The floor is therefore some
# fifty-nine chunks' worth, which nothing short of a fault reaches. It has to
# clear, with room over: a machine loaded hard enough that the speech thread
# waits whole scheduler quanta — this bug first showed on a laptop compiling
# Rust at 97% CPU — a PipeWire sink resuming from suspend, which is order a
# second, and an xrun the server recovers from. Five seconds is several times
# the worst of those, and still short enough that a wedge costs one audible
# gap instead of the tens of minutes the daemon actually spent stuck.
#
# The factor keeps the bound honest for a caller that writes more than a chunk
# at a time: the bound is never below eight times the audio the write carries,
# so a sink handed a ten-second block gets eighty seconds rather than five.
# `StreamingPlayer` writes 2048 frames, for which the floor always wins.
_MIN_WRITE_TIMEOUT = 5.0
_WRITE_TIMEOUT_FACTOR = 8.0

# One thread per sink, never one per write. Named, because this fix exists
# because of what a `py-spy dump` showed, and the next dump should say which
# thread this is.
_WATCHDOG_THREAD_NAME = "speakd-write-watchdog"

# Long enough for an abort() that is merely slow, short enough that a shutdown
# is not held up by a device that has stopped answering altogether.
_WATCHDOG_JOIN_TIMEOUT = 2.0


class _InFlightWrite(NamedTuple):
    """One write the watchdog is timing."""

    deadline: float
    stream: sd.OutputStream
    frames: int


class AudioSink(Protocol):
    """Where audio frames go. Injected so tests never open a device."""

    frames_written: int
    errors: list[str]

    def start(self) -> None: ...

    def write(self, frames: np.ndarray) -> None: ...

    def stop(self) -> None: ...

    def close(self) -> None: ...


class FakeSink:
    """Records frames instead of playing them.

    Mirrors `SoundDeviceSink`'s state machine and not merely its method names:
    `stop()` leaves the sink reusable but not writable, `start()` makes it
    writable again, and `close()` is terminal. A fake more permissive than the
    sink it stands in for is a fake that certifies bugs — which is how the two
    real lifecycle bugs here survived a green suite.
    """

    def __init__(self) -> None:
        self.blocks: list[np.ndarray] = []
        self.frames_written = 0
        self.errors: list[str] = _ErrorLog()
        self.started = False
        self.stopped = False
        self.closed = False
        self._aborted = False

    def start(self) -> None:
        if self.closed:
            raise RuntimeError("FakeSink.start() after close()")
        self._aborted = False
        self.started = True

    def write(self, frames: np.ndarray) -> None:
        if self.closed:
            raise RuntimeError("FakeSink.write() after close()")
        if self._aborted:
            self.errors.append(_dropped(len(frames), "write() after stop()"))
            return
        self.started = True
        self.blocks.append(frames.copy())
        self.frames_written += len(frames)

    def stop(self) -> None:
        self.stopped = True
        self._aborted = True

    def close(self) -> None:
        self.closed = True


class SoundDeviceSink:
    """Writes to the system audio device through its own stream.

    Its own, deliberately: `sd.play`/`sd.stop` drive one process-wide stream, so
    with several channels one session's stop would silence another's audio.

    The lifecycle is the delicate part. `stop()` runs on the control thread
    while the playback thread is usually blocked inside `write()` — a blocking
    `OutputStream.write()` blocks for the whole duration of the chunk it is
    playing, so a stop during playback lands inside a write almost always,
    which makes this the common path rather than a rare race:

    - `stop()` aborts the stream and does nothing else. It does not close and
      does not drop the reference. `Pa_CloseStream` on a stream another thread
      is blocked writing to is undefined behaviour, not a catchable exception.
    - `write()` between a `stop()` and the next `start()` is a recorded no-op.
      It must not reopen the device, or `stop()` could not stop anything: the
      next chunk would bring the audio straight back.
    - `start()` reaps the aborted stream and opens a fresh one, which is safe
      because by then no thread is inside it.
    - `close()` is terminal. A later `write()` or `start()` raises.

    Every `_stream` transition happens under `_lock`; `write()` takes a local
    reference under the lock and writes outside it, so a `stop()` never waits
    on a write that is waiting on the device.

    A write that fails *because we just aborted it* is the expected
    consequence of stopping, so it lands in `errors` rather than raising —
    raising would turn every hush into a crashed utterance. Any other failure
    still propagates.

    The one call none of that covers is the blocking write itself. It takes no
    timeout, and on real hardware it has been seen not to return at all: a
    `py-spy dump` of a live daemon found the speech thread parked in
    `_raw_write` for tens of minutes while the daemon went on accepting speech
    it would never say, with nothing on the bus and nothing in the journal. So
    every write is timed, by a watchdog thread this sink owns, and one that
    stays inside the device past `_write_bound` has its stream aborted from
    outside — the only thing that releases such a write — and then raises
    `AudioDeviceWedged` on its way out. See that class for why this one
    failure raises where a hush records.
    """

    def __init__(self, sample_rate: int, blocksize: int = 1024) -> None:
        self.sample_rate = sample_rate
        self.blocksize = blocksize
        self.frames_written = 0
        self.errors: list[str] = _ErrorLog()
        self._lock = threading.Lock()
        self._stream: sd.OutputStream | None = None
        self._aborted = False
        self._closed = False
        self._writers = 0
        # The watchdog shares `_lock` rather than taking one of its own: it
        # fires by aborting the stream, which is exactly what `stop()` does
        # under that lock, and a second lock would be a second ordering to
        # get wrong.
        self._watch = threading.Condition(self._lock)
        self._watchdog: threading.Thread | None = None
        self._watch_exit = False
        self._inflight: dict[int, _InFlightWrite] = {}
        self._timed_out: set[int] = set()
        self._next_write_id = 0

    def start(self) -> None:
        with self._lock:
            if self._closed:
                raise RuntimeError("SoundDeviceSink.start() after close()")
            if self._stream is not None and not self._aborted:
                # Already running: a second start() must not orphan the first
                # stream. Owning exactly one stream per sink is the reason
                # this class exists.
                return
            self._reap_locked()
            self._stream = self._open()

    def write(self, frames: np.ndarray) -> None:
        with self._lock:
            if self._closed:
                raise RuntimeError("SoundDeviceSink.write() after close()")
            if self._aborted:
                self.errors.append(_dropped(len(frames), "write() after stop()"))
                return
            if self._stream is None:
                self._stream = self._open()
            stream = self._stream
            self._writers += 1
            write_id = self._next_write_id
            self._next_write_id += 1
            bound = self._write_bound(len(frames))
            self._inflight[write_id] = _InFlightWrite(
                deadline=time.monotonic() + bound,
                stream=stream,
                frames=len(frames),
            )
            announcement = self._ensure_watchdog_locked()
            # Wakes a watchdog idling with nothing to time. Deadlines only
            # move forward, so one already asleep on an earlier deadline needs
            # no telling — but this costs a notify, not a thread, so it is not
            # worth being clever about.
            self._watch.notify_all()
        if announcement:
            _to_journal(announcement)

        failure: Exception | None = None
        ours = False
        try:
            stream.write(frames)
        except Exception as exc:
            failure = exc
        finally:
            with self._lock:
                self._writers -= 1
                # Cleared under the same lock the watchdog fires under, so a
                # write that came back on its own can never be blamed on a
                # timeout, and one the watchdog did cut loose can never slip
                # through as an ordinary success.
                self._inflight.pop(write_id, None)
                timed_out = write_id in self._timed_out
                self._timed_out.discard(write_id)
                # "Ours" as in: we are the reason this write failed, because
                # we aborted the stream out from under it.
                ours = self._aborted or self._stream is not stream

        if timed_out:
            # Ahead of the `failure is None` check on purpose: a stream that
            # released its writer cleanly after the abort still spent longer
            # than the bound inside the device, and the frames it carried are
            # not the ones that were played.
            message = _wedged(len(frames), bound)
            self.errors.append(message)
            raise AudioDeviceWedged(message) from failure
        if failure is None:
            with self._lock:
                self.frames_written += len(frames)
            return
        if not ours:
            raise failure
        self.errors.append(_dropped(len(frames), f"playback stopped mid-write ({failure})"))

    def stop(self) -> None:
        """Abort the current playback, leaving the sink reusable through `start()`.

        Abort only: see the class docstring for why closing here is the bug
        this replaces. The abort happens under the lock so a concurrent
        `start()` cannot reap the stream between the flag and the call.
        """
        with self._lock:
            stream = self._stream
            if stream is None or self._aborted or self._closed:
                return
            self._aborted = True
            try:
                stream.abort()
            except Exception as exc:
                # A hush must never become a crashed utterance. The stream is
                # unusable either way, and the next start() reaps it.
                self.errors.append(f"could not abort the stream: {exc}")

    def close(self) -> None:
        """Release the device for good. Terminal: a later write() or start() raises."""
        try:
            with self._lock:
                # Set before the already-closed check, and the join runs from
                # the `finally` regardless: every path out of close() has to
                # take the watchdog with it, or a terminal sink leaves a
                # thread behind holding a reference to the stream it aborts.
                self._watch_exit = True
                self._watch.notify_all()
                if self._closed:
                    return
                self._closed = True
                stream, self._stream = self._stream, None
                if stream is None:
                    return
                if not self._aborted:
                    try:
                        stream.abort()
                    except Exception as exc:
                        self.errors.append(f"could not abort the stream on close: {exc}")
                self._aborted = True
                if self._writers:
                    self.errors.append("closed with a write in flight: stream released unclosed")
                    return
                try:
                    stream.close()
                except Exception as exc:
                    self.errors.append(f"could not close the stream: {exc}")
        finally:
            self._join_watchdog()

    def _write_bound(self, frames: int) -> float:
        """How long this particular write may take before the watchdog acts."""
        return max(_MIN_WRITE_TIMEOUT, frames / self.sample_rate * _WRITE_TIMEOUT_FACTOR)

    def _ensure_watchdog_locked(self) -> str | None:
        """Start this sink's one watchdog thread. Caller holds `_lock`.

        Started at the first write rather than at `start()`, so a sink that
        never writes never spawns anything, and kept for the life of the sink
        rather than per write: a thread per 85 ms chunk is twelve thread
        creations a second for as long as the daemon speaks.

        Returns a line for the journal when it could not start, for the caller
        to emit once it has let go of the lock.
        """
        if self._watchdog is not None:
            return None
        self._watch_exit = False
        watchdog = threading.Thread(
            target=self._watch_writes, name=_WATCHDOG_THREAD_NAME, daemon=True
        )
        try:
            watchdog.start()
        except RuntimeError as exc:
            # Out of threads. The write still goes ahead, because refusing to
            # play is worse than playing unwatched — but unwatched is the
            # state this class exists to stop happening quietly, so it is said
            # in both places a wedge itself would be said.
            self.errors.append(f"write watchdog could not start: {exc}")
            return f"write watchdog could not start ({exc}): writes are unbounded"
        self._watchdog = watchdog
        return None

    def _watch_writes(self) -> None:
        """Run the watch, and stand down visibly if the watch itself breaks.

        A watchdog that dies quietly leaves every later write unbounded for
        the life of the process — which is the state this whole class exists
        to end, restored without a word. So the post is vacated rather than
        abandoned: it is recorded, it reaches the journal, and the next write
        starts a fresh watchdog instead of going out unwatched.
        """
        try:
            self._watch_loop()
        except Exception as exc:
            with self._lock:
                if self._watchdog is threading.current_thread():
                    self._watchdog = None
                self.errors.append(f"write watchdog stopped: {exc!r}")
            _to_journal(f"write watchdog stopped ({exc!r}): the next write will start another")

    def _watch_loop(self) -> None:
        """Cut loose any write that has been inside the device too long.

        Asleep on `_watch` whenever nothing is in flight, so an ordinary write
        costs a condition notify rather than a thread. The abort is the whole
        point: it is the only thing that releases a `sounddevice` write which
        has stopped returning, and it has to come from a thread that is not
        the one stuck inside it.
        """
        while True:
            with self._watch:
                if self._watch_exit:
                    return
                if not self._inflight:
                    self._watch.wait()
                    continue
                now = time.monotonic()
                overdue = [(i, w) for i, w in self._inflight.items() if w.deadline <= now]
                if not overdue:
                    soonest = min(w.deadline for w in self._inflight.values())
                    # Floored rather than allowed to be zero: a deadline that
                    # has just passed costs one more trip round the loop, not
                    # a spin.
                    self._watch.wait(max(soonest - now, 0.001))
                    continue
                announcements = [self._cut_loose_locked(i, w) for i, w in overdue]
            # Outside the lock deliberately. stderr is a pipe to journald, and
            # a journal that has stopped reading must not become the next
            # thing to block this sink's lock — which is the entire subject
            # of this class.
            for message in announcements:
                _to_journal(message)

    def _cut_loose_locked(self, write_id: int, write: _InFlightWrite) -> str:
        """Abort the stream a write is stuck in. Caller holds `_lock`.

        Marks the write so it raises rather than returning quietly: the
        recorded-drop path belongs to a hush, which is expected, and this is
        not.
        """
        self._timed_out.add(write_id)
        # Struck off the register first, so a stream that does not release its
        # writer even now is aborted once rather than once per pass.
        self._inflight.pop(write_id, None)
        if write.stream is self._stream:
            # Only the stream this sink still owns. A write stuck in one that
            # has since been replaced is already "ours" by identity, and
            # flagging the abort here would silence the live stream instead.
            self._aborted = True
        try:
            write.stream.abort()
        except Exception as exc:
            # Nothing further can be done for the stuck writer; say so, and
            # leave the stream for the next start() to reap.
            self.errors.append(f"could not abort the wedged stream: {exc}")
        return _wedged(write.frames, self._write_bound(write.frames))

    def _join_watchdog(self) -> None:
        """Wait for the watchdog to leave, so close() really is the end."""
        with self._lock:
            watchdog, self._watchdog = self._watchdog, None
        if watchdog is None:
            return
        watchdog.join(timeout=_WATCHDOG_JOIN_TIMEOUT)
        if watchdog.is_alive():
            # It can only be inside an abort() that has not come back, which
            # is the same device trouble it was started to survive.
            self.errors.append("the write watchdog did not exit")

    def _open(self) -> sd.OutputStream:
        """Caller holds `_lock`."""
        import sounddevice as sd

        stream = sd.OutputStream(
            samplerate=self.sample_rate,
            channels=1,
            dtype="float32",
            blocksize=self.blocksize,
        )
        stream.start()
        return stream

    def _reap_locked(self) -> None:
        """Close the stream a `stop()` abandoned. Caller holds `_lock`."""
        stream, self._stream, self._aborted = self._stream, None, False
        if stream is None:
            return
        if self._writers:
            # A writer released by abort() has not necessarily returned from
            # write() yet. Closing in that window is the undefined behaviour
            # this class exists to avoid, so the handle is released unclosed
            # instead: a leak, recorded, rather than a crash.
            self.errors.append("aborted stream released unclosed: a write was in flight")
            return
        try:
            stream.close()
        except Exception as exc:
            self.errors.append(f"could not close the aborted stream: {exc}")


class StreamingPlayer:
    """Blocks until the segment finishes, or until it is interrupted.

    Keeping `play()` blocking is deliberate: the pipeline uses playback as its
    clock, and its depth-one queue and drain-to-sentinel teardown are what stop a
    producer thread leaking on every cancellation. This class changes how promptly
    playback can be abandoned, not who rate-limits whom.

    Pause is a property of the player, not of an utterance: it spans segments
    and survives `stop()`, so a listener who paused stays paused until they
    say otherwise.
    """

    def __init__(self, sink: AudioSink, chunk_frames: int = 2048) -> None:
        self._sink = sink
        self._chunk = chunk_frames
        self._interrupt = threading.Event()
        self._resume = threading.Event()
        self._resume.set()
        self.frames_played = 0
        self.interrupted = False

    @property
    def paused(self) -> bool:
        return not self._resume.is_set()

    @property
    def errors(self) -> list[str]:
        """What the sink recorded: dropped buffers and device failures.

        A live view of the sink's own list, not a snapshot — read-only in the
        sense that the player never has its own. The pipeline holds a player,
        not a sink, so this is the only place the record is reachable from
        without touching `pipeline.py`.
        """
        return self._sink.errors

    def play(self, audio: np.ndarray, sample_rate: int) -> None:
        """Write the segment to the sink in chunks, returning when it has
        finished or when it has been interrupted.

        One window is accepted here rather than closed. A `stop()` landing
        between the interrupt check below and the `self._sink.start()` just
        after it will open a stream and write one more chunk before the next
        check returns — about 85 ms at 2048 frames and 24 kHz. Closing it
        means moving the interrupt decision inside the sink, which is a much
        larger change than one chunk nobody will hear as a defect is worth.
        Measured and accepted, not missed.
        """
        self.interrupted = False
        for start in range(0, len(audio), self._chunk):
            while not self._resume.wait(timeout=0.05):
                if self._interrupt.is_set():
                    break
            if self._interrupt.is_set():
                self._interrupt.clear()
                self.interrupted = True
                return
            # Started here, below the interrupt check, rather than before the
            # loop: a segment that writes nothing — empty, or interrupted at
            # the first chunk — must not open a device stream to write nothing
            # into. start() is idempotent, so paying for it per chunk is a
            # lock acquisition, not a device call.
            self._sink.start()
            block = audio[start : start + self._chunk]
            self._sink.write(block)
            self.frames_played += len(block)
        # Repeated, not hoisted: a zero-length segment never enters the loop,
        # so without this an interrupt that landed on it stays set and kills
        # the *next* play(). Clearing at entry instead would erase a stop()
        # that arrived before play() was called, which is a real ordering in
        # the daemon — the flag is cleared only where it is detected.
        if self._interrupt.is_set():
            self._interrupt.clear()
            self.interrupted = True

    def pause(self) -> None:
        """Suspend playback between chunks; `play()` keeps blocking.

        Pause is sticky: it outlives the current segment and survives
        `stop()`. See `stop()` for why.
        """
        self._resume.clear()

    def resume(self) -> None:
        """Take playback up again from where it was suspended."""
        self._resume.set()

    def close(self) -> None:
        """Give the device back for good. Terminal: the sink refuses all after.

        `stop()` deliberately leaves the sink reusable -- that is what makes a
        hush a hush rather than a shutdown -- so nothing in the speech path
        ever releases the device. Without this the daemon keeps it claimed for
        its whole life, which other applications on the machine notice.
        """
        self._sink.close()

    def stop(self) -> None:
        """End the current playback.

        Pause state is sticky across this: stop() does not touch `_resume`.
        The write loop's own poll (`while not self._resume.wait(timeout=0.05):
        if self._interrupt.is_set(): break`) already bounds how long a paused
        play() takes to notice the interrupt, so releasing the resume wait
        here is not needed to avoid a deadlock — and doing it anyway lets a
        pause() that lands between this line and the producer observing the
        interrupt get silently cleared, leaving the *next*, unrelated play()
        starting paused. A hush must leave the paused indicator lit exactly
        as it found it: a user who paused, then hushed, then sees new text
        arrive expects it to wait for them, not start talking on its own.
        """
        self._interrupt.set()
        self._sink.stop()
