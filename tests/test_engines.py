"""Tests for choosing which engine speaks an utterance (design §2).

`choose_engine` is pure: the voice-presence check is injected, so every row
runs with no engine, no model and no audio device. The table walks the
spec's first-match-wins list, including what happens when Kokoro is needed
but unloaded -- `Declined`, never an implicit load.
"""

from __future__ import annotations

import pytest

from speakd.engines import Declined, EngineChoice, choose_engine
from speakd.languages import normalise

KOKORO = EngineChoice("kokoro", None)


@pytest.mark.parametrize(
    (
        "lang",
        "engine_setting",
        "piper_voices",
        "has_voice",
        "piper_ok",
        "kokoro_loaded",
        "expected",
    ),
    [
        pytest.param(
            "en",
            "kokoro",
            {"en": "en_US-ryan-medium"},
            True,
            True,
            True,
            KOKORO,
            id="kokoro-setting-wins-even-with-piper-ready",
        ),
        pytest.param(
            "en",
            "piper",
            {"en": "en_US-ryan-medium"},
            True,
            True,
            True,
            EngineChoice("piper", "en_US-ryan-medium"),
            id="piper-setting-voice-mapped-and-present",
        ),
        pytest.param(
            "en",
            "piper",
            {"en": "en_US-ryan-medium"},
            False,
            True,
            True,
            KOKORO,
            id="piper-setting-voice-mapped-but-missing",
        ),
        pytest.param(
            "fr",
            "piper",
            {"en": "en_US-ryan-medium"},
            True,
            True,
            True,
            KOKORO,
            id="piper-setting-language-unmapped",
        ),
        pytest.param(
            "en",
            "piper",
            {"en": "en_US-ryan-medium"},
            True,
            False,
            True,
            KOKORO,
            id="piper-setting-but-piper-not-ok",
        ),
        pytest.param(
            "en",
            "kokoro",
            {},
            False,
            True,
            False,
            Declined("no voice for en"),
            id="kokoro-setting-but-kokoro-unloaded",
        ),
        pytest.param(
            "en",
            "piper",
            {"en": "en_US-ryan-medium"},
            False,
            True,
            False,
            Declined("no voice for en"),
            id="piper-falls-back-but-kokoro-unloaded",
        ),
        pytest.param(
            "en",
            "piper",
            {"en": "en_US-ryan-medium"},
            True,
            True,
            False,
            EngineChoice("piper", "en_US-ryan-medium"),
            id="piper-chosen-kokoro-not-needed",
        ),
    ],
)
def test_choose_engine_table(
    lang: str,
    engine_setting: str,
    piper_voices: dict[str, str],
    has_voice: bool,
    piper_ok: bool,
    kokoro_loaded: bool,
    expected: EngineChoice | Declined,
) -> None:
    assert (
        choose_engine(
            lang,
            engine_setting,
            piper_voices,
            lambda voice: has_voice,
            piper_ok,
            kokoro_loaded,
        )
        == expected
    )


def test_the_voice_presence_check_receives_the_mapped_voice() -> None:
    asked: list[str] = []

    def piper_has_voice(voice: str) -> bool:
        asked.append(voice)
        return True

    result = choose_engine(
        "de", "piper", {"de": "de_DE-thorsten-medium"}, piper_has_voice, True, True
    )
    assert result == EngineChoice("piper", "de_DE-thorsten-medium")
    assert asked == ["de_DE-thorsten-medium"]


def test_de_normalises_to_de() -> None:
    assert normalise("de") == "de"
    assert normalise("Deutsch") == "de"
