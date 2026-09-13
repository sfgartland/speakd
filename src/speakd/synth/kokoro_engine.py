"""Kokoro adapter.

Kokoro's KPipeline splits text by its own `split_pattern`, which defaults to
newlines. Callers that flatten text therefore get one enormous segment and no
streaming at all. We hand it one already-segmented unit at a time and pin
`split_pattern` to a pattern that cannot match, so engine-side splitting is
mechanically off and the pipeline keeps sole control of latency.

Pinning it matters because a unit can contain a newline: the segmenter needs
`[.!?]` before whitespace to break a sentence, so a bare heading followed by
a newline and a body sentence stays one unit. Left at its default, Kokoro
would re-split exactly there — putting engine-side splitting back on the seam
this project closed. Milestone 3's markdown transform, which emits headings
and list items, would hit this constantly.
"""

from __future__ import annotations

from typing import Any

import numpy as np

# An empty negative lookahead: zero-width, and fails at every position, so
# `re.split` with it never finds a boundary. Kokoro therefore treats whatever
# we hand it as exactly one unit, newlines included.
NO_SPLIT = r"(?!)"


class KokoroEngine:
    name = "kokoro"
    sample_rate = 24000

    def __init__(self, repo_id: str = "hexgrad/Kokoro-82M", lang_code: str = "a") -> None:
        import kokoro

        self._pipeline: Any = kokoro.KPipeline(lang_code=lang_code, repo_id=repo_id)

    def synthesize(self, text: str, voice: str, speed: float) -> np.ndarray:
        if not text.strip():
            return np.zeros(0, dtype=np.float32)
        results = self._pipeline(text, voice=voice, speed=speed, split_pattern=NO_SPLIT)
        chunks = [audio for _, _, audio in results if audio is not None]
        if not chunks:
            return np.zeros(0, dtype=np.float32)
        return np.concatenate([np.asarray(c, dtype=np.float32) for c in chunks])
