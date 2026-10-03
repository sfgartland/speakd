"""The daemon with the OpenRouter engine: choice, unit length and fallback.

The remote engine is the real `OpenRouterEngine` with its HTTP call scripted,
so the daemon exercises the same holdoff and error classification it would
in use; Kokoro is a `FakeEngine`.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from pathlib import Path

import numpy as np
import pytest

from speakd.channels import ChannelTable
from speakd.daemon import Daemon, ProfileView
from speakd.events import Event, EventBus
from speakd.model import Piece, Span
from speakd.pipeline import speak
from speakd.player import RecordingPlayer
from speakd.protocol import Request, Verb
from speakd.settings.registry import Settings
from speakd.settings.store import SettingsStore
from speakd.synth.fake import FakeEngine
from speakd.synth.openrouter_engine import OpenRouterEngine, Reply
from speakd.tempo import Tempo

LONG_TEXT = " ".join(f"This is sentence number {n} of a longer answer." for n in range(1, 9))


def pcm(text: str) -> Reply:
    """A tone whose length tracks the text, loud enough to survive the trim."""
    seconds = max(0.1, len(text) / 15.0)
    t = np.arange(int(seconds * 24000)) / 24000
    wave = (0.5 * np.sin(2 * np.pi * 220 * t) * 32767).astype("<i2")
    return Reply(200, "audio/pcm;rate=24000;channels=1", wave.tobytes())


class Remote:
    """Scripted HTTP: answers with audio, or fails from call `fail_from` on."""

    def __init__(self, fail_from: int | None = None, status: int = 402) -> None:
        self.inputs: list[str] = []
        self.fail_from = fail_from
        self.status = status
        self.lock = threading.Lock()

    def __call__(self, url: str, headers: dict[str, str], body: bytes, timeout: float) -> Reply:
        import json

        text = str(json.loads(body)["input"])
        with self.lock:
            self.inputs.append(text)
            if self.fail_from is not None and len(self.inputs) > self.fail_from:
                return Reply(self.status, "application/json", b'{"error": "no credits"}')
        return pcm(text)


def profile_for(name: str) -> ProfileView:
    return ProfileView(
        voice="af_heart", speed=1.0, interrupt_on=(), prepare=lambda p: (list(p), [])
    )


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(SettingsStore(tmp_path / "settings.toml", tmp_path / "settings-schema.json"))


def build(
    settings: Settings, remote: Remote, *, key: Callable[[], str | None] = lambda: "sk"
) -> tuple[Daemon, FakeEngine, list[Event]]:
    kokoro = FakeEngine()
    d = Daemon(
        kokoro,
        RecordingPlayer(),
        profile_for,
        bus=EventBus(),
        channels=ChannelTable(),
        settings=settings,
        remote=OpenRouterEngine(api_key=key, post=remote, sleep=lambda s: None),
    )
    settings.set("speech.detect_language", False)
    seen: list[Event] = []
    lock = threading.Lock()

    def record(event: Event) -> None:
        with lock:
            seen.append(event)

    d.bus.subscribe(record)
    return d, kokoro, seen


def enqueue(d: Daemon, text: str) -> None:
    d.handle(Request(verb=Verb.ENQUEUE, source_id="s", payload={"text": text}))
    assert d.wait_idle(timeout=10.0)


def of(seen: list[Event], kind: str) -> list[Event]:
    return [e for e in list(seen) if e.kind == kind]


def test_openrouter_speaks_long_units_with_a_short_first_one(settings: Settings) -> None:
    remote = Remote()
    d, kokoro, seen = build(settings, remote)
    settings.set("speech.engine", "openrouter")
    d.start()
    try:
        enqueue(d, LONG_TEXT)
    finally:
        d.stop()
    started = of(seen, "started")
    assert [e.data["engine"] for e in started] == ["openrouter"]
    # The first unit is one sentence, for time to first audio; the rest are
    # merged up to the unit length, so far fewer requests than sentences.
    assert remote.inputs[0] == "This is sentence number 1 of a longer answer."
    assert len(remote.inputs) < 8
    assert all(len(text) <= 300 for text in remote.inputs)
    assert " ".join(remote.inputs) == LONG_TEXT
    assert kokoro.synthesized_langs == []


def test_a_mid_utterance_failure_finishes_on_kokoro_from_the_next_unit(
    settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    remote = Remote(fail_from=1)
    d, kokoro, seen = build(settings, remote)
    settings.set("speech.engine", "openrouter")
    settings.set("speech.openrouter_unit_chars", 60)
    d.start()
    try:
        enqueue(d, LONG_TEXT)
        enqueue(d, "And one more.")
    finally:
        d.stop()
    segments = of(seen, "started")[0].data["segments"]
    assert isinstance(segments, list)
    units = [s["text"] for s in segments]
    positions = of(seen, "position")
    first = [p for p in positions if p.data["index"] == 0]
    assert first[0].data["engine"] == "openrouter"
    # Every unit is still spoken exactly once, in order: the first remotely,
    # the rest locally, starting where the remote engine stopped.
    spoken = [p.data["text"] for p in positions[: len(units)]]
    assert spoken == units
    assert [p.data["engine"] for p in positions[1 : len(units)]] == ["kokoro"] * (len(units) - 1)
    assert any("openrouter" in str(e.data["message"]) for e in of(seen, "error"))
    assert "falling back" in capsys.readouterr().err
    # Held off: the next utterance goes straight to Kokoro, with no request.
    assert of(seen, "started")[1].data["engine"] == "kokoro"
    assert len(remote.inputs) == 2
    finished = of(seen, "finished")
    assert [e.data for e in finished] == [{"cancelled": False, "aborted": False}] * 2


def test_choosing_openrouter_without_a_key_is_refused(settings: Settings) -> None:
    d, _, seen = build(settings, Remote(), key=lambda: None)
    response = d.handle(
        Request(
            verb=Verb.SET_SETTING,
            source_id="",
            payload={"key": "speech.engine", "value": "openrouter"},
        )
    )
    assert not response.ok
    assert "OPENROUTER_API_KEY" in str(response.error)
    assert settings.get("speech.engine") == "kokoro"


def test_status_reports_the_remote_engine(settings: Settings) -> None:
    d, _, _ = build(settings, Remote())
    settings.set("speech.engine", "openrouter")
    response = d.handle(Request(verb=Verb.STATUS, source_id="", payload={}))
    engine = response.data["engine"]
    assert isinstance(engine, dict)
    assert engine["name"] == "openrouter"
    remote = engine["openrouter"]
    assert isinstance(remote, dict) and remote["key"] is True
    languages = response.data["languages"]
    assert isinstance(languages, dict)
    assert "de" in languages["supported"]


def test_a_fixed_speed_engine_is_made_at_one_and_stretched_by_the_player() -> None:
    """Profile speed and tempo both reach the stretch; neither reaches the request."""
    remote = Remote()
    engine = OpenRouterEngine(api_key=lambda: "sk", post=remote)
    made: list[float] = []

    class Stretching(RecordingPlayer):
        def play_at(
            self, audio: np.ndarray, sample_rate: int, *, made_at: float, tempo: Tempo
        ) -> float:
            made.append(made_at)
            self.play(audio, sample_rate)
            return len(audio) / sample_rate

    text = "One sentence. Another one."
    speak(
        [Piece(span=Span(0, len(text)), spoken=text)],
        engine,
        Stretching(),
        speed=1.25,
        tempo=Tempo(1.5),
    )
    assert made == [0.8, 0.8]
