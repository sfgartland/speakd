"""Tests for setting declarations and validation, pure functions only."""

from __future__ import annotations

import pytest

from speakd.settings.types import Declaration, SettingError, parse_declaration, to_json, validate


def _raw(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "name": "default_language",
        "type": "choice",
        "default": "en",
        "label": "Default language",
        "help": "Used when nothing else says otherwise.",
        "options": ["en", "fr"],
    }
    base.update(overrides)
    return base


def test_parse_declaration_builds_the_key_from_owner_and_name() -> None:
    decl = parse_declaration("speech", _raw())
    assert decl.key == "speech.default_language"
    assert decl.type == "choice"
    assert decl.default == "en"
    assert decl.options == ("en", "fr")


def test_parse_declaration_refuses_an_unknown_type() -> None:
    with pytest.raises(SettingError, match="unknown type"):
        parse_declaration("speech", _raw(type="paragraph"))


def test_parse_declaration_refuses_a_bad_name() -> None:
    with pytest.raises(SettingError, match="name"):
        parse_declaration("speech", _raw(name="Default-Language"))
    with pytest.raises(SettingError, match="name"):
        parse_declaration("speech", _raw(name="1default"))


def test_parse_declaration_refuses_a_choice_with_no_options() -> None:
    with pytest.raises(SettingError, match="options"):
        parse_declaration("speech", _raw(options=None, default="en"))
    with pytest.raises(SettingError, match="options"):
        parse_declaration("speech", _raw(options=[], default="en"))


def test_parse_declaration_refuses_min_greater_than_max() -> None:
    raw = _raw(name="section_chars", type="int", default=1800, options=None, min=4000, max=600)
    with pytest.raises(SettingError, match="min"):
        parse_declaration("zotero", raw)


def test_parse_declaration_refuses_a_default_that_fails_validate() -> None:
    with pytest.raises(SettingError):
        parse_declaration("speech", _raw(default="de"))  # not in options


def test_bool_type_accepts_and_refuses() -> None:
    decl = parse_declaration("speech", _raw(name="detect_language", type="bool", default=True, options=None))
    assert validate(decl, False) is False
    with pytest.raises(SettingError, match="bool"):
        validate(decl, 1)


def test_int_type_accepts_and_refuses_bounds_and_bools() -> None:
    decl = parse_declaration(
        "zotero",
        _raw(name="section_chars", type="int", default=1800, options=None, min=600, max=4000),
    )
    assert validate(decl, 700) == 700
    with pytest.raises(SettingError, match="int"):
        validate(decl, True)
    with pytest.raises(SettingError, match="maximum|4000"):
        validate(decl, 5000)
    with pytest.raises(SettingError, match="minimum|600"):
        validate(decl, 100)


def test_float_type_accepts_an_int_as_a_float() -> None:
    decl = parse_declaration(
        "speech",
        _raw(name="speed", type="float", default=1.0, options=None, min=0.5, max=2.0),
    )
    assert validate(decl, 1) == 1.0
    assert isinstance(validate(decl, 1), float)
    with pytest.raises(SettingError):
        validate(decl, 3.0)


def test_string_type_with_multiline() -> None:
    decl = parse_declaration(
        "claude-code", _raw(name="briefing_guide", type="string", default="hello", options=None, multiline=True)
    )
    assert decl.multiline is True
    assert validate(decl, "a\nb") == "a\nb"
    with pytest.raises(SettingError):
        validate(decl, 5)


def test_choice_type_refuses_a_value_outside_options() -> None:
    decl = parse_declaration("speech", _raw())
    with pytest.raises(SettingError, match="not one of"):
        validate(decl, "de")


def test_voice_type_validates_as_a_non_empty_string() -> None:
    decl = parse_declaration("speech", _raw(name="voice", type="voice", default="af_heart", options=None))
    assert validate(decl, "ff_siwis") == "ff_siwis"
    with pytest.raises(SettingError):
        validate(decl, "")
    with pytest.raises(SettingError):
        validate(decl, 5)


def test_voice_map_type_validates_language_codes_and_values() -> None:
    decl = parse_declaration(
        "speech",
        _raw(name="voices", type="voice_map", default={"en": "af_heart"}, options=None),
    )
    assert validate(decl, {"fr": "ff_siwis", "pt-br": "pf_dora"}) == {"fr": "ff_siwis", "pt-br": "pf_dora"}
    with pytest.raises(SettingError):
        validate(decl, {"FR": "ff_siwis"})
    with pytest.raises(SettingError):
        validate(decl, {"fr": ""})
    with pytest.raises(SettingError):
        validate(decl, {"fr": 5})
    with pytest.raises(SettingError):
        validate(decl, "not a dict")


def test_to_json_carries_every_declared_field() -> None:
    decl = parse_declaration("speech", _raw())
    body = to_json(decl)
    assert body == {
        "key": "speech.default_language",
        "type": "choice",
        "default": "en",
        "label": "Default language",
        "help": "Used when nothing else says otherwise.",
        "options": ["en", "fr"],
        "min": None,
        "max": None,
        "step": None,
        "multiline": False,
        "restart": False,
    }


def test_declaration_is_frozen() -> None:
    decl = parse_declaration("speech", _raw())
    with pytest.raises(Exception):
        decl.key = "speech.other"  # type: ignore[misc]
