"""Tests for the service registry."""

import pytest

from speakd.plugins import Disposable
from speakd.plugins.registry import ServiceRegistry


def test_provide_then_get() -> None:
    registry = ServiceRegistry()
    registry.provide("bibliography", {"key": "value"})
    assert registry.get("bibliography") == {"key": "value"}


def test_get_unknown_service_is_none() -> None:
    assert ServiceRegistry().get("nothing") is None


def test_disposing_a_provider_removes_the_service() -> None:
    registry = ServiceRegistry()
    handle = registry.provide("bibliography", object())
    handle.dispose()
    assert registry.get("bibliography") is None


def test_providing_twice_is_rejected() -> None:
    registry = ServiceRegistry()
    registry.provide("bibliography", object())
    with pytest.raises(ValueError, match="already provided"):
        registry.provide("bibliography", object())


def test_a_disposed_name_can_be_provided_again() -> None:
    registry = ServiceRegistry()
    registry.provide("bibliography", "first").dispose()
    registry.provide("bibliography", "second")
    assert registry.get("bibliography") == "second"


def test_watch_fires_when_a_provider_appears_and_disappears() -> None:
    registry = ServiceRegistry()
    seen: list[object | None] = []
    registry.watch("bibliography", seen.append)
    assert seen == [None]
    handle = registry.provide("bibliography", "value")
    assert seen == [None, "value"]
    handle.dispose()
    assert seen == [None, "value", None]


def test_watch_fires_immediately_for_an_existing_provider() -> None:
    registry = ServiceRegistry()
    registry.provide("bibliography", "value")
    seen: list[object | None] = []
    registry.watch("bibliography", seen.append)
    assert seen == ["value"]


def test_disposing_a_watch_stops_notifications() -> None:
    registry = ServiceRegistry()
    seen: list[object | None] = []
    watch = registry.watch("bibliography", seen.append)
    watch.dispose()
    registry.provide("bibliography", "value")
    assert seen == [None]


def test_a_raising_watcher_does_not_block_the_others() -> None:
    registry = ServiceRegistry()
    seen: list[object | None] = []

    def boom(value: object | None) -> None:
        raise RuntimeError("watcher failed")

    registry.watch("svc", boom)
    registry.watch("svc", seen.append)
    registry.provide("svc", "value")
    assert seen == [None, "value"]


def test_bound_method_watchers_use_identity_not_equality() -> None:
    """Bound methods compare equal but are distinct objects.

    Pins the identity-based removal in unwatch(). Registering the same bound
    method twice creates two distinct objects that compare equal. If unwatch()
    uses equality (in/remove), disposing h2 will remove h1 (first match).
    """
    registry = ServiceRegistry()

    class TrackedCallback:
        """Callback that tracks invocations and can act equal to others."""

        _counter = 0

        def __init__(self) -> None:
            self.id = TrackedCallback._counter
            TrackedCallback._counter += 1
            self.calls: list[object | None] = []

        def __call__(self, value: object | None) -> None:
            self.calls.append(value)

        def __eq__(self, other: object) -> bool:
            # All TrackedCallback instances compare equal (simulates bound method equality)
            return isinstance(other, TrackedCallback)

        def __hash__(self) -> int:
            # All instances hash the same so they compare equal in set/dict ops
            return 0

    cb1 = TrackedCallback()
    cb2 = TrackedCallback()

    # Register two callbacks that are equal but distinct
    _h1 = registry.watch("svc", cb1)
    h2 = registry.watch("svc", cb2)

    # Both receive initial None
    assert cb1.calls == [None]
    assert cb2.calls == [None]

    provider = registry.provide("svc", "value1")
    # Both receive the event
    assert cb1.calls == [None, "value1"]
    assert cb2.calls == [None, "value1"]

    # Dispose h2 (should remove cb2 from watchers with correct identity-based impl)
    h2.dispose()

    # With identity fix: cb2 is removed, cb1 remains
    # With bug (equality-based): cb1 (first match) is removed, cb2 remains
    # We can tell by which one receives the next event:

    provider.dispose()
    registry.provide("svc", "value2")

    # With identity fix: cb1 receives value2 (it remained)
    # With bug: cb2 receives value2 (cb1 was incorrectly removed)
    assert "value2" in cb1.calls, "cb1 should remain active after h2.dispose()"
    assert "value2" not in cb2.calls, "cb2 should NOT receive events after h2.dispose()"


def test_disposing_stale_provider_after_reprovision() -> None:
    """Pins the identity check in revoke().

    Verifies that disposing an old provider handle doesn't remove a new
    provider that was registered after the old one was disposed.
    """
    registry = ServiceRegistry()

    # Provide first value and dispose it
    h1 = registry.provide("service", "value1")
    h1.dispose()

    # Provide second value with same name
    h2 = registry.provide("service", "value2")
    assert registry.get("service") == "value2"

    # Dispose h1 again (stale handle) — should not affect h2
    h1.dispose()
    assert registry.get("service") == "value2"

    # Dispose h2 to clean up
    h2.dispose()
    assert registry.get("service") is None


def test_watcher_that_disposes_another_during_notification() -> None:
    """Pins the snapshot in _notify().

    Verifies that when a watcher disposes another watcher during its callback,
    the disposed watcher still receives the current notification (because
    _notify() uses a snapshot).
    """
    registry = ServiceRegistry()
    seen_a: list[object | None] = []
    seen_b: list[object | None] = []
    seen_c: list[object | None] = []
    watch_b_ref: list[Disposable] = []

    def watcher_a(value: object | None) -> None:
        seen_a.append(value)
        # When notified, dispose watcher_b
        if value == "event":
            watch_b_ref[0].dispose()

    def watcher_b(value: object | None) -> None:
        seen_b.append(value)

    def watcher_c(value: object | None) -> None:
        seen_c.append(value)

    _watch_a = registry.watch("svc", watcher_a)
    watch_b_ref.append(registry.watch("svc", watcher_b))
    _watch_c = registry.watch("svc", watcher_c)

    # All receive initial None
    assert seen_a == [None]
    assert seen_b == [None]
    assert seen_c == [None]

    # Provide a value: watcher_a will dispose watcher_b during notification
    _provider = registry.provide("svc", "event")

    # With snapshot semantics, all three should receive the event
    # even though watcher_a disposes watcher_b during the notification round
    assert seen_a == [None, "event"]
    assert seen_b == [None, "event"]
    assert seen_c == [None, "event"]


def test_a_raising_watcher_is_recorded_rather_than_swallowed() -> None:
    """Nothing the registry catches may disappear: the failure is drainable."""
    registry = ServiceRegistry()

    def boom(value: object | None) -> None:
        raise RuntimeError("watcher failed")

    registry.watch("svc", boom)
    assert any("watcher failed" in e for e in registry.errors)
    assert any("svc" in e for e in registry.errors)

    registry.take_errors()
    registry.provide("svc", "value")
    assert any("watcher failed" in e for e in registry.errors)

    drained = registry.take_errors()
    assert drained != []
    assert registry.errors == []


def test_providing_none_is_rejected() -> None:
    """None means absent, so storing it would poison the name invisibly."""
    registry = ServiceRegistry()
    with pytest.raises(ValueError, match="absence is expressed by not providing"):
        registry.provide("bibliography", None)
    assert registry.get("bibliography") is None
    registry.provide("bibliography", "real")
    assert registry.get("bibliography") == "real"
