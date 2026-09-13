"""Tests for disposable registrations."""

from collections.abc import Callable

from speakd.plugins import DisposableGroup, Disposer


def test_disposer_calls_its_function() -> None:
    calls: list[str] = []

    def on_dispose() -> None:
        calls.append("gone")

    Disposer(on_dispose).dispose()
    assert calls == ["gone"]


def test_disposer_is_idempotent() -> None:
    calls: list[str] = []

    def on_dispose() -> None:
        calls.append("gone")

    d = Disposer(on_dispose)
    d.dispose()
    d.dispose()
    assert calls == ["gone"]


def test_group_disposes_in_reverse_order() -> None:
    order: list[int] = []
    group = DisposableGroup()
    for i in range(3):

        def make_appender(index: int) -> Callable[[], None]:
            def append_order() -> None:
                order.append(index)

            return append_order

        group.add(Disposer(make_appender(i)))
    group.dispose()
    assert order == [2, 1, 0]


def test_group_is_idempotent() -> None:
    order: list[int] = []
    group = DisposableGroup()

    def on_dispose() -> None:
        order.append(1)

    group.add(Disposer(on_dispose))
    group.dispose()
    group.dispose()
    assert order == [1]


def test_group_disposes_the_rest_when_one_raises() -> None:
    order: list[int] = []

    def boom() -> None:
        raise RuntimeError("teardown failed")

    group = DisposableGroup()

    def append_0() -> None:
        order.append(0)

    def append_2() -> None:
        order.append(2)

    group.add(Disposer(append_0))
    group.add(Disposer(boom))
    group.add(Disposer(append_2))
    errors = group.dispose()
    assert order == [2, 0]
    assert len(errors) == 1
    assert "teardown failed" in errors[0]
