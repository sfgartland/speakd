"""Tests for playback sinks."""

import sys
from unittest.mock import MagicMock

import numpy as np
import pytest

from speakd.player import RecordingPlayer


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


def test_fake_sink_stop_discards_pending_but_keeps_the_count() -> None:
    from speakd.player import FakeSink

    sink = FakeSink()
    sink.start()
    sink.write(np.zeros(10, dtype=np.float32))
    sink.stop()
    assert sink.stopped is True
    assert sink.frames_written == 10


def test_importing_player_still_does_not_require_sounddevice() -> None:
    import speakd.player  # noqa: F401


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


def test_fake_sink_write_after_stop_restarts_and_stays_observable() -> None:
    """stop() ends playback but leaves the sink reusable: a later write()
    is a fresh start, and it must succeed exactly like the real sink does
    once SoundDeviceSink clears its stream on stop()."""
    from speakd.player import FakeSink

    sink = FakeSink()
    sink.start()
    sink.write(np.zeros(10, dtype=np.float32))
    sink.stop()
    sink.write(np.zeros(5, dtype=np.float32))
    assert sink.frames_written == 15
    assert [len(b) for b in sink.blocks] == [10, 5]
    # stop() having happened stays visible even though the sink went on
    # to accept more audio afterward.
    assert sink.stopped is True


def _install_fake_sounddevice(monkeypatch: pytest.MonkeyPatch) -> list[MagicMock]:
    """Injects a fake `sounddevice` module and returns the OutputStream mocks
    it hands out, in creation order — so SoundDeviceSink's own logic runs for
    real, against a fake device, with no hardware and no extra installed."""
    streams: list[MagicMock] = []

    def make_stream(**_kwargs: object) -> MagicMock:
        stream = MagicMock()
        streams.append(stream)
        return stream

    fake_module = MagicMock()
    fake_module.OutputStream.side_effect = make_stream
    monkeypatch.setitem(sys.modules, "sounddevice", fake_module)
    return streams


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
    assert streams[0].start.call_count == 1


def test_sounddevice_sink_stop_then_write_starts_a_new_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """stop() ends the current playback but leaves the sink reusable: the
    next write() must succeed by starting a fresh stream, not by writing
    into the aborted one."""
    from speakd.player import SoundDeviceSink

    streams = _install_fake_sounddevice(monkeypatch)

    sink = SoundDeviceSink(sample_rate=24000)
    sink.start()
    sink.write(np.zeros(4, dtype=np.float32))
    sink.stop()

    first_stream = streams[0]
    first_stream.abort.assert_called_once()
    first_stream.close.assert_called_once()

    sink.write(np.zeros(4, dtype=np.float32))

    assert len(streams) == 2
    streams[1].write.assert_called_once()
    assert sink.frames_written == 8
