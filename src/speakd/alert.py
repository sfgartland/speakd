"""The sound speakd makes when it cannot say something it was asked to.

Generated rather than shipped as a file: two short falling tones, soft-edged
so they do not click, quiet enough to sit under whatever else is playing. It
is played through the daemon's own player, so it reaches the same sink as
speech and needs no model -- the point of it is to be heard when the engine
that would have spoken has failed.
"""

from __future__ import annotations

import numpy as np

# A falling fifth: reads as "something did not work" without sounding like an
# alarm, and is unlike anything a voice produces.
_TONES_HZ = (880.0, 587.33)
_TONE_SECONDS = 0.14
_GAP_SECONDS = 0.04
_FADE_SECONDS = 0.012
_LEVEL = 0.25


def chime(sample_rate: int) -> np.ndarray:
    """The alert, as float32 mono at `sample_rate`."""
    count = int(_TONE_SECONDS * sample_rate)
    t = np.arange(count) / sample_rate
    fade = min(int(_FADE_SECONDS * sample_rate), count // 2)
    envelope = np.ones(count)
    if fade:
        ramp = np.linspace(0.0, 1.0, fade)
        envelope[:fade] = ramp
        envelope[-fade:] = ramp[::-1]
    gap = np.zeros(int(_GAP_SECONDS * sample_rate))
    parts: list[np.ndarray] = []
    for hz in _TONES_HZ:
        parts.append(_LEVEL * envelope * np.sin(2 * np.pi * hz * t))
        parts.append(gap)
    return np.concatenate(parts).astype(np.float32)
