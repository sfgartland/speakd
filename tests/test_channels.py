"""Tests for the channel table."""

import threading

from speakd.channels import Channel, ChannelTable
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


def test_set_label_names_a_channel() -> None:
    table = ChannelTable()
    table.set_label("s", "Claude Code · a session")
    channel = table.get("s")
    assert channel is not None
    assert channel.label == "Claude Code · a session"


def test_set_label_on_an_unknown_source_opens_it() -> None:
    # Consistent with set_role and set_priority: a source may name itself
    # before it has said anything.
    table = ChannelTable()
    table.set_label("new", "named")
    assert table.get("new") is not None


def test_all_lists_channels_highest_priority_first() -> None:
    table = ChannelTable()
    table.open("low", priority=0)
    table.open("high", priority=9)
    table.open("mid", priority=4)
    assert [c.source_id for c in table.all()] == ["high", "mid", "low"]


# --- Whole-branch review: every caller is a connection thread ---


class RacingChannels(dict[str, Channel]):
    """Lets a second thread run a whole open() inside the first one's lookup."""

    def __init__(self, other_thread_may_run: threading.Event, it_did: threading.Event) -> None:
        super().__init__()
        self._other_thread_may_run = other_thread_may_run
        self._it_did = it_did
        self.first_lookup = threading.Event()

    def get(self, key: str, default: Channel | None = None) -> Channel | None:  # type: ignore[override]
        found = super().get(key, default)
        if not self.first_lookup.is_set():
            self.first_lookup.set()
            self._other_thread_may_run.set()
            # Bounded: with a lock in place, the other thread cannot get in
            # here at all, and this simply falls through when the wait
            # expires.
            self._it_did.wait(timeout=0.5)
        return found


def test_a_first_touch_open_cannot_lose_a_concurrent_set_role() -> None:
    """open() is a read-modify-write, and every caller is a connection thread.

    Two first-touch opens on one source_id both find nothing, both build a
    default Channel and both store it: the second overwrites the first, and
    any standing set on the first object in between -- a role, a priority --
    is gone, silently, leaving a channel that speaks when it was told not to.
    """
    may_run = threading.Event()
    it_did = threading.Event()
    table = ChannelTable()
    table._channels = RacingChannels(may_run, it_did)

    def other() -> None:
        assert may_run.wait(timeout=5.0)
        table.set_role("s", Role.BACKGROUND)
        it_did.set()

    racer = threading.Thread(target=other, name="other-connection")
    racer.start()
    table.open("s")
    racer.join(timeout=5.0)
    assert not racer.is_alive()

    channel = table.get("s")
    assert channel is not None
    assert channel.role is Role.BACKGROUND, "a concurrent set_role was overwritten and lost"
