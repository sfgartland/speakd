"""Per-source channels and their standing.

Arbitration is rules, not inference: roles, priorities and profiles. An agent
changes its own standing by sending a control verb; the daemon never
deliberates about who should be heard.

An unknown source is opened implicitly on first use. An agent shelling out to
`speakctl` should not have to register before it can speak.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from speakd.model import Role


@dataclass
class Channel:
    source_id: str
    role: Role = Role.FOREGROUND
    priority: int = 0
    profile: str = "default"
    label: str = ""
    muted: bool = False
    # When this channel last *tried* to speak, as `time.time()`: spoken,
    # declined or muted alike. It is how a monitor ranks channels by
    # relevance, and a muted session that just finished has spoken up even
    # though nobody heard it. Zero means never.
    last_output: float = 0.0


class ChannelTable:
    """The set of live channels, ordered by priority when listed.

    Shared across threads: every caller is one of the transport's
    per-connection threads, and the daemon's speech worker reads through
    `open` too. `open` is a read-modify-write -- two first-touch opens on one
    source_id would otherwise both find nothing, both build a default Channel
    and both store it, so the second silently overwrites the first along with
    any standing set on it in between. It is taken under `_lock`, which also
    covers `close` and the listing so neither can see the table mid-write.
    The `Channel` objects handed back are not themselves guarded: a caller
    holding one sees another thread's update to it, which is the intent --
    the table hands out the live channel, not a copy.
    """

    def __init__(self, muted_by_default: Callable[[str], bool] | None = None) -> None:
        self._channels: dict[str, Channel] = {}
        self._lock = threading.Lock()
        # Which new channels start silent. Applied once, when the channel is
        # first opened, so a channel someone has since unmuted stays audible
        # however often its client opens it again.
        self._muted_by_default = muted_by_default

    def open(
        self,
        source_id: str,
        *,
        role: Role | None = None,
        priority: int | None = None,
        profile: str | None = None,
        label: str | None = None,
        muted: bool | None = None,
    ) -> Channel:
        """Create the channel, or update the one already there."""
        with self._lock:
            channel = self._channels.get(source_id)
            if channel is None:
                starts_muted = self._muted_by_default is not None and self._muted_by_default(
                    source_id
                )
                channel = Channel(source_id=source_id, muted=starts_muted)
                self._channels[source_id] = channel
            if role is not None:
                channel.role = role
            if priority is not None:
                channel.priority = priority
            if profile is not None:
                channel.profile = profile
            if label is not None:
                channel.label = label
            if muted is not None:
                channel.muted = muted
            return channel

    def touch(self, source_id: str) -> float:
        """Record that `source_id` just tried to speak, and say when."""
        now = time.time()
        with self._lock:
            channel = self._channels.get(source_id)
            if channel is not None:
                channel.last_output = now
        return now

    def get(self, source_id: str) -> Channel | None:
        with self._lock:
            return self._channels.get(source_id)

    def close(self, source_id: str) -> None:
        with self._lock:
            self._channels.pop(source_id, None)

    def set_role(self, source_id: str, role: Role) -> None:
        self.open(source_id, role=role)

    def set_priority(self, source_id: str, priority: int) -> None:
        self.open(source_id, priority=priority)

    def set_label(self, source_id: str, label: str) -> None:
        self.open(source_id, label=label)

    def set_muted(self, source_id: str, muted: bool) -> None:
        self.open(source_id, muted=muted)

    def all(self) -> list[Channel]:
        with self._lock:
            channels = list(self._channels.values())
        return sorted(channels, key=lambda c: -c.priority)
