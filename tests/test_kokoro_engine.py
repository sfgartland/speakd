"""Tests for the Kokoro adapter.

The adapter's own logic — None filtering, the float32 cast, the empty-chunk
and whitespace guards — runs against a stub pipeline, unconditionally. This
is the one module where a regression silently restores the founding bug, so
it must not depend on an extra that CI does not install.

The tests that need the real model stay gated behind the extra.
"""

import re
from collections.abc import Iterator
from typing import Any

import numpy as np
import pytest

from speakd.synth.kokoro_engine import (
    LEADING_SILENCE_SECONDS,
    NO_SPLIT,
    TRAILING_SILENCE_SECONDS,
    KokoroEngine,
)


class StubPipeline:
    """Stands in for kokoro.KPipeline: records its call, yields fixed chunks.

    KPipeline yields (graphemes, phonemes, audio) triples and can yield a
    None audio for a chunk it produced nothing for.
    """

    def __init__(self, chunks: list[Any]) -> None:
        self.chunks = chunks
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def __call__(self, text: str, **kwargs: Any) -> Iterator[tuple[str, str, Any]]:
        self.calls.append((text, dict(kwargs)))
        for chunk in self.chunks:
            yield ("gs", "ps", chunk)


def engine_with(chunks: list[Any]) -> tuple[KokoroEngine, StubPipeline]:
    """A KokoroEngine whose __init__ (and its torch-pulling import) is skipped."""
    engine = KokoroEngine.__new__(KokoroEngine)
    pipeline = StubPipeline(chunks)
    engine._pipeline = pipeline
    return engine, pipeline


def test_reports_its_sample_rate_without_the_extra() -> None:
    assert KokoroEngine.sample_rate == 24000
    assert KokoroEngine.name == "kokoro"


def test_none_chunks_are_filtered_out() -> None:
    engine, _ = engine_with([np.ones(3, dtype=np.float32), None, np.ones(2, dtype=np.float32)])
    audio = engine.synthesize("Hello there.", voice="af_heart", speed=1.1)
    assert len(audio) == 5
    assert audio.dtype == np.float32


def test_non_float32_chunks_are_cast() -> None:
    engine, _ = engine_with([np.ones(4, dtype=np.float64)])
    audio = engine.synthesize("Hello there.", voice="af_heart", speed=1.1)
    assert audio.dtype == np.float32
    assert audio.tolist() == [1.0, 1.0, 1.0, 1.0]


def test_a_pipeline_yielding_nothing_gives_empty_audio() -> None:
    engine, _ = engine_with([])
    audio = engine.synthesize("Hello there.", voice="af_heart", speed=1.1)
    assert len(audio) == 0
    assert audio.dtype == np.float32


def test_a_pipeline_yielding_only_none_gives_empty_audio() -> None:
    engine, _ = engine_with([None, None])
    audio = engine.synthesize("Hello there.", voice="af_heart", speed=1.1)
    assert len(audio) == 0
    assert audio.dtype == np.float32


def test_empty_text_never_reaches_the_pipeline() -> None:
    engine, pipeline = engine_with([np.ones(3, dtype=np.float32)])
    assert len(engine.synthesize("", voice="af_heart", speed=1.1)) == 0
    assert pipeline.calls == []


def test_whitespace_only_text_never_reaches_the_pipeline() -> None:
    engine, pipeline = engine_with([np.ones(3, dtype=np.float32)])
    assert len(engine.synthesize("  \n\t ", voice="af_heart", speed=1.1)) == 0
    assert pipeline.calls == []


def test_split_pattern_is_pinned_so_the_engine_cannot_resplit() -> None:
    # A unit can contain a newline — the segmenter needs [.!?] before
    # whitespace to break a sentence — so the default newline split_pattern
    # would re-split inside a unit we deliberately kept whole.
    engine, pipeline = engine_with([np.ones(3, dtype=np.float32)])
    unit = "A heading\nand a body sentence."
    engine.synthesize(unit, voice="af_heart", speed=1.1)

    text, kwargs = pipeline.calls[0]
    assert text == unit
    assert kwargs["split_pattern"] == NO_SPLIT
    assert re.split(NO_SPLIT, unit) == [unit], "the pinned pattern must never match"


def test_voice_and_speed_are_passed_through() -> None:
    engine, pipeline = engine_with([np.ones(3, dtype=np.float32)])
    engine.synthesize("Hello there.", voice="af_bella", speed=0.9)
    _, kwargs = pipeline.calls[0]
    assert kwargs["voice"] == "af_bella"
    assert kwargs["speed"] == 0.9


def padded_chunk(lead: float, speech: int, trail: float) -> np.ndarray:
    """A chunk shaped like Kokoro's: speech between two pads of dither."""
    rate = KokoroEngine.sample_rate
    audio = np.full(int(lead * rate) + speech + int(trail * rate), 1e-6, dtype=np.float32)
    audio[int(lead * rate) : int(lead * rate) + speech] = 0.5
    return audio


def test_the_padding_is_trimmed_off_what_the_engine_returns() -> None:
    kept_lead = round(LEADING_SILENCE_SECONDS * KokoroEngine.sample_rate)
    kept_trail = round(TRAILING_SILENCE_SECONDS * KokoroEngine.sample_rate)
    engine, _ = engine_with([padded_chunk(lead=0.28, speech=2400, trail=0.47)])
    audio = engine.synthesize("Hello there.", voice="af_heart", speed=1.1)
    assert len(audio) == kept_lead + 2400 + kept_trail
    assert audio.dtype == np.float32


def test_trimming_spans_the_join_between_chunks() -> None:
    # The pad is on the outside of the whole utterance, so it has to be found
    # after the chunks are concatenated -- not chunk by chunk, which would
    # cut a hole at every seam.
    kept_lead = round(LEADING_SILENCE_SECONDS * KokoroEngine.sample_rate)
    kept_trail = round(TRAILING_SILENCE_SECONDS * KokoroEngine.sample_rate)
    first = padded_chunk(lead=0.3, speech=1200, trail=0.0)
    second = padded_chunk(lead=0.0, speech=1200, trail=0.4)
    engine, _ = engine_with([first, second])
    audio = engine.synthesize("Hello there.", voice="af_heart", speed=1.1)
    assert len(audio) == kept_lead + 2400 + kept_trail
    # Every sample between the residuals is speech: no hole at the seam.
    assert float(audio[kept_lead:-kept_trail].min()) == 0.5


def test_a_silent_result_keeps_its_length_rather_than_becoming_empty() -> None:
    # Emptying it would collide with the contract's meaning for zero-length
    # audio, which the pipeline and player both read as "nothing to say".
    engine, _ = engine_with([np.full(4800, 1e-6, dtype=np.float32)])
    audio = engine.synthesize("Hello there.", voice="af_heart", speed=1.1)
    assert len(audio) == 4800


@pytest.fixture(scope="module")
def engine() -> KokoroEngine:
    """The real engine. Skips the tests below unless the extra is installed."""
    pytest.importorskip("kokoro", reason="requires the 'kokoro' extra")
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


def test_the_real_engine_returns_no_more_padding_than_the_residuals(
    engine: KokoroEngine,
) -> None:
    from speakd.synth.kokoro_engine import SILENCE_THRESHOLD

    audio = engine.synthesize("Yes.", voice="af_heart", speed=1.1)
    loud = np.flatnonzero(np.abs(audio) > SILENCE_THRESHOLD)
    lead = int(loud[0]) / engine.sample_rate
    trail = (len(audio) - 1 - int(loud[-1])) / engine.sample_rate
    assert lead == pytest.approx(LEADING_SILENCE_SECONDS, abs=1e-6)
    assert trail == pytest.approx(TRAILING_SILENCE_SECONDS, abs=1e-6)
