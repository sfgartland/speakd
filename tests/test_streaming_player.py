"""Tests for interruptible playback."""

import threading

import numpy as np

from speakd.player import FakeSink, StreamingPlayer, Stretchable
from speakd.tempo import Tempo


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


def test_pause_suspends_and_resume_continues() -> None:
    import threading
    import time

    sink = FakeSink()
    player = StreamingPlayer(sink, chunk_frames=100)
    player.pause()
    done = threading.Event()

    def run() -> None:
        player.play(audio(1000), 24000)
        done.set()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    time.sleep(0.05)
    assert not done.is_set(), "paused playback should not finish"
    assert player.paused is True
    written_while_paused = sink.frames_written
    player.resume()
    assert done.wait(timeout=5.0), "resume should let playback finish"
    thread.join(timeout=5.0)
    assert sink.frames_written == 1000
    assert written_while_paused == 0, "a pause taken before play() must write nothing at all"


def test_stop_while_paused_does_not_deadlock() -> None:
    import threading
    import time

    sink = FakeSink()
    player = StreamingPlayer(sink, chunk_frames=100)
    player.pause()
    done = threading.Event()

    def run() -> None:
        player.play(audio(10_000), 24000)
        done.set()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    time.sleep(0.05)
    player.stop()
    assert done.wait(timeout=5.0), "stop must release a paused play()"
    thread.join(timeout=5.0)
    assert player.interrupted is True
    assert player.paused is True, "stop() must not clear pause state"


def test_pause_is_sticky_across_stop() -> None:
    import threading
    import time

    sink = FakeSink()
    player = StreamingPlayer(sink, chunk_frames=100)
    player.pause()
    done = threading.Event()

    def run() -> None:
        player.play(audio(10_000), 24000)
        done.set()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    time.sleep(0.05)
    player.stop()
    assert done.wait(timeout=5.0), "stop must release a paused play()"
    thread.join(timeout=5.0)
    assert player.paused is True

    # A fresh, unrelated play() must start paused too: pause outlives the stop.
    frames_before = sink.frames_written
    second_done = threading.Event()

    def run_again() -> None:
        player.play(audio(500), 24000)
        second_done.set()

    second_thread = threading.Thread(target=run_again, daemon=True)
    second_thread.start()
    time.sleep(0.05)
    assert not second_done.is_set(), "next play() should start paused"
    assert sink.frames_written == frames_before

    player.resume()
    assert second_done.wait(timeout=5.0), "resume should let the next play() finish"
    second_thread.join(timeout=5.0)
    assert sink.frames_written == frames_before + 500


def test_resume_before_play_releases_a_pause_taken_before_play() -> None:
    """resume() has to actually lift the pause, not merely be callable.

    Asserting only that a never-paused player still plays tests the `set()`
    in `__init__` and passes with resume()'s whole body deleted, which is how
    this one used to read.
    """
    import threading

    sink = FakeSink()
    player = StreamingPlayer(sink, chunk_frames=100)
    player.pause()
    player.resume()
    assert player.paused is False
    done = threading.Event()

    def run() -> None:
        player.play(audio(100), 24000)
        done.set()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    assert done.wait(timeout=5.0), "resume() must release the pause it was given"
    thread.join(timeout=5.0)
    assert sink.frames_written == 100


def test_a_stop_landing_on_an_empty_segment_is_consumed() -> None:
    """An interrupt must be consumed even when there is no chunk to check it in.

    kokoro returns a zero-length array for blank text and for a segment it
    yields no audio for, so this is a real segment shape, not a contrived one.
    An interrupt left set by it silently kills the *next* utterance.
    """
    sink = FakeSink()
    player = StreamingPlayer(sink, chunk_frames=100)
    player.stop()
    player.play(audio(0), 24000)
    assert player.interrupted is True, "the stop must be reported on the segment it hit"

    player.play(audio(100), 24000)
    assert player.interrupted is False, "and must not survive into the next segment"
    assert sink.frames_written == 100


def test_an_interrupted_segment_opens_no_device() -> None:
    """A play() that writes nothing must not have opened a stream to do it.

    Both sinks open lazily on write(), so starting eagerly at the top of
    play() only ever buys a device stream for an utterance that never speaks.
    """
    sink = FakeSink()
    player = StreamingPlayer(sink, chunk_frames=100)
    player.stop()
    player.play(audio(1000), 24000)
    assert player.interrupted is True
    assert sink.started is False, "an interrupted segment must open no device"


def test_an_empty_segment_opens_no_device() -> None:
    sink = FakeSink()
    player = StreamingPlayer(sink, chunk_frames=100)
    player.play(audio(0), 24000)
    assert sink.started is False, "an empty segment must open no device"


def test_player_exposes_what_the_sink_recorded() -> None:
    """Dropped audio has to reach someone. The pipeline owns the player, not
    the sink, so the player is where `errors` has to be readable from."""
    sink = FakeSink()
    player = StreamingPlayer(sink, chunk_frames=100)
    assert player.errors == []

    sink.stop()
    sink.write(audio(100))

    assert len(player.errors) == 1
    assert "dropped 100 frames" in player.errors[0]


def tone(frames: int) -> np.ndarray:
    return (0.3 * np.sin(np.arange(frames) * 2 * np.pi * 220 / 24000)).astype(np.float32)


def test_play_at_the_speed_it_was_made_is_plain_play() -> None:
    sink = FakeSink()
    seconds = StreamingPlayer(sink, chunk_frames=1000).play_at(
        tone(24000), 24000, made_at=1.2, tempo=Tempo(1.2)
    )
    assert sink.frames_written == 24000
    assert seconds == 1.0


def test_play_at_stretches_to_the_current_tempo() -> None:
    sink = FakeSink()
    seconds = StreamingPlayer(sink, chunk_frames=1000).play_at(
        tone(24000), 24000, made_at=1.0, tempo=Tempo(1.5)
    )
    assert abs(sink.frames_written - 16000) <= 2
    assert abs(seconds - 16000 / 24000) < 1e-3


def test_a_tempo_change_mid_segment_stretches_only_the_rest() -> None:
    tempo = Tempo(1.0)

    class Changing(FakeSink):
        def write(self, frames: np.ndarray) -> None:
            super().write(frames)
            if self.frames_written == 12000:
                tempo.set(1.5)

    sink = Changing()
    StreamingPlayer(sink, chunk_frames=1000).play_at(tone(24000), 24000, made_at=1.0, tempo=tempo)
    assert abs(sink.frames_written - (12000 + 8000)) <= 2


def test_the_streaming_player_is_stretchable() -> None:
    assert isinstance(StreamingPlayer(FakeSink()), Stretchable)


def test_clear_interrupt_forgets_a_stop_aimed_at_finished_playback() -> None:
    sink = FakeSink()
    player = StreamingPlayer(sink, chunk_frames=100)
    player.stop()
    player.clear_interrupt()
    player.play(audio(250), 24000)
    assert player.interrupted is False
    assert sink.frames_written == 250


def test_speed_changes_do_not_compound() -> None:
    """Every stretch is taken from the audio as it was made, never from an
    earlier stretch of it: going to 1.5 and back to 1.0 leaves the rest of the
    segment exactly as synthesised, not a copy of a copy."""
    tempo = Tempo(1.0)
    source = tone(24000)

    class Toggling(FakeSink):
        def write(self, frames: np.ndarray) -> None:
            super().write(frames)
            if self.frames_written == 6000:
                tempo.set(1.5)
            elif self.frames_written == 8000:
                tempo.set(1.0)

    sink = Toggling()
    StreamingPlayer(sink, chunk_frames=1000).play_at(source, 24000, made_at=1.0, tempo=tempo)
    written = np.concatenate(sink.blocks)
    # 6000 at 1.0, then 2000 at 1.5 consume 3000 more: 9000 of the source.
    assert abs(len(written) - (8000 + 15000)) <= 2
    np.testing.assert_array_equal(written[-14000:], source[-14000:])


def test_a_stretch_has_no_dropout_at_its_start() -> None:
    from speakd.stretch import time_stretch

    out = time_stretch(tone(24000) + 0.5, 1.3, 24000)
    assert out[0] > 0.1


def test_a_stretch_only_ever_covers_a_short_look_ahead(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """A ramp changes speed every chunk; re-stretching all of a long sentence
    each time costs as much CPU as the synthesiser needs."""
    import speakd.player as player_module
    from speakd.stretch import time_stretch as real

    lengths: list[int] = []

    def recording(audio: np.ndarray, ratio: float, sample_rate: int) -> np.ndarray:
        lengths.append(len(audio))
        return real(audio, ratio, sample_rate)

    monkeypatch.setattr(player_module, "time_stretch", recording)
    sink = FakeSink()
    source = tone(24000 * 20)  # twenty seconds
    StreamingPlayer(sink, chunk_frames=2048).play_at(source, 24000, made_at=1.0, tempo=Tempo(1.5))
    assert max(lengths) <= 24000 * player_module.STRETCH_LOOKAHEAD_SECONDS + 1
    # And still the whole sentence, at the new speed.
    assert abs(sink.frames_written - len(source) / 1.5) < 24000 * 0.05
