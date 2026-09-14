"""Tests for the padding trim, against synthetic arrays only.

The trim is a free function rather than a method so that it can be tested
without the `kokoro` extra, a model download or an audio device: every array
here is built by hand. The engine's own use of it is tested next door against
the stub pipeline.
"""

import numpy as np
import pytest

from speakd.synth.kokoro_engine import (
    LEADING_SILENCE_SECONDS,
    SILENCE_THRESHOLD,
    TRAILING_SILENCE_SECONDS,
    trim_silence,
)

SR = 1000  # one sample per millisecond, so a residual in seconds reads off as samples


def padded(lead: int, speech: int, trail: int, level: float = 0.5) -> np.ndarray:
    """Speech between two pads of sub-threshold dither, the shape Kokoro emits.

    The pads are noise near 1e-6 rather than zeros, because that is what the
    real engine produces -- a trim that tested for exact zero would find
    nothing to cut.
    """
    rng = np.random.default_rng(0)
    audio = rng.uniform(-1e-6, 1e-6, lead + speech + trail).astype(np.float32)
    audio[lead : lead + speech] = level
    return audio


def test_the_leading_pad_is_cut_back_to_the_residual() -> None:
    audio = padded(lead=500, speech=200, trail=500)
    out = trim_silence(audio, SR, leading=0.03, trailing=0.1)
    assert len(out) == 30 + 200 + 100
    assert float(np.abs(out[:30]).max()) <= SILENCE_THRESHOLD
    assert out[30] == pytest.approx(0.5)


def test_the_trailing_pad_is_cut_back_to_the_residual() -> None:
    audio = padded(lead=500, speech=200, trail=500)
    out = trim_silence(audio, SR, leading=0.03, trailing=0.1)
    assert out[-101] == pytest.approx(0.5)
    assert float(np.abs(out[-100:]).max()) <= SILENCE_THRESHOLD


def test_only_the_ends_are_cut_and_what_is_between_them_is_untouched() -> None:
    audio = padded(lead=500, speech=200, trail=500)
    out = trim_silence(audio, SR, leading=0.03, trailing=0.1)
    assert np.array_equal(out, audio[470:800])
    assert out.dtype == np.float32


def test_the_residuals_are_seconds_so_the_sample_rate_sets_their_length() -> None:
    audio = padded(lead=500, speech=200, trail=500)
    out = trim_silence(audio, 2000, leading=0.03, trailing=0.1)
    assert len(out) == 60 + 200 + 200


def test_the_default_residuals_are_used_when_none_are_given() -> None:
    audio = padded(lead=500, speech=200, trail=500)
    out = trim_silence(audio, SR)
    lead = round(LEADING_SILENCE_SECONDS * SR)
    trail = round(TRAILING_SILENCE_SECONDS * SR)
    assert len(out) == lead + 200 + trail


def test_a_pad_shorter_than_the_residual_is_left_alone() -> None:
    # The trim only ever shortens. Padding the other way would invent audio,
    # and a segment that arrived tight should stay tight.
    audio = padded(lead=5, speech=50, trail=20)
    out = trim_silence(audio, SR, leading=0.03, trailing=0.1)
    assert np.array_equal(out, audio)


def test_speech_in_the_very_first_sample_is_not_wrapped_round_the_end() -> None:
    # A negative start index would slice from the tail instead of clamping,
    # returning a later chunk of the buffer and losing the speech entirely.
    audio = np.zeros(400, dtype=np.float32)
    audio[0] = 0.5
    out = trim_silence(audio, SR, leading=0.03, trailing=0.1)
    assert out[0] == pytest.approx(0.5)
    assert len(out) == 101


def test_speech_in_the_very_last_sample_keeps_what_there_is() -> None:
    audio = np.zeros(400, dtype=np.float32)
    audio[-1] = 0.5
    out = trim_silence(audio, SR, leading=0.03, trailing=0.1)
    assert out[-1] == pytest.approx(0.5)
    assert len(out) == 31


def test_a_sample_exactly_at_the_threshold_counts_as_silence() -> None:
    # Both levels are exact in float32, so this pins the comparison itself
    # rather than a rounding accident.
    audio = np.zeros(600, dtype=np.float32)
    audio[100] = 0.25
    audio[300] = 0.5
    out = trim_silence(audio, SR, threshold=0.25, leading=0.03, trailing=0.1)
    assert len(out) == 30 + 1 + 100
    assert out[30] == pytest.approx(0.5)


def test_audio_with_nothing_above_the_floor_is_returned_unchanged() -> None:
    # `np.zeros(0)` is reserved by the Synthesizer contract for blank text and
    # for a pipeline that yielded nothing. Audio that merely holds no speech
    # is a third thing, and emptying it here would tell the pipeline a lie
    # about how long the segment lasts.
    audio = np.full(500, 1e-6, dtype=np.float32)
    out = trim_silence(audio, SR)
    assert np.array_equal(out, audio)


def test_empty_audio_stays_empty_and_float32() -> None:
    out = trim_silence(np.zeros(0, dtype=np.float32), SR)
    assert len(out) == 0
    assert out.dtype == np.float32


def test_the_threshold_sits_between_the_measured_pad_and_the_measured_speech() -> None:
    # Measured over 135 Kokoro clips (27 texts x 5 voice/speed combinations):
    # the loudest sample anywhere inside a pad was 1.99e-6 and the quietest
    # genuine speech onset was 1.02e-5. The threshold has to separate those
    # two populations, with room on both sides.
    assert 1.99e-6 < SILENCE_THRESHOLD < 1.02e-5
    # And below the level at which a 16-bit sample rounds to zero, so nothing
    # it discards could have survived quantisation anyway.
    assert SILENCE_THRESHOLD < 0.5 / 32768


def test_the_leading_residual_is_the_smaller_one() -> None:
    # The pause a listener hears between two sentences is delivered by the
    # previous segment's trailing residual. A leading residual buys nothing
    # perceptual and is charged straight to time-to-first-audio.
    assert 0 < LEADING_SILENCE_SECONDS < TRAILING_SILENCE_SECONDS
