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
    assert group.dispose() == []
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


def test_group_disposes_a_member_added_after_teardown() -> None:
    """A spent group cannot hold anything, so it tears down what it is handed."""
    order: list[str] = []
    group = DisposableGroup()
    group.dispose()

    def late() -> None:
        order.append("late")

    group.add(Disposer(late))
    assert order == ["late"]


def test_a_member_registered_during_teardown_is_not_dropped() -> None:
    """dispose() flips the flag before walking, so a late add lands on the guard.

    Without it the trailing _members.clear() drops the newcomer silently: a
    teardown that registers its own follow-up never runs it.
    """
    order: list[str] = []
    group = DisposableGroup()

    def late() -> None:
        order.append("late")

    def teardown() -> None:
        order.append("first")
        group.add(Disposer(late))

    group.add(Disposer(teardown))
    assert group.dispose() == []
    assert order == ["first", "late"]
