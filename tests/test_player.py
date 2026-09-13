"""Tests for playback sinks."""

import numpy as np

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
    header = module_source.split("class ")[0]
    assert "import sounddevice" not in header, "and must not be at module level"
