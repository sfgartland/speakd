"""Tests for playback sinks."""

import sys
import threading
import types

import numpy as np
import pytest

from speakd.player import AudioSink, RecordingPlayer


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
