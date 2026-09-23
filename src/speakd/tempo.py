"""The listener's speed: one number, shared by the synthesiser and the player.

A multiplier on the profile's own speed rather than a speed of its own, so
that 1.0 always means "as the profile was tuned" whichever profile is speaking.
Read on the speech thread and written on the control thread, hence the lock.
"""

from __future__ import annotations

import threading

MIN_SPEED = 0.7
MAX_SPEED = 1.6
_STEP = 0.05


def clamp_speed(value: float) -> float:
    """Bound a requested speed and put it on the step the controls move in."""
    bounded = min(max(float(value), MIN_SPEED), MAX_SPEED)
    return round(round(bounded / _STEP) * _STEP, 2)


class Tempo:
    def __init__(self, value: float = 1.0) -> None:
        self._lock = threading.Lock()
        self._value = clamp_speed(value)

    @property
    def value(self) -> float:
        with self._lock:
            return self._value

    def set(self, value: float) -> float:
        """Take a new speed, answering with the one actually applied."""
        applied = clamp_speed(value)
        with self._lock:
            self._value = applied
        return applied
