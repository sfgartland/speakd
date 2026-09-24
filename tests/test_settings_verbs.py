"""Tests for the daemon's three settings verbs, and core's own declarations."""

from __future__ import annotations

from pathlib import Path

import pytest

from speakd.channels import ChannelTable
from speakd.daemon import Daemon, ProfileView
from speakd.events import Event, EventBus
from speakd.player import FakeSink, StreamingPlayer
from speakd.protocol import Request, Verb
from speakd.settings.registry import Settings
from speakd.settings.store import SettingsStore
from speakd.synth.fake import FakeEngine


def _profile_for(_name: str) -> ProfileView:
    return ProfileView(voice="v", speed=1.0, interrupt_on=(), prepare=lambda p: (list(p), []))


def _dict(value: object) -> dict[str, object]:
    """A response field known to be a dict, narrowed for mypy's sake."""
    assert isinstance(value, dict)
    return value


def _list(value: object) -> list[object]:
    assert isinstance(value, list)
    return value


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(SettingsStore(tmp_path / "settings.toml", tmp_path / "settings-schema.json"))


@pytest.fixture
def daemon(settings: Settings) -> Daemon:
    return Daemon(
        FakeEngine(),
        StreamingPlayer(FakeSink()),
        _profile_for,
        bus=EventBus(),
        channels=ChannelTable(),
        settings=settings,
    )


def test_core_declares_its_own_settings(daemon: Daemon, settings: Settings) -> None:
    keys = {decl["key"] for decl in settings.schema("speech")}
    assert "speech.default_language" in keys
    assert "speech.detect_language" in keys
    assert settings.get("speech.default_language") == "en"
    assert settings.get("speech.detect_language") is True


def test_daemon_builds_its_own_settings_when_none_is_given(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    d = Daemon(
        FakeEngine(),
        StreamingPlayer(FakeSink()),
        _profile_for,
        bus=EventBus(),
        channels=ChannelTable(),
    )
    response = d.handle(Request(verb=Verb.SETTINGS, source_id="", payload={"owner": "speech"}))
    assert response.ok
    assert _dict(response.data["values"])["speech.default_language"] == "en"


def test_settings_verb_answers_schema_and_values(daemon: Daemon) -> None:
    response = daemon.handle(Request(verb=Verb.SETTINGS, source_id="", payload={"owner": "speech"}))
    assert response.ok
    assert _dict(response.data["values"])["speech.detect_language"] is True
    keys = {_dict(decl)["key"] for decl in _list(response.data["schema"])}
    assert keys == {
        "speech.default_language",
        "speech.detect_language",
        "speech.voices",
        "speech.unsupported_language",
        "speech.sentence_gap_ms",
    }


def test_settings_verb_with_no_owner_answers_every_owner(daemon: Daemon) -> None:
    response = daemon.handle(Request(verb=Verb.SETTINGS, source_id="", payload={}))
    assert response.ok
    assert "speech.default_language" in _dict(response.data["values"])


def test_set_setting_answers_the_value_and_publishes_an_event(daemon: Daemon) -> None:
    seen: list[Event] = []
    daemon.bus.subscribe(lambda e: seen.append(e), kinds=["setting"])
    response = daemon.handle(
        Request(
            verb=Verb.SET_SETTING,
            source_id="",
            payload={"key": "speech.default_language", "value": "fr"},
        )
    )
    assert response.ok
    assert response.data == {"value": "fr"}
    assert [e.data for e in seen] == [{"key": "speech.default_language", "value": "fr"}]


def test_a_failed_set_setting_publishes_nothing(daemon: Daemon) -> None:
    seen: list[Event] = []
    daemon.bus.subscribe(lambda e: seen.append(e), kinds=["setting"])
    response = daemon.handle(
        Request(
            verb=Verb.SET_SETTING,
            source_id="",
            payload={"key": "speech.default_language", "value": "not-a-language"},
        )
    )
    assert not response.ok
    assert seen == []


def test_declare_settings_uses_the_owner_from_the_source_id(
    daemon: Daemon, settings: Settings
) -> None:
    response = daemon.handle(
        Request(
            verb=Verb.DECLARE_SETTINGS,
            source_id="zotero:K",
            payload={
                "settings": [
                    {
                        "name": "section_chars",
                        "type": "int",
                        "default": 1800,
                        "label": "Section size",
                        "help": "h",
                        "min": 600,
                        "max": 4000,
                    }
                ]
            },
        )
    )
    assert response.ok
    assert response.data == {"declared": 1}
    assert settings.get("zotero.section_chars") == 1800


def test_declare_settings_refuses_an_empty_owner(daemon: Daemon) -> None:
    response = daemon.handle(
        Request(verb=Verb.DECLARE_SETTINGS, source_id="", payload={"settings": []})
    )
    assert not response.ok
