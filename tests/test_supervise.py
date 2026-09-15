"""Tests for keeping the follower alive without risking the daemon."""

from speakd.supervise import Supervisor


class FakeChild:
    def __init__(self) -> None:
        self.alive = True
        self.terminated = False

    def poll(self) -> int | None:
        return None if self.alive else 1

    def terminate(self) -> None:
        self.terminated = True
        self.alive = False

    def wait(self, timeout: float | None = None) -> int:
        return 0


def supervisor(children: list[FakeChild], fail_first: int = 0) -> Supervisor:
    attempts = {"n": 0}

    def spawn() -> FakeChild:
        attempts["n"] += 1
        if attempts["n"] <= fail_first:
            raise OSError("no such file")
        child = FakeChild()
        children.append(child)
        return child

    s = Supervisor(["speakd-claude-follow"], backoff=(0.0,))
    s._spawn = spawn  # type: ignore[method-assign]
    return s


def test_it_spawns_once_on_start() -> None:
    children: list[FakeChild] = []
    s = supervisor(children)
    s.start()
    try:
        assert s.wait_for_children(1, timeout=2.0)
    finally:
        s.stop()


def test_a_child_that_dies_is_restarted() -> None:
    children: list[FakeChild] = []
    s = supervisor(children)
    s.start()
    try:
        assert s.wait_for_children(1, timeout=2.0)
        children[0].alive = False
        assert s.wait_for_children(2, timeout=2.0)
    finally:
        s.stop()


def test_stop_terminates_the_child_and_stops_restarting() -> None:
    children: list[FakeChild] = []
    s = supervisor(children)
    s.start()
    assert s.wait_for_children(1, timeout=2.0)
    s.stop()
    assert children[0].terminated
    assert not s.wait_for_children(2, timeout=0.3)


def test_a_spawn_that_fails_is_retried_rather_than_fatal() -> None:
    # A follower that cannot start must never stop the daemon speaking for
    # every other client.
    children: list[FakeChild] = []
    s = supervisor(children, fail_first=2)
    s.start()
    try:
        assert s.wait_for_children(1, timeout=3.0)
    finally:
        s.stop()


def test_stop_is_safe_before_start_and_twice() -> None:
    s = supervisor([])
    s.stop()
    s.start()
    s.stop()
    s.stop()
