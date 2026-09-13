"""Kokoro adapter.

Kokoro's KPipeline splits text by its own `split_pattern`, which defaults to
newlines. Callers that flatten text therefore get one enormous segment and no
streaming at all. We hand it one already-segmented unit at a time, so its
splitting never applies and the pipeline keeps control of latency.
"""

from __future__ import annotations

from typing import Any

import numpy as np


class KokoroEngine:
    name = "kokoro"
    sample_rate = 24000

    def __init__(self, repo_id: str = "hexgrad/Kokoro-82M", lang_code: str = "a") -> None:
        import kokoro

        self._pipeline: Any = kokoro.KPipeline(lang_code=lang_code, repo_id=repo_id)

    def synthesize(self, text: str, voice: str, speed: float) -> np.ndarray:
        if not text.strip():
            return np.zeros(0, dtype=np.float32)
        chunks = [
            audio
            for _, _, audio in self._pipeline(text, voice=voice, speed=speed)
            if audio is not None
        ]
        if not chunks:
            return np.zeros(0, dtype=np.float32)
        return np.concatenate([np.asarray(c, dtype=np.float32) for c in chunks])
