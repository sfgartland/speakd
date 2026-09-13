"""Per-source channels and their standing.

Arbitration is rules, not inference: roles, priorities and profiles. An agent
changes its own standing by sending a control verb; the daemon never
deliberates about who should be heard.

An unknown source is opened implicitly on first use. An agent shelling out to
`speakctl` should not have to register before it can speak.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass

from speakd.model import Role


@dataclass
class Channel:
    source_id: str
    role: Role = Role.FOREGROUND
    priority: int = 0
    profile: str = "default"
    label: str = ""


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

    def __init__(self) -> None:
        self._channels: dict[str, Channel] = {}
        self._lock = threading.Lock()

    def open(
        self,
        source_id: str,
        *,
        role: Role | None = None,
        priority: int | None = None,
        profile: str | None = None,
        label: str | None = None,
    ) -> Channel:
        """Create the channel, or update the one already there."""
        with self._lock:
            channel = self._channels.get(source_id)
            if channel is None:
                channel = Channel(source_id=source_id)
                self._channels[source_id] = channel
            if role is not None:
                channel.role = role
            if priority is not None:
                channel.priority = priority
            if profile is not None:
                channel.profile = profile
            if label is not None:
                channel.label = label
            return channel

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

    def all(self) -> list[Channel]:
        with self._lock:
            channels = list(self._channels.values())
        return sorted(channels, key=lambda c: -c.priority)
