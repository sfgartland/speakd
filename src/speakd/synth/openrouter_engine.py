"""OpenRouter adapter: a hosted TTS model behind OpenAI's speech API.

The first remote engine. OpenRouter serves text-to-speech at
`POST /api/v1/audio/speech` in the shape of OpenAI's speech endpoint --
`model`, `input`, `voice`, `response_format` -- and answers with raw audio,
not JSON. We ask for `pcm` (16-bit little-endian mono); its `Content-Type`
names the rate (`audio/pcm;rate=24000;channels=1`), and anything that is not
the player's 24 kHz is resampled here, so the sink never has to reopen.

Nothing is imported beyond the standard library and numpy: the request is a
plain `urllib` POST, so this engine costs no extra and `test_import_cost.py`
stays green. The HTTP call is injectable (`post`) so tests never touch the
network.

**Speed.** Qwen's TTS ignores `speed`, and some providers reject a non-default
value outright, so it is never sent. The engine always makes speed-1.0 audio
and says so through `fixed_speed`: the pipeline then records the audio as made
at tempo 1.0 and the player's time-stretch does the rest. That is also what
keeps a tempo change from costing a single new request -- the cached audio is
stretched rather than synthesised again.

**Failure.** Two kinds, kept apart because the daemon must treat them apart:

- A request the provider refused for *this text* (400, 413, 422) raises an
  ordinary exception, which the pipeline already records as one bad segment.
- Anything that says the engine itself is unusable right now -- no key, a key
  refused (401/403), credits gone (402), rate limiting or an upstream outage
  that outlasts one retry (429, 5xx), or no network at all -- raises
  `RemoteEngineError`. The pipeline stops on it the way it stops on a Piper
  voice that will not load, and the daemon says the rest of the utterance with
  a local engine. Speech must never go quiet because the network did.

After such a failure the engine marks itself unavailable for a while
(`available()`), so the next utterances go straight to the local engine
instead of each paying a timeout to rediscover the same outage.
"""

from __future__ import annotations

import http.client
import json
import os
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from math import gcd

import numpy as np

from speakd.synth.audio import trim_silence

ENDPOINT = "https://openrouter.ai/api/v1/audio/speech"

DEFAULT_MODEL = "qwen/qwen-audio-3.0-tts-flash"
DEFAULT_VOICE = "loongjohn"

# What Qwen-Audio-3.0-TTS speaks, as this project's codes. It detects the
# language from the text itself, so none of this is sent: it is what the
# daemon's language check reads before choosing the engine.
SUPPORTED: tuple[str, ...] = (
    "en",
    "en-gb",
    "de",
    "fr",
    "es",
    "it",
    "pt-br",
    "ru",
    "ja",
    "ko",
    "zh",
    "ar",
)

# A request's whole life, connect to last byte. Long enough for a few
# hundred characters on a slow upstream; short enough that a stalled
# connection falls back to the local engine before the listener gives up.
TIMEOUT_SECONDS = 20.0

# One retry for what is plausibly momentary (429, 5xx, a dropped socket),
# after at most this long. Bounded hard: the listener is hearing silence for
# the whole of it, and the local engine is the better answer to anything that
# outlasts a couple of seconds.
RETRY_DELAY_SECONDS = 1.0
MAX_RETRY_DELAY_SECONDS = 3.0

# How long the engine stays out of the running after failing. A transient
# outage gets a short holdoff; a refused key or empty account will not mend
# itself, so it is held off longer -- `reset()` (a settings change) ends
# either at once.
TRANSIENT_HOLDOFF_SECONDS = 60.0
FATAL_HOLDOFF_SECONDS = 600.0

# Statuses that mean the request itself, not the engine, was at fault.
_TEXT_FAULTS = frozenset({400, 404, 413, 422})
# Statuses that will not get better by asking again.
_FATAL = frozenset({401, 402, 403})

KEY_ENV = "OPENROUTER_API_KEY"

# The `secret` setting the key is entered in (see `speakd.settings.secrets`
# for how a secret is kept: its own 0600 file, never reported back).
KEY_SETTING = "speech.openrouter_api_key"

NO_KEY = f"no OpenRouter key: enter one in Settings ({KEY_SETTING}) or set {KEY_ENV}"


def env_api_key() -> str | None:
    """`$OPENROUTER_API_KEY`, or None. The daemon tries it before the setting."""
    return os.environ.get(KEY_ENV, "").strip() or None


class RemoteEngineError(Exception):
    """The remote engine cannot speak right now, and why.

    `fatal` separates "will not mend by itself" (a key refused, credits gone,
    no key at all) from an outage that may. Both stop the utterance's remote
    synthesis; only the holdoff differs.
    """

    def __init__(self, reason: str, *, fatal: bool = False) -> None:
        super().__init__(reason)
        self.reason = reason
        self.fatal = fatal


@dataclass(frozen=True)
class Reply:
    """What one POST came back with: status, content type and body."""

    status: int
    content_type: str
    body: bytes
    retry_after: float | None = None


# A request: URL, headers, JSON body, timeout -> reply. Raises OSError for a
# failure below HTTP (DNS, refused, reset, timeout), or http.client's
# HTTPException for a reply cut short.
Post = Callable[[str, dict[str, str], bytes, float], Reply]


def _urllib_post(url: str, headers: dict[str, str], body: bytes, timeout: float) -> Reply:
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return Reply(
                status=response.status,
                content_type=response.headers.get("Content-Type", ""),
                body=response.read(),
            )
    except urllib.error.HTTPError as exc:
        retry_after: float | None = None
        raw = exc.headers.get("Retry-After") if exc.headers is not None else None
        if raw:
            try:
                retry_after = float(raw)
            except ValueError:
                retry_after = None
        try:
            payload = exc.read()
        except OSError:
            payload = b""
        return Reply(
            status=exc.code,
            content_type=exc.headers.get("Content-Type", "") if exc.headers is not None else "",
            body=payload,
            retry_after=retry_after,
        )


def _pcm_rate(content_type: str, default: int) -> int:
    """The `rate=` parameter of a PCM content type, else `default`."""
    for part in content_type.split(";")[1:]:
        name, _, value = part.strip().partition("=")
        if name.strip().lower() == "rate":
            try:
                return int(value.strip())
            except ValueError:
                break
    return default


def _resample(audio: np.ndarray, rate: int, target: int) -> np.ndarray:
    """`audio` at `rate`, brought to `target`.

    scipy's polyphase filter when it is installed (it comes with both local
    engines' extras); linear interpolation otherwise, which is fine for
    speech going up in rate and only mildly aliased coming down.
    """
    if rate == target or audio.size == 0:
        return audio
    try:
        from scipy.signal import resample_poly
    except ImportError:
        count = round(audio.size * target / rate)
        positions = np.arange(count, dtype=np.float64) * (rate / target)
        return np.asarray(np.interp(positions, np.arange(audio.size), audio), dtype=np.float32)
    step = gcd(target, rate)
    return np.asarray(resample_poly(audio, target // step, rate // step), dtype=np.float32)


def _error_message(reply: Reply) -> str:
    """The provider's own message when the body carries one, else the status."""
    try:
        data = json.loads(reply.body.decode("utf-8", "replace"))
    except ValueError:
        data = None
    if isinstance(data, dict):
        error = data.get("error")
        if isinstance(error, dict) and isinstance(error.get("message"), str):
            return f"HTTP {reply.status}: {error['message']}"
        if isinstance(error, str):
            return f"HTTP {reply.status}: {error}"
    return f"HTTP {reply.status}"


class OpenRouterEngine:
    """A hosted TTS model through OpenRouter, one request per unit."""

    name = "openrouter"
    sample_rate = 24000
    # Qwen says years as people do; nothing to rewrite first.
    reads_years = True
    # Speed is the player's business here, never the request's; see the
    # module docstring.
    fixed_speed = True
    # Network I/O, not a model in this process: nothing to serialise behind
    # the synthesis lock, and a request must not make a render or a live
    # sentence wait behind it. The pipeline reads this.
    remote = True

    def __init__(
        self,
        model: Callable[[], str] = lambda: DEFAULT_MODEL,
        *,
        # Called on every request, never cached, so a key entered or cleared
        # while the daemon runs takes effect at the next sentence.
        api_key: Callable[[], str | None] = env_api_key,
        post: Post = _urllib_post,
        endpoint: str = ENDPOINT,
        timeout: float = TIMEOUT_SECONDS,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        # A callable, not a string: the model is a setting, and reading it per
        # request means a change applies to the next sentence with no rebuild.
        self._model = model
        self._api_key = api_key
        self._post = post
        self._endpoint = endpoint
        self._timeout = timeout
        self._sleep = sleep
        self._clock = clock
        self._lock = threading.Lock()
        self._unavailable_until = 0.0
        # Whether the current holdoff is for a failure that will not mend by
        # itself. Kept apart because a daemon with no local engine to fall
        # back to ignores a transient holdoff -- see `available`.
        self._held_fatal = False
        self._last_error: str | None = None
        # Characters sent, for `status`: the provider bills per character.
        self._chars_sent = 0

    # -- availability -------------------------------------------------------

    def has_key(self) -> bool:
        return bool(self._api_key())

    def available(self, *, ignore_transient: bool = False) -> bool:
        """Whether to choose this engine now: a key, and not held off.

        `ignore_transient` is for a caller with nothing to fall back to: a
        holdoff after an outage exists to send the next utterances to a local
        engine without each paying a timeout, and with no local engine it
        would only mean a minute of speech refused after the network is back.
        A key refused or credits gone still hold, since asking again cannot
        succeed.
        """
        if not self.has_key():
            return False
        with self._lock:
            if self._clock() >= self._unavailable_until:
                return True
            return ignore_transient and not self._held_fatal

    def unavailable_reason(self) -> str:
        """Why `available()` says no, in words a listener can act on."""
        if not self.has_key():
            return NO_KEY
        with self._lock:
            return self._last_error or "OpenRouter is unavailable"

    def reset(self) -> None:
        """End any holdoff -- a setting changed, so try again."""
        with self._lock:
            self._unavailable_until = 0.0
            self._held_fatal = False
            self._last_error = None

    def status(self) -> dict[str, object]:
        with self._lock:
            held = max(0.0, self._unavailable_until - self._clock())
            return {
                "key": self.has_key(),
                "model": self._model(),
                "available": self.has_key() and held == 0.0,
                "retry_in": round(held, 1),
                "last_error": self._last_error,
                "chars_sent": self._chars_sent,
            }

    def supported_languages(self) -> list[str]:
        return list(SUPPORTED)

    def _fail(self, reason: str, *, fatal: bool) -> RemoteEngineError:
        holdoff = FATAL_HOLDOFF_SECONDS if fatal else TRANSIENT_HOLDOFF_SECONDS
        with self._lock:
            self._unavailable_until = self._clock() + holdoff
            self._held_fatal = fatal
            self._last_error = reason
        return RemoteEngineError(reason, fatal=fatal)

    # -- synthesis ----------------------------------------------------------

    def synthesize(
        self,
        text: str,
        voice: str,
        speed: float,
        lang: str = "en",
        *,
        cancelled: Callable[[], bool] = lambda: False,
    ) -> np.ndarray:
        """`cancelled` is asked before a retry: an utterance that was hushed
        while the request was in flight has no one left to wait for it."""
        if not text.strip():
            return np.zeros(0, dtype=np.float32)
        key = self._api_key()
        if not key:
            raise self._fail(
                NO_KEY,
                fatal=True,
            )
        body = json.dumps(
            {
                "model": self._model(),
                "input": text,
                "voice": voice or DEFAULT_VOICE,
                "response_format": "pcm",
            }
        ).encode("utf-8")
        headers = {
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            # OpenRouter's app attribution headers; harmless anywhere else.
            "HTTP-Referer": "https://github.com/sfgartland/speakd",
            "X-Title": "speakd",
        }
        reply = self._request(headers, body, cancelled)
        with self._lock:
            self._chars_sent += len(text)
        if not reply.body:
            return np.zeros(0, dtype=np.float32)
        if "json" in reply.content_type.lower():
            # A 200 carrying JSON is an error envelope, not audio; playing it
            # as PCM would be a burst of noise.
            raise self._fail(_error_message(reply), fatal=False)
        usable = len(reply.body) - len(reply.body) % 2
        pcm = np.frombuffer(reply.body[:usable], dtype="<i2").astype(np.float32) / 32768.0
        rate = _pcm_rate(reply.content_type, self.sample_rate)
        audio = _resample(pcm, rate, self.sample_rate)
        return trim_silence(np.asarray(audio, dtype=np.float32), self.sample_rate)

    def _request(
        self, headers: dict[str, str], body: bytes, cancelled: Callable[[], bool]
    ) -> Reply:
        """POST once, and once more for what may be momentary.

        Not once more when `cancelled`: the retry would sleep up to three
        seconds and ask again for audio nobody will hear. The failure raised
        instead is not recorded as a holdoff, since it says nothing about the
        network, only that the listener moved on.
        """
        for attempt in (0, 1):
            try:
                reply = self._post(self._endpoint, headers, body, self._timeout)
            except (OSError, http.client.HTTPException) as exc:
                if cancelled():
                    raise RemoteEngineError(f"cancelled: {exc}") from exc
                if attempt == 0:
                    self._sleep(RETRY_DELAY_SECONDS)
                    continue
                raise self._fail(f"cannot reach OpenRouter: {exc}", fatal=False) from exc
            if 200 <= reply.status < 300:
                with self._lock:
                    self._last_error = None
                return reply
            if reply.status in _TEXT_FAULTS:
                # This text, not the engine: one bad segment.
                raise ValueError(_error_message(reply))
            if reply.status in _FATAL:
                raise self._fail(_error_message(reply), fatal=True)
            if attempt == 0 and (reply.status == 429 or reply.status >= 500):
                if cancelled():
                    raise RemoteEngineError(f"cancelled: {_error_message(reply)}")
                delay = reply.retry_after if reply.retry_after is not None else RETRY_DELAY_SECONDS
                self._sleep(min(max(delay, 0.0), MAX_RETRY_DELAY_SECONDS))
                continue
            raise self._fail(_error_message(reply), fatal=False)
        raise AssertionError("unreachable")  # pragma: no cover
