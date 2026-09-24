"""Real-Kokoro tests for per-language pipelines (plan Task 6).

Skipped without the `kokoro` extra, like the real-engine section of
`test_kokoro_engine.py`. Not run against `misaki`'s ja/zh G2P -- those need
`pyopenjtalk` and `ordered_set` respectively, which the `kokoro` extra does
not pull in (see the plan's "Facts verified locally"), so this only proves a
second, non-English pipeline actually works and that supported_languages()
reports the real world accurately on a machine without them.
"""

from __future__ import annotations

import numpy as np
import pytest

from speakd.synth.kokoro_engine import KokoroEngine


@pytest.fixture(scope="module")
def engine() -> KokoroEngine:
    pytest.importorskip("kokoro", reason="requires the 'kokoro' extra")
    return KokoroEngine()


def test_a_second_language_produces_audible_audio(engine: KokoroEngine) -> None:
    audio = engine.synthesize(
        "Bonjour, comment allez-vous aujourd'hui ?", voice="ff_siwis", speed=1.1, lang="fr"
    )
    assert audio.dtype == np.float32
    assert len(audio) > 1000
    assert float(np.abs(audio).max()) > 0.0


def test_supported_languages_includes_french(engine: KokoroEngine) -> None:
    assert "fr" in engine.supported_languages()


def test_supported_languages_agrees_with_whether_the_ja_g2p_actually_imports(
    engine: KokoroEngine,
) -> None:
    # On a machine that installed only the `kokoro` extra (not misaki's own
    # ja/zh extras), pyopenjtalk is absent and "ja" must be reported as
    # unsupported -- this is the "Facts verified locally" case the plan
    # names. Phrased as an agreement rather than a hard-coded exclusion so it
    # stays true if a future machine adds pyopenjtalk.
    assert ("ja" in engine.supported_languages()) == _ja_g2p_available()


def _ja_g2p_available() -> bool:
    try:
        import misaki.ja  # noqa: F401
    except ImportError:
        return False
    return True
