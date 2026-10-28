"""kokoro-onnx adapter.

A second implementation of `Synthesizer`, kept beside the torch one so the two
can be measured against each other. It is not the default and nothing wires it
into the daemon.

Two things differ from `KokoroEngine` and neither is cosmetic.

The first is phonemisation. The torch path goes through `misaki`, which looks
words up in a dictionary and falls back to espeak-ng only for what it misses.
This path goes through espeak-ng for everything. `speakd.transforms.pronunciation`
was ported from `claude-code-narrator` against misaki's behaviour, so its table
is tuned for the other engine; abbreviations, symbols and acronyms are where the
two are most likely to diverge.

The second is that `Kokoro.create` inserts its own pauses at sentence and clause
boundaries and trims leading and trailing silence, neither of which the torch
path does. We keep those defaults rather than flattening them, because they are
what an adopter of this engine would actually get, and a comparison against a
tuned-down variant would not describe the real choice.

Unlike the torch adapter there is no `split_pattern` to pin. `create` chunks
internally only to stay under the model's phoneme limit and returns one
concatenated array, so engine-side chunking is not a streaming decision here and
cannot reopen the seam that pinning closes for Kokoro's KPipeline.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import numpy as np

MODEL_FILENAME = "kokoro-v1.0.onnx"
VOICES_FILENAME = "voices-v1.0.bin"

RELEASE_URL = "https://github.com/thewh1teagle/kokoro-onnx/releases/tag/model-files-v1.0"


def default_model_dir() -> Path:
    """Where the two model files live: `$XDG_CACHE_HOME/speakd/kokoro-onnx`.

    They are 353 MB together and are downloaded by hand from a GitHub release,
    so they belong in a cache directory rather than in the repository.
    """
    configured = os.environ.get("XDG_CACHE_HOME")
    root = Path(configured) if configured else Path.home() / ".cache"
    return root / "speakd" / "kokoro-onnx"


def resolve_model_files(model_dir: Path | None = None) -> tuple[Path, Path]:
    """Locate the model and voices files, or say in one line what to fetch.

    Split out from the engine so the failure path can be tested without the
    optional dependency installed.
    """
    directory = default_model_dir() if model_dir is None else model_dir
    model = directory / MODEL_FILENAME
    voices = directory / VOICES_FILENAME
    missing = [path.name for path in (model, voices) if not path.is_file()]
    if missing:
        raise FileNotFoundError(
            f"speakd: kokoro-onnx needs {' and '.join(missing)} in {directory} "
            f"-- download from {RELEASE_URL}"
        )
    return model, voices


class KokoroOnnxEngine:
    name = "kokoro-onnx"
    sample_rate = 24000

    def __init__(self, model_dir: Path | None = None, lang: str = "en-us") -> None:
        try:
            import kokoro_onnx
        except ModuleNotFoundError as exc:
            raise ModuleNotFoundError(
                "speakd: the kokoro-onnx extra is not installed "
                "(uv pip install 'speakd[kokoro-onnx]')"
            ) from exc

        model, voices = resolve_model_files(model_dir)
        self._lang = lang
        self._kokoro: Any = kokoro_onnx.Kokoro(str(model), str(voices))

    def synthesize(self, text: str, voice: str, speed: float) -> np.ndarray:
        if not text.strip():
            return np.zeros(0, dtype=np.float32)
        audio, _ = self._kokoro.create(text, voice=voice, speed=speed, lang=self._lang)
        if audio is None:
            return np.zeros(0, dtype=np.float32)
        # Audio of length zero needs no special case: asarray already yields an
        # empty float32 array, which is the contract for "nothing to play".
        return np.asarray(audio, dtype=np.float32)
