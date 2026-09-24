"""Tests for `speakd.languages`: normalisation, Kokoro codes, voice prefixes."""

from __future__ import annotations

from speakd import languages


def test_supported_languages_and_kokoro_codes_agree() -> None:
    assert set(languages.SUPPORTED) == set(languages.KOKORO_CODES)
    assert languages.KOKORO_CODES == {
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


def test_normalise_lower_cases_and_maps_underscore_to_hyphen() -> None:
    assert languages.normalise("EN") == "en"
    assert languages.normalise("en_gb") == "en-gb"


def test_normalise_pt_becomes_pt_br() -> None:
    assert languages.normalise("pt") == "pt-br"
    assert languages.normalise("PT") == "pt-br"


def test_normalise_en_us_becomes_en() -> None:
    assert languages.normalise("en-us") == "en"


def test_normalise_zh_variants_become_zh() -> None:
    assert languages.normalise("zh-cn") == "zh"
    assert languages.normalise("zh-hans") == "zh"


def test_normalise_an_unknown_region_falls_back_to_the_primary_tag() -> None:
    assert languages.normalise("fr-ca") == "fr"


def test_normalise_de_is_known_but_kokoro_cannot_speak_it() -> None:
    # Forward compatibility: de is a real code we recognise so it can flow to
    # speech.unsupported_language handling, even though no engine here speaks
    # it yet.
    assert languages.normalise("de") == "de"
    assert "de" not in languages.SUPPORTED


def test_normalise_an_unparseable_code_is_kept_as_the_unsupported_sentinel() -> None:
    assert languages.normalise("klingon") == languages.UNSUPPORTED
    assert languages.normalise("xx-yy") == languages.UNSUPPORTED


def test_normalise_empty_is_none() -> None:
    assert languages.normalise("") is None
    assert languages.normalise("   ") is None


def test_voice_language_reads_the_first_letter() -> None:
    assert languages.voice_language("af_heart") == "en"
    assert languages.voice_language("bf_emma") == "en-gb"
    assert languages.voice_language("ef_dora") == "es"
    assert languages.voice_language("ff_siwis") == "fr"
    assert languages.voice_language("hf_alpha") == "hi"
    assert languages.voice_language("if_sara") == "it"
    assert languages.voice_language("pf_dora") == "pt-br"
    assert languages.voice_language("jf_alpha") == "ja"
    assert languages.voice_language("zf_xiaobei") == "zh"


def test_voice_language_unknown_prefix_is_none() -> None:
    assert languages.voice_language("qf_nobody") is None
    assert languages.voice_language("") is None


def test_supported_languages_keeps_ja_and_zh_when_their_g2p_imports() -> None:
    assert languages.supported_languages(lambda module: True) == list(languages.SUPPORTED)


def test_supported_languages_drops_ja_and_zh_when_their_g2p_does_not_import() -> None:
    result = languages.supported_languages(lambda module: False)
    assert "ja" not in result
    assert "zh" not in result
    assert set(result) == set(languages.SUPPORTED) - {"ja", "zh"}


def test_supported_languages_is_selective_per_module() -> None:
    def importable(module: str) -> bool:
        return module != "misaki.zh"

    result = languages.supported_languages(importable)
    assert "ja" in result
    assert "zh" not in result


def test_resolve_prefers_payload_then_channel_then_detected_then_default() -> None:
    assert languages.resolve("fr", "es", "it", "en") == languages.Resolution("fr", "payload")
    assert languages.resolve(None, "es", "it", "en") == languages.Resolution("es", "channel")
    assert languages.resolve(None, None, "it", "en") == languages.Resolution("it", "detected")
    assert languages.resolve(None, None, None, "en") == languages.Resolution("en", "default")


def test_resolve_treats_empty_strings_as_not_given() -> None:
    assert languages.resolve("", "", "", "en") == languages.Resolution("en", "default")
