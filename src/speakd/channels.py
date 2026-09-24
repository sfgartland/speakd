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
from dataclasses import dataclass, field

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
    # Whether something can brief the user for this channel: an agent
    # connected through speakd's MCP server, or a client with logic of its
    # own. Only such a channel has a mode worth choosing.
    briefs: bool = False
    # "brief" speaks what the briefer chose to say; "full" speaks every
    # response. Brief by default, because a channel only reads it once it has
    # a briefer, and an agent that connects is there to brief.
    mode: str = "brief"
    # Whether the briefer has spoken since the user last prompted, so the end
    # of a turn can stay quiet when the agent has already said its piece.
    briefed_this_turn: bool = False
    # Set with `set_language`; None means "not pinned" -- the channel defers
    # to detection or the default (`speakd.languages.resolve`). Not the same
    # as `speakd.languages.UNSUPPORTED`: that sentinel can be stored here too,
    # for a code the verb could not parse, so it still reaches
    # speech.unsupported_language rather than silently falling through.
    lang: str | None = None
    # When anything last touched this channel -- a request naming it, or an
    # attempt to speak -- so one nobody has used in a long time can be
    # forgotten rather than listed for ever.
    last_used: float = field(default_factory=time.time)


MODES = ("full", "brief")


def effective_mode(channel: Channel) -> str:
    """The mode that decides what is spoken: a channel with no briefer is full."""
    return channel.mode if channel.briefs else "full"


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

    def __init__(self, defaults: Callable[[str], dict[str, object]] | None = None) -> None:
        self._channels: dict[str, Channel] = {}
        self._lock = threading.Lock()
        # How a new channel starts, by its id: field name to value. Applied
        # once, when the channel is first opened, so a channel someone has
        # since unmuted stays audible however often its client opens it again.
        self._defaults = defaults

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
                channel = Channel(source_id=source_id)
                if self._defaults is not None:
                    for name, value in self._defaults(source_id).items():
                        setattr(channel, name, value)
                self._channels[source_id] = channel
            channel.last_used = time.time()
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
                channel.last_used = now
        return now

    def prune(self, before: float, keep: set[str]) -> list[str]:
        """Forget channels unused since `before`, except those in `keep`."""
        with self._lock:
            gone = [
                source_id
                for source_id, channel in self._channels.items()
                if channel.last_used < before and source_id not in keep
            ]
            for source_id in gone:
                del self._channels[source_id]
        return gone

    def set_mode(self, source_id: str, mode: str) -> Channel:
        channel = self.open(source_id)
        with self._lock:
            channel.mode = mode
        return channel

    def set_briefs(self, source_id: str, briefs: bool) -> Channel:
        channel = self.open(source_id)
        with self._lock:
            channel.briefs = briefs
        return channel

    def set_lang(self, source_id: str, lang: str | None) -> Channel:
        channel = self.open(source_id)
        with self._lock:
            channel.lang = lang
        return channel

    def mark_briefed(self, source_id: str, briefed: bool) -> None:
        with self._lock:
            channel = self._channels.get(source_id)
            if channel is not None:
                channel.briefed_this_turn = briefed

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
