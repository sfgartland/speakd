"""A deterministic engine for tests: no model, no audio device, no torch."""

from __future__ import annotations

import time
from collections.abc import Sequence

import numpy as np

from speakd import languages
from speakd.synth import UnsupportedLanguage


class FakeEngine:
    """Produces silence whose length is proportional to the text.

    `synthesis_cost` simulates a slow engine so tests can prove that
    synthesis and playback actually overlap. `supported` is the set this
    engine claims to speak, standing in for the real check
    `speakd.languages.supported_languages` makes against Kokoro's G2P
    imports; `raise_for` names languages whose `synthesize` raises
    `UnsupportedLanguage`, simulating the first pipeline for a language
    failing to build (design's Review Focus, plan Task 4).
    """

    name = "fake"
    reads_years = True

    def __init__(
        self,
        sample_rate: int = 24000,
        chars_per_second: float = 15.0,
        synthesis_cost: float = 0.0,
        supported: Sequence[str] = languages.SUPPORTED,
        raise_for: Sequence[str] = (),
    ) -> None:
        self.sample_rate = sample_rate
        self.chars_per_second = chars_per_second
        self.synthesis_cost = synthesis_cost
        self._supported = tuple(supported)
        self._raise_for = frozenset(raise_for)
        # Every lang this engine was asked to speak, in call order -- what a
        # test reads to prove the resolved language actually reached the
        # engine.
        self.synthesized_langs: list[str] = []

    def synthesize(self, text: str, voice: str, speed: float, lang: str = "en") -> np.ndarray:
        self.synthesized_langs.append(lang)
        if lang in self._raise_for:
            raise UnsupportedLanguage(lang)
        if self.synthesis_cost:
            time.sleep(self.synthesis_cost)
        if not text:
            return np.zeros(0, dtype=np.float32)
        seconds = len(text) / self.chars_per_second / speed
        return np.zeros(int(self.sample_rate * seconds), dtype=np.float32)

    def supported_languages(self) -> list[str]:
        return list(self._supported)
