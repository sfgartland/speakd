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

from speakd.stretch import time_stretch

if TYPE_CHECKING:
    import sounddevice as sd

    from speakd.tempo import Tempo


class Player(Protocol):
    def play(self, audio: np.ndarray, sample_rate: int) -> None: ...

    def stop(self) -> None: ...


# How much audio a speed change stretches at a time: enough that a window's
# seams are seconds apart, little enough that a ramp's re-stretching stays a
# small fraction of real time on a machine that is also synthesising.
STRETCH_LOOKAHEAD_SECONDS = 2.0


@runtime_checkable
class Stretchable(Protocol):
    """A player that can follow a speed change inside a segment.

    Separate from `Player` for the reason `Pausable` is: nothing else in the
    speech path needs it, and a test double should not have to carry it.
    `made_at` is the speed multiplier the audio was synthesised at; the
    player holds what is left of it to `tempo`, and answers with the seconds
    it actually played, which a stretch makes different from the audio's own.
    """

    def play_at(
        self, audio: np.ndarray, sample_rate: int, *, made_at: float, tempo: Tempo
    ) -> float: ...


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
    """The device stopped taking audio and did not start again.

    Deliberately not the recorded drop a hush produces, and raised rather than
    recorded for that reason. A hush is expected, and turning one into a
    crashed utterance would be worse than the hush; a device that has stopped
    taking audio is not expected, every remaining chunk of the utterance would
    cost another full stall timeout against it, and a recorded drop reaches
    `sink.errors` and stops there. Raising is what carries the failure up
    through `speak()` to the event bus, which is where a user can see it.
    """


def _wedged(frames: int, seconds: float) -> str:
    """The one wording for a stalled hand-over, so the log and the raise agree."""
    return (
        f"audio device wedged: {frames} frames went unwritten because the device "
        f"took nothing for {seconds:.1f}s; the stream was given up rather than waited on"
    )


def _to_journal(message: str) -> None:
    """Say it where the operator of a systemd unit will find it.

    Straight to stderr, like every other operator-facing line in this project:
    journald captures a unit's stderr and the daemon configures no logging. A
    device that wedges and then recovers in silence is barely better than one
    that stays wedged, and the event bus alone is not enough — a daemon whose
    speech worker is in trouble may have no subscriber listening at all.

    Guarded, because a stderr closed underneath it, which is what the end of a
    test looks like, must not be the thing that kills an utterance.
    """
    try:
        sys.stderr.write(f"speakd: {message}\n")
        sys.stderr.flush()
    except Exception:
        pass


# How long the device may take nothing at all before the write gives up on it.
#
# This bounds a stall, not a write: any frame the device accepts resets it, so
# a caller handing over ten seconds of audio through a buffer that holds a
# twentieth of a second is never cut off, however long the hand-over runs.
# What it catches is a `write_available` that stops growing — a PipeWire sink
# that has stopped calling back, which is the state a blocking write used to
# wait out forever.
#
# Five seconds clears everything short of a fault: a machine loaded hard
# enough that the speech thread waits whole scheduler quanta — this bug first
# showed on a laptop compiling Rust at 97% CPU — a sink resuming from suspend,
# which is order a second, and an xrun the server recovers from. It is still
# short enough that a wedge costs one audible gap instead of the tens of
# minutes the daemon actually spent stuck.
_STALL_TIMEOUT = 5.0

# How long to wait before asking the device for room again.
#
# Short relative to a chunk, which is 85 ms at 2048 frames and 24 kHz, because
# this is the margin the hand-over gives up: measured on this machine, an
# output stream's ring buffer holds 2048 frames and the device takes a 1024
# frame block every 42.7 ms, so the buffer still holds some 38 ms of audio by
# the time the loop notices the room. It is also how long a hush spends
# unnoticed inside a write, which is why it is not larger.
_WRITE_POLL_INTERVAL = 0.005


class _Handover(NamedTuple):
    """What became of one attempt to give a block of frames to the device."""

    accepted: int
    failure: Exception | None
    stalled: bool


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
    while the playback thread is usually inside `write()` — a chunk is handed
    to the device at the speed the device drains, which is the speed it is
    played — so a stop during playback lands inside a write almost always,
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

    None of that covers the write itself, and the write is where this daemon
    died. `sounddevice`'s blocking write takes no timeout and waits inside
    PortAudio whenever it is handed more than the device can take; on real
    hardware it was seen not to come back at all, a `py-spy dump` finding the
    speech thread parked in `_raw_write` for tens of minutes while the daemon
    went on accepting speech it would never say. Aborting the stream from
    another thread does not release such a write — a dump taken minutes after
    one showed the same frame — and closing it is the undefined behaviour
    above, so there is no way out of that wait once it has begun.

    So this sink never begins it. PortAudio only waits when it is handed more
    than `write_available` frames, so `_hand_over` writes at most that many
    and waits in a loop of its own between attempts: a hush is noticed in a
    poll interval rather than whenever the device feels like returning, and a
    device that takes nothing for `_STALL_TIMEOUT` is given up on with an
    `AudioDeviceWedged` rather than waited on. See that class for why this one
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

        # Pre-set so the bookkeeping below is safe even if `_hand_over` raises
        # something it was supposed to catch: the writer count must come back
        # down on every path, or a later start() would decline to reap for
        # ever.
        handover = _Handover(accepted=0, failure=None, stalled=False)
        try:
            handover = self._hand_over(stream, frames)
        finally:
            with self._lock:
                self._writers -= 1
                self.frames_written += handover.accepted
                # "Ours" as in: we are the reason this write failed, because
                # we aborted the stream out from under it.
                ours = self._aborted or not self._is_current(stream)

        unwritten = len(frames) - handover.accepted
        if handover.stalled:
            # Ahead of everything else on purpose: a device that has stopped
            # taking audio is not a hush, and the frames it did not take were
            # not played.
            self._abandon(stream)
            message = _wedged(unwritten, _STALL_TIMEOUT)
            self.errors.append(message)
            _to_journal(message)
            raise AudioDeviceWedged(message)
        if unwritten == 0:
            return
        if handover.failure is not None and not ours:
            raise handover.failure
        # A partial hand-over that stopped because we stopped it. The device
        # may have refused the last block (`failure`) or the loop may simply
        # have seen the abort between attempts; either way the frames left
        # over are dropped audio, and dropped audio is recorded rather than
        # raised, because a hush must never become a crashed utterance.
        detail = (
            "playback stopped mid-write"
            if handover.failure is None
            else f"playback stopped mid-write ({handover.failure})"
        )
        self.errors.append(_dropped(unwritten, detail))

    def _is_current(self, stream: object) -> bool:
        """Is this the stream the sink still owns? Caller holds `_lock`.

        A method rather than the comparison written out at each site: `is`
        against `_stream`, which is optional, narrows the caller's own handle
        to an optional one for the rest of the function, and the `None` checks
        that then become necessary would be apologising for a state the caller
        cannot be in.
        """
        return self._stream is stream

    def _hand_over(self, stream: sd.OutputStream, frames: np.ndarray) -> _Handover:
        """Give `frames` to the device without ever waiting inside it.

        PortAudio's blocking write waits only when it is handed more than the
        device can take: `write_available` frames or fewer are copied into the
        ring buffer and the call returns — 0.04 to 0.10 ms of it, measured on
        this machine against a live device. So the waiting that used to happen
        inside PortAudio happens here instead, in a loop this thread can see
        out of, which is what makes a hush escapable and a dead device
        reportable rather than terminal.

        The sink's own state is what the loop watches, not the stream's:
        measured on the same device, `write_available` goes on cheerfully
        reporting the whole buffer free after an `abort()`. Trusting it would
        put frames into a dead stream and keep the utterance going. `_aborted`
        under `_lock` is the truth, and it is checked before every attempt.
        """
        total = len(frames)
        accepted = 0
        last_progress = time.monotonic()
        while accepted < total:
            with self._lock:
                if self._closed or self._aborted or not self._is_current(stream):
                    return _Handover(accepted=accepted, failure=None, stalled=False)
            try:
                room = int(stream.write_available)
            except Exception as exc:
                return _Handover(accepted=accepted, failure=exc, stalled=False)
            if room <= 0:
                if time.monotonic() - last_progress >= _STALL_TIMEOUT:
                    return _Handover(accepted=accepted, failure=None, stalled=True)
                time.sleep(_WRITE_POLL_INTERVAL)
                continue
            block = frames[accepted : accepted + min(room, total - accepted)]
            try:
                stream.write(block)
            except Exception as exc:
                return _Handover(accepted=accepted, failure=exc, stalled=False)
            accepted += len(block)
            last_progress = time.monotonic()
        return _Handover(accepted=accepted, failure=None, stalled=False)

    def _abandon(self, stream: sd.OutputStream) -> None:
        """Take a device that stopped accepting audio out of service.

        Without this the sink keeps the same dead stream for good — `start()`
        returns early while the current stream is live — so every later
        utterance would spend the whole stall timeout per chunk against a
        device that has already proved it is not listening. Aborting puts it
        in the state a hush leaves behind, which the next `start()` knows how
        to reap.

        The abort itself is safe to make from this thread: it is the release
        it was supposed to perform that this stack does not honour, not the
        call, and `stop()` has always made it from the control thread.
        """
        with self._lock:
            if self._closed or self._aborted or not self._is_current(stream):
                return
            self._aborted = True
            try:
                stream.abort()
            except Exception as exc:
                # Nothing further can be done for the device; say so, and
                # leave the stream for the next start() to reap.
                self.errors.append(f"could not abort the wedged stream: {exc}")

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
        self._play(audio, sample_rate, 1.0, None)

    def play_at(
        self, audio: np.ndarray, sample_rate: int, *, made_at: float, tempo: Tempo
    ) -> float:
        """`play()`, holding the rest of the segment to `tempo` as it changes.

        Checked once a chunk, so a speed change is heard within one chunk
        (about 85 ms) rather than at the next sentence. What is left is
        stretched by the ratio between the speed it currently represents and
        the one wanted now, and never re-stretched from the original: the
        played part is gone, and the rest is all that can still change.
        """
        return self._play(audio, sample_rate, made_at, tempo) / sample_rate

    def _play(
        self, audio: np.ndarray, sample_rate: int, made_at: float, tempo: Tempo | None
    ) -> int:
        """The write loop both entry points share. Returns frames written.

        Positions are kept in the audio as it was made (`consumed`), and every
        stretch is cut from there -- never from the previous stretch. A ramp
        changes the speed every chunk, and stretching a stretch each time
        would pile the artifacts of one on the next until the voice smeared.
        """
        self.interrupted = False
        consumed = 0.0  # samples of `audio` played so far, at whatever speed
        buffer: np.ndarray | None = None
        window_end = 0  # where in `audio` the current buffer was cut up to
        effective = made_at
        pos = 0
        written = 0
        window = int(STRETCH_LOOKAHEAD_SECONDS * sample_rate)
        while True:
            while not self._resume.wait(timeout=0.05):
                if self._interrupt.is_set():
                    break
            if self._interrupt.is_set():
                self._interrupt.clear()
                self.interrupted = True
                return written
            wanted = tempo.value if tempo is not None else made_at
            spent = buffer is not None and pos >= len(buffer)
            if buffer is None or spent or abs(wanted / effective - 1.0) > 1e-3:
                if spent:
                    # Exactly where the window ended, rather than the running
                    # estimate, so windows butt up with nothing lost or doubled.
                    consumed = float(window_end)
                start = int(round(consumed))
                if start >= len(audio):
                    break
                ratio = wanted / made_at
                if abs(ratio - 1.0) < 1e-3:
                    # At the speed it was made: the rest as it is, uncopied.
                    window_end = len(audio)
                    buffer = audio[start:]
                else:
                    # Only a short look-ahead is stretched. A ramp changes the
                    # speed every chunk, and stretching the whole rest of a
                    # long sentence each time costs as much CPU as the
                    # synthesiser needs; the next window is cut when this one
                    # runs out.
                    window_end = min(len(audio), start + window)
                    buffer = time_stretch(audio[start:window_end], ratio, sample_rate)
                effective = wanted
                pos = 0
                if len(buffer) == 0:
                    consumed = float(window_end)
                    buffer = None
                    continue
            # Started here, below the interrupt check, rather than before the
            # loop: a segment that writes nothing — empty, or interrupted at
            # the first chunk — must not open a device stream to write nothing
            # into. start() is idempotent, so paying for it per chunk is a
            # lock acquisition, not a device call.
            self._sink.start()
            block = buffer[pos : pos + self._chunk]
            self._sink.write(block)
            self.frames_played += len(block)
            written += len(block)
            pos += len(block)
            consumed += len(block) * effective / made_at
        # Repeated, not hoisted: a zero-length segment never enters the loop,
        # so without this an interrupt that landed on it stays set and kills
        # the *next* play(). Clearing at entry instead would erase a stop()
        # that arrived before play() was called, which is a real ordering in
        # the daemon — the flag is cleared only where it is detected.
        if self._interrupt.is_set():
            self._interrupt.clear()
            self.interrupted = True
        return written

    def clear_interrupt(self) -> None:
        """Forget a stop that was aimed at playback which has already ended.

        A seek stops the player and then speaks again at once. The stop the
        pipeline sends on its way out of the cancelled pass lands after the
        last `play()` has returned, and would otherwise cut off the first
        chunk of the segment that was sought.
        """
        self._interrupt.clear()

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
