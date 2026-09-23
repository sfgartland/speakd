# Follow-along text, working seek, live speed — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The pinned window shows the whole utterance as rendered markdown with a highlight that walks through it, ←/→ actually seek, and − / + change speed live.

**Architecture:** The daemon segments before speaking and announces every segment in `started`; `position` carries the segment index. `seek` re-enters `speak()` at a new index, reusing a per-utterance audio cache. Speed is a shared `Tempo` multiplier: new audio is synthesised at the multiplied speed, audio already made is WSOLA-stretched by the player at chunk boundaries. The window renders markdown with a small in-repo renderer that tracks source offsets, and locates each segment's text in it.

**Tech Stack:** Python 3.11+, numpy, pytest (`uv run pytest`); vanilla ES modules in a Tauri 2 webview; Rust only for one allowlist line.

**Spec:** `docs/superpowers/specs/2026-09-23-follow-along-seek-speed-design.md`

## Global Constraints

- Speed multiplier clamped to [0.7, 1.6], rounded to 0.05; default 1.0.
- Audio cache cap: 64 MiB of float32 per utterance.
- No new Python or JS dependencies.
- Window code never assigns message/utterance text through `innerHTML`.
- Every `state.save` writes `dataclasses.replace(state.load(), …)`.
- Full suite green at the end (874 passing at start). Lint: `uv run ruff check src tests`, types: `uv run mypy src`.
- Comment style: match the repo — comments explain *why*, in full sentences.

---

### Task 1: WSOLA time-stretch

**Files:**
- Create: `src/speakd/stretch.py`
- Test: `tests/test_stretch.py`

**Interfaces:**
- Produces: `time_stretch(audio: np.ndarray, ratio: float, sample_rate: int) -> np.ndarray` — `ratio > 1` shortens (faster). float32 out.

- [ ] **Step 1: Write the failing tests**

```python
"""Tests for pitch-preserving time-stretch."""

import numpy as np

from speakd.stretch import time_stretch

SR = 24000


def sine(freq: float, seconds: float) -> np.ndarray:
    t = np.arange(int(SR * seconds)) / SR
    return (0.5 * np.sin(2 * np.pi * freq * t)).astype(np.float32)


def dominant(audio: np.ndarray) -> float:
    spectrum = np.abs(np.fft.rfft(audio * np.hanning(len(audio))))
    return float(np.fft.rfftfreq(len(audio), 1 / SR)[int(np.argmax(spectrum))])


def test_faster_shortens_by_the_ratio() -> None:
    out = time_stretch(sine(220, 1.0), 1.5, SR)
    assert abs(len(out) - SR / 1.5) <= 2


def test_slower_lengthens_by_the_ratio() -> None:
    out = time_stretch(sine(220, 1.0), 0.8, SR)
    assert abs(len(out) - SR / 0.8) <= 2


def test_pitch_is_kept() -> None:
    for ratio in (0.7, 1.3, 1.6):
        assert abs(dominant(time_stretch(sine(220, 1.0), ratio, SR)) - 220) < 6


def test_ratio_one_is_the_identity() -> None:
    audio = sine(220, 0.2)
    assert time_stretch(audio, 1.0, SR) is audio


def test_short_and_empty_input_are_safe() -> None:
    assert len(time_stretch(np.zeros(0, dtype=np.float32), 1.5, SR)) == 0
    out = time_stretch(sine(220, 0.01), 1.5, SR)
    assert abs(len(out) - int(round(SR * 0.01 / 1.5))) <= 1
    assert out.dtype == np.float32
```

- [ ] **Step 2: Run to verify failure** — `uv run pytest tests/test_stretch.py -q` → ImportError.

- [ ] **Step 3: Implement**

```python
"""Change speech rate without changing its pitch.

WSOLA: cut the input into overlapping windowed frames, lay them down at a
fixed output hop, and take each frame from wherever near its nominal input
position best continues the one before it. Playing samples faster instead
would raise the pitch with the rate, which is the one thing a listener speeding
up speech does not want.

Used only for audio already synthesised when the speed changes -- the sentence
playing and the one buffered behind it. Everything after is synthesised at the
new speed by the engine itself, which sounds better than any stretch.
"""

from __future__ import annotations

import math

import numpy as np

_FRAME_SECONDS = 0.030
_TOLERANCE_SECONDS = 0.005


def time_stretch(audio: np.ndarray, ratio: float, sample_rate: int) -> np.ndarray:
    """Return `audio` played `ratio` times as fast, at the same pitch."""
    if abs(ratio - 1.0) < 1e-3 or len(audio) == 0:
        return audio
    out_len = int(round(len(audio) / ratio))
    frame = int(_FRAME_SECONDS * sample_rate) // 2 * 2
    if len(audio) < 2 * frame:
        # Too short for a frame to have a neighbour. A tail this short is
        # heard as a click either way; resampling it keeps the length right.
        positions = np.linspace(0, len(audio) - 1, num=out_len)
        return np.interp(positions, np.arange(len(audio)), audio).astype(np.float32)
    hop_out = frame // 2
    hop_in = hop_out * ratio
    tol = int(_TOLERANCE_SECONDS * sample_rate)
    pad_end = frame + 2 * tol + hop_out + math.ceil(hop_in)
    padded = np.concatenate(
        [np.zeros(tol, np.float32), audio.astype(np.float32), np.zeros(pad_end, np.float32)]
    )
    window = np.hanning(frame).astype(np.float32)
    out = np.zeros(out_len + frame, np.float32)
    norm = np.zeros(out_len + frame, np.float32)
    previous = tol
    k = 0
    while k * hop_out < out_len:
        nominal = tol + int(round(k * hop_in))
        if k == 0:
            best = nominal
        else:
            # The frame that would have followed the last one had nothing
            # been stretched; the chosen frame is the one most like it.
            natural = padded[previous + hop_out : previous + hop_out + frame]
            region = padded[nominal - tol : nominal + tol + frame]
            best = nominal - tol + int(np.argmax(np.correlate(region, natural, mode="valid")))
        at = k * hop_out
        out[at : at + frame] += padded[best : best + frame] * window
        norm[at : at + frame] += window
        previous = best
        k += 1
    norm[norm < 1e-6] = 1.0
    return (out / norm)[:out_len].astype(np.float32)
```

- [ ] **Step 4: Run to verify pass** — `uv run pytest tests/test_stretch.py -q` → 5 passed.
- [ ] **Step 5: Commit** — `git add src/speakd/stretch.py tests/test_stretch.py && git commit -m "Stretch speech in time without moving its pitch"`

---

### Task 2: Tempo and the persisted speed

**Files:**
- Create: `src/speakd/tempo.py`
- Modify: `src/speakd/state.py` (add `speed`), `src/speakd/daemon.py:453,631` (use `replace`)
- Test: `tests/test_tempo.py`, `tests/test_state.py` (append)

**Interfaces:**
- Produces: `MIN_SPEED = 0.7`, `MAX_SPEED = 1.6`, `clamp_speed(value: float) -> float`, `class Tempo(value: float = 1.0)` with `.value -> float` and `.set(value: float) -> float` (returns applied). `DaemonState.speed: float = 1.0`.

- [ ] **Step 1: Failing tests** — `tests/test_tempo.py`:

```python
from speakd.tempo import MAX_SPEED, MIN_SPEED, Tempo, clamp_speed


def test_clamps_and_rounds_to_a_twentieth() -> None:
    assert clamp_speed(5) == MAX_SPEED
    assert clamp_speed(0.1) == MIN_SPEED
    assert clamp_speed(1.234) == 1.25
    assert clamp_speed(1.0) == 1.0


def test_set_returns_what_was_applied() -> None:
    tempo = Tempo()
    assert tempo.value == 1.0
    assert tempo.set(9) == MAX_SPEED
    assert tempo.value == MAX_SPEED


def test_a_constructed_value_is_clamped_too() -> None:
    assert Tempo(0.2).value == MIN_SPEED
```

Append to `tests/test_state.py`:

```python
def test_speed_round_trips_and_defaults() -> None:
    from dataclasses import replace

    from speakd import state

    assert state.load().speed == 1.0
    state.save(replace(state.load(), speed=1.3))
    assert state.load().speed == 1.3
    assert state.load().muted is False


def test_a_nonsense_speed_reads_as_the_default() -> None:
    from speakd import state

    state.state_path().parent.mkdir(parents=True, exist_ok=True)
    state.state_path().write_text('{"speed": "fast"}', encoding="utf-8")
    assert state.load().speed == 1.0
```

- [ ] **Step 2: Run** — `uv run pytest tests/test_tempo.py tests/test_state.py -q` → fails.

- [ ] **Step 3: Implement** `src/speakd/tempo.py`:

```python
"""The listener's speed: one number, shared by the synthesiser and the player.

A multiplier on the profile's own speed rather than a speed of its own, so
that 1.0 always means "as the profile was tuned" whichever profile is speaking.
Read on the speech thread and written on the control thread, hence the lock.
"""

from __future__ import annotations

import threading

MIN_SPEED = 0.7
MAX_SPEED = 1.6
_STEP = 0.05


def clamp_speed(value: float) -> float:
    bounded = min(max(float(value), MIN_SPEED), MAX_SPEED)
    return round(round(bounded / _STEP) * _STEP, 2)


class Tempo:
    def __init__(self, value: float = 1.0) -> None:
        self._lock = threading.Lock()
        self._value = clamp_speed(value)

    @property
    def value(self) -> float:
        with self._lock:
            return self._value

    def set(self, value: float) -> float:
        applied = clamp_speed(value)
        with self._lock:
            self._value = applied
        return applied
```

`state.py`: add `speed: float = 1.0` to `DaemonState`; in `load()`:

```python
    raw_speed = body.get("speed", 1.0)
    speed = (
        float(raw_speed)
        if isinstance(raw_speed, (int, float)) and not isinstance(raw_speed, bool)
        and math.isfinite(raw_speed) and raw_speed > 0
        else 1.0
    )
    return DaemonState(muted=..., disabled=..., speed=speed)
```

`daemon.py`: `from dataclasses import dataclass, replace`; line 453 → `state.save(replace(state.load(), disabled=not wanted))`; line 631 → `state.save(replace(state.load(), muted=wanted))`.

- [ ] **Step 4: Run** — `uv run pytest tests/test_tempo.py tests/test_state.py tests/test_mute.py -q` → pass.
- [ ] **Step 5: Commit** — "Keep the listener's speed, and stop state writers dropping each other's fields"

---

### Task 3: The player keeps up with the tempo

**Files:**
- Modify: `src/speakd/player.py` (add `Stretchable`, refactor `StreamingPlayer.play` into `_play`, add `play_at`)
- Test: `tests/test_streaming_player.py` (append)

**Interfaces:**
- Consumes: `Tempo`, `time_stretch`.
- Produces: `class Stretchable(Protocol)` (runtime_checkable) with `play_at(self, audio, sample_rate, *, made_at: float, tempo: Tempo) -> float`; `StreamingPlayer.play_at` returns seconds actually written.

- [ ] **Step 1: Failing tests**

```python
from speakd.player import Stretchable
from speakd.tempo import Tempo


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
```

- [ ] **Step 2: Run** — fails (ImportError).

- [ ] **Step 3: Implement.** In `player.py` add, after `Pausable`:

```python
@runtime_checkable
class Stretchable(Protocol):
    """A player that can follow a speed change inside a segment.

    Separate from `Player` for the reason `Pausable` is: nothing else in the
    speech path needs it, and a test double should not have to carry it.
    """

    def play_at(
        self, audio: np.ndarray, sample_rate: int, *, made_at: float, tempo: Tempo
    ) -> float: ...
```

(`Tempo` imported under `TYPE_CHECKING`; `runtime_checkable` from `typing` if not already.) In `StreamingPlayer`, move the loop body of `play` into `_play(audio, sample_rate, made_at, tempo) -> int` (frames written) and make:

```python
    def play(self, audio: np.ndarray, sample_rate: int) -> None:
        self._play(audio, sample_rate, 1.0, None)

    def play_at(self, audio, sample_rate, *, made_at, tempo) -> float:
        """`play()`, holding the rest of the segment to `tempo` as it changes.

        Checked once a chunk, so a speed change is heard within one chunk
        (about 85 ms) rather than at the next sentence. What is left is
        stretched by the ratio between the speed it currently represents and
        the one wanted now, and never re-stretched from the original: the
        played part is gone, and the rest is all that can still change.
        """
        return self._play(audio, sample_rate, made_at, tempo) / sample_rate
```

The `_play` loop becomes position-based:

```python
        self.interrupted = False
        buffer, effective, pos, written = audio, made_at, 0, 0
        while pos < len(buffer):
            while not self._resume.wait(timeout=0.05):
                if self._interrupt.is_set():
                    break
            if self._interrupt.is_set():
                self._interrupt.clear()
                self.interrupted = True
                return written
            if tempo is not None:
                wanted = tempo.value
                if abs(wanted / effective - 1.0) > 1e-3:
                    rest = time_stretch(buffer[pos:], wanted / effective, sample_rate)
                    buffer = np.concatenate([buffer[:pos], rest])
                    effective = wanted
                    if pos >= len(buffer):
                        break
            self._sink.start()
            block = buffer[pos : pos + self._chunk]
            self._sink.write(block)
            self.frames_played += len(block)
            written += len(block)
            pos += len(block)
        if self._interrupt.is_set():
            self._interrupt.clear()
            self.interrupted = True
        return written
```

Keep the existing docstring/comments of `play` on `_play`.

- [ ] **Step 4: Run** — `uv run pytest tests/test_streaming_player.py tests/test_player.py -q` → pass.
- [ ] **Step 5: Commit** — "Let the player follow a speed change inside a sentence"

---

### Task 4: Pipeline — given units, a cache, and the tempo

**Files:**
- Modify: `src/speakd/model.py` (`Segment.index: int = 0`), `src/speakd/pipeline.py`
- Test: `tests/test_pipeline.py` (append)

**Interfaces:**
- Consumes: `Tempo`, `Stretchable`.
- Produces: `class AudioCache(max_bytes: int = 64 * 1024 * 1024)` with `get(index) -> tuple[np.ndarray, float] | None`, `put(index, audio, made_at) -> None`, `near: int` attribute (eviction centre). `speak(..., units: Sequence[Piece] | None = None, cache: AudioCache | None = None, tempo: Tempo | None = None)`. Each announced/recorded `Segment` has `index` = absolute unit index.

- [ ] **Step 1: Failing tests**

```python
from speakd.pipeline import AudioCache
from speakd.tempo import Tempo


class CountingEngine(FakeEngine):
    def __init__(self) -> None:
        super().__init__()
        self.calls: list[tuple[str, float]] = []

    def synthesize(self, text: str, voice: str, speed: float) -> np.ndarray:
        self.calls.append((text, speed))
        return super().synthesize(text, voice, speed)


def test_given_units_are_spoken_as_given() -> None:
    units = [Piece(span=Span(0, 4), spoken="One."), Piece(span=Span(5, 9), spoken="Two.")]
    player = RecordingPlayer()
    result = speak([], FakeEngine(), player, units=units)
    assert result.units == tuple(units)
    assert len(player.played) == 2


def test_segments_carry_their_absolute_index() -> None:
    seen: list[int] = []
    speak(
        [Piece(span=Span(0, 14), spoken="One. Two. Six.")],
        FakeEngine(),
        RecordingPlayer(),
        start_index=1,
        on_playing=lambda seg: seen.append(seg.index),
    )
    assert seen == [1, 2]


def test_a_cached_unit_is_not_synthesised_again() -> None:
    engine = CountingEngine()
    cache = AudioCache()
    pieces = [Piece(span=Span(0, 9), spoken="One. Two.")]
    speak(pieces, engine, RecordingPlayer(), cache=cache)
    speak(pieces, engine, RecordingPlayer(), cache=cache)
    assert len(engine.calls) == 2


def test_new_audio_is_made_at_the_tempo() -> None:
    engine = CountingEngine()
    speak([Piece(span=Span(0, 4), spoken="One.")], engine, RecordingPlayer(), speed=1.1, tempo=Tempo(1.5))
    assert engine.calls == [("One.", pytest.approx(1.65))]


def test_the_cache_evicts_furthest_from_where_playback_is() -> None:
    cache = AudioCache(max_bytes=3 * 400)
    for i in range(4):
        cache.near = i
        cache.put(i, np.zeros(100, np.float32), 1.0)
    assert cache.get(0) is None
    assert cache.get(3) is not None
```

(Imports at the top of the test file as already present there: `numpy as np`, `pytest`, `FakeEngine`, `RecordingPlayer`, `Piece`, `Span`, `speak`; add any missing.)

- [ ] **Step 2: Run** — fails.

- [ ] **Step 3: Implement.** `model.py` `Segment`: add last field `index: int = 0` with a docstring line ("the unit's position in its utterance's segmentation; what seek addresses"). In `pipeline.py`:

```python
class AudioCache:
    """Audio already made for this utterance, so going back costs nothing.

    Without it every ← re-synthesises, which at a real-time factor of 0.75 is
    four seconds of silence before a five-second sentence. Bounded, because an
    utterance is bounded by nothing: over the cap, what goes first is what is
    furthest from where playback is (`near`), which is the least likely to be
    asked for again.
    """

    def __init__(self, max_bytes: int = 64 * 1024 * 1024) -> None:
        self.max_bytes = max_bytes
        self.near = 0
        self._entries: dict[int, tuple[np.ndarray, float]] = {}
        self._bytes = 0
        self._lock = threading.Lock()

    def get(self, index: int) -> tuple[np.ndarray, float] | None:
        with self._lock:
            return self._entries.get(index)

    def put(self, index: int, audio: np.ndarray, made_at: float) -> None:
        with self._lock:
            old = self._entries.pop(index, None)
            if old is not None:
                self._bytes -= old[0].nbytes
            self._entries[index] = (audio, made_at)
            self._bytes += audio.nbytes
            while self._bytes > self.max_bytes and len(self._entries) > 1:
                victim = max(self._entries, key=lambda i: abs(i - self.near))
                self._bytes -= self._entries.pop(victim)[0].nbytes
```

`speak()` changes:
- new kwargs `units`, `cache`, `tempo` (document in the docstring).
- `units = list(units) if units is not None else segment(pieces, max_chars)`.
- `_Item = tuple[Piece, int, np.ndarray, float] | str | None`.
- producer: `for index, unit in enumerate(to_speak, start=max(0, start_index)):` — if `cache` has it use `(audio, made_at)`, else `made_at = tempo.value if tempo is not None else 1.0`, synthesise at `speed * made_at`, record the window, and `cache.put(index, audio, made_at)` when there is a cache. `work.put((unit, index, audio, made_at))`.
- consumer: unpack; build `played = Segment(..., index=index)`; `if cache is not None: cache.near = index`; announce; then

```python
                if tempo is not None and isinstance(player, Stretchable):
                    duration = player.play_at(audio, engine.sample_rate, made_at=made_at, tempo=tempo)
                    played = replace(played, duration=duration)
                else:
                    player.play(audio, engine.sample_rate)
```

(`from dataclasses import dataclass, field, replace`.)

- [ ] **Step 4: Run** — `uv run pytest tests/test_pipeline.py tests/test_model.py tests/test_timeline.py -q` → pass.
- [ ] **Step 5: Commit** — "Speak from given units, reuse made audio, and synthesise at the tempo"

---

### Task 5: Daemon — announce segments, index positions, set_speed

**Files:**
- Modify: `src/speakd/protocol.py` (`SET_SPEED = "set_speed"`), `src/speakd/daemon.py`
- Test: `tests/test_speed.py` (create), `tests/test_daemon.py` (append one test)

**Interfaces:**
- Produces: `Daemon.tempo: Tempo`; `started.data.segments: list[{index, text, span_start, span_end}]`; `position.data.index: int`; verb `set_speed {speed}` → `{speed}`; event `speed {speed}`; `status.data.speed`.

- [ ] **Step 1: Failing tests** — `tests/test_speed.py`:

```python
"""Tests for the listener's speed switch."""

from collections.abc import Sequence

import numpy as np
import pytest

from speakd import state
from speakd.channels import ChannelTable
from speakd.daemon import Daemon, ProfileView
from speakd.events import Event, EventBus
from speakd.model import Piece
from speakd.player import RecordingPlayer
from speakd.protocol import Request, Verb
from speakd.synth.fake import FakeEngine


def profile_for(name: str) -> ProfileView:
    return ProfileView(voice="af_heart", speed=1.1, interrupt_on=(), prepare=lambda p: (list(p), []))


class SpeedEngine(FakeEngine):
    def __init__(self) -> None:
        super().__init__()
        self.speeds: list[float] = []

    def synthesize(self, text: str, voice: str, speed: float) -> np.ndarray:
        self.speeds.append(speed)
        return super().synthesize(text, voice, speed)


def build(engine: FakeEngine | None = None) -> Daemon:
    return Daemon(engine or FakeEngine(), RecordingPlayer(), profile_for, bus=EventBus(), channels=ChannelTable())


def set_speed(d: Daemon, value: object):  # type: ignore[no-untyped-def]
    return d.handle(Request(verb=Verb.SET_SPEED, source_id="", payload={"speed": value}))


def test_set_speed_clamps_and_answers_what_it_applied() -> None:
    d = build()
    assert set_speed(d, 9).data == {"speed": 1.6}
    assert set_speed(d, 1.23).data == {"speed": 1.25}


@pytest.mark.parametrize("bad", [None, "fast", True, 0, -1])
def test_set_speed_refuses_what_is_not_a_positive_number(bad: object) -> None:
    assert not set_speed(build(), bad).ok


def test_speed_is_announced_reported_and_kept() -> None:
    d = build()
    seen: list[Event] = []
    d.bus.subscribe(seen.append)
    set_speed(d, 1.3)
    assert [e.data for e in seen if e.kind == "speed"] == [{"speed": 1.3}]
    assert d.handle(Request(verb=Verb.STATUS, source_id="")).data["speed"] == 1.3
    assert build().tempo.value == 1.3


def test_setting_speed_leaves_the_mute_flag_alone() -> None:
    d = build()
    d.handle(Request(verb=Verb.MUTE, source_id="", payload={"muted": True}))
    set_speed(d, 1.2)
    assert state.load().muted is True


def test_speech_is_synthesised_at_the_multiplied_speed() -> None:
    engine = SpeedEngine()
    d = build(engine)
    set_speed(d, 1.5)
    d.start()
    try:
        d.handle(Request(verb=Verb.ENQUEUE, source_id="s", payload={"text": "One.", "kind": "response"}))
        assert d.wait_idle(timeout=5.0)
    finally:
        d.stop()
    assert engine.speeds == [pytest.approx(1.65)]
```

Append to `tests/test_daemon.py`:

```python
def test_started_announces_every_segment_and_positions_say_which(daemon) -> None:  # type: ignore[no-untyped-def]
    d, _player, bus = daemon
    seen: list[Event] = []
    bus.subscribe(seen.append)
    d.handle(enqueue("s", "One. Two. Six."))
    assert d.wait_idle(timeout=5.0)
    started = next(e for e in seen if e.kind == "started")
    assert [s["text"] for s in started.data["segments"]] == ["One.", "Two.", "Six."]
    assert [s["index"] for s in started.data["segments"]] == [0, 1, 2]
    assert [e.data["index"] for e in seen if e.kind == "position"] == [0, 1, 2]
```

- [ ] **Step 2: Run** — fails.

- [ ] **Step 3: Implement.** `protocol.py`: add `SET_SPEED = "set_speed"` after `SET_ENGINE`. `daemon.py`:
- imports: `import math`; `from speakd.pipeline import AudioCache, speak`; `from speakd.segmenter import segment`; `from speakd.tempo import Tempo`.
- `__init__`: `self.tempo = Tempo(state.load().speed)` beside `self.muted`.
- `handle`: add

```python
        if request.verb is Verb.SET_SPEED:
            raw = request.payload.get("speed")
            if (
                not isinstance(raw, (int, float))
                or isinstance(raw, bool)
                or not math.isfinite(raw)
                or raw <= 0
            ):
                return Response(ok=False, error="set_speed needs a positive number 'speed'")
            applied = self.tempo.set(float(raw))
            state.save(replace(state.load(), speed=applied))
            self._publish("speed", "", {"speed": applied})
            return Response(ok=True, data={"speed": applied})
```

- `STATUS` data: add `"speed": self.tempo.value`.
- `_speak`: after `prepare`, `units = segment(pieces)`; `started` payload becomes `{"text": text, "segments": [{"index": i, "text": u.spoken, "span_start": u.span.start, "span_end": u.span.end} for i, u in enumerate(units)]}`; `position` payload adds `"index": segment.index`; `speak(...)` call passes `units=units, cache=AudioCache(), tempo=self.tempo`.

- [ ] **Step 4: Run** — `uv run pytest tests/test_speed.py tests/test_daemon.py -q` → pass.
- [ ] **Step 5: Commit** — "Announce every segment up front, and let the listener set the speed"

---

### Task 6: Daemon — seek

**Files:**
- Modify: `src/speakd/daemon.py`
- Test: `tests/test_seek.py` (create); `tests/test_daemon.py` (delete `test_seek_is_still_refused`)

**Interfaces:**
- Produces: verb `seek` with payload `{"index": int}` or `{"by": int}` → `{"index": k}` or `{"index": None, "ended": True}`; refused with "nothing is speaking" when idle.

- [ ] **Step 1: Failing tests** — `tests/test_seek.py`:

```python
"""Tests for moving playback within the utterance being spoken."""

import threading
import time

import numpy as np

from speakd.channels import ChannelTable
from speakd.daemon import Daemon, ProfileView
from speakd.events import Event, EventBus
from speakd.player import FakeSink, StreamingPlayer
from speakd.protocol import Request, Verb
from speakd.synth.fake import FakeEngine

TEXT = "Zero. One. Two. Three."


def profile_for(name: str) -> ProfileView:
    return ProfileView(voice="af_heart", speed=1.0, interrupt_on=(), prepare=lambda p: (list(p), []))


class SlowSink(FakeSink):
    """Takes real time per block, so there is an utterance in flight to seek."""

    def write(self, frames: np.ndarray) -> None:
        super().write(frames)
        time.sleep(len(frames) / 24000 / 20)  # twenty times real time


class CountingEngine(FakeEngine):
    def __init__(self) -> None:
        super().__init__()
        self.texts: list[str] = []

    def synthesize(self, text: str, voice: str, speed: float) -> np.ndarray:
        self.texts.append(text)
        return super().synthesize(text, voice, speed)


def until(predicate, timeout: float = 5.0) -> bool:  # type: ignore[no-untyped-def]
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return bool(predicate())


def build(engine: FakeEngine | None = None):  # type: ignore[no-untyped-def]
    d = Daemon(
        engine or FakeEngine(),
        StreamingPlayer(SlowSink(), chunk_frames=512),
        profile_for,
        bus=EventBus(),
        channels=ChannelTable(),
    )
    seen: list[Event] = []
    lock = threading.Lock()

    def record(event: Event) -> None:
        with lock:
            seen.append(event)

    d.bus.subscribe(record)
    d.start()
    return d, seen


def positions(seen: list[Event]) -> list[int]:
    return [e.data["index"] for e in list(seen) if e.kind == "position"]


def seek(d: Daemon, **payload: int):  # type: ignore[no-untyped-def]
    return d.handle(Request(verb=Verb.SEEK, source_id="", payload=payload))


def speak(d: Daemon) -> None:
    d.handle(Request(verb=Verb.ENQUEUE, source_id="s", payload={"text": TEXT, "kind": "response"}))


def test_seek_with_nothing_speaking_is_refused() -> None:
    d, _ = build()
    try:
        response = seek(d, by=1)
        assert not response.ok and "nothing is speaking" in response.error
    finally:
        d.stop()


def test_seek_needs_exactly_one_integer() -> None:
    d, _ = build()
    try:
        assert not seek(d).ok
        assert not d.handle(Request(verb=Verb.SEEK, source_id="", payload={"index": "2"})).ok
        assert not seek(d, index=1, by=1).ok
    finally:
        d.stop()


def test_seek_to_an_index_plays_from_there() -> None:
    d, seen = build()
    try:
        speak(d)
        assert until(lambda: positions(seen) == [0])
        assert seek(d, index=2).data == {"index": 2}
        assert d.wait_idle(timeout=10.0)
        assert positions(seen) == [0, 2, 3]
        assert [e.kind for e in seen].count("started") == 1
        assert [e.kind for e in seen].count("finished") == 1
    finally:
        d.stop()


def test_back_from_the_first_sentence_restarts_it() -> None:
    d, seen = build()
    try:
        speak(d)
        assert until(lambda: positions(seen) == [0])
        assert seek(d, by=-1).data == {"index": 0}
        assert d.wait_idle(timeout=10.0)
        assert positions(seen) == [0, 0, 1, 2, 3]
    finally:
        d.stop()


def test_forward_past_the_end_finishes_the_utterance() -> None:
    d, seen = build()
    try:
        speak(d)
        assert until(lambda: positions(seen) == [0])
        assert seek(d, index=9).data == {"index": None, "ended": True}
        assert d.wait_idle(timeout=10.0)
        finished = [e for e in seen if e.kind == "finished"]
        assert finished[-1].data["cancelled"] is True
    finally:
        d.stop()


def test_going_back_reuses_the_audio_already_made() -> None:
    engine = CountingEngine()
    d, seen = build(engine)
    try:
        speak(d)
        assert until(lambda: positions(seen)[-1:] == [1])
        seek(d, by=-1)
        assert d.wait_idle(timeout=10.0)
        assert engine.texts.count("Zero.") == 1
    finally:
        d.stop()


def test_the_sentence_sought_is_played_in_full() -> None:
    d, seen = build()
    try:
        speak(d)
        assert until(lambda: positions(seen) == [0])
        seek(d, index=3)
        assert d.wait_idle(timeout=10.0)
        # "Three." at 15 chars/s is 0.4 s of audio; the player must write it
        # all, not drop it to an interrupt left over from the seek.
        assert d.player.frames_played >= int(24000 * len("Three.") / 15)
        assert d.player.interrupted is False
    finally:
        d.stop()


def test_a_hush_after_a_seek_wins() -> None:
    d, seen = build()
    try:
        speak(d)
        assert until(lambda: positions(seen) == [0])
        seek(d, index=2)
        d.handle(Request(verb=Verb.HUSH, source_id=""))
        assert d.wait_idle(timeout=10.0)
        assert 3 not in positions(seen)
    finally:
        d.stop()
```

Delete `test_seek_is_still_refused` from `tests/test_daemon.py`.

- [ ] **Step 2: Run** — `uv run pytest tests/test_seek.py -q` → fails.

- [ ] **Step 3: Implement** in `daemon.py`:
- Delete `_SEEK_NOT_IMPLEMENTED` and its comment.
- Add after `_Job`:

```python
@dataclass
class _Current:
    """The utterance in flight, as seek needs to see it.

    `index` is the segment last announced, so a relative seek is relative to
    what a listener was shown rather than to what the producer has reached.
    `seek_to` is a request `_speak` picks up once `speak()` returns; a hush
    clears it, so a hush racing a seek wins.
    """

    units: tuple[Piece, ...]
    index: int = 0
    seek_to: int | None = None
```

- `__init__`: `self._current: _Current | None = None`.
- `handle`: `if request.verb is Verb.SEEK: return self._seek(request.payload)`.
- new method:

```python
    def _seek(self, payload: dict[str, object]) -> Response:
        """Move playback to another segment of the utterance being spoken.

        By re-entering `speak()` at that segment: the audio of segments
        already played is in the utterance's cache, so going back is
        immediate, and anything ahead is synthesised as it would have been.
        """
        given = [key for key in ("index", "by") if key in payload]
        value = payload.get(given[0]) if len(given) == 1 else None
        if not isinstance(value, int) or isinstance(value, bool):
            return Response(ok=False, error="seek needs one integer, 'index' or 'by'")
        with self._idle:
            current = self._current
            if current is None:
                return Response(ok=False, error="nothing is speaking")
            target = max(0, value if given[0] == "index" else current.index + value)
            ended = target >= len(current.units)
            current.seek_to = None if ended else target
            self._cancel.set()
        self._stop_player()
        if ended:
            return Response(ok=True, data={"index": None, "ended": True})
        return Response(ok=True, data={"index": target})
```

- `_stop_speaking`: inside the `if sounding:` under the lock, add `if self._current is not None: self._current.seek_to = None`.
- `_speak`: after `units = segment(pieces)`, `current = _Current(units=tuple(units))`, install `self._current = current` under `_idle`. In `announce`, `with self._idle: current.index = segment.index` before publishing. Replace the single `speak(...)` call with a loop:

```python
        cache = AudioCache()
        start = 0
        try:
            while True:
                try:
                    result = speak(
                        pieces, self.engine, self.player,
                        voice=job.profile.voice, speed=job.profile.speed,
                        cancel=cancel, window=self._synthesis, timeline=timeline,
                        on_playing=announce, units=units, cache=cache,
                        tempo=self.tempo, start_index=start,
                    )
                except BaseException as exc:  # noqa: B036 - re-raising would drop `finished`
                    ...existing error/finished publishing...; return
                for message in result.errors:
                    self._publish("error", job.source_id, {"message": message})
                with self._idle:
                    target, current.seek_to = current.seek_to, None
                    if target is None or not self._running:
                        break
                    # A seek: same utterance, fresh Event and timeline, and no
                    # `finished`/`started` pair -- the next `position` is the
                    # whole of what a monitor needs to be told.
                    cancel = threading.Event()
                    timeline = Timeline()
                    self._cancel = cancel
                    self._timeline = timeline
                clear = getattr(self.player, "clear_interrupt", None)
                if callable(clear):
                    clear()
                start = target
        finally:
            with self._idle:
                if self._current is current:
                    self._current = None
        self._publish("finished", job.source_id, {"cancelled": result.cancelled, "aborted": result.aborted})
```

- `player.py` `StreamingPlayer`: add

```python
    def clear_interrupt(self) -> None:
        """Forget a stop that was aimed at playback which has already ended.

        A seek stops the player and then speaks again at once; the stop the
        pipeline sent on its way out would otherwise cut off the first chunk
        of the segment that was sought.
        """
        self._interrupt.clear()
```

- [ ] **Step 4: Run** — `uv run pytest tests/test_seek.py tests/test_daemon.py tests/test_mute.py tests/test_queue.py -q` → pass.
- [ ] **Step 5: Commit** — "Seek within the utterance being spoken"

---

### Task 7: speakctl — speed and seek

**Files:**
- Modify: `src/speakd/cli.py`
- Test: `tests/test_cli.py` (append)

**Interfaces:**
- Produces: `speakctl speed [X]`, `speakctl seek (--by N | --index K)`.

- [ ] **Step 1: Failing tests** — find the existing pattern in `tests/test_cli.py` for verbs that talk to a daemon over a socket (a fake server or `_call` monkeypatch) and add:

```python
def test_speed_sends_set_speed(monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
    sent: list[Request] = []

    def fake_call(socket, request):  # type: ignore[no-untyped-def]
        sent.append(request)
        return Response(ok=True, data={"speed": 1.3})

    monkeypatch.setattr(cli, "_call", fake_call)
    assert cli.main(["speed", "1.3"]) == 0
    assert sent[0].verb is Verb.SET_SPEED and sent[0].payload == {"speed": 1.3}
    assert "1.3" in capsys.readouterr().out


def test_speed_with_no_value_prints_the_current_one(monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(cli, "_call", lambda s, r: Response(ok=True, data={"speed": 1.2}))
    assert cli.main(["speed"]) == 0
    assert capsys.readouterr().out.strip() == "1.2"


def test_seek_sends_by_or_index(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    sent: list[Request] = []
    monkeypatch.setattr(cli, "_call", lambda s, r: sent.append(r) or Response(ok=True, data={"index": 1}))
    assert cli.main(["seek", "--by", "-1"]) == 0
    assert cli.main(["seek", "--index", "3"]) == 0
    assert [r.payload for r in sent] == [{"by": -1}, {"index": 3}]
```

- [ ] **Step 2: Run** — fails.
- [ ] **Step 3: Implement** — parsers (beside pause/resume):

```python
    speed = sub.add_parser("speed", parents=[common], help="print or set the speaking speed")
    speed.add_argument("value", nargs="?", help="a multiplier, 0.7 to 1.6; omit to print it")
    seek = sub.add_parser("seek", parents=[common], help="move within what is being spoken")
    where = seek.add_mutually_exclusive_group(required=True)
    where.add_argument("--by", type=int, help="segments forward (negative: back)")
    where.add_argument("--index", type=int, help="the segment to go to, from 0")
```

Handlers:

```python
def _speed(args: argparse.Namespace) -> int:
    """Print the speed, or set it and print what the daemon applied.

    Printed either way, because the daemon clamps: asking for 3 and being
    told 1.6 is the answer, and a silent success would hide it.
    """
    if args.value is None:
        request = Request(verb=Verb.STATUS, source_id=args.source)
    else:
        try:
            value = float(args.value)
        except ValueError:
            print(f"speakctl: speed must be a number, got {args.value!r}", file=sys.stderr)
            return _UNREACHABLE
        request = Request(verb=Verb.SET_SPEED, source_id=args.source, payload={"speed": value})
    response = _call(args.socket, request)
    if response is None:
        return _UNREACHABLE
    if not response.ok:
        return _refused(response)
    print(response.data.get("speed"))
    return 0


def _seek(args: argparse.Namespace) -> int:
    payload = {"by": args.by} if args.by is not None else {"index": args.index}
    response = _call(args.socket, Request(verb=Verb.SEEK, source_id=args.source, payload=payload))
    if response is None:
        return _UNREACHABLE
    return 0 if response.ok else _refused(response)
```

`main`: route `"speed"` → `_speed(args)`, `"seek"` → `_seek(args)`.

- [ ] **Step 4: Run** — `uv run pytest tests/test_cli.py tests/test_cli_verbs.py -q` → pass.
- [ ] **Step 5: Commit** — "Give speakctl the speed and seek verbs"

---

### Task 8: Sources — the wire's index, a markdown fixture, seek and speed in the simulation, the shell allowlist

**Files:**
- Modify: `clients/gui/shared/speakd-source.js`, `clients/gui/app/src-tauri/src/bridge.rs`

**Interfaces:**
- Produces (to Task 9): `started.data = { text, segments: [{index, text, span_start, span_end}] }` from both sources; `position.data.index` from both; `speed` event `{ speed }`; `status.data.speed`; `send("seek", {index}|{by})`, `send("set_speed", {speed})`.

- [ ] **Step 1: `bridge.rs`** — add `"set_speed"` to `FORWARDED`, and rewrite the `seek` paragraph of its doc comment: seek is now implemented; `set_speed` is the listener's speed and, like pause, is global. `cd clients/gui/app/src-tauri && cargo build` → compiles.

- [ ] **Step 2: `ShellSource._normalise`** — for `position`, use the wire's index when present:

```js
    if (kind === "position") {
      // The daemon numbers segments itself now. Counting positions was a
      // guess that a seek breaks: after going back, the count and the
      // segment being spoken part company for the rest of the utterance.
      if (typeof data.index === "number") return { kind, source_id, data };
      ...existing counting fallback unchanged...
    }
```

Update the module doc comment for `started` (now carries `segments`), `position` (`index` from the daemon), `speed`, and `send` (seek implemented; `set_speed` forwarded).

- [ ] **Step 3: `SimulatedSource`** — replace the fixture with markdown:

```js
    // Markdown, because that is what the daemon is mostly handed, and a
    // simulation reading plain sentences could not show the renderer doing
    // anything. Spans are block-level, as the real markdown transform's are:
    // every sentence of a block carries the whole block's range.
    const blocks = [
      { src: "## Transcendental, at B25", spoken: [["Transcendental, at B25.", 1.9]] },
      {
        src:
          "Kant gives the official definition at **B25**. He calls transcendental all " +
          "cognition occupied not so much with objects as with our *mode of cognition* of " +
          "objects, insofar as this is to be possible a priori.",
        spoken: [
          ["Kant gives the official definition at B25.", 2.31],
          ["He calls transcendental all cognition occupied not so much with objects as with our mode of cognition of objects, insofar as this is to be possible a priori.", 8.42],
        ],
      },
      { src: "- The term is reflexive, or `second order`.", spoken: [["The term is reflexive, or second order.", 3.2]] },
      { src: "- It does not name a special class of objects.", spoken: [["It does not name a special class of objects.", 3.0]] },
      { src: "It names an inquiry that turns back on cognition itself.", spoken: [["It names an inquiry that turns back on cognition itself.", 3.02]] },
    ];
    this._text = blocks.map((b) => b.src).join("\n\n");
    this._segments = [];
    let cursor = 0;
    for (const block of blocks) {
      for (const [text, dur] of block.spoken) {
        this._segments.push({ text, dur, synth: dur * 0.72, span_start: cursor, span_end: cursor + block.src.length });
      }
      cursor += block.src.length + 2;
    }
```

(The two list items are separated by `\n\n` in this fixture; fine for the renderer, which treats each item line as its own block.) Then:
- `_emitSnapshot` and the replay `started` send `data: { text: this._text, segments: this._segments.map((s, i) => ({ index: i, text: s.text, span_start: s.span_start, span_end: s.span_end, duration: s.dur })) }`.
- `_positionEvent` already sends `index`.
- `this._speed = 1.0` in the constructor; `_status()` adds `speed: this._speed`; `send` gains `case "set_speed": return this._doSetSpeed(payload);` implemented as:

```js
  _doSetSpeed(payload) {
    const raw = payload.speed;
    if (typeof raw !== "number" || !Number.isFinite(raw) || raw <= 0) {
      return Promise.resolve({ ok: false, error: "set_speed needs a positive number 'speed'" });
    }
    // The daemon's clamp and step, so the window sees the same answers here.
    this._speed = Math.round(Math.min(1.6, Math.max(0.7, raw)) * 20) / 20;
    this._emit({ kind: "speed", source_id: "", data: { speed: this._speed } });
    return Promise.resolve({ ok: true, data: { speed: this._speed } });
  }
```

- `_tick`: `this._within += dt * this._speed;` (durations stay nominal; the clock runs faster).
- `_doSeek(payload)` becomes:

```js
  _doSeek(payload) {
    const hasIndex = Number.isInteger(payload.index);
    const hasBy = Number.isInteger(payload.by);
    if (hasIndex === hasBy) return Promise.resolve({ ok: false, error: "seek needs one integer, 'index' or 'by'" });
    if (this._utterance || this._hushed) return Promise.resolve({ ok: false, error: "nothing is speaking" });
    const target = Math.max(0, hasIndex ? payload.index : this._index + payload.by);
    if (target >= this._segments.length) {
      this._doCancel("");
      return Promise.resolve({ ok: true, data: { index: null, ended: true } });
    }
    this._index = target;
    this._within = 0;
    this._emit(this._positionEvent());
    return Promise.resolve({ ok: true, data: { index: target } });
  }
```

- [ ] **Step 4: Check** — `python3 -m http.server 8765 --directory clients/gui` and load `/pinned.html`; the console must be free of errors (full check comes with Task 9).
- [ ] **Step 5: Commit** — "Carry segment indices and speed through both sources"

---

### Task 9: The window — rendered markdown, moving highlight, seek and speed controls

**Files:**
- Create: `clients/gui/shared/markdown.js`
- Modify: `clients/gui/pinned.html`

**Interfaces:**
- Consumes: Task 8's events and verbs.
- Produces: `renderMarkdown(text: string) -> { root: HTMLElement, blocks: {start:number, end:number, el:HTMLElement}[] }`; `locate(el: HTMLElement, spoken: string, from: number) -> {start:number, end:number} | null`; `flatText(el) -> string`; `wrapRange(el, start, end, className) -> HTMLElement[]`; `unwrap(spans: HTMLElement[]) -> void`; `offsetAt(el, node, offset) -> number`.

- [ ] **Step 1: `markdown.js`** — block parser over lines with source offsets:
  - fence (```` ``` ```` / `~~~`) through closing fence → `pre > code` (text as-is)
  - `#{1,6} ` → `h1..h6` (styled small; one level scale)
  - `[-*+] ` → consecutive items into one `ul`, each `li` its own block; `\d+[.)] ` likewise `ol`
  - `> ` lines → `blockquote > p` block
  - rule (`---`, `***`, `___`) → `hr` (no block; nothing is spoken for it)
  - pipe-table rows → `table` with `td`s; header separator row skipped; the table is one block
  - blank line ends a paragraph; other consecutive lines → `p` block, lines joined with a space
  - inline, applied inside every block except code: earliest match of `` `code` `` → `code`; `[t](u)` → `span.link` with `title=u` (never an `a`: this webview must not navigate); `**t**` → `strong`; `*t*` / `_t_` → `em`; recursion into strong/em/link text.
  - All nodes built with `createElement`/`createTextNode`. Each block's `{start, end}` is the source range of its lines (end exclusive, before the newline).
  - `flatText(el)`: concatenation of descendant text nodes in document order (`TreeWalker(SHOW_TEXT)`).
  - `locate(el, spoken, from)`: normalise both strings to `[\p{L}\p{N}]` characters lower-cased, keeping an index map from normalised to flat positions; `indexOf(normSpoken, fromNorm)`; if not found, try the first 16 normalised chars for the start and the last 16 (searched after the start) for the end; return flat `{start, end}` or `null`. `from` is a flat offset; convert via the map.
  - `wrapRange(el, start, end, cls)`: walk text nodes, `splitText` at the boundaries, wrap each covered piece in `span.cls`, return spans. `unwrap(spans)`: replace each span with its text, then `parent.normalize()`.
  - `offsetAt(el, node, offset)`: flat offset of a caret position inside `el`.

- [ ] **Step 2: `pinned.html` markup** — replace the `.text-inner` three lines with `<div class="doc" id="doc"><p class="idle">nothing speaking</p></div>`; controls row becomes:

```html
  <div class="ctls">
    <button class="primary" id="play">Play</button>
    <button id="back" title="Previous sentence (←)">&larr;</button>
    <button id="fwd" title="Next sentence (→)">&rarr;</button>
    <div class="speed" role="group" aria-label="Speed">
      <button id="slower" title="Slower (−)">&minus;</button>
      <span id="speed" aria-live="polite">1.0×</span>
      <button id="faster" title="Faster (+)">+</button>
    </div>
    <button class="danger" id="hush">Hush</button>
  </div>
```

- [ ] **Step 3: CSS** — remove `.line*` rules; `.text` keeps its scroll; add document styles in the window's vocabulary: Newsreader 15px/1.5 for `p, li, blockquote`; headings IBM Plex Sans 12.5px 600 uppercase-free; `pre` IBM Plex Mono 11px on `--ground` with a rule border, `white-space: pre-wrap`; `code` inline mono 0.85em; `.link` underlined dotted; `table` 11px collapsed with `--rule` borders; blocks spaced 8px. `.lit { background: var(--lit-mark); box-shadow: 0 0 0 2px var(--lit-mark); border-radius: 2px; color: var(--ink); }`; `.spent { color: var(--spent); }` on blocks; paused: `.win[data-transport="paused"] .lit { background: none; box-shadow: none; text-decoration: underline dashed var(--lit-edge); text-decoration-thickness: 2px; text-underline-offset: 3px; }`; hushed: `.win[data-transport="hushed"] .lit { background: none; box-shadow: none; }`; `.doc p.idle` faint italic 13px; `.doc [data-seg]`-free — clicking uses caret position; `.doc { cursor: text }` and blocks `cursor: pointer`. `.speed` group: flex 1.3, three tight parts, the label mono 11px tabular-nums.

- [ ] **Step 4: Controller** in the module script:
  - state: `doc = null` (`{ blocks }`), `segs = []`, `ranges = new Map()` (index → `{ block, start, end }` where block is a blocks entry; start/end flat offsets, or `whole: true`), `litSpans = []`, `speed = null`, `userScrolledAt = 0`.
  - `started`: `buildDoc(data.text ?? "", data.segments ?? [])` — render via `renderMarkdown`, replace `#doc` children, compute ranges (for each segment: blocks overlapping `[span_start, span_end)` — first such block; `locate(block.el, seg.text, lastEndInThatBlock)`; null → `whole: true`), clear and rebuild ticks from all segments with `ensureSegment`. If `segments` is absent (older daemon), render the text and fall back to highlighting nothing but the scrubber.
  - `position`: `currentIndex = data.index`; `ensureSegment` as today; `highlight(currentIndex)`.
  - `highlight(i)`: `unwrap(litSpans)`; look up range; `litSpans = whole ? wrapRange(el, 0, flatText(el).length, "lit") : wrapRange(el, start, end, "lit")`; mark blocks whose `end <= segs[i].span_start` with `.spent`, others not; follow: if `Date.now() - userScrolledAt > 4000` scroll `litSpans[0]` into view `{ block: "center", behavior: reducedMotion ? "auto" : "smooth" }`.
  - `.text` `wheel`/`touchmove`/`keydown(PageUp/PageDown)` set `userScrolledAt = Date.now()`.
  - `finished`: `unwrap(litSpans)`; keep the document on screen; clear `.spent`.
  - `render()`: drop the prev/now/next writes; idle marker `win.dataset.idle` when no document; `back`/`fwd` disabled when `hushed || !segs.length || utterance === null`; speed label `speed == null ? "—" : speed.toFixed(1) + "×"` (two decimals when not a tenth); `slower`/`faster` disabled when `speed == null` or at the bounds (0.7 / 1.6).
  - `← / →` click → `source.send("seek", { by: ∓1 })`, error to `message`.
  - `seekTo(index)` → `source.send("seek", { index })`.
  - doc click → `caretRangeFromPoint` (or `caretPositionFromPoint`) → block containing the node → `offsetAt` → the segment whose range in that block contains it, else the block's first segment → `seekTo`.
  - speed: `slower`/`faster` send `set_speed` with `Math.round((speed ∓ 0.1) * 100) / 100`; on `ok` set `speed = res.data.speed`; `speed` event sets it; `refreshStatus` reads `data.speed`.
  - keyboard on `document` when `event.target` is not the textarea and no modifier: `ArrowLeft`/`ArrowRight` → back/fwd click; `-`/`_` → slower; `+`/`=` → faster; `" "` → play click; `preventDefault` for each handled key.

- [ ] **Step 5: Browser check** — `python3 -m http.server 8765 --directory clients/gui`, open `http://localhost:8765/pinned.html` with Playwright/chromium: screenshot; Play; confirm the highlight moves across the heading, paragraph (two sentences within one paragraph — sentence-level highlight), list items, last paragraph; click → jumps; − / + change label and pace; no console errors. Fix what is found.
- [ ] **Step 6: Commit** — "Show the whole utterance as it was written, and follow it as it is spoken"

---

### Task 10: Docs, full suite, the real thing

**Files:**
- Modify: `README.md` (Using it: `speakctl speed`, `speakctl seek`; The window: the follow-along text, controls, keys)

- [ ] **Step 1:** README edits.
- [ ] **Step 2:** `uv run pytest -q`, `uv run ruff check src tests`, `uv run ruff format --check src tests`, `uv run mypy src` → all clean.
- [ ] **Step 3:** Restart the daemon (`systemctl --user restart speakd`), wait for the socket, rebuild and launch the shell (`cargo run`), `speakctl enqueue` a markdown passage with a heading, bold, list; screenshot the window; press ←, →, +, − via `speakctl seek`/`speakctl speed` and via the window; watch `speakctl subscribe` for `position` indices and `speed` events.
- [ ] **Step 4: Commit** — "Document following along, seeking and speed"
