"""Tests for playback sinks."""

import sys
import threading
import time
import types
from collections.abc import Sequence

import numpy as np
import pytest

from speakd.model import Piece
from speakd.player import _MAX_ERRORS, AudioSink, RecordingPlayer, _ErrorLog


def _passthrough(pieces: Sequence[Piece]) -> tuple[list[Piece], list[str]]:
    return list(pieces), []


def test_recording_player_keeps_what_it_played() -> None:
    player = RecordingPlayer()
    first = np.zeros(4, dtype=np.float32)
    second = np.ones(2, dtype=np.float32)
    player.play(first, 24000)
    player.play(second, 24000)
    assert [len(a) for a in player.played] == [4, 2]


def test_recording_player_timestamps_each_play() -> None:
    player = RecordingPlayer()
    player.play(np.zeros(1, dtype=np.float32), 24000)
    player.play(np.zeros(1, dtype=np.float32), 24000)
    assert len(player.timestamps) == 2
    assert player.timestamps[1] >= player.timestamps[0]


def test_recording_player_records_stop() -> None:
    player = RecordingPlayer()
    assert player.stopped is False
    player.stop()
    assert player.stopped is True


def test_importing_player_does_not_require_sounddevice() -> None:
    import speakd.player  # noqa: F401  — must not raise without the extra


def test_fake_sink_records_what_was_written() -> None:
    from speakd.player import FakeSink

    sink = FakeSink()
    sink.start()
    sink.write(np.zeros(10, dtype=np.float32))
    sink.write(np.zeros(5, dtype=np.float32))
    assert sink.frames_written == 15
    assert [len(b) for b in sink.blocks] == [10, 5]


def test_fake_sink_stop_is_recorded_and_keeps_the_frame_count() -> None:
    from speakd.player import FakeSink

    sink = FakeSink()
    sink.start()
    sink.write(np.zeros(10, dtype=np.float32))
    sink.stop()
    assert sink.stopped is True
    assert sink.frames_written == 10


def test_sounddevice_sink_defers_its_import() -> None:
    import inspect

    from speakd.player import SoundDeviceSink

    source = inspect.getsource(SoundDeviceSink)
    assert "import sounddevice" in source, "the import must exist"

    module_source = inspect.getsource(__import__("speakd.player", fromlist=["x"]))
    # Splitting on "class " (or on the class's own name) breaks the moment a
    # second class also imports sounddevice lazily — SoundDevicePlayer already
    # does — because the split point can no longer separate "module level"
    # from "inside some earlier method body". Checking indentation directly
    # is the actual definition of module level, and stays correct regardless
    # of how many classes in the file do the same lazy import.
    top_level_lines = [line for line in module_source.splitlines() if not line[:1].isspace()]
    assert not any("import sounddevice" in line for line in top_level_lines), (
        "must not be imported at module level"
    )


def test_fake_sink_copies_written_blocks() -> None:
    """A caller reusing its buffer after write() must not mutate history."""
    from speakd.player import FakeSink

    sink = FakeSink()
    sink.start()
    buffer = np.zeros(4, dtype=np.float32)
    sink.write(buffer)
    buffer[:] = 1.0
    assert sink.blocks[0] == pytest.approx(np.zeros(4, dtype=np.float32))


def test_fake_sink_write_after_stop_is_a_recorded_no_op() -> None:
    """stop() ends playback and leaves the sink reusable — but reuse goes
    through start(), not through a write() that silently reopens a device.
    A write landing in the window between the two is dropped audio, and
    dropped audio is recorded rather than swallowed."""
    from speakd.player import FakeSink

    sink = FakeSink()
    sink.start()
    sink.write(np.zeros(10, dtype=np.float32))
    sink.stop()
    sink.write(np.zeros(5, dtype=np.float32))

    assert sink.frames_written == 10
    assert [len(b) for b in sink.blocks] == [10]
    assert sink.stopped is True
    assert len(sink.errors) == 1
    assert "dropped 5 frames" in sink.errors[0]


def test_fake_sink_start_after_stop_makes_it_writable_again() -> None:
    from speakd.player import FakeSink

    sink = FakeSink()
    sink.start()
    sink.write(np.zeros(10, dtype=np.float32))
    sink.stop()
    sink.start()
    sink.write(np.zeros(5, dtype=np.float32))

    assert sink.frames_written == 15
    assert [len(b) for b in sink.blocks] == [10, 5]
    assert sink.errors == []


# --------------------------------------------------------------------------
# The fake `sounddevice` module.
#
# SoundDeviceSink is the sink that actually plays audio and the one no test
# used to exercise: every test ran against FakeSink, so the two could — and
# did — disagree about what stop() leaves behind. A MagicMock stream does not
# close that gap, because it accepts everything: writing to an aborted stream,
# closing one a thread is blocked inside. Both are errors on a real PortAudio
# stream, and both were happening.
#
# So the fake models the state machine instead. Nothing here opens a device;
# `sounddevice` stays uninstalled.
# --------------------------------------------------------------------------


class FakePortAudioError(Exception):
    """Stands in for `sounddevice.PortAudioError`."""


class FakeStream:
    """A PortAudio output stream, including the ways it refuses to be used.

    States run `created -> running -> aborted|stopped -> closed`. write()
    raises on any stream that is not running, which is what makes a
    write-after-abort visible instead of silent.

    A real blocking write() blocks for the whole duration of the chunk it is
    playing, so a stop() during playback lands inside write() almost always.
    `block_writes()` reproduces that window deterministically, and
    `pin_writer_inside()` holds a released writer inside the call so the
    narrower window — aborted, but the writing thread has not left yet — can
    be tested rather than raced for.
    """

    def __init__(self, **kwargs: object) -> None:
        self.kwargs = kwargs
        self.state = "created"
        self.writes: list[np.ndarray] = []
        self.violations: list[str] = []
        self.start_calls = 0
        self.abort_calls = 0
        self.close_calls = 0
        self.fail_next_write: Exception | None = None
        self.fail_next_abort: Exception | None = None
        self.fail_next_close: Exception | None = None
        self.entered_write = threading.Event()
        self.woke_in_write = threading.Event()
        self._gate = threading.Event()
        self._gate.set()
        self._exit_gate = threading.Event()
        self._exit_gate.set()
        self._lock = threading.Lock()
        self._writers = 0

    # -- knobs the tests drive -------------------------------------------
    def block_writes(self) -> None:
        self.entered_write.clear()
        self.woke_in_write.clear()
        self._gate.clear()

    def pin_writer_inside(self) -> None:
        self._exit_gate.clear()

    def release_writer(self) -> None:
        self._exit_gate.set()

    @property
    def writers_inside(self) -> int:
        with self._lock:
            return self._writers

    # -- the surface SoundDeviceSink uses --------------------------------
    def start(self) -> None:
        with self._lock:
            if self.state == "closed":
                raise FakePortAudioError("start() on a closed stream")
            self.start_calls += 1
            self.state = "running"

    def write(self, frames: np.ndarray) -> None:
        with self._lock:
            if self.state != "running":
                raise FakePortAudioError(f"write() on a {self.state} stream")
            self._writers += 1
        try:
            if self.fail_next_write is not None:
                failure, self.fail_next_write = self.fail_next_write, None
                raise failure
            self.entered_write.set()
            if not self._gate.wait(timeout=10.0):
                raise AssertionError("a blocked write was never released")
            self.woke_in_write.set()
            if not self._exit_gate.wait(timeout=10.0):
                raise AssertionError("a pinned writer was never released")
            with self._lock:
                if self.state != "running":
                    raise FakePortAudioError(f"stream was {self.state} mid-write")
                self.writes.append(np.asarray(frames).copy())
        finally:
            with self._lock:
                self._writers -= 1

    def abort(self) -> None:
        if self.fail_next_abort is not None:
            failure, self.fail_next_abort = self.fail_next_abort, None
            raise failure
        with self._lock:
            if self.state == "closed":
                raise FakePortAudioError("abort() on a closed stream")
            self.abort_calls += 1
            self.state = "aborted"
        # A real abort() returns at once and releases a blocked write.
        self._gate.set()

    def stop(self) -> None:
        with self._lock:
            if self.state == "closed":
                raise FakePortAudioError("stop() on a closed stream")
            self.state = "stopped"
        self._gate.set()

    def close(self) -> None:
        if self.fail_next_close is not None:
            failure, self.fail_next_close = self.fail_next_close, None
            raise failure
        with self._lock:
            if self._writers:
                # Pa_CloseStream on a stream another thread is blocked writing
                # to is undefined behaviour, not a catchable exception. A real
                # run gets a segfault or silence; here it gets recorded.
                self.violations.append("close() with a thread inside write()")
            self.close_calls += 1
            self.state = "closed"
        self._gate.set()
        self._exit_gate.set()


class _FakeSoundDeviceModule(types.ModuleType):
    def __init__(self, streams: list[FakeStream]) -> None:
        super().__init__("sounddevice")
        self.streams = streams
        self.PortAudioError = FakePortAudioError

    def OutputStream(self, **kwargs: object) -> FakeStream:
        stream = FakeStream(**kwargs)
        self.streams.append(stream)
        return stream


def _install_fake_sounddevice(monkeypatch: pytest.MonkeyPatch) -> list[FakeStream]:
    """Injects a fake `sounddevice` module and returns the streams it hands
    out, in creation order — so SoundDeviceSink's own logic runs for real,
    against a modelled device, with no hardware and no extra installed."""
    streams: list[FakeStream] = []
    monkeypatch.setitem(sys.modules, "sounddevice", _FakeSoundDeviceModule(streams))
    return streams


class _Writer(threading.Thread):
    """Runs one sink.write() on its own thread and keeps whatever it raised."""

    def __init__(self, sink: object, frames: np.ndarray) -> None:
        super().__init__(daemon=True)
        self._sink = sink
        self._frames = frames
        self.error: Exception | None = None

    def run(self) -> None:
        try:
            self._sink.write(self._frames)  # type: ignore[attr-defined]
        except Exception as exc:
            self.error = exc


def _frames(n: int) -> np.ndarray:
    return np.zeros(n, dtype=np.float32)


def test_sounddevice_sink_start_is_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    """A second start() must not orphan the first stream. Owning a single
    stream per sink is the entire point of this class; a sink that can end
    up holding two live streams has stopped being an owner."""
    from speakd.player import SoundDeviceSink

    streams = _install_fake_sounddevice(monkeypatch)

    sink = SoundDeviceSink(sample_rate=24000)
    sink.start()
    sink.start()

    assert len(streams) == 1
    assert streams[0].start_calls == 1


def test_sounddevice_sink_stop_aborts_without_closing(monkeypatch: pytest.MonkeyPatch) -> None:
    """stop() aborts only. Closing is what creates the undefined behaviour,
    because the thread that stop() is interrupting is usually still inside
    write() at that moment."""
    from speakd.player import SoundDeviceSink

    streams = _install_fake_sounddevice(monkeypatch)

    sink = SoundDeviceSink(sample_rate=24000)
    sink.start()
    sink.write(_frames(4))
    sink.stop()

    assert streams[0].abort_calls == 1
    assert streams[0].close_calls == 0, "stop() must not close the stream"
    assert streams[0].state == "aborted"


def test_sounddevice_sink_stop_during_a_blocked_write_records_the_drop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The common path, not a rare race: a real write() blocks for the whole
    chunk, so a stop() during playback lands inside it. It must release the
    writer, record the frames it cost, and not raise — a hush that crashes
    the utterance is worse than the hush itself."""
    from speakd.player import SoundDeviceSink

    streams = _install_fake_sounddevice(monkeypatch)

    sink = SoundDeviceSink(sample_rate=24000)
    sink.start()
    stream = streams[0]
    stream.block_writes()

    writer = _Writer(sink, _frames(2048))
    writer.start()
    assert stream.entered_write.wait(timeout=2.0), "the writer never reached write()"

    sink.stop()

    writer.join(timeout=2.0)
    assert not writer.is_alive(), "stop() must unblock a concurrent write()"
    assert writer.error is None, f"the write must not raise: {writer.error!r}"
    assert sink.frames_written == 0
    assert len(sink.errors) == 1
    assert "dropped 2048 frames" in sink.errors[0]
    assert stream.violations == [], "nothing may close a stream a thread is inside"


def test_sounddevice_sink_write_after_stop_opens_no_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Between stop() and the next start() a write is a recorded no-op. The
    alternative — reopening the device lazily — makes stop() unable to stop
    anything, because the very next chunk brings the audio back."""
    from speakd.player import SoundDeviceSink

    streams = _install_fake_sounddevice(monkeypatch)

    sink = SoundDeviceSink(sample_rate=24000)
    sink.start()
    sink.write(_frames(4))
    sink.stop()
    sink.write(_frames(5))

    assert len(streams) == 1, "a write after stop() must not open a device stream"
    assert sink.frames_written == 4
    assert len(sink.errors) == 1
    assert "dropped 5 frames" in sink.errors[0]


def test_sounddevice_sink_start_after_stop_reaps_and_reopens(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """start() is what makes the sink usable again: it closes the aborted
    stream — safe now, because no thread is inside it — and opens a fresh one."""
    from speakd.player import SoundDeviceSink

    streams = _install_fake_sounddevice(monkeypatch)

    sink = SoundDeviceSink(sample_rate=24000)
    sink.start()
    sink.write(_frames(4))
    sink.stop()
    sink.start()
    sink.write(_frames(4))

    assert len(streams) == 2
    assert streams[0].close_calls == 1, "the aborted stream must be reaped"
    assert streams[0].violations == []
    assert streams[1].state == "running"
    assert len(streams[1].writes) == 1
    assert sink.frames_written == 8
    assert sink.errors == []


def test_sounddevice_sink_does_not_reap_a_stream_a_writer_is_still_inside(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A writer released by abort() has not necessarily returned yet. Closing
    in that window is the undefined behaviour the whole design avoids, so the
    reap is skipped and the fact is recorded instead."""
    from speakd.player import SoundDeviceSink

    streams = _install_fake_sounddevice(monkeypatch)

    sink = SoundDeviceSink(sample_rate=24000)
    sink.start()
    stream = streams[0]
    stream.block_writes()
    stream.pin_writer_inside()

    writer = _Writer(sink, _frames(64))
    writer.start()
    assert stream.entered_write.wait(timeout=2.0)

    sink.stop()
    assert stream.woke_in_write.wait(timeout=2.0), "abort() must release the writer"
    assert stream.writers_inside == 1

    sink.start()

    assert stream.violations == [], "must not close a stream a thread is inside"
    assert stream.close_calls == 0
    assert len(streams) == 2, "and must still hand back a working stream"
    assert any("in flight" in message for message in sink.errors)

    stream.release_writer()
    writer.join(timeout=2.0)
    assert not writer.is_alive()


def test_sounddevice_sink_propagates_a_failure_it_did_not_cause(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only a write that failed *because we aborted it* is recorded. A device
    that disappears mid-utterance is a different event and keeps propagating."""
    from speakd.player import SoundDeviceSink

    streams = _install_fake_sounddevice(monkeypatch)

    sink = SoundDeviceSink(sample_rate=24000)
    sink.start()
    streams[0].fail_next_write = FakePortAudioError("device disconnected")

    with pytest.raises(FakePortAudioError, match="device disconnected"):
        sink.write(_frames(4))

    assert sink.frames_written == 0


def test_sounddevice_sink_close_releases_the_device(monkeypatch: pytest.MonkeyPatch) -> None:
    from speakd.player import SoundDeviceSink

    streams = _install_fake_sounddevice(monkeypatch)

    sink = SoundDeviceSink(sample_rate=24000)
    sink.start()
    sink.close()

    assert streams[0].close_calls == 1
    assert streams[0].state == "closed"


def test_sounddevice_sink_write_after_close_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    from speakd.player import SoundDeviceSink

    streams = _install_fake_sounddevice(monkeypatch)

    sink = SoundDeviceSink(sample_rate=24000)
    sink.start()
    sink.close()

    with pytest.raises(RuntimeError):
        sink.write(_frames(4))
    assert len(streams) == 1, "close() is terminal: no write may reopen the device"


def test_sounddevice_sink_start_after_close_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    from speakd.player import SoundDeviceSink

    streams = _install_fake_sounddevice(monkeypatch)

    sink = SoundDeviceSink(sample_rate=24000)
    sink.start()
    sink.close()

    with pytest.raises(RuntimeError):
        sink.start()
    assert len(streams) == 1


def test_a_hushed_segment_opens_no_device_stream(monkeypatch: pytest.MonkeyPatch) -> None:
    """Checked against the real sink rather than assumed. With stop() no
    longer nulling `_stream`, a write after a stop can no longer reopen the
    device on its own — but play()'s eager start() still would have, which is
    why that call had to move below the first interrupt check."""
    from speakd.player import SoundDeviceSink, StreamingPlayer

    streams = _install_fake_sounddevice(monkeypatch)
    sink = SoundDeviceSink(sample_rate=24000)
    player = StreamingPlayer(sink, chunk_frames=100)

    player.stop()
    player.play(_frames(1000), 24000)

    assert player.interrupted is True
    assert streams == [], "a hushed segment must open no device stream at all"


def test_the_player_speaks_again_on_a_fresh_stream_after_a_hush(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The whole cycle against SoundDeviceSink: speak, hush, the segment the
    hush lands on, then speak again. The aborted stream is reaped exactly
    once and the new audio goes to a new stream."""
    from speakd.player import SoundDeviceSink, StreamingPlayer

    streams = _install_fake_sounddevice(monkeypatch)
    sink = SoundDeviceSink(sample_rate=24000)
    player = StreamingPlayer(sink, chunk_frames=100)

    player.play(_frames(200), 24000)
    player.stop()
    player.play(_frames(100), 24000)
    assert player.interrupted is True

    player.play(_frames(100), 24000)
    assert player.interrupted is False

    assert sink.frames_written == 300
    assert len(streams) == 2
    assert streams[0].state == "closed"
    assert streams[0].close_calls == 1
    assert streams[1].state == "running"
    assert player.errors == []


# --------------------------------------------------------------------------
# The bounded write.
#
# `sounddevice`'s blocking write() takes no timeout. It normally returns in
# about the time its own audio takes to play — 2048 frames at 24 kHz is
# 85 ms — but it is not guaranteed to. A py-spy dump of a live daemon found
# the speech thread parked in `_raw_write` for tens of minutes while the
# daemon went on accepting speech nothing was left to consume.
#
# These tests drive that against a stream that blocks until it is released,
# so the wedge is reproduced deterministically and no device is opened.
# --------------------------------------------------------------------------


def _bound_writes_at(monkeypatch: pytest.MonkeyPatch, seconds: float) -> None:
    """Shrink the watchdog's floor so a test need not wait out the real bound.

    Patched by name with `raising` left at its default: if the constant is
    ever renamed, this fails loudly rather than quietly leaving every test
    below to run against the five-second production bound, where they would
    stop testing the watchdog and start testing pytest's patience.
    """
    import speakd.player

    monkeypatch.setattr(speakd.player, "_MIN_WRITE_TIMEOUT", seconds)


def test_a_write_that_never_returns_releases_the_writing_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The bug, reproduced. Nobody hushes here and nothing fails: the device
    simply never comes back from write(). Before the watchdog the writing
    thread stayed inside it for good, which is what the daemon did on real
    hardware — still answering `ok` to enqueue, with no consumer left."""
    from speakd.player import SoundDeviceSink

    _bound_writes_at(monkeypatch, 0.1)
    streams = _install_fake_sounddevice(monkeypatch)
    sink = SoundDeviceSink(sample_rate=24000)
    sink.start()
    stream = streams[0]
    stream.block_writes()

    writer = _Writer(sink, _frames(2048))
    writer.start()
    assert stream.entered_write.wait(timeout=2.0), "the writer never reached write()"

    writer.join(timeout=2.0)
    assert not writer.is_alive(), "the write never returned: the speech thread is wedged"
    assert stream.abort_calls == 1, "only an abort from outside can release a stuck write()"


class _Journal:
    """Stands in for the process's stderr, which is what journald captures.

    Records rather than prints, and announces the first line with an Event:
    the watchdog writes from its own thread, so a test that merely looked
    afterwards would be reading a race.
    """

    def __init__(self) -> None:
        self.lines: list[str] = []
        self.wrote = threading.Event()

    def write(self, text: str) -> int:
        self.lines.append(text)
        self.wrote.set()
        return len(text)

    def flush(self) -> None:
        pass

    @property
    def text(self) -> str:
        return "".join(self.lines)


def _capture_journal(monkeypatch: pytest.MonkeyPatch) -> _Journal:
    """Take over the real stderr, because that is the channel under test.

    Not `capsys`: the assertion is specifically that the line goes to the
    process's stderr, where a systemd unit's journal picks it up, and it has
    to be waitable because it is written from the watchdog thread.
    """
    journal = _Journal()
    monkeypatch.setattr(sys, "stderr", journal)
    return journal


def _watchdog_threads() -> list[threading.Thread]:
    from speakd.player import _WATCHDOG_THREAD_NAME

    return [t for t in threading.enumerate() if t.name == _WATCHDOG_THREAD_NAME]


def test_a_wedged_write_raises_rather_than_recording_the_drop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A hush is expected and must not crash the utterance, so its dropped
    frames are recorded. A device that has stopped taking audio is neither:
    every remaining chunk would cost another full bound against it, and a
    recorded drop reaches `sink.errors` and goes no further. Raising is what
    abandons the utterance and carries the failure up to the bus."""
    from speakd.player import AudioDeviceWedged, SoundDeviceSink

    _bound_writes_at(monkeypatch, 0.1)
    streams = _install_fake_sounddevice(monkeypatch)
    sink = SoundDeviceSink(sample_rate=24000)
    sink.start()
    streams[0].block_writes()

    writer = _Writer(sink, _frames(64))
    writer.start()
    writer.join(timeout=2.0)

    assert not writer.is_alive()
    assert isinstance(writer.error, AudioDeviceWedged), f"raised {writer.error!r}"
    assert sink.frames_written == 0, "frames the device never took must not be counted"
    sink.close()


def test_a_wedged_write_is_recorded_in_the_sinks_error_log(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The raise tells the caller; the log is what a later `speakctl status`
    or a dump of the sink can still show."""
    from speakd.player import SoundDeviceSink

    _bound_writes_at(monkeypatch, 0.1)
    streams = _install_fake_sounddevice(monkeypatch)
    sink = SoundDeviceSink(sample_rate=24000)
    sink.start()
    streams[0].block_writes()

    writer = _Writer(sink, _frames(64))
    writer.start()
    writer.join(timeout=2.0)

    assert not writer.is_alive()
    assert any("audio device wedged" in message for message in sink.errors), sink.errors
    assert any("64-frame write" in message for message in sink.errors), sink.errors
    sink.close()


def test_a_wedged_write_reaches_the_journal(monkeypatch: pytest.MonkeyPatch) -> None:
    """A device that wedges and then recovers in silence is barely better
    than one that stays wedged. The bus needs a subscriber; stderr needs
    nothing but the unit, so this is the line that is always there."""
    from speakd.player import SoundDeviceSink

    _bound_writes_at(monkeypatch, 0.1)
    streams = _install_fake_sounddevice(monkeypatch)
    journal = _capture_journal(monkeypatch)
    sink = SoundDeviceSink(sample_rate=24000)
    sink.start()
    streams[0].block_writes()

    writer = _Writer(sink, _frames(64))
    writer.start()

    assert journal.wrote.wait(timeout=2.0), "nothing reached the journal"
    assert "audio device wedged" in journal.text, journal.text
    writer.join(timeout=2.0)
    assert not writer.is_alive()
    sink.close()


def test_the_sink_is_reusable_after_a_watchdog_abort(monkeypatch: pytest.MonkeyPatch) -> None:
    """The watchdog aborts through the same door `stop()` uses, so the
    existing contract holds: start() reaps the aborted stream and opens a
    fresh one, and the next utterance is audible."""
    from speakd.player import AudioDeviceWedged, SoundDeviceSink

    _bound_writes_at(monkeypatch, 0.1)
    streams = _install_fake_sounddevice(monkeypatch)
    sink = SoundDeviceSink(sample_rate=24000)
    sink.start()
    streams[0].block_writes()

    writer = _Writer(sink, _frames(64))
    writer.start()
    writer.join(timeout=2.0)
    assert isinstance(writer.error, AudioDeviceWedged)

    sink.start()
    sink.write(_frames(32))

    assert len(streams) == 2, "start() must open a fresh stream after the abort"
    assert streams[0].close_calls == 1, "the aborted stream must be reaped"
    assert streams[0].violations == []
    assert streams[1].state == "running"
    assert sink.frames_written == 32
    sink.close()


def test_the_watchdog_does_not_fire_after_a_write_that_returned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The bound must be invisible in normal operation. A write that came
    back leaves nothing for the watchdog to find, however long the sink then
    sits idle."""
    from speakd.player import SoundDeviceSink

    _bound_writes_at(monkeypatch, 0.05)
    streams = _install_fake_sounddevice(monkeypatch)
    journal = _capture_journal(monkeypatch)
    sink = SoundDeviceSink(sample_rate=24000)
    sink.start()

    sink.write(_frames(64))
    time.sleep(0.4)  # eight times the bound, with the sink left idle

    assert streams[0].abort_calls == 0, "a completed write must not be aborted afterwards"
    assert streams[0].state == "running"
    assert sink.errors == []
    assert sink.frames_written == 64
    assert journal.text == ""
    sink.close()


def test_the_bound_scales_with_the_audio_the_write_carries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The floor is sized for a 2048-frame chunk. A caller that writes ten
    seconds of audio in one go must not be cut off after five: the bound is
    never less than several times the audio in the write itself."""
    from speakd.player import SoundDeviceSink

    _bound_writes_at(monkeypatch, 0.05)
    streams = _install_fake_sounddevice(monkeypatch)
    sink = SoundDeviceSink(sample_rate=24000)
    sink.start()
    stream = streams[0]
    stream.block_writes()

    writer = _Writer(sink, _frames(24000 * 10))  # ten seconds of audio
    writer.start()
    assert stream.entered_write.wait(timeout=2.0)
    time.sleep(0.4)

    assert stream.abort_calls == 0, "a long write was cut off at the floor"
    assert writer.is_alive()

    sink.stop()  # release the writer so the test does not leak it
    writer.join(timeout=2.0)
    assert not writer.is_alive()
    assert writer.error is None, f"a hush must not raise: {writer.error!r}"
    sink.close()


def test_the_watchdog_is_one_thread_per_sink_not_one_per_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A chunk is 85 ms, so a thread per write is twelve thread creations a
    second for as long as the daemon speaks."""
    from speakd.player import SoundDeviceSink

    _install_fake_sounddevice(monkeypatch)
    sink = SoundDeviceSink(sample_rate=24000)
    sink.start()
    before = len(_watchdog_threads())

    for _ in range(50):
        sink.write(_frames(64))

    assert len(_watchdog_threads()) == before + 1, "one watchdog, not one per write"
    sink.close()


def test_close_takes_the_watchdog_with_it(monkeypatch: pytest.MonkeyPatch) -> None:
    """close() is terminal for the device, and has to be terminal for the
    thread holding a reference to it too — otherwise a daemon that opens a
    sink per session accumulates watchdogs for streams nobody owns."""
    from speakd.player import SoundDeviceSink

    _install_fake_sounddevice(monkeypatch)
    sink = SoundDeviceSink(sample_rate=24000)
    sink.start()
    # A difference rather than a count: watchdogs share one thread name, so earlier tests
    # in the same session may still have theirs parked, and this test is about
    # the one thread this sink started.
    before = set(_watchdog_threads())
    sink.write(_frames(64))
    mine = set(_watchdog_threads()) - before
    assert len(mine) == 1, "this sink started no watchdog of its own"

    sink.close()

    assert all(not t.is_alive() for t in mine), "the watchdog outlived close()"
    assert sink.errors == []


def test_a_watchdog_that_falls_over_is_replaced_rather_than_quietly_missed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A watchdog that dies without a word restores exactly the state this
    class exists to end: every later write unbounded, for the life of the
    process, with nothing to see. So it vacates the post rather than
    abandoning it, and the next write puts someone back on it."""
    import speakd.player
    from speakd.player import AudioDeviceWedged, SoundDeviceSink

    _bound_writes_at(monkeypatch, 0.1)
    streams = _install_fake_sounddevice(monkeypatch)
    wording = speakd.player._wedged
    calls = {"n": 0}

    def flaky(frames: int, seconds: float) -> str:
        """Breaks the watchdog on its first firing and works thereafter."""
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("a bug in the watchdog itself")
        return wording(frames, seconds)

    monkeypatch.setattr(speakd.player, "_wedged", flaky)

    sink = SoundDeviceSink(sample_rate=24000)
    sink.start()
    streams[0].block_writes()
    first = _Writer(sink, _frames(64))
    first.start()
    first.join(timeout=2.0)

    assert not first.is_alive(), "the abort must land before the watchdog falls over"
    assert isinstance(first.error, AudioDeviceWedged), f"raised {first.error!r}"
    assert any("write watchdog stopped" in message for message in sink.errors), sink.errors

    sink.start()
    streams[1].block_writes()
    second = _Writer(sink, _frames(64))
    second.start()
    second.join(timeout=2.0)

    assert not second.is_alive(), "no watchdog took the vacant post: writes are unbounded again"
    assert isinstance(second.error, AudioDeviceWedged), f"raised {second.error!r}"
    sink.close()


def test_a_wedged_device_reaches_the_event_bus(monkeypatch: pytest.MonkeyPatch) -> None:
    """End to end, which is the point of the whole change: the daemon used to
    go on answering `ok` to enqueue with its speech worker parked inside
    write() and nothing on the bus to say so. One utterance is now lost
    loudly instead of the daemon being lost quietly."""
    from speakd.channels import ChannelTable
    from speakd.daemon import Daemon, ProfileView
    from speakd.events import Event, EventBus
    from speakd.player import SoundDeviceSink, StreamingPlayer
    from speakd.protocol import Request, Verb
    from speakd.synth.fake import FakeEngine

    _bound_writes_at(monkeypatch, 0.1)
    streams = _install_fake_sounddevice(monkeypatch)
    sink = SoundDeviceSink(sample_rate=24000)
    # Opened before the daemon runs so the wedge can be armed on the stream
    # the speech worker will actually use; play()'s own start() is idempotent.
    sink.start()
    streams[0].block_writes()

    bus = EventBus()
    seen: list[Event] = []
    bus.subscribe(seen.append)
    daemon = Daemon(
        FakeEngine(),
        StreamingPlayer(sink, chunk_frames=2048),
        lambda name: ProfileView(
            voice="af_heart", speed=1.1, interrupt_on=(), prepare=_passthrough
        ),
        bus=bus,
        channels=ChannelTable(),
    )
    daemon.start()
    try:
        assert daemon.handle(
            Request(verb=Verb.ENQUEUE, source_id="s", payload={"text": "the device is gone"})
        ).ok
        assert daemon.wait_idle(timeout=5.0)
    finally:
        daemon.stop()

    errors = [e for e in seen if e.kind == "error"]
    assert any("audio device wedged" in str(e.data.get("message")) for e in errors), seen
    finished = [e for e in seen if e.kind == "finished"]
    assert finished and finished[-1].data["aborted"] is True, seen


# --------------------------------------------------------------------------
# The contract both sinks owe, asserted against both of them. FakeSink is
# what every other test in the project runs on; if it is allowed to be more
# forgiving than SoundDeviceSink, those tests certify a sink nobody ships.
# --------------------------------------------------------------------------


@pytest.fixture(params=["FakeSink", "SoundDeviceSink"])
def either_sink(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> AudioSink:
    from speakd.player import FakeSink, SoundDeviceSink

    if request.param == "FakeSink":
        return FakeSink()
    _install_fake_sounddevice(monkeypatch)
    return SoundDeviceSink(sample_rate=24000)


def test_both_sinks_record_a_write_that_lands_after_stop(either_sink: AudioSink) -> None:
    from speakd.player import _dropped

    either_sink.start()
    either_sink.write(_frames(10))
    either_sink.stop()
    either_sink.write(_frames(5))

    assert either_sink.frames_written == 10
    assert either_sink.errors == [_dropped(5, "write() after stop()")]


def test_both_sinks_are_writable_again_after_start(either_sink: AudioSink) -> None:
    either_sink.start()
    either_sink.write(_frames(10))
    either_sink.stop()
    either_sink.start()
    either_sink.write(_frames(5))

    assert either_sink.frames_written == 15
    assert either_sink.errors == []


def test_close_is_terminal_for_both_sinks(either_sink: AudioSink) -> None:
    either_sink.start()
    either_sink.close()

    with pytest.raises(RuntimeError):
        either_sink.write(_frames(4))
    with pytest.raises(RuntimeError):
        either_sink.start()


# --------------------------------------------------------------------------
# Teardown that fails. stop() and close() record rather than raise for the
# same reason write() does: a hush that crashes the utterance is worse than
# the hush. The stream is unusable either way, so the sink goes on saying so
# instead of going on failing.
# --------------------------------------------------------------------------


def test_sounddevice_sink_records_an_abort_that_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """A device that disappears mid-utterance must not turn the hush that
    follows into a traceback."""
    from speakd.player import SoundDeviceSink

    streams = _install_fake_sounddevice(monkeypatch)
    sink = SoundDeviceSink(sample_rate=24000)
    sink.start()
    sink.write(_frames(4))
    streams[0].fail_next_abort = FakePortAudioError("device vanished")

    sink.stop()  # must not raise

    assert any("could not abort the stream" in message for message in sink.errors)

    # and the sink is still reusable through start(), which is what the
    # contract promises whether or not the abort landed.
    sink.start()
    sink.write(_frames(4))
    assert len(streams) == 2
    assert sink.frames_written == 8


def test_sounddevice_sink_records_a_reap_that_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reaping is housekeeping. Its failure is recorded, but it must not
    abort the utterance that triggered it."""
    from speakd.player import SoundDeviceSink

    streams = _install_fake_sounddevice(monkeypatch)
    sink = SoundDeviceSink(sample_rate=24000)
    sink.start()
    sink.write(_frames(4))
    sink.stop()
    streams[0].fail_next_close = FakePortAudioError("close failed")

    sink.start()  # must not raise
    sink.write(_frames(4))

    assert any("could not close the aborted stream" in message for message in sink.errors)
    assert len(streams) == 2
    assert streams[1].state == "running"
    assert sink.frames_written == 8


def test_sounddevice_sink_records_a_close_that_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """close() is terminal even when the device refuses to be released:
    a sink that stayed writable after a failed close would keep writing to a
    stream nobody owns."""
    from speakd.player import SoundDeviceSink

    streams = _install_fake_sounddevice(monkeypatch)
    sink = SoundDeviceSink(sample_rate=24000)
    sink.start()
    streams[0].fail_next_close = FakePortAudioError("close failed")

    sink.close()  # must not raise

    assert any("could not close the stream" in message for message in sink.errors)
    with pytest.raises(RuntimeError):
        sink.write(_frames(4))
    with pytest.raises(RuntimeError):
        sink.start()


def test_sounddevice_sink_records_an_abort_that_fails_on_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """close() aborts first to release any blocked writer. A failure there is
    recorded and the close goes ahead: the point of close() is the release."""
    from speakd.player import SoundDeviceSink

    streams = _install_fake_sounddevice(monkeypatch)
    sink = SoundDeviceSink(sample_rate=24000)
    sink.start()
    streams[0].fail_next_abort = FakePortAudioError("abort failed")

    sink.close()  # must not raise

    assert any("could not abort the stream on close" in message for message in sink.errors)
    assert streams[0].close_calls == 1, "the device must still be released"
    with pytest.raises(RuntimeError):
        sink.start()


def test_both_sinks_bound_the_error_log(either_sink: AudioSink) -> None:
    """A device that has begun failing fails repeatedly, and this daemon runs
    for days. The log has to keep answering "what went wrong" without
    answering it ten thousand times."""
    from speakd.player import _MAX_ERRORS

    either_sink.start()
    either_sink.stop()
    # Distinct frame counts, so "kept the first N" is distinguishable from
    # "kept the last N" — with identical messages it would not be.
    for n in range(1, _MAX_ERRORS + 201):
        either_sink.write(_frames(n))

    assert len(either_sink.errors) == _MAX_ERRORS + 1, "the first N, plus one running tally"
    assert either_sink.errors[-1] == "... and 200 further errors not recorded"
    assert "dropped 1 frames" in either_sink.errors[0], "the first are kept, not the last"
    assert f"dropped {_MAX_ERRORS} frames" in either_sink.errors[_MAX_ERRORS - 1]


# A GIL hand-over inside a few bytecodes is not reachable by luck: sixty
# trials of six threads hammering the real class produced zero interleavings.
# So the two decision points inside append() are widened on purpose.
_YIELD = 0.0005


class _SlowErrorLog(_ErrorLog):
    """`_ErrorLog` with the windows inside its inherited `append()` widened.

    `append()` is *not* overridden — it is the code under test. All this
    subclass does is make the length check and the `dropped` read-modify-write
    slow enough that the interleaving they must survive actually happens. A
    correct append() is unaffected by how slow its own reads are; an
    unsynchronised one is not.
    """

    def __len__(self) -> int:
        length = list.__len__(self)
        time.sleep(_YIELD)
        return length

    @property
    def dropped(self) -> int:
        time.sleep(_YIELD)
        return self._dropped

    @dropped.setter
    def dropped(self, value: int) -> None:
        self._dropped = value


def _hammer_log(log: _ErrorLog, barrier: threading.Barrier, count: int) -> None:
    barrier.wait(timeout=10.0)
    for _ in range(count):
        log.append("overflow")


@pytest.mark.parametrize("seed", [_MAX_ERRORS - 1, _MAX_ERRORS])
def test_the_error_log_holds_its_cap_under_concurrent_appends(seed: int) -> None:
    """The log is written from two threads — the playback thread records a
    write it lost, the control thread records a teardown that failed — and
    `SoundDeviceSink._lock` does not cover both: the mid-write record happens
    after that lock is released, and `FakeSink` has no lock at all. So the cap
    has to be the log's own business.

    Two hazards, one invariant. Threads crossing `len(self) < _MAX_ERRORS`
    together all append, and the list grows past its bound. Threads crossing
    the first overflow together can both read `dropped == 0`, both take the
    first-overflow branch and both append the tally — after which later drops
    overwrite the last entry and never touch the duplicate beneath it, so the
    breach is permanent. Seeding at the cap and just under it puts the threads
    on each boundary in turn.
    """
    for attempt in range(6):
        log = _SlowErrorLog()
        for i in range(seed):
            log.append(f"seed {i}")

        barrier = threading.Barrier(8)
        threads = [
            threading.Thread(target=_hammer_log, args=(log, barrier, 6), daemon=True)
            for _ in range(8)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10.0)
        assert not any(thread.is_alive() for thread in threads)

        tallies = [m for m in log if m.startswith("... and ")]
        where = f"seed={seed} attempt={attempt}"
        assert len(tallies) == 1, f"{where}: {len(tallies)} tally entries, expected exactly 1"
        assert len(log) == _MAX_ERRORS + 1, f"{where}: cap breached, len={len(log)}"
        assert log[-1] == tallies[0], f"{where}: the tally must be the last entry"
        assert log[0] == "seed 0", f"{where}: the first messages must survive"


def test_sounddevice_sink_close_does_not_release_a_stream_a_writer_is_inside(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The twin of the start() reap guard. close() aborts first, which
    releases a blocked writer — but released is not returned, and closing in
    that window is the undefined behaviour this class exists to avoid. It
    declines, records, and stays terminal regardless."""
    from speakd.player import SoundDeviceSink

    streams = _install_fake_sounddevice(monkeypatch)
    sink = SoundDeviceSink(sample_rate=24000)
    sink.start()
    stream = streams[0]
    stream.block_writes()
    stream.pin_writer_inside()

    writer = _Writer(sink, _frames(64))
    writer.start()
    assert stream.entered_write.wait(timeout=2.0), "the writer never reached write()"

    sink.close()

    assert stream.violations == [], "must not close a stream a thread is inside"
    assert stream.close_calls == 0
    assert stream.abort_calls == 1, "but it must still abort, to release the writer"
    assert any("in flight" in message for message in sink.errors)

    # Declining to close does not make close() any less terminal.
    with pytest.raises(RuntimeError):
        sink.start()
    with pytest.raises(RuntimeError):
        sink.write(_frames(4))

    stream.release_writer()
    writer.join(timeout=2.0)
    assert not writer.is_alive()
    assert writer.error is None, f"the released write must not raise: {writer.error!r}"


def test_streaming_player_is_pausable() -> None:
    from speakd.player import FakeSink, Pausable, StreamingPlayer

    assert isinstance(StreamingPlayer(FakeSink()), Pausable)


def test_recording_player_is_not_pausable() -> None:
    from speakd.player import Pausable, RecordingPlayer

    assert not isinstance(RecordingPlayer(), Pausable)


def test_a_player_that_cannot_pause_is_still_a_player() -> None:
    """The point of a separate protocol: no existing player has to change."""
    from speakd.player import Player, RecordingPlayer

    player: Player = RecordingPlayer()
    player.play(np.zeros(4, dtype=np.float32), 24000)
    player.stop()
    assert isinstance(player, RecordingPlayer)
    assert len(player.played) == 1
    assert player.stopped is True
