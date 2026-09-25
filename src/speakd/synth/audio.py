"""Audio helpers shared by engines, importable without an engine's extras.

`trim_silence` and its constants started life in `kokoro_engine.py`, where they
trimmed the padding Kokoro wraps its output in. Piper needs the same trim --
the plan's requirement that sentence gaps be the same on both engines -- and
importing Kokoro's module for it would drag an engine behind every other one.
So the trim lives here, and `kokoro_engine` re-exports it to keep its surface.
"""

from __future__ import annotations

import numpy as np

# Above this, a sample counts as speech. Not zero: Kokoro's padding is not
# digital silence but dither around 1e-6, so a test for exact zero finds
# nothing to trim.
#
# The number is measured, not chosen. Across 135 clips -- 27 texts, five
# voice/speed combinations -- the loudest sample found anywhere inside a pad
# was 1.99e-6, and the quietest genuine speech onset (the soft fricative
# opening "Should he have known?") was 1.02e-5. Those two populations do not
# overlap, and 5e-6 is very near the geometric centre of the gap between
# them: 2.5x above the loudest padding, 2.0x below the softest speech.
#
# It is also -106 dBFS, which is about 10 dB below the level at which a
# 16-bit sample rounds to zero. Nothing discarded at this threshold could
# have survived quantisation, let alone been heard.
SILENCE_THRESHOLD = 5e-6

# What is left standing at each end, in seconds. Trimming to zero is the
# wrong target: sentences need a pause between them or the speech runs
# together. The defect is that Kokoro's pad is excessive, not that it exists.
#
# The two ends are not worth the same, which is why they differ. The pause a
# listener hears between two sentences is delivered by the *trailing*
# residual of the one before; a leading residual adds nothing to it and is
# charged directly to time-to-first-audio, the number this project has spent
# real effort on. So the leading residual is sized as pure safety margin
# instead: across the same 135 clips, the distance between a 5e-6 crossing
# and a 3e-5 crossing never exceeded 25 ms, so 30 ms absorbs a six-fold
# error in the threshold without touching a syllable.
#
# The trailing residual is the audible one. 0.1 s is the short end of a
# natural sentence-final pause, and it sits on top of the final syllable's
# own decay, which this keeps -- that decay is speech, not padding. Measured
# at a loud threshold the pad looks like half a second, but most of that
# half second is the decay; trimming to it would clip the last word.
LEADING_SILENCE_SECONDS = 0.03
TRAILING_SILENCE_SECONDS = 0.1


def trim_silence(
    audio: np.ndarray,
    sample_rate: int,
    threshold: float = SILENCE_THRESHOLD,
    leading: float = LEADING_SILENCE_SECONDS,
    trailing: float = TRAILING_SILENCE_SECONDS,
) -> np.ndarray:
    """Cut the padding around the speech back to a residual at each end.

    A free function, and not a method, so that it can be tested against
    hand-built arrays without an engine's extra, a model or an audio device.

    Only ever shortens. Where a segment arrives with less padding than the
    residual the trim stops at the buffer's own ends and it comes back as it
    was: padding audio out to a target length would invent silence the engine
    never produced.

    Audio with nothing above `threshold` is returned unchanged rather than
    emptied. `np.zeros(0)` belongs to the two cases the `Synthesizer`
    contract gives it -- blank text, and a pipeline that yielded nothing --
    and a segment that merely holds no speech is a third thing, whose length
    the pipeline's timeline still has to account for.
    """
    loud = np.flatnonzero(np.abs(audio) > threshold)
    if loud.size == 0:
        return audio
    # Clamped at the low end only, and the asymmetry is deliberate: a
    # negative start would slice from the tail and hand back a later chunk of
    # the buffer with the speech gone, where an end past the last sample is
    # exactly what slicing already does. A `min` here would be a line no test
    # could ever fail on.
    start = max(0, int(loud[0]) - round(leading * sample_rate))
    end = int(loud[-1]) + 1 + round(trailing * sample_rate)
    return audio[start:end]
