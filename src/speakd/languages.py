"""Language codes: normalisation, Kokoro's mapping, and what an engine can speak.

Kept separate from any one engine and from *resolution* -- `resolve` below is
a pure function of the four things resolution order cares about, with no
Kokoro in it. A later engine (Piper, Supertonic, chosen per utterance after
resolution, per the design's forward-compatibility note) reads the same
resolved code this module hands back; only `daemon.py`'s voice choice and
`kokoro_engine.py`'s pipeline cache know Kokoro's codes exist.
"""

from __future__ import annotations

import importlib
from collections.abc import Callable
from dataclasses import dataclass

# BCP-47 primary tags Kokoro speaks, lower-case, with a region only where
# Kokoro itself distinguishes one.
SUPPORTED: tuple[str, ...] = ("en", "en-gb", "es", "fr", "hi", "it", "pt-br", "ja", "zh")

# Kokoro's own single-letter lang codes.
KOKORO_CODES: dict[str, str] = {
    "en": "a",
    "en-gb": "b",
    "es": "e",
    "fr": "f",
    "hi": "h",
    "it": "i",
    "pt-br": "p",
    "ja": "j",
    "zh": "z",
}

# The reverse of KOKORO_CODES, keyed by a Kokoro voice's first letter -- what
# "the profile's voice is used when its first letter maps to the resolved
# language" (design §2, "Voices") reads off.
_VOICE_PREFIX_LANGUAGE: dict[str, str] = {code: lang for lang, code in KOKORO_CODES.items()}

# Codes normalise() recognises but that no engine here can speak yet: real
# BCP-47 tags, not gibberish, so they must not be treated the same as
# "klingon". They flow to speech.unsupported_language handling rather than
# being dropped, which is what makes them worth naming at all -- a later
# engine phase may add a pipeline for one of these.
_KNOWN_UNSUPPORTED: tuple[str, ...] = ("de",)

# A code normalise() could not make sense of. Not None: None means "nothing
# was given" (empty string), which a caller reads as "use resolution's next
# candidate." This sentinel means "something was given, and it names no
# language this project knows" -- which must still flow to
# speech.unsupported_language, not be silently dropped (Review Focus, plan
# Task 2).
UNSUPPORTED = "und"

# ja and zh need a G2P extra (misaki's ja/zh) to actually work; without it
# they are not really supported however Kokoro is asked. Checked lazily and
# only by `supported_languages`, so importing this module never imports them.
_CONDITIONAL: dict[str, str] = {"ja": "misaki.ja", "zh": "misaki.zh"}


def _default_importable(module: str) -> bool:
    try:
        importlib.import_module(module)
    except ImportError:
        return False
    return True


def normalise(code: str) -> str | None:
    """A client- or user-given code, normalised to ours.

    None means nothing usable was given (empty or whitespace) -- resolution
    treats that as "not specified" and moves to its next candidate.
    `UNSUPPORTED` means something was given but names no language this
    project recognises, Kokoro-speakable or not.
    """
    if not code or not code.strip():
        return None
    lowered = code.strip().lower().replace("_", "-")
    if lowered in SUPPORTED or lowered in _KNOWN_UNSUPPORTED:
        return lowered
    if lowered == "pt":
        return "pt-br"
    if lowered == "en-us":
        return "en"
    if lowered in ("zh-cn", "zh-hans"):
        return "zh"
    primary, sep, _region = lowered.partition("-")
    if sep and (primary in SUPPORTED or primary in _KNOWN_UNSUPPORTED):
        return primary
    return UNSUPPORTED


def voice_language(voice: str) -> str | None:
    """The language a Kokoro voice belongs to, from its first letter."""
    if not voice:
        return None
    return _VOICE_PREFIX_LANGUAGE.get(voice[0])


def supported_languages(
    importable: Callable[[str], bool] = _default_importable,
) -> list[str]:
    """Kokoro's languages, dropping ja/zh unless their G2P import succeeds.

    `importable` is injectable so a test can assert the ja/zh switch without
    the misaki extras actually being installed either way.
    """
    return [
        lang for lang in SUPPORTED if lang not in _CONDITIONAL or importable(_CONDITIONAL[lang])
    ]


@dataclass(frozen=True)
class Resolution:
    lang: str
    reason: str  # "payload", "channel", "detected" or "default"


def resolve(
    payload_lang: str | None,
    channel_lang: str | None,
    detected_lang: str | None,
    default_lang: str,
) -> Resolution:
    """The language for one utterance, first match wins (design §2).

    Pure: nothing here decides *whether* to detect (that is `speech.
    detect_language`, the detector's own availability, and the 20-letter
    rule -- all the caller's business, so `detected_lang` is already the
    answer, or already None because none of those held) and nothing here
    knows what an engine can actually speak (that is the unsupported-language
    handling built on top of this in `daemon.py`).
    """
    if payload_lang:
        return Resolution(payload_lang, "payload")
    if channel_lang:
        return Resolution(channel_lang, "channel")
    if detected_lang:
        return Resolution(detected_lang, "detected")
    return Resolution(default_lang, "default")
