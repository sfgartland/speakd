"""Tests for the arbitration rules."""

from speakd.channels import Channel
from speakd.model import Role
from speakd.scheduler import SpeechRequest, decide


def foreground(**kwargs: object) -> Channel:
    return Channel(source_id="s", role=Role.FOREGROUND, **kwargs)  # type: ignore[arg-type]


def background(**kwargs: object) -> Channel:
    return Channel(source_id="s", role=Role.BACKGROUND, **kwargs)  # type: ignore[arg-type]


def test_a_foreground_channel_is_read_in_full() -> None:
    decision = decide(SpeechRequest("s", "hello"), foreground(), interrupt_on=())
    assert decision.speak is True
    assert decision.prefix == ""


def test_a_foreground_channel_speaks_regardless_of_kind() -> None:
    decision = decide(SpeechRequest("s", "hi", kind="progress"), foreground(), interrupt_on=())
    assert decision.speak is True


def test_a_background_channel_stays_silent_for_ordinary_output() -> None:
    decision = decide(
        SpeechRequest("s", "hi", kind="response"), background(), interrupt_on=("error", "done")
    )
    assert decision.speak is False
    assert "background" in decision.reason


def test_a_background_channel_speaks_for_an_interrupting_kind() -> None:
    decision = decide(
        SpeechRequest("s", "it failed", kind="error"), background(), interrupt_on=("error",)
    )
    assert decision.speak is True


def test_a_background_announcement_carries_the_channel_label() -> None:
    channel = background(label="PhD")
    decision = decide(SpeechRequest("s", "done", kind="done"), channel, interrupt_on=("done",))
    assert decision.prefix == "PhD:"


def test_a_background_channel_without_a_label_gets_no_prefix() -> None:
    decision = decide(SpeechRequest("s", "done", kind="done"), background(), interrupt_on=("done",))
    assert decision.prefix == ""


def test_empty_text_is_never_spoken() -> None:
    decision = decide(SpeechRequest("s", "   "), foreground(), interrupt_on=())
    assert decision.speak is False
    assert "empty" in decision.reason
