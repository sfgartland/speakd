"""Optional language detection, on lingua, imported lazily.

Importing `lingua` at module scope would put it in `sys.modules` for every
process that imports `speakd.daemon`, including the Claude Code hook whose
import cost `tests/test_import_cost.py` guards -- so it is imported only
inside `Detector._ensure`, on first real use, and only if the `lang` extra is
installed. Without it, detection is off and one line at startup says why.
"""

from __future__ import annotations

import sys
import threading
from typing import Any

# Detecting a handful of letters is noise, not a language -- "Hi" and "Oui"
# are each a coin flip. Short text falls through to the channel's or the
# default language instead.
_MIN_LETTERS = 20

# Our code -> lingua's own `Language` member name. One entry per detectable
# language: the nine we speak, plus near neighbours (design §2) whose
# recognition keeps them from being guessed as English rather than correctly
# read as "not one of ours" (Norwegian is Bokmål here, lingua's more common
# written form).
_LANGUAGE_NAMES: dict[str, str] = {
    "en": "ENGLISH",
    "en-gb": "ENGLISH",
    "es": "SPANISH",
    "fr": "FRENCH",
    "hi": "HINDI",
    "it": "ITALIAN",
    "pt-br": "PORTUGUESE",
    "ja": "JAPANESE",
    "zh": "CHINESE",
    "de": "GERMAN",
    "nl": "DUTCH",
    "sv": "SWEDISH",
    "nb": "BOKMAL",
    "da": "DANISH",
    "pl": "POLISH",
}

# Results that map to one of our own codes by name rather than by lingua's
# ISO 639-1 code: lingua has one ENGLISH for both en and en-gb (we always read
# it as en, since detection cannot tell the accent), one PORTUGUESE for our
# pt-br, and CHINESE's ISO code is "zh" already but named here for symmetry.
_SPECIAL = {"ENGLISH": "en", "PORTUGUESE": "pt-br", "CHINESE": "zh"}


def _letters(text: str) -> int:
    return sum(1 for ch in text if ch.isalpha())


def _map_result(language: Any) -> str | None:
    name = getattr(language, "name", None)
    if name is None:
        return None
    if name in _SPECIAL:
        return _SPECIAL[name]
    iso = getattr(language, "iso_code_639_1", None)
    code = getattr(iso, "name", None)
    return code.lower() if isinstance(code, str) else None


class Detector:
    """Wraps a lingua detector, built at most once and only when needed."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._built = False
        self._detector: Any = None
        self._warned = False

    @property
    def available(self) -> bool:
        self._ensure()
        return self._detector is not None

    def _ensure(self) -> None:
        with self._lock:
            if self._built:
                return
            self._built = True
            try:
                from lingua import Language, LanguageDetectorBuilder
            except ImportError:
                if not self._warned:
                    self._warned = True
                    print(
                        "speakd: language detection is off (install the "
                        "'lang' extra: uv sync --extra lang)",
                        file=sys.stderr,
                    )
                return
            wanted = [getattr(Language, name) for name in dict.fromkeys(_LANGUAGE_NAMES.values())]
            self._detector = LanguageDetectorBuilder.from_languages(*wanted).build()

    def detect(self, text: str) -> str | None:
        """The text's language, or None if it is not confidently one of ours.

        None for text under 20 letters, for a detector that is unavailable,
        for a result lingua could not map onto a code (should not happen with
        the language set built above, but a future lingua release renaming
        something must not raise), and for a detector call that itself
        raised -- reported once, since a flood of the same warning on every
        short utterance would teach nobody anything a second time did not.
        """
        if _letters(text) < _MIN_LETTERS:
            return None
        self._ensure()
        if self._detector is None:
            return None
        try:
            result = self._detector.detect_language_of(text)
        except Exception as exc:
            print(f"speakd: language detection failed: {exc!r}", file=sys.stderr)
            return None
        if result is None:
            return None
        return _map_result(result)
