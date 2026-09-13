# Speaking Pipeline Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn text into streaming speech that starts playing while later segments are still being synthesised, publishing a map of which source text is spoken when.

**Architecture:** Pieces are split into segments; a producer thread synthesises one segment ahead while the main thread plays the current one; each played segment is appended to a `Timeline` that maps source spans to playback time. The engine is dumb (one segment in, audio out) so the pipeline owns all timing and ordering, and tests run against a fake engine with no model and no audio device.

**Tech Stack:** Python 3.10–3.13, numpy, kokoro (optional extra), sounddevice (optional extra), pytest, ruff, mypy.

**Spec:** `docs/design/2026-09-13-speakd-design.md`

## Global Constraints

- Python `>=3.10,<3.14` — `misaki`, a kokoro dependency, does not support 3.14.
- Core dependencies stay minimal: numpy only. `kokoro`, `sounddevice` and `scipy` live behind the `kokoro` extra; `litellm` behind `llm`.
- No test may require torch or an audio device. Kokoro tests are skipped when the extra is absent.
- Imports of optional dependencies happen inside functions, never at module import time.
- Speech never disappears silently: a failing segment is recorded and skipped, never swallowed.
- ruff line length 100; mypy strict; `uv run pytest`, `uv run ruff check .` and `uv run ruff format --check .` must pass before every commit.
- Commit after every task.

---

### Task 1: Timeline

**Files:**
- Modify: `src/speakd/timeline.py`
- Test: `tests/test_timeline.py`

**Interfaces:**
- Consumes: `speakd.model.Segment`, `speakd.model.Span`
- Produces: `Timeline()` with `append(segment: Segment) -> None`, `span_at(audio_time: float) -> Span | None`, `time_of(offset: int) -> float | None`, property `duration: float`, `__len__`

- [ ] **Step 1: Write the failing test**

```python
"""Tests for the span-to-time map."""

import pytest

from speakd.model import Segment, Span
from speakd.timeline import Timeline


def seg(start: int, end: int, offset: float, duration: float) -> Segment:
    return Segment(span=Span(start, end), text="x", audio_offset=offset, duration=duration)


def test_empty_timeline_has_no_duration_and_no_spans() -> None:
    t = Timeline()
    assert len(t) == 0
    assert t.duration == 0.0
    assert t.span_at(0.0) is None
    assert t.time_of(0) is None


def test_span_at_returns_the_span_being_spoken() -> None:
    t = Timeline()
    t.append(seg(0, 5, 0.0, 1.0))
    t.append(seg(5, 12, 1.0, 2.0))
    assert t.span_at(0.5) == Span(0, 5)
    assert t.span_at(1.5) == Span(5, 12)


def test_boundary_belongs_to_the_later_segment() -> None:
    t = Timeline()
    t.append(seg(0, 5, 0.0, 1.0))
    t.append(seg(5, 12, 1.0, 2.0))
    assert t.span_at(1.0) == Span(5, 12)


def test_span_at_past_the_end_is_none() -> None:
    t = Timeline()
    t.append(seg(0, 5, 0.0, 1.0))
    assert t.span_at(1.0) is None
    assert t.span_at(99.0) is None


def test_time_of_returns_when_an_offset_is_spoken() -> None:
    t = Timeline()
    t.append(seg(0, 5, 0.0, 1.0))
    t.append(seg(5, 12, 1.0, 2.0))
    assert t.time_of(0) == 0.0
    assert t.time_of(4) == 0.0
    assert t.time_of(5) == 1.0


def test_time_of_unknown_offset_is_none() -> None:
    t = Timeline()
    t.append(seg(0, 5, 0.0, 1.0))
    assert t.time_of(50) is None


def test_duration_is_the_end_of_the_last_segment() -> None:
    t = Timeline()
    t.append(seg(0, 5, 0.0, 1.0))
    t.append(seg(5, 12, 1.0, 2.5))
    assert t.duration == pytest.approx(3.5)


def test_append_rejects_out_of_order_segments() -> None:
    t = Timeline()
    t.append(seg(0, 5, 0.0, 1.0))
    with pytest.raises(ValueError, match="playback order"):
        t.append(seg(5, 12, 0.5, 1.0))


def test_append_rejects_negative_duration() -> None:
    t = Timeline()
    with pytest.raises(ValueError, match="duration"):
        t.append(seg(0, 5, 0.0, -1.0))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_timeline.py -v`
Expected: FAIL with `NotImplementedError` from the existing stubs.

- [ ] **Step 3: Write minimal implementation**

Replace the body of `src/speakd/timeline.py` below its docstring:

```python
from __future__ import annotations

import bisect

from speakd.model import Segment, Span


class Timeline:
    """Bidirectional map between source spans and playback time."""

    def __init__(self) -> None:
        self._segments: list[Segment] = []
        self._starts: list[float] = []

    def __len__(self) -> int:
        return len(self._segments)

    @property
    def duration(self) -> float:
        if not self._segments:
            return 0.0
        last = self._segments[-1]
        return last.audio_offset + last.duration

    def append(self, segment: Segment) -> None:
        if segment.duration < 0:
            raise ValueError("segment duration must not be negative")
        if self._segments and segment.audio_offset < self.duration - 1e-9:
            raise ValueError("segments must be appended in playback order without overlap")
        self._segments.append(segment)
        self._starts.append(segment.audio_offset)

    def span_at(self, audio_time: float) -> Span | None:
        """Which source span is being spoken at `audio_time`."""
        index = bisect.bisect_right(self._starts, audio_time) - 1
        if index < 0:
            return None
        segment = self._segments[index]
        if audio_time < segment.audio_offset + segment.duration:
            return segment.span
        return None

    def time_of(self, offset: int) -> float | None:
        """When source `offset` is spoken."""
        for segment in self._segments:
            if segment.span.start <= offset < segment.span.end:
                return segment.audio_offset
        return None
```

- [ ] **Step 4: Run tests and linters**

Run: `uv run pytest -q && uv run ruff check . && uv run ruff format --check .`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/speakd/timeline.py tests/test_timeline.py
git commit -m "Implement Timeline, the span-to-time map"
```

---

### Task 2: Segmenter

**Files:**
- Create: `src/speakd/segmenter.py`
- Test: `tests/test_segmenter.py`

**Interfaces:**
- Consumes: `speakd.model.Piece`, `speakd.model.Span`
- Produces: `segment(pieces: Sequence[Piece], max_chars: int = DEFAULT_MAX_CHARS) -> list[Piece]`, constant `DEFAULT_MAX_CHARS = 180`

Splitting rule: sentence boundaries first; any unit longer than `max_chars` is split again at clause boundaries; anything still too long is split at word boundaries. Span rule: if a piece is unrewritten (`len(spoken) == span.end - span.start`) sub-spans are computed exactly; otherwise every sub-piece inherits the parent span, because offsets into rewritten text mean nothing in the source.

- [ ] **Step 1: Write the failing test**

```python
"""Tests for sentence and clause segmentation."""

from speakd.model import Piece, Span
from speakd.segmenter import DEFAULT_MAX_CHARS, segment


def piece(text: str, start: int = 0) -> Piece:
    return Piece(span=Span(start, start + len(text)), spoken=text)


def test_splits_on_sentence_boundaries() -> None:
    out = segment([piece("One. Two. Three.")])
    assert [p.spoken for p in out] == ["One.", "Two.", "Three."]


def test_unrewritten_pieces_get_exact_spans() -> None:
    source = "One. Two."
    out = segment([piece(source)])
    assert [source[p.span.start : p.span.end].strip() for p in out] == ["One.", "Two."]


def test_rewritten_pieces_inherit_the_parent_span() -> None:
    # spoken length differs from the span length, so offsets cannot be trusted
    parent = Piece(span=Span(0, 14), spoken="Stiegler, nineteen ninety-four. And more.")
    out = segment([parent])
    assert len(out) == 2
    assert all(p.span == Span(0, 14) for p in out)


def test_long_sentence_is_split_at_clause_boundaries() -> None:
    text = "alpha " * 20 + ", " + "beta " * 20 + "."
    out = segment([piece(text)], max_chars=60)
    assert len(out) > 1
    assert all(len(p.spoken) <= 60 for p in out)


def test_unbroken_run_is_split_at_word_boundaries() -> None:
    text = "word " * 100
    out = segment([piece(text)], max_chars=40)
    assert all(len(p.spoken) <= 40 for p in out)
    assert "".join(p.spoken.replace(" ", "") for p in out) == text.replace(" ", "")


def test_whitespace_only_input_produces_nothing() -> None:
    assert segment([piece("   \n  ")]) == []


def test_default_max_chars_is_exported() -> None:
    assert DEFAULT_MAX_CHARS == 180
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_segmenter.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'speakd.segmenter'`.

- [ ] **Step 3: Write minimal implementation**

Create `src/speakd/segmenter.py`:

```python
"""Splits pieces into units small enough to keep time-to-first-audio low.

Time-to-first-audio tracks the length of the first unit, so a single long
sentence would otherwise dominate it. Sentence boundaries come first because
they preserve prosody; clause and word splitting are fallbacks that only
apply when a unit is too long to wait for.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Sequence

from speakd.model import Piece, Span

DEFAULT_MAX_CHARS = 180

_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")
_CLAUSE_END = re.compile(r"(?<=[,;:])\s+")


def _split_keep_offsets(text: str, pattern: re.Pattern[str]) -> list[tuple[int, str]]:
    out: list[tuple[int, str]] = []
    pos = 0
    for match in pattern.finditer(text):
        chunk = text[pos : match.start()]
        if chunk.strip():
            out.append((pos, chunk))
        pos = match.end()
    tail = text[pos:]
    if tail.strip():
        out.append((pos, tail))
    return out


def _split_words(base: int, text: str, max_chars: int) -> Iterator[tuple[int, str]]:
    start = 0
    length = len(text)
    while start < length:
        end = min(start + max_chars, length)
        if end < length:
            cut = text.rfind(" ", start + 1, end + 1)
            if cut != -1:
                end = cut
        chunk = text[start:end]
        if chunk.strip():
            yield base + start, chunk
        start = end
        while start < length and text[start] == " ":
            start += 1


def _units(text: str, max_chars: int) -> Iterator[tuple[int, str]]:
    for offset, sentence in _split_keep_offsets(text, _SENTENCE_END):
        if len(sentence) <= max_chars:
            yield offset, sentence
            continue
        for clause_offset, clause in _split_keep_offsets(sentence, _CLAUSE_END):
            if len(clause) <= max_chars:
                yield offset + clause_offset, clause
            else:
                yield from _split_words(offset + clause_offset, clause, max_chars)


def segment(pieces: Sequence[Piece], max_chars: int = DEFAULT_MAX_CHARS) -> list[Piece]:
    """Split pieces into speakable units, preserving provenance where it is real."""
    result: list[Piece] = []
    for piece in pieces:
        exact = len(piece.spoken) == piece.span.end - piece.span.start
        for offset, chunk in _units(piece.spoken, max_chars):
            spoken = chunk.strip()
            if not spoken:
                continue
            if exact:
                span = Span(piece.span.start + offset, piece.span.start + offset + len(chunk))
            else:
                span = piece.span
            result.append(Piece(span=span, spoken=spoken))
    return result
```

- [ ] **Step 4: Run tests and linters**

Run: `uv run pytest -q && uv run ruff check . && uv run ruff format --check .`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/speakd/segmenter.py tests/test_segmenter.py
git commit -m "Add segmenter with sentence, clause and word fallbacks"
```

---

### Task 3: Synthesizer interface and fake engine

**Files:**
- Modify: `src/speakd/synth/__init__.py`
- Create: `src/speakd/synth/fake.py`
- Modify: `pyproject.toml` (add `numpy` to `dependencies`)
- Test: `tests/test_fake_engine.py`

**Interfaces:**
- Produces: `Synthesizer` protocol with attributes `name: str`, `sample_rate: int` and method `synthesize(text: str, voice: str, speed: float) -> np.ndarray`; `FakeEngine(sample_rate=24000, chars_per_second=15.0, synthesis_cost=0.0)`

The engine synthesises one already-segmented unit at a time. Keeping it this dumb is deliberate: the pipeline owns ordering and timing, and the original bug being fixed here was an engine splitting text by its own rules.

- [ ] **Step 1: Write the failing test**

```python
"""Tests for the deterministic test engine."""

import time

import numpy as np

from speakd.synth.fake import FakeEngine


def test_audio_length_scales_with_text_length() -> None:
    engine = FakeEngine(sample_rate=1000, chars_per_second=10.0)
    short = engine.synthesize("abcde", voice="v", speed=1.0)
    long = engine.synthesize("abcde" * 4, voice="v", speed=1.0)
    assert len(long) == 4 * len(short)


def test_speed_shortens_audio() -> None:
    engine = FakeEngine(sample_rate=1000, chars_per_second=10.0)
    normal = engine.synthesize("abcdefghij", voice="v", speed=1.0)
    fast = engine.synthesize("abcdefghij", voice="v", speed=2.0)
    assert len(fast) < len(normal)


def test_audio_is_float32() -> None:
    engine = FakeEngine()
    assert engine.synthesize("hello", voice="v", speed=1.0).dtype == np.float32


def test_synthesis_cost_is_spent() -> None:
    engine = FakeEngine(synthesis_cost=0.05)
    start = time.monotonic()
    engine.synthesize("hello", voice="v", speed=1.0)
    assert time.monotonic() - start >= 0.04


def test_empty_text_yields_empty_audio() -> None:
    assert len(FakeEngine().synthesize("", voice="v", speed=1.0)) == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_fake_engine.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'speakd.synth.fake'`.

- [ ] **Step 3: Write minimal implementation**

In `pyproject.toml`, change `dependencies = []` to:

```toml
dependencies = ["numpy>=1.24"]
```

Replace `src/speakd/synth/__init__.py` **entirely** — the existing docstring describes engines yielding audio incrementally, which this task replaces with one-unit-at-a-time synthesis:

```python
"""Synthesiser tier.

Engines synthesise one already-segmented unit at a time. Segmentation and
timing belong to the pipeline, so that adding another engine is an adapter
rather than a rewrite of how streaming works.
"""

from __future__ import annotations

from typing import Protocol

import numpy as np


class Synthesizer(Protocol):
    """Turns one already-segmented unit of text into audio.

    Engines do not split text. Segmentation happens before them so the
    pipeline controls how soon the first sound can play.
    """

    name: str
    sample_rate: int

    def synthesize(self, text: str, voice: str, speed: float) -> np.ndarray: ...
```

Create `src/speakd/synth/fake.py`:

```python
"""A deterministic engine for tests: no model, no audio device, no torch."""

from __future__ import annotations

import time

import numpy as np


class FakeEngine:
    """Produces silence whose length is proportional to the text.

    `synthesis_cost` simulates a slow engine so tests can prove that
    synthesis and playback actually overlap.
    """

    name = "fake"

    def __init__(
        self,
        sample_rate: int = 24000,
        chars_per_second: float = 15.0,
        synthesis_cost: float = 0.0,
    ) -> None:
        self.sample_rate = sample_rate
        self.chars_per_second = chars_per_second
        self.synthesis_cost = synthesis_cost

    def synthesize(self, text: str, voice: str, speed: float) -> np.ndarray:
        if self.synthesis_cost:
            time.sleep(self.synthesis_cost)
        if not text:
            return np.zeros(0, dtype=np.float32)
        seconds = len(text) / self.chars_per_second / speed
        return np.zeros(int(self.sample_rate * seconds), dtype=np.float32)
```

- [ ] **Step 4: Run tests and linters**

Run: `uv sync && uv run pytest -q && uv run ruff check . && uv run ruff format --check .`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml uv.lock src/speakd/synth/__init__.py src/speakd/synth/fake.py tests/test_fake_engine.py
git commit -m "Add synthesizer interface and fake engine"
```

---

### Task 4: Player

**Files:**
- Create: `src/speakd/player.py`
- Test: `tests/test_player.py`

**Interfaces:**
- Produces: `Player` protocol with `play(audio: np.ndarray, sample_rate: int) -> None` (blocking) and `stop() -> None`; `RecordingPlayer` with attributes `played: list[np.ndarray]`, `timestamps: list[float]`, `stopped: bool`; `SoundDevicePlayer`

`SoundDevicePlayer` imports sounddevice inside its methods so that importing `speakd.player` never requires the optional extra.

- [ ] **Step 1: Write the failing test**

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_player.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'speakd.player'`.

- [ ] **Step 3: Write minimal implementation**

Create `src/speakd/player.py`:

```python
"""Playback sinks.

`play` blocks until the audio has finished, which is what lets the pipeline
use playback as its clock while a producer thread runs ahead.
"""

from __future__ import annotations

import time
from typing import Protocol

import numpy as np


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
```

- [ ] **Step 4: Run tests and linters**

Run: `uv run pytest -q && uv run ruff check . && uv run ruff format --check .`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/speakd/player.py tests/test_player.py
git commit -m "Add player protocol with recording and sounddevice sinks"
```

---

### Task 5: Streaming pipeline

**Files:**
- Create: `src/speakd/pipeline.py`
- Test: `tests/test_pipeline.py`

**Interfaces:**
- Consumes: `speakd.segmenter.segment`, `speakd.timeline.Timeline`, `speakd.synth.Synthesizer`, `speakd.player.Player`, `speakd.model.Piece`, `speakd.model.Segment`
- Produces: `SpeechResult(timeline: Timeline, cancelled: bool, errors: list[str])`; `speak(pieces, engine, player, *, voice="af_heart", speed=1.1, cancel=None, max_chars=DEFAULT_MAX_CHARS) -> SpeechResult`

This is the task that delivers the plan's goal. A producer thread synthesises into a queue of depth one while the main thread plays, so segment N+1 is being made while segment N is heard. An engine that raises on one segment must not lose the rest.

- [ ] **Step 1: Write the failing test**

```python
"""Tests for the streaming pipeline."""

import threading
import time

import numpy as np

from speakd.model import Piece, Span
from speakd.pipeline import speak
from speakd.player import RecordingPlayer
from speakd.synth.fake import FakeEngine


def piece(text: str) -> Piece:
    return Piece(span=Span(0, len(text)), spoken=text)


class SleepingPlayer(RecordingPlayer):
    """A sink that takes real time, so overlap can be measured."""

    def __init__(self, cost: float) -> None:
        super().__init__()
        self.cost = cost

    def play(self, audio: np.ndarray, sample_rate: int) -> None:
        super().play(audio, sample_rate)
        time.sleep(self.cost)


class ExplodingEngine(FakeEngine):
    """Fails on one specific text and works for everything else."""

    def __init__(self, bad: str) -> None:
        super().__init__()
        self.bad = bad

    def synthesize(self, text: str, voice: str, speed: float) -> np.ndarray:
        if text == self.bad:
            raise RuntimeError("engine exploded")
        return super().synthesize(text, voice, speed)


def test_plays_every_segment_in_order() -> None:
    player = RecordingPlayer()
    result = speak([piece("One. Two. Three.")], FakeEngine(), player)
    assert len(player.played) == 3
    assert len(result.timeline) == 3
    assert result.cancelled is False
    assert result.errors == []


def test_timeline_offsets_accumulate() -> None:
    player = RecordingPlayer()
    result = speak(
        [piece("One. Two.")], FakeEngine(sample_rate=1000, chars_per_second=10.0), player
    )
    assert result.timeline.span_at(0.0) is not None
    assert result.timeline.duration > 0.0


def test_synthesis_overlaps_playback() -> None:
    # Four segments, each costing 0.05s to synthesise and 0.05s to play.
    # Serial would be ~0.40s; overlapped should be ~0.25s.
    player = SleepingPlayer(cost=0.05)
    engine = FakeEngine(synthesis_cost=0.05)
    start = time.monotonic()
    speak([piece("One. Two. Three. Four.")], engine, player)
    elapsed = time.monotonic() - start
    assert len(player.played) == 4
    assert elapsed < 0.35, f"no overlap: {elapsed:.2f}s"


def test_cancel_stops_early_and_stops_the_player() -> None:
    cancel = threading.Event()
    cancel.set()
    player = RecordingPlayer()
    result = speak([piece("One. Two. Three.")], FakeEngine(), player, cancel=cancel)
    assert result.cancelled is True
    assert player.played == []


def test_a_failing_segment_does_not_lose_the_others() -> None:
    player = RecordingPlayer()
    result = speak([piece("One. Two. Three.")], ExplodingEngine(bad="Two."), player)
    assert len(player.played) == 2
    assert len(result.errors) == 1
    assert "exploded" in result.errors[0]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_pipeline.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'speakd.pipeline'`.

- [ ] **Step 3: Write minimal implementation**

Create `src/speakd/pipeline.py`:

```python
"""Streaming synthesis: play segment N while segment N+1 is being made.

A queue of depth one is deliberate. Deeper buffering would synthesise far
ahead of playback, which wastes work when speech is cancelled — and speech
is cancelled often, because the user typing is a cancel.
"""

from __future__ import annotations

import queue
import threading
from collections.abc import Sequence
from dataclasses import dataclass, field

import numpy as np

from speakd.model import Piece, Segment
from speakd.player import Player
from speakd.segmenter import DEFAULT_MAX_CHARS, segment
from speakd.synth import Synthesizer
from speakd.timeline import Timeline


@dataclass
class SpeechResult:
    timeline: Timeline
    cancelled: bool = False
    errors: list[str] = field(default_factory=list)


_Item = tuple[Piece, np.ndarray] | str | None


def speak(
    pieces: Sequence[Piece],
    engine: Synthesizer,
    player: Player,
    *,
    voice: str = "af_heart",
    speed: float = 1.1,
    cancel: threading.Event | None = None,
    max_chars: int = DEFAULT_MAX_CHARS,
) -> SpeechResult:
    """Speak `pieces`, returning the timeline of what was actually played."""
    cancel = cancel or threading.Event()
    units = segment(pieces, max_chars)
    work: queue.Queue[_Item] = queue.Queue(maxsize=1)

    def produce() -> None:
        try:
            for unit in units:
                if cancel.is_set():
                    break
                try:
                    audio = engine.synthesize(unit.spoken, voice, speed)
                except Exception as exc:  # speech must not vanish on one bad segment
                    work.put(f"{unit.spoken[:40]!r}: {exc}")
                    continue
                work.put((unit, audio))
        finally:
            work.put(None)

    worker = threading.Thread(target=produce, daemon=True)
    worker.start()

    timeline = Timeline()
    errors: list[str] = []
    offset = 0.0
    while True:
        item = work.get()
        if item is None:
            break
        if isinstance(item, str):
            errors.append(item)
            continue
        if cancel.is_set():
            player.stop()
            break
        unit, audio = item
        duration = len(audio) / engine.sample_rate
        timeline.append(
            Segment(
                span=unit.span,
                text=unit.spoken,
                audio_offset=offset,
                duration=duration,
            )
        )
        player.play(audio, engine.sample_rate)
        offset += duration

    worker.join(timeout=1.0)
    return SpeechResult(timeline=timeline, cancelled=cancel.is_set(), errors=errors)
```

- [ ] **Step 4: Run tests and linters**

Run: `uv run pytest -q && uv run ruff check . && uv run ruff format --check .`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/speakd/pipeline.py tests/test_pipeline.py
git commit -m "Add streaming pipeline with one-segment prefetch"
```

---

### Task 6: Kokoro engine adapter

**Files:**
- Create: `src/speakd/synth/kokoro_engine.py`
- Test: `tests/test_kokoro_engine.py`

**Interfaces:**
- Consumes: `speakd.synth.Synthesizer`
- Produces: `KokoroEngine(repo_id="hexgrad/Kokoro-82M", lang_code="a")` with `name`, `sample_rate = 24000`, `synthesize(text, voice, speed) -> np.ndarray`

Because text arrives already segmented, each call synthesises one short unit and Kokoro's own `split_pattern` never matters. That is precisely the bug this project exists to fix: the default pattern splits on newlines, so a flattened response became one long segment and nothing could stream.

- [ ] **Step 1: Write the failing test**

```python
"""Tests for the Kokoro adapter. Skipped unless the extra is installed."""

import numpy as np
import pytest

kokoro = pytest.importorskip("kokoro", reason="requires the 'kokoro' extra")

from speakd.synth.kokoro_engine import KokoroEngine  # noqa: E402


@pytest.fixture(scope="module")
def engine() -> KokoroEngine:
    return KokoroEngine()


def test_reports_its_sample_rate(engine: KokoroEngine) -> None:
    assert engine.sample_rate == 24000
    assert engine.name == "kokoro"


def test_synthesizes_audible_audio(engine: KokoroEngine) -> None:
    audio = engine.synthesize("Hello there.", voice="af_heart", speed=1.1)
    assert audio.dtype == np.float32
    assert len(audio) > 1000
    assert float(np.abs(audio).max()) > 0.0


def test_empty_text_yields_empty_audio(engine: KokoroEngine) -> None:
    assert len(engine.synthesize("", voice="af_heart", speed=1.1)) == 0
```

- [ ] **Step 2: Run test to verify it is skipped or fails**

Run: `uv run pytest tests/test_kokoro_engine.py -v`
Expected without the extra: SKIPPED. With the extra installed: FAIL with `ModuleNotFoundError: No module named 'speakd.synth.kokoro_engine'`.

- [ ] **Step 3: Write minimal implementation**

Create `src/speakd/synth/kokoro_engine.py`:

```python
"""Kokoro adapter.

Kokoro's KPipeline splits text by its own `split_pattern`, which defaults to
newlines. Callers that flatten text therefore get one enormous segment and no
streaming at all. We hand it one already-segmented unit at a time, so its
splitting never applies and the pipeline keeps control of latency.
"""

from __future__ import annotations

from typing import Any

import numpy as np


class KokoroEngine:
    name = "kokoro"
    sample_rate = 24000

    def __init__(self, repo_id: str = "hexgrad/Kokoro-82M", lang_code: str = "a") -> None:
        import kokoro

        self._pipeline: Any = kokoro.KPipeline(lang_code=lang_code, repo_id=repo_id)

    def synthesize(self, text: str, voice: str, speed: float) -> np.ndarray:
        if not text.strip():
            return np.zeros(0, dtype=np.float32)
        chunks = [
            audio
            for _, _, audio in self._pipeline(text, voice=voice, speed=speed)
            if audio is not None
        ]
        if not chunks:
            return np.zeros(0, dtype=np.float32)
        return np.concatenate([np.asarray(c, dtype=np.float32) for c in chunks])
```

- [ ] **Step 4: Run tests and linters**

Run: `uv run pytest -q && uv run ruff check . && uv run ruff format --check .`
Expected: all pass (Kokoro tests skipped unless the extra is present).

- [ ] **Step 5: Commit**

```bash
git add src/speakd/synth/kokoro_engine.py tests/test_kokoro_engine.py
git commit -m "Add Kokoro engine adapter"
```

---

### Task 7: speakctl say

**Files:**
- Modify: `src/speakd/cli.py`
- Test: `tests/test_cli.py`

**Interfaces:**
- Consumes: `speakd.pipeline.speak`, `speakd.synth.fake.FakeEngine`, `speakd.synth.kokoro_engine.KokoroEngine`, `speakd.player.RecordingPlayer`, `speakd.player.SoundDevicePlayer`, `speakd.model.Piece`, `speakd.model.Span`
- Produces: `main(argv: Sequence[str] | None = None) -> int`

`--dry-run` swaps in the fake engine and a recording player and prints the timeline as JSON, so the whole path is testable without audio hardware or a model.

- [ ] **Step 1: Write the failing test**

```python
"""Tests for the speakctl command line."""

import json

from speakd.cli import main


def test_dry_run_prints_a_timeline(capsys) -> None:  # type: ignore[no-untyped-def]
    code = main(["say", "One. Two.", "--dry-run"])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert [s["text"] for s in payload["segments"]] == ["One.", "Two."]
    assert payload["segments"][0]["audio_offset"] == 0.0
    assert payload["duration"] > 0.0


def test_dry_run_reads_stdin_when_no_text_given(capsys, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    import io

    monkeypatch.setattr("sys.stdin", io.StringIO("Hello there."))
    assert main(["say", "--dry-run"]) == 0
    assert "Hello there." in capsys.readouterr().out


def test_empty_input_is_not_an_error(capsys) -> None:  # type: ignore[no-untyped-def]
    assert main(["say", "   ", "--dry-run"]) == 0
    assert json.loads(capsys.readouterr().out)["segments"] == []


def test_unknown_subcommand_exits_nonzero() -> None:
    assert main([]) == 2
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_cli.py -v`
Expected: FAIL with `NotImplementedError`.

- [ ] **Step 3: Write minimal implementation**

Replace `src/speakd/cli.py` entirely:

```python
"""speakctl — the lowest common denominator client.

Any agent that can run a shell command can drive speech through this.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence

from speakd.model import Piece, Span
from speakd.pipeline import speak
from speakd.segmenter import DEFAULT_MAX_CHARS


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="speakctl")
    sub = parser.add_subparsers(dest="command")

    say = sub.add_parser("say", help="speak text")
    say.add_argument("text", nargs="?", help="text to speak; reads stdin when omitted")
    say.add_argument("--voice", default="af_heart")
    say.add_argument("--speed", type=float, default=1.1)
    say.add_argument("--max-chars", type=int, default=DEFAULT_MAX_CHARS)
    say.add_argument(
        "--dry-run",
        action="store_true",
        help="use the fake engine and print the timeline instead of playing",
    )
    return parser


def _say(args: argparse.Namespace) -> int:
    text = args.text if args.text is not None else sys.stdin.read()

    if args.dry_run:
        from speakd.player import RecordingPlayer
        from speakd.synth.fake import FakeEngine

        engine: object = FakeEngine()
        player: object = RecordingPlayer()
    else:
        from speakd.player import SoundDevicePlayer
        from speakd.synth.kokoro_engine import KokoroEngine

        engine = KokoroEngine()
        player = SoundDevicePlayer()

    pieces = [Piece(span=Span(0, len(text)), spoken=text)]
    result = speak(
        pieces,
        engine,  # type: ignore[arg-type]
        player,  # type: ignore[arg-type]
        voice=args.voice,
        speed=args.speed,
        max_chars=args.max_chars,
    )

    if args.dry_run:
        print(
            json.dumps(
                {
                    "duration": result.timeline.duration,
                    "errors": result.errors,
                    "segments": [
                        {
                            "text": s.text,
                            "span": [s.span.start, s.span.end],
                            "audio_offset": s.audio_offset,
                            "duration": s.duration,
                        }
                        for s in result.timeline._segments
                    ],
                },
                indent=2,
            )
        )
    for message in result.errors:
        print(f"speakctl: {message}", file=sys.stderr)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.command == "say":
        return _say(args)
    parser.print_usage(sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
```

Reaching into `timeline._segments` is a wart. Add a public accessor to `Timeline` in the same commit:

```python
    @property
    def segments(self) -> tuple[Segment, ...]:
        return tuple(self._segments)
```

and use `result.timeline.segments` in the CLI.

- [ ] **Step 4: Run tests and linters**

Run: `uv run pytest -q && uv run ruff check . && uv run ruff format --check .`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/speakd/cli.py src/speakd/timeline.py tests/test_cli.py
git commit -m "Add speakctl say with a dry-run timeline"
```

---

### Task 8: Latency benchmark in CI

**Files:**
- Create: `tests/test_benchmark.py`
- Modify: `README.md` (record the measured numbers)

**Interfaces:**
- Consumes: `speakd.pipeline.speak`, `speakd.synth.fake.FakeEngine`, `speakd.player.RecordingPlayer`

The benchmark measures pipeline overhead with a stubbed engine, not hardware speed, so it is meaningful on any CI machine. It guards the property the project exists for: the first sound must not wait for the last segment.

- [ ] **Step 1: Write the failing test**

```python
"""Guards the property this project exists for: first audio comes early."""

import time

from speakd.model import Piece, Span
from speakd.pipeline import speak
from speakd.player import RecordingPlayer
from speakd.synth.fake import FakeEngine

PASSAGE = (
    "Kant gives the official definition at B25. "
    "The crucial thing is that the term is reflexive. "
    "It does not name a special class of objects. "
    "It names an inquiry that turns back on cognition itself. "
    "Hence the second distinction, the one Kant polices hardest."
)


def test_first_audio_does_not_wait_for_the_last_segment() -> None:
    player = RecordingPlayer()
    engine = FakeEngine(synthesis_cost=0.05)
    start = time.monotonic()
    result = speak([Piece(span=Span(0, len(PASSAGE)), spoken=PASSAGE)], engine, player)
    total = time.monotonic() - start

    assert len(result.timeline) == 5
    time_to_first_audio = player.timestamps[0] - start
    # One segment's synthesis, plus overhead — not five.
    assert time_to_first_audio < 0.12, f"first audio took {time_to_first_audio:.3f}s"
    assert time_to_first_audio < total / 2
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_benchmark.py -v`
Expected: FAIL — the file does not exist yet; after creating it, it must pass against the Task 5 implementation.

- [ ] **Step 3: Record the numbers in the README**

Replace the `## Status` section of `README.md` with:

```markdown
## Status

Early — the speaking pipeline works; the daemon and plugin host are next.

Measured on an i7-10510U with Kokoro on CPU (RTF 0.75), on a 417-character
passage: batching every segment before playing gave 20.2s to first audio.
Sentence segmentation with play-as-you-go gave 10.9s, with no playback
underruns. Time-to-first-audio tracks the length of the first segment, which
is what `--max-chars` bounds.
```

- [ ] **Step 4: Run the whole suite and linters**

Run: `uv run pytest -q && uv run ruff check . && uv run ruff format --check .`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add tests/test_benchmark.py README.md
git commit -m "Add latency benchmark guarding time-to-first-audio"
```

---

## Done when

- `uv run pytest` passes with the Kokoro tests skipped, and passes again with `uv sync --extra kokoro` with them running.
- `uv run speakctl say "One. Two. Three." --dry-run` prints three segments with increasing audio offsets.
- `uv run speakctl say "..."` speaks aloud through the system audio device.
- The benchmark proves first audio arrives before synthesis finishes.

## Not in this plan

The daemon, transports, channels, roles, profiles and control verbs (plan 2);
the plugin host, transforms, the vault pack and the Claude Code client (plan 3).
