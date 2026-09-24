"""Tests for the settings store: the TOML values file and the JSON schema file."""

from __future__ import annotations

from pathlib import Path

import pytest
import tomllib

from speakd.settings.store import SettingsStore


@pytest.fixture
def store(tmp_path: Path) -> SettingsStore:
    return SettingsStore(tmp_path / "settings.toml", tmp_path / "settings-schema.json")


def test_round_trip_of_every_value_type(store: SettingsStore) -> None:
    store.write_value("speech.detect_language", True)
    store.write_value("speech.default_language", "en")
    store.write_value("zotero.section_chars", 1800)
    store.write_value("speech.speed", 1.25)
    store.write_value("speech.voices", {"fr": "ff_siwis", "it": "if_sara"})
    values = store.load_values()
    assert values == {
        "speech.detect_language": True,
        "speech.default_language": "en",
        "zotero.section_chars": 1800,
        "speech.speed": 1.25,
        "speech.voices": {"fr": "ff_siwis", "it": "if_sara"},
    }


def test_a_multiline_string_with_quotes_round_trips(store: SettingsStore) -> None:
    text = 'Say "hello" then\nread the next paragraph.\tDone.'
    store.write_value("claude-code.briefing_guide", text)
    assert store.load_values()["claude-code.briefing_guide"] == text


def test_an_unknown_key_is_preserved_across_a_write(store: SettingsStore, tmp_path: Path) -> None:
    store.write_value("plugin-x.setting", "value")
    store.write_value("speech.detect_language", True)
    values = store.load_values()
    assert values["plugin-x.setting"] == "value"
    assert values["speech.detect_language"] is True


def test_a_hand_edit_between_two_writes_is_kept(store: SettingsStore) -> None:
    store.write_value("speech.detect_language", True)
    # Simulate a human editing the file directly while the daemon is up.
    with open(store.values_path, "a", encoding="utf-8") as handle:
        handle.write('\n[http]\nport = "8080"\n')
    store.write_value("speech.default_language", "fr")
    values = store.load_values()
    assert values["speech.detect_language"] is True
    assert values["speech.default_language"] == "fr"
    assert values["http.port"] == "8080"


def test_the_write_is_atomic_and_leaves_no_temp_file(store: SettingsStore) -> None:
    store.write_value("speech.detect_language", True)
    leftovers = [p for p in store.values_path.parent.iterdir() if p != store.values_path]
    assert leftovers == []


def test_a_corrupt_file_is_read_as_empty_with_a_warning(
    store: SettingsStore, capsys: pytest.CaptureFixture[str]
) -> None:
    store.values_path.parent.mkdir(parents=True, exist_ok=True)
    store.values_path.write_text("this is not [valid toml", encoding="utf-8")
    assert store.load_values() == {}
    assert "not valid toml" in capsys.readouterr().err.lower()
    # Not overwritten by the mere act of reading it.
    assert store.values_path.read_text(encoding="utf-8") == "this is not [valid toml"


def test_a_corrupt_file_is_replaced_on_the_next_write(store: SettingsStore) -> None:
    store.values_path.parent.mkdir(parents=True, exist_ok=True)
    store.values_path.write_text("this is not [valid toml", encoding="utf-8")
    store.write_value("speech.detect_language", True)
    assert store.load_values() == {"speech.detect_language": True}
    # The result is valid TOML that tomllib itself accepts.
    with open(store.values_path, "rb") as handle:
        tomllib.load(handle)


def test_load_values_on_a_missing_file_is_empty(store: SettingsStore) -> None:
    assert store.load_values() == {}


def test_schema_round_trips_per_owner(store: SettingsStore) -> None:
    speech_raws: list[dict[str, object]] = [
        {"name": "default_language", "type": "choice", "default": "en"}
    ]
    zotero_raws: list[dict[str, object]] = [
        {"name": "section_chars", "type": "int", "default": 1800}
    ]
    store.write_schema("speech", speech_raws)
    store.write_schema("zotero", zotero_raws)
    schema = store.load_schema()
    assert schema == {"speech": speech_raws, "zotero": zotero_raws}


def test_write_schema_replaces_only_its_own_owner(store: SettingsStore) -> None:
    store.write_schema("speech", [{"name": "a", "type": "bool", "default": True}])
    store.write_schema("zotero", [{"name": "b", "type": "bool", "default": True}])
    store.write_schema("speech", [{"name": "c", "type": "bool", "default": False}])
    schema = store.load_schema()
    assert schema["speech"] == [{"name": "c", "type": "bool", "default": False}]
    assert schema["zotero"] == [{"name": "b", "type": "bool", "default": True}]


def test_load_schema_on_a_missing_file_is_empty(store: SettingsStore) -> None:
    assert store.load_schema() == {}
