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
