"""Which engine speaks an utterance, chosen after language resolution.

One pure function, `choose_engine`, reads the resolved language and the
settings and answers the remote engine, Piper-with-a-voice or Kokoro -- first
match wins, per
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

from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass


@dataclass(frozen=True)
class EngineChoice:
    """The engine for one utterance, with the Piper voice only when Piper speaks."""

    engine: str
    voice: str | None


@dataclass(frozen=True)
class Declined:
    """No engine can speak this utterance, and why.

    `alert` marks a refusal the listener should hear about, not only read:
    the remote engine they chose is unusable and they asked not to fall back,
    so without a sound they would simply miss what was said.
    """

    reason: str
    alert: bool = False


def choose_engine(
    lang: str,
    engine_setting: str,
    piper_voices: Mapping[str, str],
    piper_has_voice: Callable[[str], bool],
    piper_ok: bool,
    kokoro_loaded: bool,
    *,
    remote_voice: str | None = None,
    remote_languages: Collection[str] = (),
    remote_fallback: bool = True,
    remote_unavailable: str = "",
) -> EngineChoice | Declined:
    """The engine for one resolved language: the remote engine, Piper,
    Kokoro, or declined.

    Pure: everything slow or stateful (file existence, whether the piper
    extra is importable, Kokoro's load switch, whether the remote engine has
    a key and is not held off after a failure) arrives as an argument, so
    this runs in tests with no engine, no model and no audio device.

    `remote_voice` is None when the remote engine is not usable right now;
    otherwise it speaks `remote_languages` whenever `speech.engine` names it.
    A language it does not speak falls through to the local engines exactly
    as though it were not configured. An outage does too while
    `remote_fallback` holds; without it, an unusable remote engine is a
    `Declined` carrying `remote_unavailable` and the alert flag -- the
    listener chose not to have a local model loaded, and must not have one
    loaded for them, nor be left in silence without knowing why.
    """
    if engine_setting == "openrouter" and lang in remote_languages:
        if remote_voice is not None:
            return EngineChoice("openrouter", remote_voice)
        if not remote_fallback:
            return Declined(remote_unavailable or "OpenRouter is unavailable", alert=True)
    voice = piper_voices.get(lang)
    if engine_setting == "piper" and piper_ok and voice is not None and piper_has_voice(voice):
        return EngineChoice("piper", voice)
    if kokoro_loaded:
        return EngineChoice("kokoro", None)
    return Declined(f"no voice for {lang}")
