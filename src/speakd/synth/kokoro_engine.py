"""Kokoro adapter: misaki's pronunciation, rendered by kokoro-onnx.

Kokoro is the same 82M-parameter model as before; only the runtime changed.
Running it through onnxruntime instead of torch costs ~0.7 GB less RAM and
loads in about a sixth of the time. Pronunciation is kept by feeding the model
phonemes from misaki -- the G2P the torch pipeline used -- instead of letting
kokoro-onnx phonemise through espeak-ng alone: `speakd.transforms.pronunciation`
is tuned against misaki's dictionary-first behaviour, and the voice should not
change with the runtime.

We hand it one already-segmented unit at a time, so the pipeline keeps sole
control of latency. kokoro-onnx splits a unit only to stay under the model's
510-phoneme limit and returns one concatenated array, so that splitting is not
a streaming decision and cannot reopen the seam a pinned `split_pattern` closed
for the torch pipeline. A unit can contain a newline, which the G2P and
kokoro-onnx both treat as plain whitespace.

Kokoro also pads what it returns with near-silence at both ends -- measured
at about 0.21 s before the speech and 0.18 s after it -- and the pipeline
segments per sentence, so that padding is paid once per sentence rather than
once per utterance. A five-sentence utterance carried close to two seconds of
it. We cut it back to a residual as the audio leaves the engine. That trim
lives in `speakd.synth.audio`, shared with Piper and re-exported here; it is
the only trim, so `create` is told not to apply kokoro-onnx's own (which also
inserts pauses the pipeline does not expect).
"""

from __future__ import annotations

import os
import sys
import threading
import urllib.request
from collections.abc import Callable
from pathlib import Path
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
    "GAIN",
    "KokoroEngine",
    "LEADING_SILENCE_SECONDS",
    "SILENCE_THRESHOLD",
    "TRAILING_SILENCE_SECONDS",
    "trim_silence",
]

MODEL_FILENAME = "kokoro-v1.0.onnx"
VOICES_FILENAME = "voices-v1.0.bin"
RELEASE_URL = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0"

# onnxruntime renders the same voice louder than torch did. Measured on three
# sentences (af_heart, speed 1.1) through the old torch engine and this one:
# speech RMS over samples above 0.01 was 2.74 dB higher here (2.71-2.80 per
# sentence), so scaling by 10 ** (-2.74 / 20) = 0.73 makes the swap a change of
# runtime, not of volume. The figure depends on the 0.01 threshold (it decides
# which quiet samples count as speech), so treat it as good to a few tenths of
# a dB, not as a physical constant.
GAIN = 0.73

# What kokoro-onnx accepts; it raises outside this range.
MIN_SPEED = 0.5
MAX_SPEED = 2.0

# Kokoro's own language-code table (kokoro.pipeline.LANG_CODES), for the codes
# that go to espeak-ng: misaki wants the espeak voice name, not the letter.
_ESPEAK_LANGUAGES = {"e": "es", "f": "fr-fr", "h": "hi", "i": "it", "p": "pt-br"}


def default_model_dir() -> Path:
    """Where the two model files live: `$XDG_CACHE_HOME/speakd/kokoro-onnx`.

    They are 353 MB together and not part of any wheel, so they belong in a
    cache directory rather than in the repository or the virtualenv.
    """
    configured = os.environ.get("XDG_CACHE_HOME")
    root = Path(configured) if configured else Path.home() / ".cache"
    return root / "speakd" / "kokoro-onnx"


def _download(url: str, destination: Path) -> None:
    with urllib.request.urlopen(url, timeout=60) as response, destination.open("wb") as out:
        written = 0
        while chunk := response.read(1 << 20):
            out.write(chunk)
            written += len(chunk)
    expected = response.headers.get("Content-Length")
    # A connection that drops mid-body can end the read loop quietly; without
    # this check the short file would be moved into place as the model.
    if expected is not None and written != int(expected):
        raise OSError(f"got {written} of {expected} bytes")


def ensure_model_files(
    directory: Path, fetch: Callable[[str, Path], None] = _download
) -> tuple[Path, Path]:
    """Return the model and voices paths, downloading whichever is missing.

    Each file is fetched to a temporary name and moved into place only when
    complete, so an interrupted download is never mistaken for a model on the
    next start. `fetch` is a parameter so tests never touch the network.
    """
    paths = (directory / MODEL_FILENAME, directory / VOICES_FILENAME)
    missing = [path for path in paths if not path.is_file()]
    if missing:
        names = " and ".join(path.name for path in missing)
        sys.stderr.write(f"speakd: downloading {names} to {directory} (first run only)\n")
    for path in missing:
        url = f"{RELEASE_URL}/{path.name}"
        # Per process, so two first starts do not write the same file.
        partial = path.with_name(f"{path.name}.{os.getpid()}.part")
        try:
            directory.mkdir(parents=True, exist_ok=True)
            fetch(url, partial)
            os.replace(partial, path)
        except Exception as exc:
            partial.unlink(missing_ok=True)
            raise RuntimeError(
                f"speakd: could not download {path.name} ({exc}); "
                f"fetch {url} and put it in {directory}"
            ) from exc
    return paths


class KokoroEngine:
    """Kokoro on the CPU through onnxruntime.

    `SPEAKD_DEVICE` used to choose between torch's CPU and CUDA backends. It
    means nothing here, and is ignored without comment so that service
    overrides written for the old engine keep working.
    """

    name = "kokoro"
    sample_rate = 24000
    reads_years = True

    def __init__(self, model_dir: Path | None = None) -> None:
        # Imported here so that the module -- and the daemon's entry point --
        # import without the extra; __main__ turns the ModuleNotFoundError
        # from a missing one into a one-line hint.
        import kokoro_onnx

        directory = default_model_dir() if model_dir is None else model_dir
        model, voices = ensure_model_files(directory)
        self._model: Any = kokoro_onnx.Kokoro(str(model), str(voices))
        # One G2P per Kokoro lang code, built lazily on first use and shared
        # afterwards: it is the expensive, language-specific part, while
        # `self._model` is the one thing every language shares. The lock is
        # re-entrant because Chinese asks for the English G2P while its own
        # is being built.
        self._g2ps: dict[str, Any] = {}
        self._g2ps_lock = threading.RLock()

    def _build_g2p(self, code: str) -> Any:
        # Mirrors kokoro.KPipeline.__init__, which this replaces.
        from misaki import en, espeak

        if code in ("a", "b"):
            british = code == "b"
            try:
                fallback = espeak.EspeakFallback(british=british)
            except Exception:
                # As KPipeline does: without espeak, words missing from the
                # dictionary are skipped rather than English being unusable.
                fallback = None
            return en.G2P(trf=False, british=british, fallback=fallback, unk="")
        if code == "j":
            from misaki import ja

            return ja.JAG2P()
        if code == "z":
            from misaki import zh

            # Deliberately not KPipeline's None: English words inside Chinese
            # text are now spoken, through the English G2P, not dropped.
            return zh.ZHG2P(version=None, en_callable=self._english_phonemes)
        return espeak.EspeakG2P(language=_ESPEAK_LANGUAGES[code])

    def _english_phonemes(self, text: str) -> str:
        phonemes, _ = self._g2p_for("en")(text)
        return str(phonemes)

    def _g2p_for(self, lang: str) -> Any:
        code = languages.KOKORO_CODES.get(lang)
        if code is None:
            # Not one of ours at all -- languages.normalise() would already
            # have caught this upstream, so reaching it here means a caller
            # bypassed normalisation. Still the right exception: the daemon's
            # unsupported-language handling is what catches it either way.
            raise UnsupportedLanguage(lang)
        with self._g2ps_lock:
            g2p = self._g2ps.get(code)
            if g2p is not None:
                return g2p
            try:
                g2p = self._build_g2p(code)
            except Exception as exc:
                # A missing G2P extra (misaki's ja/zh) is the case this
                # exists for, but any construction failure gets the same
                # treatment: the daemon has one thing to catch and one
                # setting (speech.unsupported_language) to act on, not a
                # zoo of engine-specific exceptions.
                raise UnsupportedLanguage(lang) from exc
            self._g2ps[code] = g2p
            return g2p

    def synthesize(self, text: str, voice: str, speed: float, lang: str = "en") -> np.ndarray:
        if not text.strip():
            return np.zeros(0, dtype=np.float32)
        phonemes, _ = self._g2p_for(lang)(text)
        # Profile speed times tempo, or a render job's raw speed, can leave
        # kokoro-onnx's range, and it raises rather than clamps -- which the
        # pipeline would turn into every sentence silently dropped.
        speed = min(max(speed, MIN_SPEED), MAX_SPEED)
        try:
            audio, _rate = self._model.create(
                phonemes, voice=voice, speed=speed, is_phonemes=True, trim=False
            )
        except ValueError as exc:
            if "Nothing to synthesize" in str(exc):
                # Text that is all punctuation or unknown symbols phonemises
                # to nothing; that is silence, not a failure.
                return np.zeros(0, dtype=np.float32)
            raise
        if audio is None or len(audio) == 0:
            return np.zeros(0, dtype=np.float32)
        audio = np.asarray(audio, dtype=np.float32) * np.float32(GAIN)
        return trim_silence(audio, self.sample_rate)

    def supported_languages(self) -> list[str]:
        return languages.supported_languages()
