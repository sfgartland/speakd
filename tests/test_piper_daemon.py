"""Tests for the daemon's per-utterance engine choice (Piper engine plan, Task 4).

Two fakes stand in for the two engines: a plain `FakeEngine` is Kokoro, and a
`FakePiperEngine` -- a `FakeEngine` plus the voice hooks the daemon's Piper
seam calls -- is Piper. The daemon is built with a real `Settings` (tmp_path
store, like `test_language_resolution.py`), so the settings are exercised the
way a real daemon reads them.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import cast

import numpy as np
import pytest

from speakd.channels import ChannelTable
from speakd.daemon import Daemon, ProfileView
from speakd.events import Event, EventBus
from speakd.player import FakeSink, Player, RecordingPlayer, StreamingPlayer
from speakd.protocol import Request, Verb
from speakd.settings.registry import Settings
from speakd.settings.store import SettingsStore
from speakd.synth import Synthesizer
from speakd.synth.fake import FakeEngine
from speakd.synth.lazy import LazyEngine
from speakd.synth.piper_engine import PiperVoiceError


class CountingEngine(FakeEngine):
    """A FakeEngine that records every text it was asked to synthesise."""

    def __init__(self) -> None:
        super().__init__()
        self.texts: list[str] = []

    def synthesize(self, text: str, voice: str, speed: float, lang: str = "en") -> np.ndarray:
        self.texts.append(text)
        return super().synthesize(text, voice, speed, lang)


class FakePiperEngine(FakeEngine):
    """Stands in for `PiperEngine`: a FakeEngine with the voice hooks the daemon uses.

    `voices` is the set of installed voice names -- removing one is how a test
    simulates a voice file deleted between utterances. `raise_errors` says how
    many of the next synthesize calls fail with `PiperVoiceError`, standing in
    for a corrupt `.onnx` the real engine cannot load.
    """

    def __init__(self) -> None:
        super().__init__()
        self.voices: set[str] = {"voice-en", "voice-de"}
        self.unloaded = 0
        self.voice_dirs: list[Path] = []
        self.raise_errors = 0
        self.texts: list[str] = []

    def has_voice(self, voice: str) -> bool:
        return voice in self.voices

    def installed_voices(self) -> list[str]:
        return sorted(self.voices)

    def unload(self) -> None:
        self.unloaded += 1

    def set_voice_dir(self, path: Path) -> None:
        self.voice_dirs.append(path)

    def synthesize(self, text: str, voice: str, speed: float, lang: str = "en") -> np.ndarray:
        self.texts.append(text)
        if self.raise_errors:
            self.raise_errors -= 1
            raise PiperVoiceError(voice, "the model file would not load")
        return super().synthesize(text, voice, speed, lang)


class SlowSink(FakeSink):
    """Takes real time per block, so an utterance is in flight long enough to act on."""

    def write(self, frames: np.ndarray) -> None:
        super().write(frames)
        time.sleep(len(frames) / 24000 / 20)  # twenty times real time


def until(predicate, timeout: float = 5.0) -> bool:  # type: ignore[no-untyped-def]
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.002)
    return bool(predicate())


def profile_for(name: str) -> ProfileView:
    return ProfileView(
        voice="af_heart", speed=1.0, interrupt_on=(), prepare=lambda p: (list(p), [])
    )


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(SettingsStore(tmp_path / "settings.toml", tmp_path / "settings-schema.json"))


@pytest.fixture
def piper_available(monkeypatch: pytest.MonkeyPatch) -> None:
    """The `piper` module is importable, as the daemon's `piper_available()` reads it."""
    monkeypatch.setattr("speakd.daemon.piper_available", lambda: True)


def build(
    settings: Settings,
    kokoro: Synthesizer,
    piper: FakePiperEngine | None = None,
    *,
    engine: str = "kokoro",
    player: Player | None = None,
    render_piper: FakePiperEngine | None = None,
) -> tuple[Daemon, list[Event]]:
    """A daemon with both engines, recording every event on a locked list."""
    d = Daemon(
        kokoro,
        player if player is not None else RecordingPlayer(),
        profile_for,
        bus=EventBus(),
        channels=ChannelTable(),
        settings=settings,
        piper=piper,
        render_piper=render_piper,
    )
    if piper is not None:
        settings.set("speech.piper_voices", {"en": "voice-en", "de": "voice-de"})
    settings.set("speech.detect_language", False)
    if engine != "kokoro":
        settings.set("speech.engine", engine)
    seen: list[Event] = []
    lock = threading.Lock()

    def record(event: Event) -> None:
        with lock:
            seen.append(event)

    d.bus.subscribe(record)
    return d, seen


def enqueue(d: Daemon, text: str, **payload: object) -> None:
    d.handle(Request(verb=Verb.ENQUEUE, source_id="s", payload={"text": text, **payload}))
    assert d.wait_idle(timeout=10.0)


def started_events(seen: list[Event]) -> list[Event]:
    return [e for e in list(seen) if e.kind == "started"]


def position_events(seen: list[Event]) -> list[Event]:
    return [e for e in list(seen) if e.kind == "position"]


# ---- the switch reaches the queue, one utterance at a time (Review Focus 4)


def test_switching_the_engine_mid_queue_starts_the_next_utterance_on_piper(
    settings: Settings, piper_available: None
) -> None:
    kokoro = FakeEngine()
    piper = FakePiperEngine()
    player = StreamingPlayer(SlowSink(), chunk_frames=512)
    d, seen = build(settings, kokoro, piper, player=player)
    d.start()
    try:
        d.handle(Request(verb=Verb.ENQUEUE, source_id="s", payload={"text": "One. Two."}))
        assert until(lambda: len(position_events(seen)) >= 1)
        settings.set("speech.engine", "piper")
        d.handle(Request(verb=Verb.ENQUEUE, source_id="s", payload={"text": "Three."}))
        assert d.wait_idle(timeout=10.0)
    finally:
        d.stop()
    started = started_events(seen)
    assert [e.data["engine"] for e in started] == ["kokoro", "piper"]
    assert [e.data["engine"] for e in position_events(seen)] == ["kokoro", "kokoro", "piper"]
    assert kokoro.synthesized_langs == ["en", "en"]
    assert piper.texts == ["Three."]


def test_replay_after_a_switch_reuses_the_cached_audio(
    settings: Settings, piper_available: None
) -> None:
    kokoro = CountingEngine()
    piper = FakePiperEngine()
    player = StreamingPlayer(SlowSink(), chunk_frames=512)
    d, seen = build(settings, kokoro, piper, player=player)
    d.start()
    try:
        d.handle(Request(verb=Verb.ENQUEUE, source_id="s", payload={"text": "One. Two."}))
        assert until(
            lambda: [cast(int, e.data["index"]) for e in position_events(seen)][-1:] == [1]
        )
        made = len(kokoro.texts)
        settings.set("speech.engine", "piper")
        response = d.handle(Request(verb=Verb.REPLAY, source_id="gui", payload={"index": 0}))
        assert response.ok
        assert d.wait_idle(timeout=10.0)
    finally:
        d.stop()
    assert len(kokoro.texts) == made
    assert piper.texts == []


# ---- the fallback to Kokoro


def test_a_language_piper_has_no_voice_for_is_spoken_by_kokoro(
    settings: Settings, piper_available: None
) -> None:
    kokoro = FakeEngine()
    piper = FakePiperEngine()
    d, seen = build(settings, kokoro, piper, engine="piper")
    d.start()
    try:
        enqueue(d, "Olá.", lang="pt-br")
    finally:
        d.stop()
    assert [e.data["engine"] for e in started_events(seen)] == ["kokoro"]
    assert kokoro.synthesized_langs == ["pt-br"]
    assert piper.texts == []


def test_kokoro_unloaded_with_an_unmapped_language_is_declined(
    settings: Settings, piper_available: None
) -> None:
    kokoro = LazyEngine(FakeEngine, sample_rate=24000)
    piper = FakePiperEngine()
    d, seen = build(settings, kokoro, piper, engine="piper")
    d.start()
    try:
        enqueue(d, "Olá.", lang="pt-br")
    finally:
        d.stop()
    declined = [e for e in list(seen) if e.kind == "declined"]
    assert declined[-1].data == {"reason": "no voice for pt-br"}
    finished = [e for e in list(seen) if e.kind == "finished"]
    assert finished[-1].data == {"cancelled": False, "aborted": True}
    assert started_events(seen) == []
    assert piper.texts == []


def test_kokoro_unloaded_still_speaks_when_piper_is_chosen(
    settings: Settings, piper_available: None
) -> None:
    kokoro = LazyEngine(FakeEngine, sample_rate=24000)
    piper = FakePiperEngine()
    d, seen = build(settings, kokoro, piper, engine="piper")
    d.start()
    try:
        enqueue(d, "Hello.")
    finally:
        d.stop()
    assert [e.data["engine"] for e in started_events(seen)] == ["piper"]
    assert piper.texts == ["Hello."]
    assert [e for e in list(seen) if e.kind == "declined"] == []
    finished = [e for e in list(seen) if e.kind == "finished"]
    assert finished[-1].data == {"cancelled": False, "aborted": False}


def test_a_voice_file_removed_between_utterances_falls_back_to_kokoro(
    settings: Settings, piper_available: None
) -> None:
    kokoro = FakeEngine()
    piper = FakePiperEngine()
    d, seen = build(settings, kokoro, piper, engine="piper")
    d.start()
    try:
        enqueue(d, "Hello one.")
        piper.voices.discard("voice-en")
        enqueue(d, "Hello two.")
    finally:
        d.stop()
    assert [e.data["engine"] for e in started_events(seen)] == ["piper", "kokoro"]
    assert piper.texts == ["Hello one."]
    assert kokoro.synthesized_langs == ["en"]


def test_a_corrupt_piper_voice_falls_back_to_kokoro_and_the_next_utterance_retries(
    settings: Settings, piper_available: None, capsys: pytest.CaptureFixture[str]
) -> None:
    kokoro = FakeEngine()
    piper = FakePiperEngine()
    piper.raise_errors = 1
    d, seen = build(settings, kokoro, piper, engine="piper")
    d.start()
    try:
        enqueue(d, "Hello one.")
        enqueue(d, "Hello two.")
    finally:
        d.stop()
    err = capsys.readouterr().err
    assert len(err.strip().splitlines()) == 1
    assert "voice-en" in err and "kokoro" in err
    assert piper.texts == ["Hello one.", "Hello two."]
    assert kokoro.synthesized_langs == ["en"]
    positions = position_events(seen)
    assert positions[0].data["engine"] == "kokoro"
    assert positions[-1].data["engine"] == "piper"
    finished = [e for e in list(seen) if e.kind == "finished"]
    assert [e.data for e in finished] == [
        {"cancelled": False, "aborted": False},
        {"cancelled": False, "aborted": False},
    ]


# ---- the refusal (Review Focus 2)


def test_setting_engine_to_piper_without_a_piper_engine_is_refused(
    settings: Settings,
) -> None:
    kokoro = FakeEngine()
    d, seen = build(settings, kokoro, piper=None)
    response = d.handle(
        Request(
            verb=Verb.SET_SETTING, source_id="", payload={"key": "speech.engine", "value": "piper"}
        )
    )
    assert not response.ok
    assert response.error == "piper-tts is not installed (pip install 'speakd[piper]')"
    assert settings.get("speech.engine") == "kokoro"
    assert [e for e in seen if e.kind == "setting"] == []


def test_setting_engine_to_piper_without_the_module_is_refused(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("speakd.daemon.piper_available", lambda: False)
    kokoro = FakeEngine()
    piper = FakePiperEngine()
    d, seen = build(settings, kokoro, piper)
    response = d.handle(
        Request(
            verb=Verb.SET_SETTING, source_id="", payload={"key": "speech.engine", "value": "piper"}
        )
    )
    assert not response.ok
    assert response.error == "piper-tts is not installed (pip install 'speakd[piper]')"
    assert settings.get("speech.engine") == "kokoro"
    assert piper.unloaded == 0
    assert [e for e in seen if e.kind == "setting"] == []


# ---- the status additions


def _dict(value: object) -> dict[str, object]:
    assert isinstance(value, dict)
    return value


def _supported(value: object) -> list[str]:
    assert isinstance(value, list)
    return value


def test_status_reports_the_piper_engine_and_its_languages(
    settings: Settings, piper_available: None
) -> None:
    kokoro = FakeEngine(supported=["en", "fr"])
    piper = FakePiperEngine()
    d, _seen = build(settings, kokoro, piper, engine="piper")
    status = d.handle(Request(verb=Verb.STATUS, source_id=""))
    engine = _dict(status.data["engine"])
    languages_status = _dict(status.data["languages"])
    assert engine["name"] == "piper"
    assert engine["piper"] == {"available": True, "voices": ["voice-de", "voice-en"]}
    assert "de" in _supported(languages_status["supported"])
    settings.set("speech.engine", "kokoro")
    status = d.handle(Request(verb=Verb.STATUS, source_id=""))
    languages_status = _dict(status.data["languages"])
    assert _dict(status.data["engine"])["name"] == "kokoro"
    assert "de" not in _supported(languages_status["supported"])
    assert _dict(status.data["engine"])["piper"] == {
        "available": True,
        "voices": ["voice-de", "voice-en"],
    }


def test_status_without_a_piper_engine_says_it_is_unavailable(
    settings: Settings,
) -> None:
    kokoro = FakeEngine()
    d, _seen = build(settings, kokoro, piper=None)
    status = d.handle(Request(verb=Verb.STATUS, source_id=""))
    engine = _dict(status.data["engine"])
    assert engine["name"] == "kokoro"
    assert engine["piper"] == {"available": False, "voices": []}


# ---- the switch hooks


def test_switching_away_from_piper_unloads_it(settings: Settings, piper_available: None) -> None:
    kokoro = FakeEngine()
    piper = FakePiperEngine()
    d, _seen = build(settings, kokoro, piper, engine="piper")
    response = d.handle(
        Request(
            verb=Verb.SET_SETTING, source_id="", payload={"key": "speech.engine", "value": "kokoro"}
        )
    )
    assert response.ok
    assert piper.unloaded == 1
    # The same value again is not a change away from piper.
    d.handle(
        Request(
            verb=Verb.SET_SETTING, source_id="", payload={"key": "speech.engine", "value": "kokoro"}
        )
    )
    assert piper.unloaded == 1
    # Back to piper and away again unloads again.
    d.handle(
        Request(
            verb=Verb.SET_SETTING, source_id="", payload={"key": "speech.engine", "value": "piper"}
        )
    )
    assert piper.unloaded == 1
    d.handle(
        Request(
            verb=Verb.SET_SETTING, source_id="", payload={"key": "speech.engine", "value": "kokoro"}
        )
    )
    assert piper.unloaded == 2


def test_changing_the_piper_voice_directory_reaches_the_engine(
    settings: Settings, piper_available: None
) -> None:
    kokoro = FakeEngine()
    piper = FakePiperEngine()
    d, _seen = build(settings, kokoro, piper, engine="piper")
    response = d.handle(
        Request(
            verb=Verb.SET_SETTING,
            source_id="",
            payload={"key": "speech.piper_voice_dir", "value": "/some/other/dir"},
        )
    )
    assert response.ok
    assert piper.voice_dirs == [Path("/some/other/dir")]


def test_changing_the_piper_voice_directory_reaches_both_engines_once(
    settings: Settings, piper_available: None
) -> None:
    kokoro = FakeEngine()
    piper = FakePiperEngine()
    render_piper = FakePiperEngine()
    d, _seen = build(settings, kokoro, piper, engine="piper", render_piper=render_piper)
    response = d.handle(
        Request(
            verb=Verb.SET_SETTING,
            source_id="",
            payload={"key": "speech.piper_voice_dir", "value": "/some/other/dir"},
        )
    )
    assert response.ok
    assert piper.voice_dirs == [Path("/some/other/dir")]
    assert render_piper.voice_dirs == [Path("/some/other/dir")]
    # The same value again is not a change: neither engine is repointed.
    d.handle(
        Request(
            verb=Verb.SET_SETTING,
            source_id="",
            payload={"key": "speech.piper_voice_dir", "value": "/some/other/dir"},
        )
    )
    assert piper.voice_dirs == [Path("/some/other/dir")]
    assert render_piper.voice_dirs == [Path("/some/other/dir")]
