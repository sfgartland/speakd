"""Tests for the listener's speed multiplier."""

from speakd.tempo import MAX_SPEED, MIN_SPEED, Tempo, clamp_speed


def test_clamps_and_rounds_to_a_twentieth() -> None:
    assert clamp_speed(5) == MAX_SPEED
    assert clamp_speed(0.1) == MIN_SPEED
    assert clamp_speed(1.234) == 1.25
    assert clamp_speed(1.0) == 1.0


def test_set_returns_what_was_applied() -> None:
    tempo = Tempo()
    assert tempo.value == 1.0
    assert tempo.set(9) == MAX_SPEED
    assert tempo.value == MAX_SPEED


def test_a_constructed_value_is_clamped_too() -> None:
    assert Tempo(0.2).value == MIN_SPEED


class Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


def test_a_ramp_glides_to_its_target() -> None:
    clock = Clock()
    tempo = Tempo(1.0, clock=clock)
    assert tempo.set(1.4, ramp=2.0) == 1.4
    assert tempo.target == 1.4
    assert tempo.value == 1.0
    clock.now += 1.0
    assert tempo.value == 1.2
    clock.now += 1.0
    assert tempo.value == 1.4
    clock.now += 5.0
    assert tempo.value == 1.4


def test_a_ramp_interrupted_starts_from_where_it_had_got_to() -> None:
    clock = Clock()
    tempo = Tempo(1.0, clock=clock)
    tempo.set(1.4, ramp=2.0)
    clock.now += 1.0  # at 1.2
    tempo.set(1.0, ramp=1.0)
    assert tempo.value == 1.2
    clock.now += 0.5
    assert tempo.value == 1.1
    clock.now += 0.5
    assert tempo.value == 1.0


def test_no_ramp_is_immediate() -> None:
    tempo = Tempo(1.0, clock=Clock())
    tempo.set(0.7)
    assert tempo.value == 0.7
