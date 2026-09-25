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

Kokoro also pads what it returns with near-silence at both ends -- measured
at about 0.21 s before the speech and 0.18 s after it -- and the pipeline
segments per sentence, so that padding is paid once per sentence rather than
once per utterance. A five-sentence utterance carried close to two seconds of
it. We cut it back to a residual as the audio leaves the engine. That trim
lives in `speakd.synth.audio`, shared with Piper and re-exported here.
"""

from __future__ import annotations

import os
import sys
import threading
from typing import Any

import numpy as np

from speakd import languages
from speakd.synth import UnsupportedLanguage
from speakd.synth.audio import (
    LEADING_SILENCE_SECONDS,
    SILENCE_THRESHOLD,
    TRAILING_SILENCE_SECONDS,
    trim_silence,
)

# The trim and its constants were moved to `speakd.synth.audio` so Piper can
# import them without this module; they stay importable from here for the
# callers that already do.
__all__ = [
    "KokoroEngine",
    "LEADING_SILENCE_SECONDS",
    "SILENCE_THRESHOLD",
    "TRAILING_SILENCE_SECONDS",
    "trim_silence",
]

# An empty negative lookahead: zero-width, and fails at every position, so
# `re.split` with it never finds a boundary. Kokoro therefore treats whatever
# we hand it as exactly one unit, newlines included.
NO_SPLIT = r"(?!)"

DEVICES = ("auto", "cpu", "cuda")


def _cuda_available() -> bool:
    import torch

    return bool(torch.cuda.is_available())


class KokoroEngine:
    """Kokoro, on the CPU or a GPU as `SPEAKD_DEVICE` says.

    `auto` (the default) uses CUDA when torch sees a GPU and the GPU actually
    starts, and the CPU otherwise. "Sees" is not "starts": the PyPI build of
    torch on Linux is a CUDA build, and on a laptop whose NVIDIA GPU is older
    than that build's cuDNN supports, `torch.cuda.is_available()` is true and
    the model still fails to initialise on it. Falling back keeps such a
    machine speaking instead of leaving the daemon with no voice. `cpu` skips
    the attempt -- the right local setting for a machine known to have such a
    GPU -- and `cuda` insists, and fails loudly, for anyone measuring it.
    """

    name = "kokoro"
    sample_rate = 24000
    reads_years = True

    def __init__(self, repo_id: str = "hexgrad/Kokoro-82M") -> None:
        import kokoro

        setting = os.environ.get("SPEAKD_DEVICE", "auto").strip().lower() or "auto"
        if setting not in DEVICES:
            raise ValueError(f"SPEAKD_DEVICE={setting!r}: expected cpu, cuda or auto")
        device = "cpu"
        if setting == "cuda" or (setting == "auto" and _cuda_available()):
            device = "cuda"
        try:
            model = kokoro.KModel(repo_id=repo_id).to(device).eval()
        except RuntimeError as exc:
            if setting != "auto" or device != "cuda":
                raise
            sys.stderr.write(
                f"speakd: the GPU would not start ({exc}); using the CPU. "
                "Set SPEAKD_DEVICE=cpu to skip trying.\n"
            )
            device = "cpu"
            model = kokoro.KModel(repo_id=repo_id).to(device).eval()
        self.repo_id = repo_id
        self.device = device
        self._model = model
        # One KPipeline per Kokoro lang code, built lazily on first use and
        # shared afterwards: a pipeline carries the G2P for its language,
        # which is the expensive, language-specific part, while `self._model`
        # -- the three gigabytes -- is the one thing every pipeline shares.
        # Keyed by Kokoro's own single-letter code so `_pipeline_for` never
        # asks the underlying package to redo `ALIASES` lookups it already
        # did once at each pipeline's construction.
        self._pipelines: dict[str, Any] = {}
        self._pipelines_lock = threading.Lock()

    def _pipeline_for(self, lang: str) -> Any:
        code = languages.KOKORO_CODES.get(lang)
        if code is None:
            # Not one of ours at all -- languages.normalise() would already
            # have caught this upstream, so reaching it here means a caller
            # bypassed normalisation. Still the right exception: the daemon's
            # unsupported-language handling is what catches it either way.
            raise UnsupportedLanguage(lang)
        with self._pipelines_lock:
            pipeline = self._pipelines.get(code)
            if pipeline is not None:
                return pipeline
            import kokoro

            try:
                pipeline = kokoro.KPipeline(
                    lang_code=code, repo_id=self.repo_id, model=self._model, device=self.device
                )
            except Exception as exc:
                # A missing G2P extra (misaki's ja/zh) is the case this
                # exists for, but any construction failure gets the same
                # treatment: the daemon has one thing to catch and one
                # setting (speech.unsupported_language) to act on, not a
                # zoo of engine-specific exceptions.
                raise UnsupportedLanguage(lang) from exc
            self._pipelines[code] = pipeline
            return pipeline

    def synthesize(self, text: str, voice: str, speed: float, lang: str = "en") -> np.ndarray:
        if not text.strip():
            return np.zeros(0, dtype=np.float32)
        pipeline = self._pipeline_for(lang)
        results = pipeline(text, voice=voice, speed=speed, split_pattern=NO_SPLIT)
        chunks = [audio for _, _, audio in results if audio is not None]
        if not chunks:
            return np.zeros(0, dtype=np.float32)
        audio = np.concatenate([np.asarray(c, dtype=np.float32) for c in chunks])
        return trim_silence(audio, self.sample_rate)

    def supported_languages(self) -> list[str]:
        return languages.supported_languages()
