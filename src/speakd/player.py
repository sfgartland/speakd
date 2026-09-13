"""Playback sinks.

`play` blocks until the audio has finished, which is what lets the pipeline
use playback as its clock while a producer thread runs ahead.
"""

from __future__ import annotations

import threading
import time
from typing import TYPE_CHECKING, Protocol, runtime_checkable

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

        failure: Exception | None = None
        ours = False
        try:
            stream.write(frames)
        except Exception as exc:
            failure = exc
        finally:
            with self._lock:
                self._writers -= 1
                # "Ours" as in: we are the reason this write failed, because
                # we aborted the stream out from under it.
                ours = self._aborted or self._stream is not stream

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
        with self._lock:
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
