"""Tests for interruptible playback."""

import threading

import numpy as np

from speakd.player import FakeSink, StreamingPlayer


def audio(frames: int) -> np.ndarray:
    return np.zeros(frames, dtype=np.float32)


def test_play_writes_the_whole_segment() -> None:
    sink = FakeSink()
    player = StreamingPlayer(sink, chunk_frames=100)
    player.play(audio(250), 24000)
    assert sink.frames_written == 250
    assert player.frames_played == 250
    assert player.interrupted is False


def test_play_chunks_the_write() -> None:
    sink = FakeSink()
    StreamingPlayer(sink, chunk_frames=100).play(audio(250), 24000)
    assert [len(b) for b in sink.blocks] == [100, 100, 50]


def test_stop_interrupts_within_one_chunk() -> None:
    sink = FakeSink()
    player = StreamingPlayer(sink, chunk_frames=100)
    player.stop()  # already stopped before play
    player.play(audio(10_000), 24000)
    assert player.interrupted is True
    assert sink.frames_written == 0


def test_stop_from_another_thread_ends_a_long_segment_early() -> None:
    class SlowSink(FakeSink):
        def write(self, frames: np.ndarray) -> None:
            super().write(frames)
            threading.Event().wait(0.01)

    sink = SlowSink()
    player = StreamingPlayer(sink, chunk_frames=100)
    stopper = threading.Timer(0.05, player.stop)
    stopper.start()
    player.play(audio(100_000), 24000)
    stopper.cancel()
    assert player.interrupted is True
    assert sink.frames_written < 100_000


def test_a_new_play_clears_the_interrupt() -> None:
    sink = FakeSink()
    player = StreamingPlayer(sink, chunk_frames=100)
    player.stop()
    player.play(audio(100), 24000)
    assert player.interrupted is True
    player.play(audio(100), 24000)
    assert player.interrupted is False
    assert sink.frames_written == 100


def test_frames_played_accumulates_across_segments() -> None:
    sink = FakeSink()
    player = StreamingPlayer(sink, chunk_frames=100)
    player.play(audio(100), 24000)
    player.play(audio(150), 24000)
    assert player.frames_played == 250


def test_empty_audio_is_harmless() -> None:
    sink = FakeSink()
    player = StreamingPlayer(sink, chunk_frames=100)
    player.play(audio(0), 24000)
    assert sink.frames_written == 0
    assert player.interrupted is False
