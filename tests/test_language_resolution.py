"""Tests for the daemon's per-utterance language resolution (design §2).

Uses `Settings` backed by a real (tmp_path) store, like test_settings_verbs.py,
so `speech.voices` and `speech.unsupported_language` are exercised through
the same path a real daemon reads them on.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from speakd.channels import ChannelTable
from speakd.daemon import Daemon, ProfileView
from speakd.events import Event, EventBus
from speakd.player import RecordingPlayer
from speakd.protocol import Request, Verb
from speakd.settings.registry import Settings
from speakd.settings.store import SettingsStore
from speakd.synth.fake import FakeEngine


def profile_for(name: str) -> ProfileView:
    return ProfileView(
        voice="af_heart", speed=1.1, interrupt_on=(), prepare=lambda p: (list(p), [])
    )


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(SettingsStore(tmp_path / "settings.toml", tmp_path / "settings-schema.json"))


def build(settings: Settings, engine: FakeEngine | None = None) -> tuple[Daemon, RecordingPlayer]:
    player = RecordingPlayer()
    d = Daemon(
        engine or FakeEngine(),
        player,
        profile_for,
        bus=EventBus(),
        channels=ChannelTable(),
        settings=settings,
    )
    return d, player


def enqueue(d: Daemon, source: str, text: str, **payload: object) -> None:
    d.handle(Request(verb=Verb.ENQUEUE, source_id=source, payload={"text": text, **payload}))
    assert d.wait_idle(timeout=5.0)


def started_events(events: list[Event]) -> list[Event]:
    return [e for e in events if e.kind == "started"]


# ---- resolution order: payload > channel > detected > default ----


def test_payload_lang_wins_over_everything(settings: Settings) -> None:
    engine = FakeEngine()
    d, _player = build(settings, engine)
    events: list[Event] = []
    d.bus.subscribe(events.append)
    d.handle(Request(Verb.SET_LANGUAGE, "s", {"lang": "es"}))
    settings.set("speech.detect_language", False)
    d.start()
    try:
        enqueue(d, "s", "Hello there.", lang="fr")
    finally:
        d.stop()
    assert engine.synthesized_langs == ["fr"]
    assert started_events(events)[-1].data["lang"] == "fr"


def test_channel_lang_wins_when_no_payload_lang_is_given(settings: Settings) -> None:
    engine = FakeEngine()
    d, _player = build(settings, engine)
    settings.set("speech.detect_language", False)
    d.handle(Request(Verb.SET_LANGUAGE, "s", {"lang": "es"}))
    d.start()
    try:
        enqueue(d, "s", "Hello there.")
    finally:
        d.stop()
    assert engine.synthesized_langs == ["es"]


def test_detection_is_used_when_neither_payload_nor_channel_says(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = FakeEngine()
    d, _player = build(settings, engine)
    monkeypatch.setattr(d._detector, "detect", lambda text: "it")
    d.start()
    try:
        enqueue(d, "s", "Questo testo e abbastanza lungo per essere rilevato.")
    finally:
        d.stop()
    assert engine.synthesized_langs == ["it"]


def test_unparseable_payload_lang_does_not_beat_detection(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A payload lang that does not even look like a language tag (free text,
    # a typo) must be treated as though none was given, so detection still
    # gets to run and win -- see languages.normalise (Review Focus #12).
    engine = FakeEngine()
    d, _player = build(settings, engine)
    monkeypatch.setattr(d._detector, "detect", lambda text: "it")
    d.start()
    try:
        enqueue(
            d,
            "s",
            "Questo testo e abbastanza lungo per essere rilevato.",
            lang="please speak italian",
        )
    finally:
        d.stop()
    assert engine.synthesized_langs == ["it"]


def test_default_is_used_when_nothing_else_says(settings: Settings) -> None:
    engine = FakeEngine()
    d, _player = build(settings, engine)
    settings.set("speech.detect_language", False)
    d.start()
    try:
        enqueue(d, "s", "Hello there.")
    finally:
        d.stop()
    assert engine.synthesized_langs == ["en"]


def test_default_is_used_when_detection_finds_nothing(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = FakeEngine()
    d, _player = build(settings, engine)
    monkeypatch.setattr(d._detector, "detect", lambda text: None)
    d.start()
    try:
        enqueue(d, "s", "Hello there, this is a normal sentence.")
    finally:
        d.stop()
    assert engine.synthesized_langs == ["en"]


def test_a_different_default_language_is_honoured(settings: Settings) -> None:
    engine = FakeEngine()
    d, _player = build(settings, engine)
    settings.set("speech.detect_language", False)
    settings.set("speech.default_language", "fr")
    d.start()
    try:
        enqueue(d, "s", "Hello there.")
    finally:
        d.stop()
    assert engine.synthesized_langs == ["fr"]


# ---- voice choice ----


def test_the_profiles_voice_is_kept_when_it_matches_the_resolved_language(
    settings: Settings,
) -> None:
    engine = FakeEngine()
    d, player = build(settings, engine)
    settings.set("speech.detect_language", False)
    d.start()
    try:
        enqueue(d, "s", "Hello there.", lang="en")
    finally:
        d.stop()
    assert player.played  # af_heart is the profile's own voice, and en is af's language


def test_the_voices_setting_is_used_when_the_profiles_voice_does_not_match(
    settings: Settings,
) -> None:
    engine = FakeEngine()
    d, _player = build(settings, engine)
    settings.set("speech.detect_language", False)
    captured: list[str] = []
    orig = engine.synthesize

    def spy(text: str, voice: str, speed: float, lang: str = "en") -> np.ndarray:
        captured.append(voice)
        return orig(text, voice, speed, lang)

    engine.synthesize = spy  # type: ignore[method-assign]
    d.start()
    try:
        # profile's voice is af_heart (en); ask for fr, whose default voice
        # is ff_siwis.
        enqueue(d, "s", "Bonjour.", lang="fr")
    finally:
        d.stop()
    assert captured == ["ff_siwis"]


def test_a_custom_voices_map_is_honoured(settings: Settings) -> None:
    engine = FakeEngine()
    d, _player = build(settings, engine)
    settings.set("speech.detect_language", False)
    settings.set("speech.voices", {**settings.get("speech.voices"), "fr": "ff_custom"})  # type: ignore[dict-item]
    captured: list[str] = []
    orig = engine.synthesize

    def spy(text: str, voice: str, speed: float, lang: str = "en") -> np.ndarray:
        captured.append(voice)
        return orig(text, voice, speed, lang)

    engine.synthesize = spy  # type: ignore[method-assign]
    d.start()
    try:
        enqueue(d, "s", "Bonjour.", lang="fr")
    finally:
        d.stop()
    assert captured == ["ff_custom"]


# ---- unsupported languages ----


def test_unsupported_default_mode_falls_back_and_announces_it(settings: Settings) -> None:
    engine = FakeEngine(supported=["en", "fr"])
    d, _player = build(settings, engine)
    settings.set("speech.detect_language", False)
    events: list[Event] = []
    d.bus.subscribe(events.append)
    d.start()
    try:
        enqueue(d, "s", "Hallo.", lang="de")
    finally:
        d.stop()
    assert engine.synthesized_langs == ["en"]
    language_events = [e for e in events if e.kind == "language"]
    assert language_events[-1].data == {"requested": "de", "used": "en", "reason": "unsupported"}


def test_unsupported_decline_mode_speaks_nothing(settings: Settings) -> None:
    engine = FakeEngine(supported=["en", "fr"])
    d, player = build(settings, engine)
    settings.set("speech.detect_language", False)
    settings.set("speech.unsupported_language", "decline")
    events: list[Event] = []
    d.bus.subscribe(events.append)
    d.start()
    try:
        enqueue(d, "s", "Hallo.", lang="de")
    finally:
        d.stop()
    assert engine.synthesized_langs == []
    assert player.played == []
    declined = [e for e in events if e.kind == "declined"]
    assert declined[-1].data == {"reason": "no voice for de"}
    finished = [e for e in events if e.kind == "finished"]
    assert finished[-1].data == {"cancelled": False, "aborted": True}
    assert not any(e.kind == "started" for e in events)


# ---- started.lang and status.languages ----


def test_started_carries_the_resolved_lang(settings: Settings) -> None:
    engine = FakeEngine()
    d, _player = build(settings, engine)
    settings.set("speech.detect_language", False)
    events: list[Event] = []
    d.bus.subscribe(events.append)
    d.start()
    try:
        enqueue(d, "s", "Bonjour.", lang="fr")
    finally:
        d.stop()
    assert started_events(events)[-1].data["lang"] == "fr"


def test_status_reports_supported_default_and_detect(settings: Settings) -> None:
    engine = FakeEngine(supported=["en", "fr"])
    d, _player = build(settings, engine)
    settings.set("speech.detect_language", False)
    response = d.handle(Request(Verb.STATUS, "", {}))
    assert response.data["languages"] == {
        "supported": ["en", "fr"],
        "default": "en",
        "detect": False,
    }


# ---- speech.voices changing mid-flight ----


def test_voices_changed_between_two_utterances_takes_effect(settings: Settings) -> None:
    engine = FakeEngine()
    d, _player = build(settings, engine)
    settings.set("speech.detect_language", False)
    captured: list[str] = []
    orig = engine.synthesize

    def spy(text: str, voice: str, speed: float, lang: str = "en") -> np.ndarray:
        captured.append(voice)
        return orig(text, voice, speed, lang)

    engine.synthesize = spy  # type: ignore[method-assign]
    d.start()
    try:
        enqueue(d, "s", "Bonjour un.", lang="fr")
        settings.set("speech.voices", {**settings.get("speech.voices"), "fr": "ff_second"})  # type: ignore[dict-item]
        enqueue(d, "s", "Bonjour deux.", lang="fr")
    finally:
        d.stop()
    assert captured == ["ff_siwis", "ff_second"]


# ---- UnsupportedLanguage raised mid-synthesis ----


def test_unsupported_language_raised_mid_synthesis_falls_back(settings: Settings) -> None:
    # The static check (engine.supported_languages()) says fr is fine, but
    # the engine's first pipeline for it still fails to build -- Review
    # Focus: this must fall back, not kill the worker.
    engine = FakeEngine(supported=["en", "fr"], raise_for=["fr"])
    d, _player = build(settings, engine)
    settings.set("speech.detect_language", False)
    events: list[Event] = []
    d.bus.subscribe(events.append)
    d.start()
    try:
        enqueue(d, "s", "Bonjour.", lang="fr")
    finally:
        d.stop()
    assert engine.synthesized_langs == ["fr", "en"]
    language_events = [e for e in events if e.kind == "language"]
    assert language_events[-1].data == {"requested": "fr", "used": "en", "reason": "unsupported"}
    finished = [e for e in events if e.kind == "finished"]
    assert finished[-1].data == {"cancelled": False, "aborted": False}


def test_unsupported_language_raised_mid_synthesis_does_not_kill_the_worker(
    settings: Settings,
) -> None:
    engine = FakeEngine(supported=["en", "fr"], raise_for=["fr"])
    d, _player = build(settings, engine)
    settings.set("speech.detect_language", False)
    d.start()
    try:
        enqueue(d, "s", "Bonjour.", lang="fr")
        # A second, ordinary utterance must still be spoken: the worker is
        # alive, not dead behind the first job's failure.
        enqueue(d, "s", "Hello again.", lang="en")
    finally:
        d.stop()
    assert engine.synthesized_langs == ["fr", "en", "en"]
