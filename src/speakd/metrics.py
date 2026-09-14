"""What the pinned monitor reads: how fast synthesis is, and how big we are.

Every number here is gathered from state the speech worker has already
written down, on whatever thread asks for it. Nothing in this module is
allowed to make the worker wait: its critical path is synthesis and playback,
and a measurement that blocks it is a liveness bug, not a slow monitor.

The worker's only part in this is `SynthesisWindow.record` -- one append per
synthesised segment, under a lock held for the length of a list operation.
"""

from __future__ import annotations

import os
import threading
from collections import deque
from pathlib import Path

# How many segments the real-time factor is averaged over. Segments are
# sentence-sized (`segmenter.DEFAULT_MAX_CHARS` is 180), so five of them is
# roughly fifteen to twenty-five seconds of speech: recent enough to say what
# the machine is doing now, long enough that one short segment cannot take
# the number over. That last part is the reason for a window at all. There is
# a fixed cost near a second in every synthesize() call, so a "Done." of a
# third of a second reads as an RTF above 3 on its own; averaged in with four
# ordinary segments it moves the answer by about 0.05, which is the honest
# size of its contribution.
_WINDOW_SEGMENTS = 5

# Linux reports resident set as the second field of this file, in pages.
_STATM = Path("/proc/self/statm")


class SynthesisWindow:
    """A trailing record of what synthesis cost against the audio it produced.

    Written by the pipeline's producer thread, read by whoever is emitting
    metrics. The lock is held only for a bounded list copy -- at most
    `_WINDOW_SEGMENTS` pairs, no I/O -- which is the same discipline
    `EventBus.publish` uses to snapshot its subscribers from the speech
    thread.
    """

    def __init__(self, size: int = _WINDOW_SEGMENTS) -> None:
        self._samples: deque[tuple[float, float]] = deque(maxlen=size)
        self._lock = threading.Lock()

    def __len__(self) -> int:
        with self._lock:
            return len(self._samples)

    def record(self, synthesis_seconds: float, audio_seconds: float) -> None:
        """Note one synthesize() call: what it cost, and what it made."""
        with self._lock:
            self._samples.append((synthesis_seconds, audio_seconds))

    def rtf(self) -> float | None:
        """Synthesis seconds per second of audio, or None if nothing is known.

        Summed and then divided, rather than averaged over the per-segment
        ratios: the question is what the last few seconds of speech cost, and
        a quarter-second segment should not weigh as much as a five-second
        one. None rather than a number when no audio has been produced yet --
        a monitor showing nothing is right where 0.0, which means infinitely
        fast, would be a lie.
        """
        with self._lock:
            samples = list(self._samples)
        audio = sum(seconds for _, seconds in samples)
        if audio <= 0:
            return None
        return sum(cost for cost, _ in samples) / audio


def resident_bytes(statm: Path = _STATM) -> int | None:
    """This process's resident set, or None where there is nothing to read.

    No dependency: `/proc/self/statm` is two syscalls and a split, where
    `psutil` is a build. None rather than an exception or a guess on a
    platform without `/proc` -- the field is simply left out of the event,
    which is the one honest answer when the number is not knowable.
    """
    try:
        fields = statm.read_text(encoding="utf-8").split()
    except OSError:
        return None
    if len(fields) < 2:
        return None
    try:
        pages = int(fields[1])
    except ValueError:
        # A `/proc` that is not Linux's. Reporting nothing beats reporting a
        # number parsed out of a file that means something else.
        return None
    return pages * os.sysconf("SC_PAGE_SIZE")
