"""Tests for the Piper adapter, against a fake `piper` and `onnxruntime`.

Piper's real package needs ONNX models and espeak-ng, so everything except the
one gated real-voice test runs against a fake pair installed into `sys.modules`
-- the way `test_engine_device.py` fakes `kokoro`. The fake `InferenceSession`
records its `sess_options` and `providers`, which is how the thread pinning is
tested without onnxruntime's own behaviour, and the fake `PiperVoice` records
the text and synthesis config it is handed.
"""

import importlib.machinery
import json
import os
import subprocess
import sys
import types
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from speakd.synth.audio import SILENCE_THRESHOLD
from speakd.synth.piper_engine import PiperEngine, PiperVoiceError, piper_available


class FakePiperConfig:
    """Stands in for piper.config.PiperConfig, down to `from_dict`'s shape."""

    def __init__(self, sample_rate: int) -> None:
        self.sample_rate = sample_rate

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "FakePiperConfig":
        return FakePiperConfig(data["audio"]["sample_rate"])


class FakeSynthesisConfig:
    """Stands in for piper.config.SynthesisConfig."""

    def __init__(self, length_scale: float = 1.0) -> None:
        self.length_scale = length_scale


class FakeSessionOptions:
    """Stands in for onnxruntime.SessionOptions.

    `intra_op_num_threads` starts absent rather than at the real class's zero,
    so a test can tell "never set" apart from "set to some number": the engine
    must skip it entirely when threads is 0.
    """

    def __init__(self) -> None:
        self.inter_op_num_threads = 0
        self.intra_op_num_threads: int | None = None


class FakeInferenceSession:
    """Stands in for onnxruntime.InferenceSession, recording how it was built."""

    def __init__(self, path: str, sess_options: FakeSessionOptions, providers: list[str]) -> None:
        self.path = path
        self.sess_options = sess_options
        self.providers = providers


class FakeAudioChunk:
    """Stands in for piper.voice.AudioChunk."""

    def __init__(self, audio: np.ndarray) -> None:
        self.audio_float_array = audio


class FakePiperVoice:
    """Stands in for piper.PiperVoice, recording its calls.

    Yields whatever chunks the owning `FakePiper` module is currently told to
    produce, at the config's own sample rate.
    """

    def __init__(
        self,
        config: FakePiperConfig,
        session: FakeInferenceSession,
        parent: "FakePiper",
    ) -> None:
        self.config = config
        self.session = session
        self._parent = parent
        self.texts: list[str] = []
        self.syn_configs: list[FakeSynthesisConfig] = []
        parent.voices.append(self)

    def synthesize(self, text: str, syn_config: FakeSynthesisConfig) -> Iterator[FakeAudioChunk]:
        self.texts.append(text)
        self.syn_configs.append(syn_config)
        for audio in self._parent.chunk_factory():
            yield FakeAudioChunk(audio)


class FakePiper(types.ModuleType):
    """Stands in for `piper`: every session and voice the engine builds, recorded."""

    def __init__(self) -> None:
        super().__init__("piper")
        self.sessions: list[FakeInferenceSession] = []
        self.voices: list[FakePiperVoice] = []
        # How many of the next session constructions fail, one each.
        self.fail_sessions = 0
        self.chunk_factory: Callable[[], list[np.ndarray]] = lambda: [
            np.full(22050, 0.5, dtype=np.float32)
        ]
        parent = self

        class _Voice(FakePiperVoice):
            def __init__(self, config: FakePiperConfig, session: FakeInferenceSession) -> None:
                super().__init__(config, session, parent)

        self.PiperVoice = _Voice


class FakeConfigModule(types.ModuleType):
    """Stands in for `piper.config`, holding what the engine imports from it."""

    def __init__(self) -> None:
        super().__init__("piper.config")
        self.PiperConfig = FakePiperConfig
        self.SynthesisConfig = FakeSynthesisConfig


class FakeOnnxRuntime(types.ModuleType):
    """Stands in for `onnxruntime`, recording sessions on the owning `FakePiper`."""

    def __init__(self, parent: FakePiper) -> None:
        super().__init__("onnxruntime")
        self.SessionOptions = FakeSessionOptions

        def make_session(
            path: str, sess_options: FakeSessionOptions, providers: list[str]
        ) -> FakeInferenceSession:
            if parent.fail_sessions:
                parent.fail_sessions -= 1
                raise RuntimeError("the model file would not load")
            session = FakeInferenceSession(path, sess_options, providers)
            parent.sessions.append(session)
            return session

        self.InferenceSession = make_session


@pytest.fixture
def fake_piper(monkeypatch: pytest.MonkeyPatch) -> FakePiper:
    """Install fakes for `piper`, `piper.config` and `onnxruntime` into sys.modules."""
    fake = FakePiper()
    monkeypatch.setitem(sys.modules, "piper", fake)
    monkeypatch.setitem(sys.modules, "piper.config", FakeConfigModule())
    monkeypatch.setitem(sys.modules, "onnxruntime", FakeOnnxRuntime(fake))
    return fake


@pytest.fixture
def needs_scipy() -> None:
    """Skip unless scipy is installed: `synthesize` lazily imports it for the resample.

    scipy comes only with the optional `piper` extra, which CI does not install;
    every other test in this file runs against fakes and must keep running.
    """
    pytest.importorskip("scipy", reason="the piper extra (which brings scipy) is not installed")


def write_voice(directory: Path, name: str, rate: int = 22050) -> None:
    """The two files a Piper voice is: an .onnx and its .onnx.json config."""
    (directory / f"{name}.onnx").write_bytes(b"not really onnx")
    (directory / f"{name}.onnx.json").write_text(json.dumps({"audio": {"sample_rate": rate}}))


def test_a_voice_loads_one_session_reused_across_calls(
    fake_piper: FakePiper, tmp_path: Path, needs_scipy: None
) -> None:
    write_voice(tmp_path, "voice-a")
    write_voice(tmp_path, "voice-b")
    engine = PiperEngine(tmp_path, threads=1)
    engine.synthesize("Hello.", "voice-a", 1.0)
    engine.synthesize("Again.", "voice-a", 1.0)
    engine.synthesize("Bonjour.", "voice-b", 1.0)
    assert [s.path for s in fake_piper.sessions] == [
        str(tmp_path / "voice-a.onnx"),
        str(tmp_path / "voice-b.onnx"),
    ]
    assert len(fake_piper.voices) == 2


def test_threads_reach_the_session_options(
    fake_piper: FakePiper, tmp_path: Path, needs_scipy: None
) -> None:
    write_voice(tmp_path, "voice-a")
    engine = PiperEngine(tmp_path, threads=3)
    engine.synthesize("Hello.", "voice-a", 1.0)
    options = fake_piper.sessions[0].sess_options
    assert options.intra_op_num_threads == 3
    assert options.inter_op_num_threads == 1
    assert fake_piper.sessions[0].providers == ["CPUExecutionProvider"]


def test_zero_threads_leaves_the_intra_setting_unset(
    fake_piper: FakePiper, tmp_path: Path, needs_scipy: None
) -> None:
    # 0 means "onnxruntime's own default", so the engine must not write the
    # setting at all -- writing a 0 would be the same thing today, but would
    # pin whatever 0 happens to mean in a later onnxruntime.
    write_voice(tmp_path, "voice-a")
    engine = PiperEngine(tmp_path, threads=0)
    engine.synthesize("Hello.", "voice-a", 1.0)
    options = fake_piper.sessions[0].sess_options
    assert options.intra_op_num_threads is None
    assert options.inter_op_num_threads == 1


def test_speed_maps_to_the_inverse_length_scale(
    fake_piper: FakePiper, tmp_path: Path, needs_scipy: None
) -> None:
    write_voice(tmp_path, "voice-a")
    engine = PiperEngine(tmp_path, threads=1)
    engine.synthesize("Hello.", "voice-a", 2.0)
    assert fake_piper.voices[0].syn_configs[-1].length_scale == 0.5


def test_audio_is_resampled_to_the_engines_rate(
    fake_piper: FakePiper, tmp_path: Path, needs_scipy: None
) -> None:
    write_voice(tmp_path, "voice-a", rate=22050)
    fake_piper.chunk_factory = lambda: [np.full(44100, 0.5, dtype=np.float32)]
    engine = PiperEngine(tmp_path, threads=1)
    audio = engine.synthesize("Hello.", "voice-a", 1.0)
    assert audio.dtype == np.float32
    assert len(audio) == pytest.approx(44100 * 24000 / 22050, abs=2)


def test_years_are_spoken_as_words_before_synthesis(
    fake_piper: FakePiper, tmp_path: Path, needs_scipy: None
) -> None:
    write_voice(tmp_path, "voice-a")
    engine = PiperEngine(tmp_path, threads=1)
    engine.synthesize("In 1994, Habermas replied.", "voice-a", 1.0)
    assert fake_piper.voices[0].texts == ["In nineteen ninety-four, Habermas replied."]


def test_has_voice_needs_both_files(tmp_path: Path) -> None:
    engine = PiperEngine(tmp_path, threads=1)
    (tmp_path / "v.onnx").write_bytes(b"")
    assert not engine.has_voice("v")
    (tmp_path / "v.onnx.json").write_text("{}")
    assert engine.has_voice("v")


def test_installed_voices_lists_sorted_stems_that_have_configs(tmp_path: Path) -> None:
    write_voice(tmp_path, "zeta")
    write_voice(tmp_path, "alpha")
    (tmp_path / "orphan.onnx").write_bytes(b"")
    engine = PiperEngine(tmp_path, threads=1)
    assert engine.installed_voices() == ["alpha", "zeta"]


def test_a_voice_that_will_not_load_raises_and_is_not_cached(
    fake_piper: FakePiper, tmp_path: Path, needs_scipy: None
) -> None:
    write_voice(tmp_path, "voice-a")
    fake_piper.fail_sessions = 1
    engine = PiperEngine(tmp_path, threads=1)
    with pytest.raises(PiperVoiceError) as excinfo:
        engine.synthesize("Hello.", "voice-a", 1.0)
    assert excinfo.value.voice == "voice-a"
    assert "would not load" in excinfo.value.reason
    audio = engine.synthesize("Hello.", "voice-a", 1.0)
    assert len(audio) > 0
    assert len(fake_piper.sessions) == 1


def test_unload_forces_the_next_synthesize_to_reload(
    fake_piper: FakePiper, tmp_path: Path, needs_scipy: None
) -> None:
    write_voice(tmp_path, "voice-a")
    engine = PiperEngine(tmp_path, threads=1)
    engine.synthesize("Hello.", "voice-a", 1.0)
    engine.unload()
    engine.synthesize("Hello.", "voice-a", 1.0)
    assert len(fake_piper.sessions) == 2


def test_set_voice_dir_unloads_and_points_at_the_new_directory(
    fake_piper: FakePiper, tmp_path: Path, needs_scipy: None
) -> None:
    write_voice(tmp_path, "voice-a")
    engine = PiperEngine(tmp_path, threads=1)
    engine.synthesize("Hello.", "voice-a", 1.0)
    other = tmp_path / "other"
    other.mkdir()
    write_voice(other, "voice-b")
    engine.set_voice_dir(other)
    engine.synthesize("Hello.", "voice-b", 1.0)
    assert len(fake_piper.sessions) == 2
    assert engine.has_voice("voice-b")
    assert not engine.has_voice("voice-a")


def test_blank_text_never_loads_a_voice(fake_piper: FakePiper, tmp_path: Path) -> None:
    write_voice(tmp_path, "voice-a")
    engine = PiperEngine(tmp_path, threads=1)
    audio = engine.synthesize("   ", "voice-a", 1.0)
    assert len(audio) == 0
    assert fake_piper.sessions == []


def test_piper_available_when_the_module_is_importable(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = types.ModuleType("piper")
    fake.__spec__ = importlib.machinery.ModuleSpec("piper", loader=None)
    monkeypatch.setitem(sys.modules, "piper", fake)
    assert piper_available()


def test_piper_unavailable_when_the_import_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "piper", None)
    assert not piper_available()


def test_importing_the_engine_imports_neither_piper_nor_onnxruntime() -> None:
    # A subprocess rather than an assertion about this interpreter's
    # `sys.modules`: pytest has already imported numpy by the time any test
    # runs, and the fakes above have put `piper` there too.
    source = "import sys; import speakd.synth.piper_engine; print('\\n'.join(sorted(sys.modules)))"
    result = subprocess.run(
        [sys.executable, "-c", source], capture_output=True, text=True, check=True
    )
    loaded = set(result.stdout.split())
    assert "piper" not in loaded
    assert "onnxruntime" not in loaded
    assert "scipy" not in loaded


def _voice_dir() -> Path:
    """Piper's voice directory, as the `speech.piper_voice_dir` default derives it.

    `$XDG_DATA_HOME`, falling back to `~/.local/share` -- the same rule the
    setting's default uses, so this test follows the daemon to wherever the
    user's data actually lives.
    """
    data_home = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    return data_home / "piper-voices"


_REAL_VOICE = "en_US-ryan-medium"

# Long enough that the comparison is a measurement over many phonemes rather
# than a single hop-length rounding.
_REAL_PASSAGE = (
    "This is a much longer passage, several sentences long, so that the fixed "
    "overhead of the synthesiser does not dominate the comparison. Piper speaks "
    "quickly and cheaply, and the whole point of doubling the speed is to halve "
    "the time it takes to hear this sentence. Every extra word here shrinks the "
    "noise in the ratio, which is exactly what a duration test needs."
)

real_voice = pytest.mark.skipif(
    not (piper_available() and (_voice_dir() / f"{_REAL_VOICE}.onnx").exists()),
    reason="requires the 'piper' extra and the en_US-ryan-medium voice",
)


@real_voice
def test_a_real_voice_comes_out_at_24khz_with_speed_scaling_and_trimmed_edges() -> None:
    engine = PiperEngine(_voice_dir(), threads=1)
    normal = engine.synthesize(_REAL_PASSAGE, _REAL_VOICE, 1.0)
    fast = engine.synthesize(_REAL_PASSAGE, _REAL_VOICE, 2.0)
    assert engine.sample_rate == 24000
    assert normal.dtype == np.float32
    assert float(np.abs(normal).max()) > 0.0
    # length_scale is 1/speed, so doubled speed should halve the duration.
    # Piper's duration predictor does not quite reach half: measured across
    # runs it lands at ~0.64x, and is nondeterministic by about a percent
    # per call (same session, same inputs, one thread). 0.75 pins "roughly
    # halved" with room above the whole observed noise band.
    assert len(fast) < 0.75 * len(normal)
    loud = np.flatnonzero(np.abs(normal) > SILENCE_THRESHOLD)
    lead = int(loud[0]) / engine.sample_rate
    trail = (len(normal) - 1 - int(loud[-1])) / engine.sample_rate
    assert lead < 0.05
    assert trail < 0.05
