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
