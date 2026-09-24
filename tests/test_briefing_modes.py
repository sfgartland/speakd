"""Tests for brief and full modes, and what each lets through."""

import pytest

from speakd.channels import ChannelTable
from speakd.daemon import Daemon, ProfileView
from speakd.events import Event, EventBus
from speakd.player import RecordingPlayer
from speakd.protocol import Request, Response, Verb
from speakd.synth.fake import FakeEngine


def build() -> tuple[Daemon, RecordingPlayer, list[Event]]:
    player = RecordingPlayer()
    d = Daemon(
        FakeEngine(),
        player,
        lambda name: ProfileView(
            voice="v", speed=1.0, interrupt_on=(), prepare=lambda p: (list(p), [])
        ),
        bus=EventBus(),
        channels=ChannelTable(),
    )
    seen: list[Event] = []
    d.bus.subscribe(seen.append)
    d.start()
    return d, player, seen


def req(d: Daemon, verb: Verb, source: str = "s", **payload: object) -> Response:
    return d.handle(Request(verb=verb, source_id=source, payload=dict(payload)))


def say(d: Daemon, kind: str, text: str = "Hello there.", **flags: object) -> Response:
    return req(d, Verb.ENQUEUE, text=text, kind=kind, **flags)


@pytest.mark.parametrize(
    ("mode", "kind", "spoken"),
    [
        ("full", "response", True),
        ("full", "brief", False),
        ("full", "attention", True),
        ("brief", "response", False),
        ("brief", "brief", True),
        ("brief", "attention", True),
    ],
)
def test_what_each_mode_lets_through(mode: str, kind: str, spoken: bool) -> None:
    d, _, _ = build()
    try:
        req(d, Verb.SET_CAPABILITIES, briefs=True)
        req(d, Verb.SET_MODE, mode=mode)
        response = say(d, kind)
        assert response.data["spoken"] is spoken
        if not spoken:
            assert response.data["reason"] == f"{mode} mode"
    finally:
        d.stop()


def test_a_channel_without_a_briefer_is_always_full() -> None:
    d, _, _ = build()
    try:
        assert not req(d, Verb.SET_MODE, mode="brief").ok
        assert say(d, "response").data["spoken"] is True
    finally:
        d.stop()


def test_set_mode_refuses_nonsense() -> None:
    d, _, _ = build()
    try:
        req(d, Verb.SET_CAPABILITIES, briefs=True)
        assert not req(d, Verb.SET_MODE, mode="loud").ok
        assert not req(d, Verb.SET_CAPABILITIES, briefs="yes").ok
    finally:
        d.stop()


def test_mode_changes_are_announced_and_reported() -> None:
    d, _, seen = build()
    try:
        req(d, Verb.SET_CAPABILITIES, briefs=True)
        req(d, Verb.SET_MODE, mode="full")
        assert [e.data for e in seen if e.kind == "mode"][-1] == {"mode": "full", "briefs": True}
        ch = req(d, Verb.STATUS, source="").data["channels"][0]
        assert (ch["briefs"], ch["mode"]) == (True, "full")
    finally:
        d.stop()


def test_an_agent_that_connects_starts_in_brief() -> None:
    d, _, _ = build()
    try:
        assert req(d, Verb.SET_CAPABILITIES, briefs=True).data == {"briefs": True, "mode": "brief"}
    finally:
        d.stop()


def test_a_brief_is_spoken_with_the_label_in_front() -> None:
    d, _, seen = build()
    try:
        req(d, Verb.SET_LABEL, label="ReSem paper")
        req(d, Verb.SET_CAPABILITIES, briefs=True)
        say(d, "brief", "Tests pass.")
        assert d.wait_idle(timeout=5.0)
        started = next(e for e in seen if e.kind == "started")
        assert started.data["text"] == "ReSem paper: Tests pass."
    finally:
        d.stop()


def test_a_fallback_is_silent_once_the_agent_has_briefed() -> None:
    d, _, _ = build()
    try:
        req(d, Verb.SET_CAPABILITIES, briefs=True)
        assert say(d, "attention", "finished", unless_briefed=True).data["spoken"] is True
        say(d, "brief", "Done.")
        again = say(d, "attention", "finished", unless_briefed=True)
        assert again.data == {"spoken": False, "reason": "already briefed"}
        req(d, Verb.HUSH)  # the next prompt
        assert say(d, "attention", "finished", unless_briefed=True).data["spoken"] is True
    finally:
        d.stop()


def test_only_in_mode_declines_in_the_other_mode() -> None:
    d, _, _ = build()
    try:
        req(d, Verb.SET_CAPABILITIES, briefs=True)
        req(d, Verb.SET_MODE, mode="full")
        response = say(d, "attention", "finished", only_in_mode="brief")
        assert response.data == {"spoken": False, "reason": "full mode"}
    finally:
        d.stop()


def test_claude_code_sessions_start_brief_and_muted() -> None:
    from speakd.__main__ import build_channels
    from speakd.channels import effective_mode

    table = build_channels()
    main = table.open("claude-code:abc")
    assert (main.briefs, effective_mode(main), main.muted) == (True, "brief", True)
    other = table.open("notify:whatsapp")
    assert (other.briefs, effective_mode(other), other.muted) == (False, "full", False)
    assert table.open("claude-code:abc:notify").briefs is False
