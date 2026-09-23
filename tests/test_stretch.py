"""Tests for pitch-preserving time-stretch."""

import numpy as np

from speakd.stretch import time_stretch

SR = 24000


def sine(freq: float, seconds: float) -> np.ndarray:
    t = np.arange(int(SR * seconds)) / SR
    return (0.5 * np.sin(2 * np.pi * freq * t)).astype(np.float32)


def dominant(audio: np.ndarray) -> float:
    spectrum = np.abs(np.fft.rfft(audio * np.hanning(len(audio))))
    return float(np.fft.rfftfreq(len(audio), 1 / SR)[int(np.argmax(spectrum))])


def test_faster_shortens_by_the_ratio() -> None:
    out = time_stretch(sine(220, 1.0), 1.5, SR)
    assert abs(len(out) - SR / 1.5) <= 2


def test_slower_lengthens_by_the_ratio() -> None:
    out = time_stretch(sine(220, 1.0), 0.8, SR)
    assert abs(len(out) - SR / 0.8) <= 2


def test_pitch_is_kept() -> None:
    for ratio in (0.7, 1.3, 1.6):
        assert abs(dominant(time_stretch(sine(220, 1.0), ratio, SR)) - 220) < 6


def test_ratio_one_is_the_identity() -> None:
    audio = sine(220, 0.2)
    assert time_stretch(audio, 1.0, SR) is audio


def test_short_and_empty_input_are_safe() -> None:
    assert len(time_stretch(np.zeros(0, dtype=np.float32), 1.5, SR)) == 0
    out = time_stretch(sine(220, 0.01), 1.5, SR)
    assert abs(len(out) - int(round(SR * 0.01 / 1.5))) <= 1
    assert out.dtype == np.float32
