"""Tests for the channel table."""

from speakd.channels import ChannelTable
from speakd.model import Role


def test_open_creates_a_foreground_channel_by_default() -> None:
    table = ChannelTable()
    channel = table.open("session:abc")
    assert channel.source_id == "session:abc"
    assert channel.role is Role.FOREGROUND
    assert channel.priority == 0
    assert channel.profile == "default"


def test_open_accepts_role_priority_profile_and_label() -> None:
    table = ChannelTable()
    channel = table.open("s", role=Role.BACKGROUND, priority=5, profile="monitor", label="PhD")
    assert channel.role is Role.BACKGROUND
    assert channel.priority == 5
    assert channel.profile == "monitor"
    assert channel.label == "PhD"


def test_open_is_idempotent_and_updates_in_place() -> None:
    table = ChannelTable()
    first = table.open("s")
    second = table.open("s", profile="monitor")
    assert first is second
    assert second.profile == "monitor"


def test_get_returns_none_for_an_unknown_source() -> None:
    assert ChannelTable().get("nobody") is None


def test_close_removes_the_channel() -> None:
    table = ChannelTable()
    table.open("s")
    table.close("s")
    assert table.get("s") is None


def test_closing_an_unknown_source_is_harmless() -> None:
    ChannelTable().close("nobody")


def test_set_role_and_priority_mutate_the_channel() -> None:
    table = ChannelTable()
    table.open("s")
    table.set_role("s", Role.BACKGROUND)
    table.set_priority("s", 3)
    channel = table.get("s")
    assert channel is not None
    assert channel.role is Role.BACKGROUND
    assert channel.priority == 3


def test_setting_a_role_on_an_unknown_source_opens_it() -> None:
    table = ChannelTable()
    table.set_role("fresh", Role.BACKGROUND)
    channel = table.get("fresh")
    assert channel is not None and channel.role is Role.BACKGROUND


def test_all_lists_channels_highest_priority_first() -> None:
    table = ChannelTable()
    table.open("low", priority=0)
    table.open("high", priority=9)
    table.open("mid", priority=4)
    assert [c.source_id for c in table.all()] == ["high", "mid", "low"]
