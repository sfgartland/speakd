"""Tests for the two off switches: the one that keeps the model, and the one that drops it."""

import threading
import time
from collections.abc import Sequence

import numpy as np

from speakd.channels import ChannelTable
from speakd.daemon import Daemon, ProfileView
from speakd.events import Event, EventBus
from speakd.model import Piece
from speakd.player import RecordingPlayer
from speakd.protocol import Request, Verb
from speakd.synth.fake import FakeEngine
from speakd.synth.lazy import LazyEngine


def passthrough(pieces: Sequence[Piece]) -> tuple[list[Piece], list[str]]:
    return list(pieces), []


def profile_for(name: str) -> ProfileView:
    return ProfileView(voice="af_heart", speed=1.1, interrupt_on=(), prepare=passthrough)


def _until(predicate, timeout: float = 5.0) -> bool:  # type: ignore[no-untyped-def]
    """Poll until `predicate` holds. Returns whether it ever did."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return bool(predicate())


class CountingEngine(FakeEngine):
    """A FakeEngine that says how many times it was asked to synthesise, and for what."""

    def __init__(self) -> None:
        super().__init__()
        self.calls = 0
        self.texts: list[str] = []

    def synthesize(self, text: str, voice: str, speed: float, lang: str = "en") -> np.ndarray:
        self.calls += 1
        self.texts.append(text)
        return super().synthesize(text, voice, speed, lang)


class HoldingPlayer:
    """Stays inside play() until released, and says when it was stopped.

    A mute has nothing to cut off unless something is already sounding, which
    a player that returns immediately can never be.
    """

    def __init__(self) -> None:
        self.playing = threading.Event()
        self.release = threading.Event()
        self.stopped = threading.Event()
        self.played: list[np.ndarray] = []

    def play(self, audio: np.ndarray, sample_rate: int) -> None:
        self.playing.set()
        self.release.wait(timeout=10.0)
        self.played.append(audio)

    def stop(self) -> None:
        self.stopped.set()
        # A real sink's stop() ends the write in flight; this one has to be
        # let out by hand or the worker never leaves the utterance.
        self.release.set()


def build() -> tuple[Daemon, CountingEngine, list[Event]]:
    engine = CountingEngine()
    bus = EventBus()
    seen: list[Event] = []
    bus.subscribe(seen.append)
    daemon = Daemon(engine, RecordingPlayer(), profile_for, bus=bus, channels=ChannelTable())
    daemon.start()
    return daemon, engine, seen


# ---------------------------------------------------------------------------
# Mute: the off switch that keeps the model loaded
# ---------------------------------------------------------------------------


def test_a_muted_channel_never_reaches_the_engine() -> None:
    daemon, engine, _ = build()
    try:
        daemon.handle(Request(verb=Verb.MUTE, source_id="s", payload={"muted": True}))
        daemon.handle(Request(verb=Verb.ENQUEUE, source_id="s", payload={"text": "Hello."}))
        daemon.wait_idle(2.0)
        assert engine.calls == 0
    finally:
        daemon.stop()


def test_an_unmuted_channel_still_speaks() -> None:
    daemon, engine, _ = build()
    try:
        daemon.handle(Request(verb=Verb.ENQUEUE, source_id="s", payload={"text": "Hello."}))
        daemon.wait_idle(2.0)
        assert engine.calls > 0
    finally:
        daemon.stop()


def test_global_mute_silences_a_channel_that_is_not_itself_muted() -> None:
    daemon, engine, _ = build()
    try:
        daemon.handle(Request(verb=Verb.MUTE, source_id="", payload={"muted": True}))
        daemon.handle(Request(verb=Verb.ENQUEUE, source_id="s", payload={"text": "Hello."}))
        daemon.wait_idle(2.0)
        assert engine.calls == 0
    finally:
        daemon.stop()


def test_clearing_the_global_mute_restores_what_each_channel_had() -> None:
    # The two flags are independent state, not one setting written twice.
    daemon, engine, _ = build()
    try:
        daemon.handle(Request(verb=Verb.MUTE, source_id="s", payload={"muted": True}))
        daemon.handle(Request(verb=Verb.MUTE, source_id="", payload={"muted": True}))
        daemon.handle(Request(verb=Verb.MUTE, source_id="", payload={"muted": False}))
        daemon.handle(Request(verb=Verb.ENQUEUE, source_id="s", payload={"text": "Hello."}))
        daemon.wait_idle(2.0)
        assert engine.calls == 0
    finally:
        daemon.stop()


def test_muting_announces_itself_on_the_bus() -> None:
    daemon, _engine, seen = build()
    try:
        daemon.handle(Request(verb=Verb.MUTE, source_id="", payload={"muted": True}))
        mutes = [e for e in seen if e.kind == "mute"]
        assert mutes and mutes[-1].data == {"muted": True, "scope": "global"}
    finally:
        daemon.stop()


def test_status_reports_both_levels() -> None:
    daemon, _engine, _ = build()
    try:
        daemon.handle(Request(verb=Verb.MUTE, source_id="s", payload={"muted": True}))
        status = daemon.handle(Request(verb=Verb.STATUS, source_id=""))
        assert status.data["muted"] is False
        channels = status.data["channels"]
        assert isinstance(channels, list)
        entry = [c for c in channels if c["source_id"] == "s"][0]
        assert entry["muted"] is True
    finally:
        daemon.stop()


def test_asking_the_status_does_not_open_a_channel_for_the_asker() -> None:
    # The same care STATUS already takes: a global mute names no source, and
    # must not leave a phantom channel behind.
    daemon, _engine, _ = build()
    try:
        daemon.handle(Request(verb=Verb.MUTE, source_id="", payload={"muted": True}))
        status = daemon.handle(Request(verb=Verb.STATUS, source_id=""))
        assert status.data["channels"] == []
    finally:
        daemon.stop()


def test_mute_needs_a_boolean() -> None:
    daemon, _engine, _ = build()
    try:
        response = daemon.handle(Request(verb=Verb.MUTE, source_id="", payload={"muted": "yes"}))
        assert not response.ok
        assert "muted" in response.error
    finally:
        daemon.stop()


def test_a_dropped_utterance_says_it_was_muted() -> None:
    """On the bus as well as in the response: a GUI has to show why nothing was said."""
    daemon, _engine, seen = build()
    try:
        daemon.handle(Request(verb=Verb.MUTE, source_id="", payload={"muted": True}))
        response = daemon.handle(
            Request(verb=Verb.ENQUEUE, source_id="s", payload={"text": "Hello."})
        )
        assert response.data["spoken"] is False
        assert response.data["reason"] == "muted"
        declined = [e for e in seen if e.kind == "declined"]
        assert declined and declined[-1].data["reason"] == "muted"
    finally:
        daemon.stop()


def test_muting_cuts_off_what_is_already_being_said() -> None:
    """An off switch that lets the current paragraph finish is not an off switch."""
    player = HoldingPlayer()
    daemon = Daemon(CountingEngine(), player, profile_for, bus=EventBus(), channels=ChannelTable())
    daemon.start()
    try:
        daemon.handle(
            Request(verb=Verb.ENQUEUE, source_id="s", payload={"text": "One. Two. Three."})
        )
        assert player.playing.wait(timeout=5.0), "the worker never reached the player"
        daemon.handle(Request(verb=Verb.MUTE, source_id="", payload={"muted": True}))
        assert player.stopped.is_set(), "the mute let the utterance run on"
    finally:
        player.release.set()
        daemon.stop()


def test_muting_one_channel_leaves_another_channels_queue_alone() -> None:
    """Sessions are separate, so their queues are.

    Silencing the channel that will not stop talking must not throw away the
    speech of the one being listened to -- which is what a drain that took
    the whole queue would do.
    """
    player = HoldingPlayer()
    engine = CountingEngine()
    daemon = Daemon(engine, player, profile_for, bus=EventBus(), channels=ChannelTable())
    daemon.start()
    try:
        daemon.handle(Request(verb=Verb.ENQUEUE, source_id="a", payload={"text": "Holding."}))
        assert player.playing.wait(timeout=5.0), "the worker never reached the player"
        daemon.handle(Request(verb=Verb.ENQUEUE, source_id="a", payload={"text": "Noisy one."}))
        daemon.handle(Request(verb=Verb.ENQUEUE, source_id="b", payload={"text": "Wanted one."}))
        daemon.handle(Request(verb=Verb.MUTE, source_id="a", payload={"muted": True}))
        player.release.set()
        assert daemon.wait_idle(timeout=5.0)
    finally:
        player.release.set()
        daemon.stop()
    assert "Wanted one." in engine.texts, "a channel mute ate another channel's queue"
    assert "Noisy one." not in engine.texts, "the muted channel spoke anyway"


def test_muting_one_channel_does_not_cut_off_another_that_is_speaking() -> None:
    """The utterance in the room belongs to someone. Only its own mute stops it."""
    player = HoldingPlayer()
    daemon = Daemon(CountingEngine(), player, profile_for, bus=EventBus(), channels=ChannelTable())
    daemon.start()
    try:
        daemon.handle(Request(verb=Verb.ENQUEUE, source_id="a", payload={"text": "Holding."}))
        assert player.playing.wait(timeout=5.0), "the worker never reached the player"
        daemon.handle(Request(verb=Verb.MUTE, source_id="b", payload={"muted": True}))
        assert not player.stopped.is_set(), "muting one channel silenced another mid-sentence"
    finally:
        player.release.set()
        daemon.stop()


def test_a_mute_survives_a_restart() -> None:
    """The flag is on disk, which is the whole reason `speakd.state` exists."""
    first, _engine, _ = build()
    try:
        first.handle(Request(verb=Verb.MUTE, source_id="", payload={"muted": True}))
    finally:
        first.stop()
    second, engine, _ = build()
    try:
        assert second.muted is True
        second.handle(Request(verb=Verb.ENQUEUE, source_id="s", payload={"text": "Hello."}))
        second.wait_idle(2.0)
        assert engine.calls == 0
    finally:
        second.stop()


# ---------------------------------------------------------------------------
# Disable: the off switch that gives the memory back
# ---------------------------------------------------------------------------


def _lazy(built: list[int]) -> LazyEngine:
    def factory() -> FakeEngine:
        built.append(1)
        return FakeEngine()

    return LazyEngine(factory, sample_rate=24000)


def test_disabling_unloads_and_enqueues_are_dropped() -> None:
    built: list[int] = []
    engine = _lazy(built)
    engine.load()
    bus = EventBus()
    daemon = Daemon(engine, RecordingPlayer(), profile_for, bus=bus, channels=ChannelTable())
    daemon.start()
    try:
        daemon.handle(Request(verb=Verb.SET_ENGINE, source_id="", payload={"loaded": False}))
        assert not engine.loaded
        response = daemon.handle(
            Request(verb=Verb.ENQUEUE, source_id="s", payload={"text": "Hello."})
        )
        assert response.data["spoken"] is False
        assert response.data["reason"] == "disabled"
    finally:
        daemon.stop()


def test_enabling_loads_off_the_request_thread() -> None:
    engine = LazyEngine(FakeEngine, sample_rate=24000)
    daemon = Daemon(engine, RecordingPlayer(), profile_for, bus=EventBus(), channels=ChannelTable())
    daemon.start()
    try:
        response = daemon.handle(
            Request(verb=Verb.SET_ENGINE, source_id="", payload={"loaded": True})
        )
        # The verb returns at once; readiness is announced on the bus.
        assert response.ok
        assert _until(lambda: engine.loaded, timeout=5.0)
    finally:
        daemon.stop()


def test_status_reports_the_engine() -> None:
    engine = LazyEngine(FakeEngine, sample_rate=24000)
    daemon = Daemon(engine, RecordingPlayer(), profile_for, bus=EventBus(), channels=ChannelTable())
    daemon.start()
    try:
        status = daemon.handle(Request(verb=Verb.STATUS, source_id=""))
        assert status.data["engine"] == {
            "loaded": False,
            "loading": False,
            "name": "lazy",
            "piper": {"available": False, "voices": []},
        }
    finally:
        daemon.stop()


def test_an_engine_that_cannot_be_unloaded_reports_as_loaded() -> None:
    """A daemon that will speak perfectly well must not read as switched off."""
    daemon, _engine, _ = build()
    try:
        status = daemon.handle(Request(verb=Verb.STATUS, source_id=""))
        assert status.data["engine"] == {
            "loaded": True,
            "loading": False,
            "name": "fake",
            "piper": {"available": False, "voices": []},
        }
    finally:
        daemon.stop()


def test_set_engine_on_an_engine_that_cannot_be_unloaded_is_refused_by_name() -> None:
    # Named, because "this daemon's engine cannot be unloaded" is something a
    # caller can act on where a bare refusal is not -- the same answer
    # `pause` gives for a player that cannot pause.
    daemon, _engine, _ = build()
    try:
        response = daemon.handle(
            Request(verb=Verb.SET_ENGINE, source_id="", payload={"loaded": False})
        )
        assert not response.ok
        assert "CountingEngine" in response.error
    finally:
        daemon.stop()


def test_set_engine_needs_a_boolean() -> None:
    engine = LazyEngine(FakeEngine, sample_rate=24000)
    daemon = Daemon(engine, RecordingPlayer(), profile_for, bus=EventBus(), channels=ChannelTable())
    daemon.start()
    try:
        response = daemon.handle(
            Request(verb=Verb.SET_ENGINE, source_id="", payload={"loaded": "off"})
        )
        assert not response.ok
        assert "loaded" in response.error
    finally:
        daemon.stop()


def test_the_engine_announces_each_state_on_the_bus() -> None:
    """A GUI cannot show a thirty-second load it is never told about."""
    engine = LazyEngine(FakeEngine, sample_rate=24000)
    bus = EventBus()
    seen: list[Event] = []
    bus.subscribe(seen.append, kinds=["engine"])
    daemon = Daemon(engine, RecordingPlayer(), profile_for, bus=bus, channels=ChannelTable())
    daemon.start()
    try:
        daemon.handle(Request(verb=Verb.SET_ENGINE, source_id="", payload={"loaded": True}))
        assert _until(lambda: [e.data["state"] for e in seen] == ["loading", "ready"])
        daemon.handle(Request(verb=Verb.SET_ENGINE, source_id="", payload={"loaded": False}))
        assert [e.data["state"] for e in seen] == ["loading", "ready", "unloaded"]
    finally:
        daemon.stop()


def test_a_disable_survives_a_restart() -> None:
    """What `__main__` reads before it builds anything, so a disabled daemon starts small."""
    from speakd import state

    engine = LazyEngine(FakeEngine, sample_rate=24000)
    daemon = Daemon(engine, RecordingPlayer(), profile_for, bus=EventBus(), channels=ChannelTable())
    daemon.start()
    try:
        daemon.handle(Request(verb=Verb.SET_ENGINE, source_id="", payload={"loaded": False}))
        assert state.load().disabled is True
    finally:
        daemon.stop()


def test_disabling_does_not_forget_the_mute_beside_it() -> None:
    """Two flags in one file: whichever is written must carry the other through."""
    from speakd import state

    engine = LazyEngine(FakeEngine, sample_rate=24000)
    daemon = Daemon(engine, RecordingPlayer(), profile_for, bus=EventBus(), channels=ChannelTable())
    daemon.start()
    try:
        daemon.handle(Request(verb=Verb.MUTE, source_id="", payload={"muted": True}))
        daemon.handle(Request(verb=Verb.SET_ENGINE, source_id="", payload={"loaded": False}))
        assert state.load() == state.DaemonState(muted=True, disabled=True)
    finally:
        daemon.stop()


def test_disabling_cuts_off_what_is_already_being_said() -> None:
    """Unloading under a live utterance is the paragraph this switch exists to end."""
    player = HoldingPlayer()
    engine = LazyEngine(FakeEngine, sample_rate=24000)
    engine.load()
    daemon = Daemon(engine, player, profile_for, bus=EventBus(), channels=ChannelTable())
    daemon.start()
    try:
        daemon.handle(
            Request(verb=Verb.ENQUEUE, source_id="s", payload={"text": "One. Two. Three."})
        )
        assert player.playing.wait(timeout=5.0), "the worker never reached the player"
        daemon.handle(Request(verb=Verb.SET_ENGINE, source_id="", payload={"loaded": False}))
        assert player.stopped.is_set(), "the disable let the utterance run on"
        assert not engine.loaded
    finally:
        player.release.set()
        daemon.stop()
