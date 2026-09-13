"""Tests for the event bus."""

from speakd.events import Event, EventBus


def test_a_subscriber_receives_published_events() -> None:
    bus = EventBus()
    seen: list[Event] = []
    bus.subscribe(seen.append)
    event = Event(kind="position", source_id="s1", data={"offset": 4})
    bus.publish(event)
    assert seen == [event]


def test_subscribers_can_filter_by_kind() -> None:
    bus = EventBus()
    positions: list[Event] = []
    bus.subscribe(positions.append, kinds=["position"])
    bus.publish(Event(kind="started", source_id="s1", data={}))
    bus.publish(Event(kind="position", source_id="s1", data={}))
    assert [e.kind for e in positions] == ["position"]


def test_disposing_a_subscription_stops_delivery() -> None:
    bus = EventBus()
    seen: list[Event] = []
    handle = bus.subscribe(seen.append)
    handle.dispose()
    bus.publish(Event(kind="position", source_id="s1", data={}))
    assert seen == []
    assert bus.subscriber_count() == 0


def test_a_raising_subscriber_is_dropped_and_the_others_still_receive() -> None:
    bus = EventBus()
    seen: list[Event] = []

    def boom(event: Event) -> None:
        raise RuntimeError("subscriber died")

    bus.subscribe(boom)
    bus.subscribe(seen.append)
    bus.publish(Event(kind="position", source_id="s1", data={}))
    bus.publish(Event(kind="position", source_id="s1", data={}))
    assert len(seen) == 2
    assert bus.subscriber_count() == 1


def test_publishing_with_no_subscribers_is_harmless() -> None:
    EventBus().publish(Event(kind="position", source_id="s1", data={}))


def test_subscribing_during_publish_does_not_disturb_the_current_delivery() -> None:
    bus = EventBus()
    seen: list[str] = []

    def adder(event: Event) -> None:
        seen.append("first")
        bus.subscribe(lambda e: seen.append("late"))

    bus.subscribe(adder)
    bus.publish(Event(kind="position", source_id="s1", data={}))
    assert seen == ["first"]
    bus.publish(Event(kind="position", source_id="s1", data={}))
    assert seen == ["first", "first", "late"]
