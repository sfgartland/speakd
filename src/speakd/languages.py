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
import re
import sys
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

# Free-form names as Zotero's own "Language" field spells them (its users
# type these by hand, in whatever language they typed the field in), mapped
# to our codes. Matched case-insensitively, after the same lower-casing
# `normalise` already does for a BCP-47 tag.
_FULL_NAMES: dict[str, str] = {
    "english": "en",
    "french": "fr",
    "français": "fr",
    "francais": "fr",
    "deutsch": "de",
    "german": "de",
    "español": "es",
    "espanol": "es",
    "spanish": "es",
    "italiano": "it",
    "italian": "it",
    "português": "pt-br",
    "portugues": "pt-br",
    "portuguese": "pt-br",
    "hindi": "hi",
    "japanese": "ja",
    "chinese": "zh",
}

# A loose shape for "this looks like a BCP-47 tag, even if not one we know" --
# a 2-3 letter primary subtag, optionally followed by one or two `-`
# subtags. Used only to decide whether an unrecognised code is `UNSUPPORTED`
# (parses, but names nothing we know -- still worth surfacing) or plain
# noise that should be treated as though nothing was given at all. Not a
# real BCP-47 validator; it only needs to separate "xx-yy" from "please
# speak spanish".
_TAG_SHAPE = re.compile(r"^[a-z]{2,3}(-[a-z0-9]{1,8}){0,2}$")

# Codes already warned about once, so a client that keeps sending the same
# noise does not spam stderr on every utterance.
_warned_unparseable: set[str] = set()


def _default_importable(module: str) -> bool:
    try:
        importlib.import_module(module)
    except ImportError:
        return False
    return True


def normalise(code: str) -> str | None:
    """A client- or user-given code, normalised to ours.

    None means nothing usable was given -- either the field was empty, or it
    held something that does not even look like a language (a full sentence,
    a typo, free text with no recognised name). Either way resolution must
    treat it as "not specified" and move on to its next candidate (channel,
    detection, default): an unparseable payload language must not silently
    beat detection the way a real one is entitled to. `UNSUPPORTED` means
    something was given that *does* look like a BCP-47 tag but names no
    language this project recognises, Kokoro-speakable or not -- that is
    still worth surfacing to `speech.unsupported_language` rather than
    dropping.
    """
    if not code or not code.strip():
        return None
    lowered = code.strip().lower().replace("_", "-")
    if lowered in SUPPORTED or lowered in _KNOWN_UNSUPPORTED:
        return lowered
    if lowered in ("pt", "pt-pt"):
        return "pt-br"
    if lowered == "en-us":
        return "en"
    if lowered in ("zh-cn", "zh-hans"):
        return "zh"
    if lowered in _FULL_NAMES:
        return _FULL_NAMES[lowered]
    primary, sep, _region = lowered.partition("-")
    if sep and (primary in SUPPORTED or primary in _KNOWN_UNSUPPORTED):
        return primary
    if _TAG_SHAPE.match(lowered):
        return UNSUPPORTED
    if lowered not in _warned_unparseable:
        _warned_unparseable.add(lowered)
        print(f"speakd: ignoring unparseable language {code!r}", file=sys.stderr)
    return None


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
