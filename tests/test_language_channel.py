"""Tests for the `set_language` verb, its channel state, and `status`."""

from __future__ import annotations

from collections.abc import Sequence

import pytest

from speakd.channels import ChannelTable
from speakd.daemon import Daemon, ProfileView
from speakd.events import Event, EventBus
from speakd.model import Piece
from speakd.player import RecordingPlayer
from speakd.protocol import Request, Verb
from speakd.synth.fake import FakeEngine


def passthrough(pieces: Sequence[Piece]) -> tuple[list[Piece], list[str]]:
    return list(pieces), []


def profile_for(name: str) -> ProfileView:
    return ProfileView(voice="af_heart", speed=1.1, interrupt_on=(), prepare=passthrough)


@pytest.fixture
def daemon():  # type: ignore[no-untyped-def]
    player = RecordingPlayer()
    bus = EventBus()
    d = Daemon(FakeEngine(), player, profile_for, bus=bus, channels=ChannelTable())
    d.start()
    try:
        yield d, bus
    finally:
        d.stop()


def test_set_language_stores_the_normalised_code(daemon) -> None:  # type: ignore[no-untyped-def]
    d, _bus = daemon
    response = d.handle(Request(Verb.SET_LANGUAGE, "s", {"lang": "FR"}))
    assert response.ok
    assert response.data["lang"] == "fr"
    assert d.channels.get("s").lang == "fr"


def test_set_language_publishes_on_the_channel(daemon) -> None:  # type: ignore[no-untyped-def]
    d, bus = daemon
    events: list[Event] = []
    bus.subscribe(events.append)
    d.handle(Request(Verb.SET_LANGUAGE, "s", {"lang": "pt"}))
    language_events = [e for e in events if e.kind == "language"]
    assert len(language_events) == 1
    assert language_events[0].source_id == "s"
    assert language_events[0].data == {"lang": "pt-br"}


def test_set_language_appears_in_status(daemon) -> None:  # type: ignore[no-untyped-def]
    d, _bus = daemon
    d.handle(Request(Verb.SET_LANGUAGE, "s", {"lang": "es"}))
    response = d.handle(Request(Verb.STATUS, "", {}))
    assert response.ok
    channels = response.data["channels"]
    assert isinstance(channels, list)
    entry = next(c for c in channels if c["source_id"] == "s")
    assert entry["lang"] == "es"


def test_a_channel_with_no_language_set_reports_none_in_status(daemon) -> None:  # type: ignore[no-untyped-def]
    d, _bus = daemon
    d.handle(Request(Verb.ENQUEUE, "s", {"text": "hi"}))
    response = d.handle(Request(Verb.STATUS, "", {}))
    entry = next(c for c in response.data["channels"] if c["source_id"] == "s")
    assert entry["lang"] is None


def test_set_language_auto_clears_it(daemon) -> None:  # type: ignore[no-untyped-def]
    d, _bus = daemon
    d.handle(Request(Verb.SET_LANGUAGE, "s", {"lang": "fr"}))
    assert d.channels.get("s").lang == "fr"
    response = d.handle(Request(Verb.SET_LANGUAGE, "s", {"lang": "auto"}))
    assert response.ok
    assert response.data["lang"] is None
    assert d.channels.get("s").lang is None


def test_set_language_empty_string_also_clears_it(daemon) -> None:  # type: ignore[no-untyped-def]
    d, _bus = daemon
    d.handle(Request(Verb.SET_LANGUAGE, "s", {"lang": "fr"}))
    response = d.handle(Request(Verb.SET_LANGUAGE, "s", {"lang": ""}))
    assert response.ok
    assert response.data["lang"] is None


def test_set_language_with_a_tag_shaped_unknown_code_is_kept_as_the_sentinel(daemon) -> None:  # type: ignore[no-untyped-def]
    d, _bus = daemon
    response = d.handle(Request(Verb.SET_LANGUAGE, "s", {"lang": "xx-yy"}))
    assert response.ok
    assert response.data["lang"] == "und"
    assert d.channels.get("s").lang == "und"


def test_set_language_with_free_text_that_is_not_tag_shaped_clears_it(daemon) -> None:  # type: ignore[no-untyped-def]
    # Review Focus #12: not even shaped like a language tag, so it is
    # treated the same as "nothing was chosen" rather than pinned as some
    # unsupported language that would decline every utterance after it.
    d, _bus = daemon
    response = d.handle(Request(Verb.SET_LANGUAGE, "s", {"lang": "klingon"}))
    assert response.ok
    assert response.data["lang"] is None
    assert d.channels.get("s").lang is None


def test_set_language_needs_a_channel(daemon) -> None:  # type: ignore[no-untyped-def]
    d, _bus = daemon
    response = d.handle(Request(Verb.SET_LANGUAGE, "", {"lang": "fr"}))
    assert not response.ok


def test_set_language_needs_a_string(daemon) -> None:  # type: ignore[no-untyped-def]
    d, _bus = daemon
    response = d.handle(Request(Verb.SET_LANGUAGE, "s", {"lang": 3}))
    assert not response.ok
