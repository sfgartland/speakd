"""Event bus for out-of-process subscribers (position, state, job lifecycle).

A subscriber that raises is dropped rather than allowed to break publishing.
A dead GUI must never silence speech.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Event:
    kind: str
    source_id: str
    data: dict[str, object] = field(default_factory=dict)


@dataclass
class _Subscription:
    callback: Callable[[Event], None]
    kinds: frozenset[str] | None


class Subscription:
    """Handle that ends a subscription. Structurally a plugin `Disposable`."""

    def __init__(self, unsubscribe: Callable[[], None]) -> None:
        self._unsubscribe = unsubscribe
        self._disposed = False

    def dispose(self) -> None:
        if self._disposed:
            return
        self._disposed = True
        self._unsubscribe()


class EventBus:
    """Fan-out to subscribers, tolerant of subscribers that die."""

    def __init__(self) -> None:
        self._subscriptions: list[_Subscription] = []
        self._lock = threading.Lock()

    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subscriptions)

    def subscribe(
        self,
        callback: Callable[[Event], None],
        kinds: Sequence[str] | None = None,
    ) -> Subscription:
        subscription = _Subscription(
            callback=callback,
            kinds=frozenset(kinds) if kinds is not None else None,
        )
        with self._lock:
            self._subscriptions.append(subscription)

        def unsubscribe() -> None:
            with self._lock:
                if subscription in self._subscriptions:
                    self._subscriptions.remove(subscription)

        return Subscription(unsubscribe)

    def publish(self, event: Event) -> None:
        # Snapshot under the lock: a subscriber may subscribe or unsubscribe
        # from inside its own callback, and publishing is called from the
        # speech thread while the control thread may be subscribing.
        with self._lock:
            current = list(self._subscriptions)
        dead: list[_Subscription] = []
        for subscription in current:
            if subscription.kinds is not None and event.kind not in subscription.kinds:
                continue
            try:
                subscription.callback(event)
            except Exception:
                dead.append(subscription)
        if dead:
            with self._lock:
                for subscription in dead:
                    if subscription in self._subscriptions:
                        self._subscriptions.remove(subscription)
