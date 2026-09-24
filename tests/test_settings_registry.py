"""Tests for the settings registry: declarations, values and change events."""

from __future__ import annotations

from pathlib import Path

import pytest

from speakd.settings.registry import Settings
from speakd.settings.store import SettingsStore
from speakd.settings.types import SettingError


@pytest.fixture
def store(tmp_path: Path) -> SettingsStore:
    return SettingsStore(tmp_path / "settings.toml", tmp_path / "settings-schema.json")


SPEECH_RAWS: list[dict[str, object]] = [
    {
        "name": "default_language",
        "type": "choice",
        "default": "en",
        "label": "Default language",
        "help": "Used when nothing else says otherwise.",
        "options": ["en", "fr", "de"],
    },
    {"name": "detect_language", "type": "bool", "default": True, "label": "Detect", "help": "h"},
]


def test_defaults_when_nothing_is_stored(store: SettingsStore) -> None:
    settings = Settings(store)
    settings.declare("speech", SPEECH_RAWS, persist=False)
    assert settings.values("speech") == {
        "speech.default_language": "en",
        "speech.detect_language": True,
    }


def test_stored_values_win_over_defaults(store: SettingsStore) -> None:
    store.write_value("speech.default_language", "fr")
    settings = Settings(store)
    settings.declare("speech", SPEECH_RAWS, persist=False)
    assert settings.get("speech.default_language") == "fr"


def test_redeclaring_with_a_changed_type_falls_back_stored_value_to_default(
    store: SettingsStore, capsys: pytest.CaptureFixture[str]
) -> None:
    settings = Settings(store)
    settings.declare(
        "zotero",
        [{"name": "section_chars", "type": "int", "default": 1800, "label": "n", "help": "h"}],
        persist=True,
    )
    settings.set("zotero.section_chars", 2000)
    # Redeclared as a bool: the stored int of 2000 no longer validates.
    settings.declare(
        "zotero",
        [{"name": "section_chars", "type": "bool", "default": False, "label": "n", "help": "h"}],
        persist=True,
    )
    assert settings.get("zotero.section_chars") is False
    assert "section_chars" in capsys.readouterr().err


def test_an_offline_clients_schema_is_loaded_from_disk(store: SettingsStore) -> None:
    settings = Settings(store)
    settings.declare("zotero", SPEECH_RAWS, persist=True)
    # A fresh Settings, as though the daemon had just restarted with zotero
    # not yet connected.
    reloaded = Settings(store)
    assert reloaded.schema("zotero") != []
    assert reloaded.values("zotero") == {
        "zotero.default_language": "en",
        "zotero.detect_language": True,
    }


def test_declarations_from_the_current_run_override_persisted_ones(store: SettingsStore) -> None:
    settings = Settings(store)
    settings.declare("zotero", SPEECH_RAWS, persist=True)
    reloaded = Settings(store)
    reloaded.declare(
        "zotero",
        [{"name": "default_language", "type": "bool", "default": False, "label": "l", "help": "h"}],
        persist=False,
    )
    assert reloaded.schema("zotero") == [
        {
            "key": "zotero.default_language",
            "type": "bool",
            "default": False,
            "label": "l",
            "help": "h",
            "options": None,
            "min": None,
            "max": None,
            "step": None,
            "multiline": False,
            "restart": False,
        }
    ]


def test_on_change_fires_on_set(store: SettingsStore) -> None:
    settings = Settings(store)
    settings.declare("speech", SPEECH_RAWS, persist=False)
    seen: list[tuple[str, object]] = []
    settings.on_change(lambda key, value: seen.append((key, value)))
    settings.set("speech.default_language", "fr")
    assert seen == [("speech.default_language", "fr")]


def test_on_change_does_not_fire_on_a_failed_set(store: SettingsStore) -> None:
    settings = Settings(store)
    settings.declare("speech", SPEECH_RAWS, persist=False)
    seen: list[tuple[str, object]] = []
    settings.on_change(lambda key, value: seen.append((key, value)))
    with pytest.raises(SettingError):
        settings.set("speech.default_language", "de-de")
    assert seen == []


def test_on_change_disposal_stops_the_callback(store: SettingsStore) -> None:
    settings = Settings(store)
    settings.declare("speech", SPEECH_RAWS, persist=False)
    seen: list[tuple[str, object]] = []
    handle = settings.on_change(lambda key, value: seen.append((key, value)))
    handle.dispose()
    settings.set("speech.default_language", "fr")
    assert seen == []


def test_undeclare_removes_a_plugins_settings(store: SettingsStore) -> None:
    settings = Settings(store)
    raw = [{"name": "flag", "type": "bool", "default": True, "label": "l", "help": "h"}]
    settings.declare("markdown", raw, persist=False)
    assert settings.schema("markdown") != []
    settings.undeclare("markdown", ["flag"])
    assert settings.schema("markdown") == []


def test_set_refuses_a_key_with_no_declaration(store: SettingsStore) -> None:
    settings = Settings(store)
    with pytest.raises(SettingError):
        settings.set("nobody.key", "x")


def test_values_with_no_owner_covers_every_declared_owner(store: SettingsStore) -> None:
    settings = Settings(store)
    settings.declare("speech", SPEECH_RAWS, persist=False)
    settings.declare(
        "zotero",
        [{"name": "section_chars", "type": "int", "default": 1800, "label": "l", "help": "h"}],
        persist=False,
    )
    values = settings.values()
    assert values["speech.default_language"] == "en"
    assert values["zotero.section_chars"] == 1800
