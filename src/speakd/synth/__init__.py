"""Synthesiser tier.

Engines synthesise one already-segmented unit at a time. Segmentation and
timing belong to the pipeline, so that adding another engine is an adapter
rather than a rewrite of how streaming works.
"""

from __future__ import annotations

from typing import Protocol

import numpy as np


class UnsupportedLanguage(Exception):
    """Raised by `synthesize` when an engine cannot make a pipeline for `lang`.

    Not the same thing as a language absent from `supported_languages()` --
    that is the daemon's static check, made before speaking a word. This is
    for the case the design calls out separately (§2, and the plan's Review
    Focus): the *first* pipeline for a language is built lazily, on the
    synthesis thread, and that build can fail for a language the static
    check did not already rule out. Pipeline code raises this rather than
    whatever the underlying failure was, so callers have one thing to catch
    regardless of engine.
    """

    def __init__(self, lang: str) -> None:
        super().__init__(lang)
        self.lang = lang


class Synthesizer(Protocol):
    """Turns one already-segmented unit of text into audio.

    Engines do not split text. Segmentation happens before them so the
    pipeline controls how soon the first sound can play.
    """

    name: str
    sample_rate: int
    # Whether the engine already says years as people do (Kokoro does) or
    # reads them digit by digit and needs the text rule in
    # `speakd.synth.years` first (Piper does not). Declared per engine; the
    # daemon itself never reads it.
    reads_years: bool

    def synthesize(self, text: str, voice: str, speed: float, lang: str = "en") -> np.ndarray: ...
