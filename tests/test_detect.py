"""Tests for `speakd.detect`, against a faked `lingua` module.

Faked, not skipped without the extra: this is the one module whose mapping
logic (lingua's own language names to our codes) must be provable without the
`lang` extra installed, the same reason `test_kokoro_engine.py` keeps its
stub-pipeline tests unconditional.
"""

from __future__ import annotations

import sys
import types

import pytest

from speakd import detect


class FakeIsoCode:
    def __init__(self, name: str) -> None:
        self.name = name


class FakeLanguage:
    def __init__(self, name: str, iso: str) -> None:
        self.name = name
        self.iso_code_639_1 = FakeIsoCode(iso)


def _fake_lingua_module(*, result: object, raises: Exception | None = None) -> types.ModuleType:
    """A `lingua` stand-in whose detector always answers `result` (or raises)."""
    module = types.ModuleType("lingua")

    class Language:
        ENGLISH = FakeLanguage("ENGLISH", "EN")
        PORTUGUESE = FakeLanguage("PORTUGUESE", "PT")
        CHINESE = FakeLanguage("CHINESE", "ZH")
        SPANISH = FakeLanguage("SPANISH", "ES")
        FRENCH = FakeLanguage("FRENCH", "FR")
        HINDI = FakeLanguage("HINDI", "HI")
        ITALIAN = FakeLanguage("ITALIAN", "IT")
        JAPANESE = FakeLanguage("JAPANESE", "JA")
        GERMAN = FakeLanguage("GERMAN", "DE")
        DUTCH = FakeLanguage("DUTCH", "NL")
        SWEDISH = FakeLanguage("SWEDISH", "SV")
        BOKMAL = FakeLanguage("BOKMAL", "NB")
        DANISH = FakeLanguage("DANISH", "DA")
        POLISH = FakeLanguage("POLISH", "PL")

    class _Detector:
        def detect_language_of(self, text: str) -> object:
            if raises is not None:
                raise raises
            return result

    class LanguageDetectorBuilder:
        @staticmethod
        def from_languages(*languages: object) -> LanguageDetectorBuilder:
            return LanguageDetectorBuilder()

        def build(self) -> _Detector:
            return _Detector()

    module.Language = Language  # type: ignore[attr-defined]
    module.LanguageDetectorBuilder = LanguageDetectorBuilder  # type: ignore[attr-defined]
    return module


LONG_TEXT = "This sentence has well more than twenty letters in it, easily."
assert sum(c.isalpha() for c in LONG_TEXT) >= 20


def test_unavailable_without_the_extra(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "lingua", None)  # simulates ImportError
    d = detect.Detector()
    assert d.available is False
    assert d.detect(LONG_TEXT) is None


def test_available_when_lingua_can_be_imported(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _fake_lingua_module(result=None)
    monkeypatch.setitem(sys.modules, "lingua", fake)
    d = detect.Detector()
    assert d.available is True


def test_short_text_is_never_detected(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _fake_lingua_module(result=None)
    monkeypatch.setitem(sys.modules, "lingua", fake)
    d = detect.Detector()
    assert d.detect("hi") is None


def test_an_exception_from_the_detector_gives_none(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _fake_lingua_module(result=None, raises=RuntimeError("boom"))
    monkeypatch.setitem(sys.modules, "lingua", fake)
    d = detect.Detector()
    assert d.detect(LONG_TEXT) is None


@pytest.mark.parametrize(
    ("attr", "expected"),
    [
        ("ENGLISH", "en"),
        ("PORTUGUESE", "pt-br"),
        ("CHINESE", "zh"),
        ("SPANISH", "es"),
        ("FRENCH", "fr"),
        ("HINDI", "hi"),
        ("ITALIAN", "it"),
        ("JAPANESE", "ja"),
        ("GERMAN", "de"),
        ("DUTCH", "nl"),
        ("SWEDISH", "sv"),
        ("BOKMAL", "nb"),
        ("DANISH", "da"),
        ("POLISH", "pl"),
    ],
)
def test_a_result_maps_to_our_code(
    monkeypatch: pytest.MonkeyPatch, attr: str, expected: str
) -> None:
    placeholder = _fake_lingua_module(result=None)
    language = getattr(placeholder.Language, attr)
    fake = _fake_lingua_module(result=language)
    monkeypatch.setitem(sys.modules, "lingua", fake)
    d = detect.Detector()
    assert d.detect(LONG_TEXT) == expected


def test_a_none_result_from_the_detector_gives_none(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _fake_lingua_module(result=None)
    monkeypatch.setitem(sys.modules, "lingua", fake)
    d = detect.Detector()
    assert d.detect(LONG_TEXT) is None


def test_the_detector_is_built_only_once(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []
    fake = _fake_lingua_module(result=None)
    original_build = fake.LanguageDetectorBuilder.build

    def counting_build(self: object) -> object:
        calls.append(1)
        return original_build(self)

    fake.LanguageDetectorBuilder.build = counting_build
    monkeypatch.setitem(sys.modules, "lingua", fake)
    d = detect.Detector()
    d.detect(LONG_TEXT)
    d.detect(LONG_TEXT)
    assert d.available is True
    assert len(calls) == 1
