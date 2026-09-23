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
