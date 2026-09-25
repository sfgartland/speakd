"""Which engine speaks an utterance, chosen after language resolution.

One pure function, `choose_engine`, reads the resolved language and the
settings and answers Piper-with-a-voice or Kokoro -- first match wins, per
the design (§2, "Choosing the engine per utterance"). Piper speaks when
`speech.engine` is piper, the language maps to a voice, that voice's files
exist (checked by an injected callable: file I/O is the daemon's business,
not this function's), and Piper is available. Anything else falls to Kokoro
exactly as it would have without Piper; a Kokoro that is needed but unloaded
is a `Declined`, never an implicit load (the `LazyEngine` contract). A
profile's own voice is a Kokoro voice, which is why a Kokoro `EngineChoice`
carries `voice = None` -- "phase 2 chooses as before".
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass


@dataclass(frozen=True)
class EngineChoice:
    """The engine for one utterance, with the Piper voice only when Piper speaks."""

    engine: str
    voice: str | None


@dataclass(frozen=True)
class Declined:
    """No engine can speak this utterance, and why."""

    reason: str


def choose_engine(
    lang: str,
    engine_setting: str,
    piper_voices: Mapping[str, str],
    piper_has_voice: Callable[[str], bool],
    piper_ok: bool,
    kokoro_loaded: bool,
) -> EngineChoice | Declined:
    """The engine for one resolved language: Piper, else Kokoro, else declined.

    Pure: everything slow or stateful (file existence, whether the piper
    extra is importable, Kokoro's load switch) arrives as an argument, so
    this runs in tests with no engine, no model and no audio device.
    """
    voice = piper_voices.get(lang)
    if engine_setting == "piper" and piper_ok and voice is not None and piper_has_voice(voice):
        return EngineChoice("piper", voice)
    if kokoro_loaded:
        return EngineChoice("kokoro", None)
    return Declined(f"no voice for {lang}")
