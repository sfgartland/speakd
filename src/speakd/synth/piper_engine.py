"""Piper adapter.

Piper speaks through an ONNX model per voice. Everything heavy -- `piper`
itself, `onnxruntime`, and `scipy` for the resample -- is imported lazily
inside the engine, so a speakd without the `piper` extra can import this
module (and `speakd.synth`) without touching any of them, and
`tests/test_import_cost.py` stays green.

Each voice is loaded on first use and kept, its session built by speakd
rather than `PiperVoice.load`: `load` gives no control over threads, where a
speakd-built `onnxruntime.SessionOptions` pins `inter_op_num_threads` to 1
(the measured single-thread win in the plan) and `intra_op_num_threads` to
whatever the caller chose. Loading happens outside the voices lock and the
result is stored only on success, so a voice that fails to load raises
`PiperVoiceError` and the next call tries again.

The voice's own sample rate comes from its config; audio is resampled to the
engine's 24 kHz so the sink never has to reopen, then trimmed with the same
`trim_silence` Kokoro gets so sentence gaps match across engines.

Years: espeak-ng reads "1994" as "one thousand nine hundred ninety four", so
`speak_years` rewrites them before the text reaches the voice; `reads_years`
declares it.
"""

from __future__ import annotations

import importlib.util
import json
import threading
from math import gcd
from pathlib import Path
from typing import Any

import numpy as np

from speakd.synth.audio import trim_silence
from speakd.synth.years import speak_years


def piper_available() -> bool:
    """Whether the `piper` package can be imported at all."""
    return importlib.util.find_spec("piper") is not None


class PiperVoiceError(Exception):
    """A voice could not be loaded, with the voice's name and the reason."""

    def __init__(self, voice: str, reason: str) -> None:
        super().__init__(f"{voice}: {reason}")
        self.voice = voice
        self.reason = reason


class PiperEngine:
    """Piper, one lazy-loaded ONNX session per voice, resampled to 24 kHz."""

    name = "piper"
    sample_rate = 24000
    reads_years = False

    def __init__(self, voice_dir: Path, threads: int) -> None:
        self.voice_dir = voice_dir
        # 0 leaves onnxruntime to its own default; anything else pins the
        # intra-op count. inter-op is always 1: the plan's measurement shows
        # one thread is the cheap setting, not a cap to be lifted.
        self._threads = threads
        self._voices: dict[str, Any] = {}
        self._voices_lock = threading.Lock()

    def has_voice(self, voice: str) -> bool:
        """Whether both of a voice's files exist: the model and its config."""
        return (self.voice_dir / f"{voice}.onnx").is_file() and (
            self.voice_dir / f"{voice}.onnx.json"
        ).is_file()

    def installed_voices(self) -> list[str]:
        """Sorted names of every `.onnx` that has a matching `.onnx.json`."""
        return sorted(
            path.stem
            for path in self.voice_dir.glob("*.onnx")
            if path.with_name(f"{path.name}.json").exists()
        )

    def _load_voice(self, voice: str) -> Any:
        import onnxruntime
        import piper
        from piper.config import PiperConfig

        try:
            config = PiperConfig.from_dict(
                json.loads((self.voice_dir / f"{voice}.onnx.json").read_text(encoding="utf-8"))
            )
            options = onnxruntime.SessionOptions()
            if self._threads:
                options.intra_op_num_threads = self._threads
            options.inter_op_num_threads = 1
            session = onnxruntime.InferenceSession(
                str(self.voice_dir / f"{voice}.onnx"),
                sess_options=options,
                providers=["CPUExecutionProvider"],
            )
            return piper.PiperVoice(config=config, session=session)
        except Exception as exc:
            raise PiperVoiceError(voice, str(exc)) from exc

    def _voice_for(self, voice: str) -> Any:
        with self._voices_lock:
            loaded = self._voices.get(voice)
            if loaded is not None:
                return loaded
        # Loading happens outside the lock -- it is the slow part, and a
        # concurrent synthesize for another voice must not wait on it. The
        # result is stored only on success, so a failed load is not cached
        # and the next call tries again.
        loaded = self._load_voice(voice)
        with self._voices_lock:
            self._voices[voice] = loaded
        return loaded

    def unload(self) -> None:
        """Drop every loaded voice; the next synthesize loads again."""
        with self._voices_lock:
            self._voices.clear()

    def set_voice_dir(self, path: Path) -> None:
        """Point at a new voice directory; anything loaded came from the old one."""
        with self._voices_lock:
            self._voices.clear()
            self.voice_dir = path

    def synthesize(self, text: str, voice: str, speed: float, lang: str = "en") -> np.ndarray:
        if not text.strip():
            return np.zeros(0, dtype=np.float32)
        from piper.config import SynthesisConfig
        from scipy.signal import resample_poly

        loaded = self._voice_for(voice)
        syn_config = SynthesisConfig(length_scale=1.0 / speed)
        chunks = [
            np.asarray(chunk.audio_float_array, dtype=np.float32)
            for chunk in loaded.synthesize(speak_years(text), syn_config)
        ]
        if not chunks:
            return np.zeros(0, dtype=np.float32)
        audio = np.concatenate(chunks)
        rate = loaded.config.sample_rate
        if rate != self.sample_rate:
            step = gcd(self.sample_rate, rate)
            audio = resample_poly(audio, self.sample_rate // step, rate // step)
        return trim_silence(np.asarray(audio, dtype=np.float32), self.sample_rate)
