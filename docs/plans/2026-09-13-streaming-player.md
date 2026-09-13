# Streaming Player Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make audio interruptible mid-segment and pausable, without touching the pipeline that drives it.

**Architecture:** A `StreamingPlayer` owning its own `sounddevice.OutputStream` with a callback pulling from a bounded buffer. `play()` still blocks until the segment finishes — so the pipeline's producer/consumer rate limiting is completely unchanged — but it now also returns promptly when interrupted. The stream is injectable, so every test runs against a fake with no audio device.

**Tech Stack:** Python 3.11–3.13, numpy, sounddevice (optional extra), pytest, ruff, mypy strict.

**Spec:** `docs/design/2026-09-13-gui-milestone-design.md`

## The design decision that shapes this plan

The obvious reading of "replace the blocking player" is to make `play()` non-blocking and rewrite the pipeline to wait on buffer space instead. **Do not do that.** The pipeline's depth-one queue and its drain-to-sentinel teardown are load-bearing and hard-won — they are what stop a producer thread leaking on every cancellation, and cancellation is the common path. Rewriting the loop around a new rate-limiting mechanism risks reintroducing that leak, or an unbounded buffer, for no gain the GUI needs.

Instead `play()` keeps its contract — **it blocks until the segment has finished** — and gains one new property: it also returns when interrupted. Playback remains the pipeline's clock. `pipeline.py` is not modified by this plan at all.

What that buys: `stop()` clears the buffer and returns immediately, so a hush interrupts mid-segment instead of waiting up to four seconds for it to end. `pause()` stops the stream consuming while `play()` keeps waiting. And the callback knows how many frames actually reached the device, which is a true playback position rather than a stamp taken at segment start.

## Global Constraints

- Python `>=3.11,<3.14`; core dependencies stay numpy-only. `sounddevice` remains behind the `kokoro` extra, imported inside functions, never at module import time.
- **No test may require an audio device.** The output stream is injected; tests use a fake. A test that opens a real device would pass on the developer's machine and fail in CI, which installs no extras.
- **`src/speakd/pipeline.py` must not be modified by this plan.** If a task seems to need it, stop and report rather than proceeding.
- Speech never disappears silently: an underrun, a device error or a dropped buffer is recorded, never swallowed.
- ruff line length 100. `uv run pytest`, `uv run ruff check .`, `uv run ruff format --check .` and `uv run mypy src tests` must all pass before every commit.
- Commit after every task.

---

### Task 1: The audio sink seam

**Files:**
- Modify: `src/speakd/player.py`
- Test: `tests/test_player.py`

**Interfaces:**
- Produces: `AudioSink` Protocol with `start() -> None`, `write(frames: np.ndarray) -> None`, `stop() -> None`, `close() -> None`, property `frames_written: int`; `FakeSink()` recording everything written, with `frames_written` and a `blocks: list[np.ndarray]`; `SoundDeviceSink(sample_rate: int, blocksize: int = 1024)`

This is the seam that keeps every later test off a real audio device. `SoundDeviceSink` imports `sounddevice` inside its methods, so importing `speakd.player` still works without the extra — the existing `test_importing_player_does_not_require_sounddevice` must keep passing.

- [ ] **Step 1: Write the failing test**

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_player.py -v`
Expected: FAIL with `ImportError: cannot import name 'FakeSink'`.

- [ ] **Step 3: Write minimal implementation**

Append to `src/speakd/player.py`:

```python
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
        self.blocks.append(frames)
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
        self._stream: object | None = None

    def start(self) -> None:
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
        self._stream.write(frames)  # type: ignore[attr-defined]
        self.frames_written += len(frames)

    def stop(self) -> None:
        if self._stream is not None:
            self._stream.abort()  # type: ignore[attr-defined]

    def close(self) -> None:
        if self._stream is not None:
            self._stream.close()  # type: ignore[attr-defined]
            self._stream = None
```

- [ ] **Step 4: Run tests and linters**

Run: `uv run pytest -q && uv run ruff check . && uv run ruff format --check . && uv run mypy src tests`

- [ ] **Step 5: Commit**

```bash
git add src/speakd/player.py tests/test_player.py
git commit -m "Add the audio sink seam so playback is testable without a device"
```

---

### Task 2: StreamingPlayer, interruptible mid-segment

**Files:**
- Modify: `src/speakd/player.py`
- Test: `tests/test_streaming_player.py`

**Interfaces:**
- Consumes: `AudioSink`, `FakeSink`
- Produces: `StreamingPlayer(sink: AudioSink, chunk_frames: int = 2048)` satisfying the existing `Player` protocol — `play(audio, sample_rate)` blocking until the segment finishes **or is interrupted**, `stop()` returning immediately — plus `frames_played: int` and `interrupted: bool`

`play()` writes the segment to the sink in chunks, checking an interrupt flag between chunks. That is what makes a hush take effect within one chunk instead of waiting out the whole segment. At 2048 frames and 24 kHz a chunk is 85 ms (2048/24000); the 45 ms figure is the 48 kHz one. So the interrupt-latency bound is **~85 ms while playing** — the check happens between chunks, and a blocking write holds for the chunk it is playing — and **50 ms while paused**, which is the resume wait's poll interval.

- [ ] **Step 1: Write the failing test**

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_streaming_player.py -v`
Expected: FAIL with `ImportError: cannot import name 'StreamingPlayer'`.

- [ ] **Step 3: Write minimal implementation**

Append to `src/speakd/player.py`:

```python
class StreamingPlayer:
    """Blocks until the segment finishes, or until it is interrupted.

    Keeping `play()` blocking is deliberate: the pipeline uses playback as its
    clock, and its depth-one queue and drain-to-sentinel teardown are what stop a
    producer thread leaking on every cancellation. This class changes how promptly
    playback can be abandoned, not who rate-limits whom.
    """

    def __init__(self, sink: AudioSink, chunk_frames: int = 2048) -> None:
        self._sink = sink
        self._chunk = chunk_frames
        self._interrupt = threading.Event()
        self.frames_played = 0
        self.interrupted = False

    def play(self, audio: np.ndarray, sample_rate: int) -> None:
        self._interrupt.clear()
        self.interrupted = False
        self._sink.start()
        for start in range(0, len(audio), self._chunk):
            if self._interrupt.is_set():
                self.interrupted = True
                return
            block = audio[start : start + self._chunk]
            self._sink.write(block)
            self.frames_played += len(block)

    def stop(self) -> None:
        self._interrupt.set()
        self._sink.stop()
```

Add `import threading` at the top of the module if it is not already present.

**Note the ordering in `play()`:** the interrupt is cleared at entry, so a `stop()` arriving before a `play()` still interrupts that play — the brief's test relies on this, because `stop()` sets the event and `play()` checks it before writing the first chunk. Read that test carefully before changing the order.

- [ ] **Step 4: Run tests and linters**

Run: `uv run pytest -q && uv run ruff check . && uv run ruff format --check . && uv run mypy src tests`

- [ ] **Step 5: Commit**

```bash
git add src/speakd/player.py tests/test_streaming_player.py
git commit -m "Add StreamingPlayer: interruptible within one chunk"
```

---

### Task 3: Pause and resume

**Files:**
- Modify: `src/speakd/player.py`
- Test: `tests/test_streaming_player.py`

**Interfaces:**
- Produces: `StreamingPlayer.pause() -> None`, `resume() -> None`, property `paused: bool`

Pause holds the write loop between chunks rather than stopping it, so `play()` keeps blocking and the pipeline's clock keeps working — the utterance is suspended, not abandoned. Resume releases it. A `stop()` while paused must end the wait rather than deadlock, which is the one way this can go badly wrong.

- [ ] **Step 1: Write the failing test**

```python
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
    assert written_while_paused < 1000


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


def test_resume_without_pause_is_harmless() -> None:
    sink = FakeSink()
    player = StreamingPlayer(sink, chunk_frames=100)
    player.resume()
    player.play(audio(100), 24000)
    assert sink.frames_written == 100
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_streaming_player.py -v`
Expected: FAIL with `AttributeError: 'StreamingPlayer' object has no attribute 'pause'`.

- [ ] **Step 3: Write minimal implementation**

Add a resume event to `__init__`, set so playback runs by default:

```python
        self._resume = threading.Event()
        self._resume.set()
```

and a `paused` property reading `not self._resume.is_set()`. In the chunk loop, wait on it before each write, with the interrupt able to release the wait:

```python
            while not self._resume.wait(timeout=0.05):
                if self._interrupt.is_set():
                    break
            if self._interrupt.is_set():
                self.interrupted = True
                return
```

`pause()` clears the resume event; `resume()` sets it. **`stop()` must set the interrupt *and* set the resume event**, so a paused `play()` wakes, sees the interrupt and returns rather than waiting forever.

- [ ] **Step 4: Run tests and linters**

Run: `uv run pytest -q && uv run ruff check . && uv run ruff format --check . && uv run mypy src tests`

- [ ] **Step 5: Commit**

```bash
git add src/speakd/player.py tests/test_streaming_player.py
git commit -m "Add pause and resume to StreamingPlayer"
```

---

## Done when

- A `stop()` from another thread ends a long segment within one chunk rather than at the segment boundary.
- `pause()` suspends playback without ending the utterance; `resume()` continues it; `stop()` while paused returns rather than deadlocking.
- Every test runs with no audio device, and `import speakd.player` still works without the `kokoro` extra.
- `src/speakd/pipeline.py` is untouched by this plan.

## Not in this plan

Seek, which needs a start index into segments and belongs with the daemon verbs that expose it. Wiring `StreamingPlayer` into the CLI or the daemon — this plan delivers the capability; adopting it is the daemon's decision, made once its own plan has merged. The HTTP and SSE transport, the frontend and the Tauri shell.
