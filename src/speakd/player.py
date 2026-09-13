"""Playback sinks.

`play` blocks until the audio has finished, which is what lets the pipeline
use playback as its clock while a producer thread runs ahead.
"""

from __future__ import annotations

import threading
import time
from typing import TYPE_CHECKING, Protocol

import numpy as np

if TYPE_CHECKING:
    import sounddevice as sd


class Player(Protocol):
    def play(self, audio: np.ndarray, sample_rate: int) -> None: ...

    def stop(self) -> None: ...


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
    """Plays through the system audio device."""

    def play(self, audio: np.ndarray, sample_rate: int) -> None:
        import sounddevice as sd

        sd.play(audio, samplerate=sample_rate)
        sd.wait()

    def stop(self) -> None:
        import sounddevice as sd

        sd.stop()


class AudioSink(Protocol):
    """Where audio frames go. Injected so tests never open a device."""

    frames_written: int

    def start(self) -> None: ...

    def write(self, frames: np.ndarray) -> None: ...

    def stop(self) -> None: ...

    def close(self) -> None: ...


class FakeSink:
    """Records frames instead of playing them."""

    def __init__(self) -> None:
        self.blocks: list[np.ndarray] = []
        self.frames_written = 0
        self.started = False
        self.stopped = False
        self.closed = False

    def start(self) -> None:
        self.started = True

    def write(self, frames: np.ndarray) -> None:
        # Mirrors SoundDeviceSink: a write always finds (or lazily re-enters)
        # a running state, even right after stop() — stop() leaves the sink
        # reusable rather than ending its life.
        self.started = True
        self.blocks.append(frames.copy())
        self.frames_written += len(frames)

    def stop(self) -> None:
        self.stopped = True

    def close(self) -> None:
        self.closed = True


class SoundDeviceSink:
    """Writes to the system audio device through its own stream.

    Its own, deliberately: `sd.play`/`sd.stop` drive one process-wide stream, so
    with several channels one session's stop would silence another's audio.
    """

    def __init__(self, sample_rate: int, blocksize: int = 1024) -> None:
        self.sample_rate = sample_rate
        self.blocksize = blocksize
        self.frames_written = 0
        self._stream: sd.OutputStream | None = None

    def start(self) -> None:
        if self._stream is not None:
            # Already running: a second start() must not orphan the first
            # stream. Owning exactly one stream per sink is the reason this
            # class exists.
            return

        import sounddevice as sd

        stream = sd.OutputStream(
            samplerate=self.sample_rate,
            channels=1,
            dtype="float32",
            blocksize=self.blocksize,
        )
        stream.start()
        self._stream = stream

    def write(self, frames: np.ndarray) -> None:
        if self._stream is None:
            self.start()
        assert self._stream is not None
        self._stream.write(frames)
        self.frames_written += len(frames)

    def stop(self) -> None:
        """End the current playback but leave the sink reusable: the next
        write() starts a fresh stream through the same lazy path start()
        already provides."""
        if self._stream is not None:
            self._stream.abort()
            self._stream.close()
            self._stream = None

    def close(self) -> None:
        if self._stream is not None:
            self._stream.close()
            self._stream = None


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

    def play(self, audio: np.ndarray, sample_rate: int) -> None:
        self.interrupted = False
        self._sink.start()
        for start in range(0, len(audio), self._chunk):
            while not self._resume.wait(timeout=0.05):
                if self._interrupt.is_set():
                    break
            if self._interrupt.is_set():
                self._interrupt.clear()
                self.interrupted = True
                return
            block = audio[start : start + self._chunk]
            self._sink.write(block)
            self.frames_played += len(block)

    def pause(self) -> None:
        """Suspend playback between chunks; `play()` keeps blocking.

        Pause is sticky: it outlives the current segment and survives
        `stop()`. See `stop()` for why.
        """
        self._resume.clear()

    def resume(self) -> None:
        """Take playback up again from where it was suspended."""
        self._resume.set()

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
