"""The listener's speed: one number, shared by the synthesiser and the player.

A multiplier on the profile's own speed rather than a speed of its own, so
that 1.0 always means "as the profile was tuned" whichever profile is speaking.
Read on the speech thread and written on the control thread, hence the lock.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable

MIN_SPEED = 0.7
MAX_SPEED = 1.6
_STEP = 0.05


def clamp_speed(value: float) -> float:
    """Bound a requested speed and put it on the step the controls move in."""
    bounded = min(max(float(value), MIN_SPEED), MAX_SPEED)
    return round(round(bounded / _STEP) * _STEP, 2)


class Tempo:
    """The multiplier, and a ramp towards a new one.

    A ramp is what makes skimming bearable: a voice that jumps from 1.0 to
    1.4 mid-word sounds like a fault, one that speeds up over a second sounds
    like a decision. `value` is where the ramp has got to now; `target` is
    where it is going, which is what a client asked for and is told back.
    """

    def __init__(self, value: float = 1.0, clock: Callable[[], float] = time.monotonic) -> None:
        self._lock = threading.Lock()
        self._clock = clock
        self._from = self._to = clamp_speed(value)
        self._started = 0.0
        self._ramp = 0.0

    def _at(self, now: float) -> float:
        """Caller holds `_lock`."""
        if self._ramp <= 0 or now >= self._started + self._ramp:
            return self._to
        done = (now - self._started) / self._ramp
        # To the hundredth: the player re-stretches whenever this moves, and a
        # value that moved on every read would have it re-stretch every chunk
        # for no audible difference.
        return round(self._from + (self._to - self._from) * done, 2)

    @property
    def value(self) -> float:
        with self._lock:
            return self._at(self._clock())

    @property
    def target(self) -> float:
        with self._lock:
            return self._to

    def set(self, value: float, ramp: float = 0.0) -> float:
        """Head for a new speed, over `ramp` seconds, answering with the one applied.

        A ramp that is interrupted starts the next one from wherever it had
        got to, so letting go of a key halfway through a speed-up does not
        snap to the top first.
        """
        applied = clamp_speed(value)
        with self._lock:
            now = self._clock()
            self._from = self._at(now)
            self._to = applied
            self._started = now
            self._ramp = max(0.0, ramp)
        return applied
