"""Tests for the OpenRouter engine: the request, the audio, and every failure.

The HTTP call is injected, so nothing here touches the network: each test
hands the engine a scripted list of replies (or exceptions) and reads back
what it was asked to send.
"""

from __future__ import annotations

import http.client
import json
from collections.abc import Sequence

import numpy as np
import pytest

from speakd.engines import EngineChoice, choose_engine
from speakd.synth.openrouter_engine import (
    DEFAULT_MODEL,
    FATAL_HOLDOFF_SECONDS,
    TRANSIENT_HOLDOFF_SECONDS,
    OpenRouterEngine,
    RemoteEngineError,
    Reply,
    env_api_key,
)


def tone(seconds: float, rate: int) -> bytes:
    """Loud enough to survive the silence trim: s16le, mono."""
    t = np.arange(int(seconds * rate)) / rate
    wave = (0.5 * np.sin(2 * np.pi * 220 * t) * 32767).astype("<i2")
    return wave.tobytes()


class Script:
    """Answers each POST with the next scripted reply, recording the request."""

    def __init__(self, replies: Sequence[Reply | BaseException]) -> None:
        self.replies = list(replies)
        self.requests: list[tuple[str, dict[str, str], dict[str, object], float]] = []

    def __call__(self, url: str, headers: dict[str, str], body: bytes, timeout: float) -> Reply:
        self.requests.append((url, headers, json.loads(body), timeout))
        reply = self.replies.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        return reply


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def ok(rate: int = 24000, seconds: float = 0.5) -> Reply:
    return Reply(200, f"audio/pcm;rate={rate};channels=1", tone(seconds, rate))


def engine(
    script: Script, *, key: str | None = "sk-test", clock: Clock | None = None
) -> tuple[OpenRouterEngine, list[float]]:
    sleeps: list[float] = []
    return (
        OpenRouterEngine(
            api_key=lambda: key,
            post=script,
            sleep=sleeps.append,
            clock=clock or Clock(),
        ),
        sleeps,
    )


# ---- the request


def test_the_request_names_model_voice_and_pcm_and_never_a_speed() -> None:
    script = Script([ok()])
    e, _ = engine(script)
    e.synthesize("Hello there.", "loongjohn", 1.7, "en")
    url, headers, body, _timeout = script.requests[0]
    assert url.endswith("/api/v1/audio/speech")
    assert headers["Authorization"] == "Bearer sk-test"
    assert body == {
        "model": DEFAULT_MODEL,
        "input": "Hello there.",
        "voice": "loongjohn",
        "response_format": "pcm",
    }


def test_the_model_is_read_per_request() -> None:
    script = Script([ok(), ok()])
    models = iter(["a/one", "b/two"])
    e = OpenRouterEngine(model=lambda: next(models), api_key=lambda: "k", post=script)
    e.synthesize("One.", "v", 1.0)
    e.synthesize("Two.", "v", 1.0)
    assert [r[2]["model"] for r in script.requests] == ["a/one", "b/two"]


def test_blank_text_sends_nothing() -> None:
    script = Script([])
    e, _ = engine(script)
    assert e.synthesize("   ", "v", 1.0).size == 0
    assert script.requests == []


# ---- the audio


def test_pcm_at_the_players_rate_comes_back_as_float_audio() -> None:
    e, _ = engine(Script([ok(24000, 0.5)]))
    audio = e.synthesize("Hello.", "v", 1.0)
    assert audio.dtype == np.float32
    assert 0.45 * 24000 < audio.size <= 0.5 * 24000 + 1
    assert 0.4 < float(np.max(np.abs(audio))) <= 0.5


@pytest.mark.parametrize("rate", [16000, 44100, 48000])
def test_other_rates_are_resampled_to_24k(rate: int) -> None:
    e, _ = engine(Script([ok(rate, 0.5)]))
    audio = e.synthesize("Hello.", "v", 1.0)
    assert abs(audio.size - 12000) < 400


def test_a_missing_rate_is_read_as_24k() -> None:
    e, _ = engine(Script([Reply(200, "audio/pcm", tone(0.5, 24000))]))
    assert abs(e.synthesize("Hello.", "v", 1.0).size - 12000) < 400


def test_a_json_body_on_200_is_an_engine_failure_not_noise() -> None:
    body = json.dumps({"error": {"message": "upstream exploded"}}).encode()
    e, _ = engine(Script([Reply(200, "application/json", body)]))
    with pytest.raises(RemoteEngineError, match="upstream exploded"):
        e.synthesize("Hello.", "v", 1.0)


def test_characters_sent_are_counted() -> None:
    e, _ = engine(Script([ok(), ok()]))
    e.synthesize("Hello.", "v", 1.0)
    e.synthesize("Bye.", "v", 1.0)
    assert e.status()["chars_sent"] == len("Hello.") + len("Bye.")


# ---- failures


def test_a_refused_text_is_one_bad_segment_not_an_engine_failure() -> None:
    body = json.dumps({"error": {"message": "input too long"}}).encode()
    e, _ = engine(Script([Reply(400, "application/json", body)]))
    with pytest.raises(ValueError, match="input too long") as info:
        e.synthesize("Hello.", "v", 1.0)
    assert not isinstance(info.value, RemoteEngineError)
    assert e.available()


@pytest.mark.parametrize("status", [401, 402, 403])
def test_auth_and_credit_failures_are_fatal_and_not_retried(status: int) -> None:
    clock = Clock()
    script = Script([Reply(status, "application/json", b"{}")])
    e, sleeps = engine(script, clock=clock)
    with pytest.raises(RemoteEngineError) as info:
        e.synthesize("Hello.", "v", 1.0)
    assert info.value.fatal
    assert len(script.requests) == 1 and sleeps == []
    assert not e.available()
    clock.now += FATAL_HOLDOFF_SECONDS
    assert e.available()


@pytest.mark.parametrize("status", [429, 500, 502, 503])
def test_momentary_failures_are_retried_once(status: int) -> None:
    script = Script([Reply(status, "", b""), ok()])
    e, sleeps = engine(script)
    assert e.synthesize("Hello.", "v", 1.0).size > 0
    assert len(script.requests) == 2 and len(sleeps) == 1
    assert e.available()


def test_retry_after_is_honoured_but_bounded() -> None:
    script = Script([Reply(429, "", b"", retry_after=60.0), ok()])
    e, sleeps = engine(script)
    e.synthesize("Hello.", "v", 1.0)
    assert sleeps == [3.0]


def test_a_failure_that_outlasts_its_retry_holds_the_engine_off() -> None:
    clock = Clock()
    script = Script([Reply(503, "", b""), Reply(503, "", b"")])
    e, _ = engine(script, clock=clock)
    with pytest.raises(RemoteEngineError) as info:
        e.synthesize("Hello.", "v", 1.0)
    assert not info.value.fatal
    assert not e.available()
    assert e.status()["last_error"] == "HTTP 503"
    clock.now += TRANSIENT_HOLDOFF_SECONDS
    assert e.available()


@pytest.mark.parametrize(
    "exc",
    [ConnectionRefusedError("refused"), TimeoutError("slow"), http.client.IncompleteRead(b"")],
)
def test_no_network_is_retried_then_an_engine_failure(exc: BaseException) -> None:
    script = Script([exc, exc])
    e, _ = engine(script)
    with pytest.raises(RemoteEngineError, match="cannot reach"):
        e.synthesize("Hello.", "v", 1.0)
    assert len(script.requests) == 2


def test_no_key_is_fatal_and_unavailable() -> None:
    e, _ = engine(Script([]), key=None)
    assert not e.available()
    with pytest.raises(RemoteEngineError) as info:
        e.synthesize("Hello.", "v", 1.0)
    assert info.value.fatal


def test_reset_ends_a_holdoff() -> None:
    e, _ = engine(Script([Reply(402, "", b"")]))
    with pytest.raises(RemoteEngineError):
        e.synthesize("Hello.", "v", 1.0)
    assert not e.available()
    e.reset()
    assert e.available()


# ---- the key


def test_the_default_key_is_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", " from-env ")
    assert env_api_key() == "from-env"
    monkeypatch.delenv("OPENROUTER_API_KEY")
    assert env_api_key() is None


# ---- the choice


def test_choose_engine_takes_the_remote_engine_only_when_usable_and_named() -> None:
    def choose(setting: str, voice: str | None, lang: str = "en") -> object:
        return choose_engine(
            lang,
            setting,
            {},
            lambda v: False,
            False,
            True,
            remote_voice=voice,
            remote_languages=("en", "de"),
        )

    assert choose("openrouter", "loongjohn") == EngineChoice("openrouter", "loongjohn")
    assert choose("openrouter", "loongjohn", "de") == EngineChoice("openrouter", "loongjohn")
    # Held off or keyless: local, as though it were never configured.
    assert choose("openrouter", None) == EngineChoice("kokoro", None)
    # A language it does not speak falls through.
    assert choose("openrouter", "loongjohn", "hi") == EngineChoice("kokoro", None)
    # Not named by the setting: never chosen.
    assert choose("kokoro", "loongjohn") == EngineChoice("kokoro", None)


# ---- a hush while the request is in flight


def test_a_cancelled_utterance_is_not_retried_or_slept_for() -> None:
    script = Script([Reply(503, "", b""), ok()])
    e, sleeps = engine(script)
    with pytest.raises(RemoteEngineError):
        e.synthesize("Hello.", "v", 1.0, cancelled=lambda: True)
    assert len(script.requests) == 1 and sleeps == []
    # The failure was the hush's doing, not evidence about the network.
    assert e.available()


def test_a_cancelled_utterance_is_not_retried_after_a_dropped_socket() -> None:
    script = Script([ConnectionResetError("reset"), ok()])
    e, sleeps = engine(script)
    with pytest.raises(RemoteEngineError):
        e.synthesize("Hello.", "v", 1.0, cancelled=lambda: True)
    assert len(script.requests) == 1 and sleeps == []
    assert e.available()
