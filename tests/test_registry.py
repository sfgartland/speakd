"""Tests for the service registry."""

import pytest

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
