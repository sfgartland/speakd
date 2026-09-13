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
