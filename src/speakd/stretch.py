"""Change speech rate without changing its pitch.

WSOLA: cut the input into overlapping windowed frames, lay them down at a
fixed output hop, and take each frame from wherever near its nominal input
position best continues the one before it. Playing samples faster instead
would raise the pitch with the rate, which is the one thing a listener speeding
up speech does not want.

Used only for audio already synthesised when the speed changes -- the sentence
playing and the one buffered behind it. Everything after is synthesised at the
new speed by the engine itself, which sounds better than any stretch.
"""

from __future__ import annotations

import math

import numpy as np

_FRAME_SECONDS = 0.030
_TOLERANCE_SECONDS = 0.005


def time_stretch(audio: np.ndarray, ratio: float, sample_rate: int) -> np.ndarray:
    """Return `audio` played `ratio` times as fast, at the same pitch."""
    if abs(ratio - 1.0) < 1e-3 or len(audio) == 0:
        return audio
    out_len = int(round(len(audio) / ratio))
    frame = int(_FRAME_SECONDS * sample_rate) // 2 * 2
    if len(audio) < 2 * frame:
        # Too short for a frame to have a neighbour. A tail this short is
        # heard as a click either way; resampling it keeps the length right.
        positions = np.linspace(0, len(audio) - 1, num=out_len)
        resampled: np.ndarray = np.interp(positions, np.arange(len(audio)), audio)
        return resampled.astype(np.float32)
    hop_out = frame // 2
    hop_in = hop_out * ratio
    tol = int(_TOLERANCE_SECONDS * sample_rate)
    pad_end = frame + 2 * tol + hop_out + math.ceil(hop_in)
    padded = np.concatenate(
        [np.zeros(tol, np.float32), audio.astype(np.float32), np.zeros(pad_end, np.float32)]
    )
    # Without its two zero endpoints: the first output sample is covered by
    # one frame only, and a window that is zero there makes it a dropout --
    # a click at the start of every stretch, which during a ramp is every
    # chunk.
    window = np.hanning(frame + 2)[1:-1].astype(np.float32)
    out = np.zeros(out_len + frame, np.float32)
    norm = np.zeros(out_len + frame, np.float32)
    previous = tol
    k = 0
    while k * hop_out < out_len:
        nominal = tol + int(round(k * hop_in))
        if k == 0:
            best = nominal
        else:
            # The frame that would have followed the last one had nothing
            # been stretched; the chosen frame is the one most like it.
            natural = padded[previous + hop_out : previous + hop_out + frame]
            region = padded[nominal - tol : nominal + tol + frame]
            best = nominal - tol + int(np.argmax(np.correlate(region, natural, mode="valid")))
        at = k * hop_out
        out[at : at + frame] += padded[best : best + frame] * window
        norm[at : at + frame] += window
        previous = best
        k += 1
    norm[norm < 1e-6] = 1.0
    return (out / norm)[:out_len].astype(np.float32)
