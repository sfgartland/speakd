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
