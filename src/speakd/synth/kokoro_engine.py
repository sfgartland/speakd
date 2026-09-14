"""Kokoro adapter.

Kokoro's KPipeline splits text by its own `split_pattern`, which defaults to
newlines. Callers that flatten text therefore get one enormous segment and no
streaming at all. We hand it one already-segmented unit at a time and pin
`split_pattern` to a pattern that cannot match, so engine-side splitting is
mechanically off and the pipeline keeps sole control of latency.

Pinning it matters because a unit can contain a newline: the segmenter needs
`[.!?]` before whitespace to break a sentence, so a bare heading followed by
a newline and a body sentence stays one unit. Left at its default, Kokoro
would re-split exactly there — putting engine-side splitting back on the seam
this project closed. Milestone 3's markdown transform, which emits headings
and list items, would hit this constantly.

Kokoro also pads what it returns with near-silence at both ends -- measured
at about 0.21 s before the speech and 0.18 s after it -- and the pipeline
segments per sentence, so that padding is paid once per sentence rather than
once per utterance. A five-sentence utterance carried close to two seconds of
it. We cut it back to a residual as the audio leaves the engine, which is
what `trim_silence` below does.
"""

from __future__ import annotations

from typing import Any

import numpy as np

# An empty negative lookahead: zero-width, and fails at every position, so
# `re.split` with it never finds a boundary. Kokoro therefore treats whatever
# we hand it as exactly one unit, newlines included.
NO_SPLIT = r"(?!)"

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
    hand-built arrays without the `kokoro` extra, a model or an audio device.

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


class KokoroEngine:
    name = "kokoro"
    sample_rate = 24000

    def __init__(self, repo_id: str = "hexgrad/Kokoro-82M", lang_code: str = "a") -> None:
        import kokoro

        self._pipeline: Any = kokoro.KPipeline(lang_code=lang_code, repo_id=repo_id)

    def synthesize(self, text: str, voice: str, speed: float) -> np.ndarray:
        if not text.strip():
            return np.zeros(0, dtype=np.float32)
        results = self._pipeline(text, voice=voice, speed=speed, split_pattern=NO_SPLIT)
        chunks = [audio for _, _, audio in results if audio is not None]
        if not chunks:
            return np.zeros(0, dtype=np.float32)
        audio = np.concatenate([np.asarray(c, dtype=np.float32) for c in chunks])
        return trim_silence(audio, self.sample_rate)
