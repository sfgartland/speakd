"""Tests for the Kokoro adapter.

The adapter is misaki's G2P feeding a kokoro-onnx model. Everything here runs
against fakes for both packages, unconditionally: this is the one module where
a regression silently changes the volume or lets the engine re-split a unit, so
it must not depend on an extra that CI does not install.

The one test that needs the real model and the extra skips without them.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from speakd.synth import UnsupportedLanguage, kokoro_engine
from speakd.synth.kokoro_engine import (
    GAIN,
    LEADING_SILENCE_SECONDS,
    MODEL_FILENAME,
    TRAILING_SILENCE_SECONDS,
    VOICES_FILENAME,
    KokoroEngine,
)


class FakeG2P:
    """Records how it was built; phonemises to a fixed string like misaki does."""

    def __init__(self, kind: str, **kwargs: Any) -> None:
        self.kind = kind
        self.kwargs = kwargs
        self.calls: list[str] = []

    def __call__(self, text: str) -> tuple[str, Any]:
        self.calls.append(text)
        return f"ph:{text}", None


class FakeKokoroModel:
    """Stands in for kokoro_onnx.Kokoro: records create() and returns fixed audio."""

    instances: list[FakeKokoroModel] = []

    def __init__(self, model_path: str, voices_path: str) -> None:
        self.paths = (model_path, voices_path)
        self.audio: Any = np.ones(100, dtype=np.float32) * 0.1
        self.calls: list[tuple[str, dict[str, Any]]] = []
        FakeKokoroModel.instances.append(self)

    def create(self, phonemes: str, **kwargs: Any) -> tuple[Any, int]:
        self.calls.append((phonemes, kwargs))
        return self.audio, 24000


class Fakes:
    def __init__(self) -> None:
        self.g2ps: list[FakeG2P] = []


@pytest.fixture
def fakes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Fakes:
    """Fake `misaki` and `kokoro_onnx`, and a model directory that already has its files."""
    state = Fakes()
    FakeKokoroModel.instances = []

    def build(kind: str) -> Any:
        def make(*_args: Any, **kwargs: Any) -> FakeG2P:
            g2p = FakeG2P(kind, **kwargs)
            state.g2ps.append(g2p)
            return g2p

        return make

    misaki = types.ModuleType("misaki")
    en = types.ModuleType("misaki.en")
    en.G2P = build("en")  # type: ignore[attr-defined]
    espeak = types.ModuleType("misaki.espeak")
    espeak.EspeakFallback = lambda british: ("fallback", british)  # type: ignore[attr-defined]
    espeak.EspeakG2P = build("espeak")  # type: ignore[attr-defined]
    ja = types.ModuleType("misaki.ja")
    ja.JAG2P = build("ja")  # type: ignore[attr-defined]
    zh = types.ModuleType("misaki.zh")
    zh.ZHG2P = build("zh")  # type: ignore[attr-defined]
    for name, module in (("en", en), ("espeak", espeak), ("ja", ja), ("zh", zh)):
        setattr(misaki, name, module)
        monkeypatch.setitem(sys.modules, f"misaki.{name}", module)
    monkeypatch.setitem(sys.modules, "misaki", misaki)

    onnx = types.ModuleType("kokoro_onnx")
    onnx.Kokoro = FakeKokoroModel  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "kokoro_onnx", onnx)

    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    directory = tmp_path / "speakd" / "kokoro-onnx"
    directory.mkdir(parents=True)
    (directory / MODEL_FILENAME).write_bytes(b"m")
    (directory / VOICES_FILENAME).write_bytes(b"v")
    monkeypatch.delenv("SPEAKD_DEVICE", raising=False)
    return state


def model_of() -> FakeKokoroModel:
    return FakeKokoroModel.instances[-1]


def test_reports_its_identity_without_the_extra() -> None:
    assert KokoroEngine.sample_rate == 24000
    assert KokoroEngine.name == "kokoro"
    assert KokoroEngine.reads_years is True


def test_the_model_is_loaded_from_the_cache_directory(fakes: Fakes, tmp_path: Path) -> None:
    KokoroEngine()
    directory = tmp_path / "speakd" / "kokoro-onnx"
    assert FakeKokoroModel.instances[0].paths == (
        str(directory / MODEL_FILENAME),
        str(directory / VOICES_FILENAME),
    )


def test_a_missing_extra_is_a_module_not_found_error(
    fakes: Fakes, monkeypatch: pytest.MonkeyPatch
) -> None:
    # __main__ catches exactly this to say "install the extra" instead of
    # leaving a traceback on the loader thread.
    monkeypatch.setitem(sys.modules, "kokoro_onnx", None)
    with pytest.raises(ModuleNotFoundError):
        KokoroEngine()


@pytest.mark.parametrize(
    ("lang", "kind", "check"),
    [
        ("en", "en", {"british": False, "trf": False, "unk": ""}),
        ("en-gb", "en", {"british": True, "trf": False, "unk": ""}),
        ("fr", "espeak", {"language": "fr-fr"}),
        ("es", "espeak", {"language": "es"}),
        ("hi", "espeak", {"language": "hi"}),
        ("it", "espeak", {"language": "it"}),
        ("pt-br", "espeak", {"language": "pt-br"}),
        ("ja", "ja", {}),
        ("zh", "zh", {"version": None}),
    ],
)
def test_the_g2p_follows_the_kokoro_language_code(
    fakes: Fakes, lang: str, kind: str, check: dict[str, Any]
) -> None:
    engine = KokoroEngine()
    engine.synthesize("Hi.", voice="v", speed=1.0, lang=lang)
    g2p = [g for g in fakes.g2ps if g.kind == kind][-1]
    for key, value in check.items():
        assert g2p.kwargs[key] == value


def test_english_g2p_gets_the_espeak_fallback_for_its_accent(fakes: Fakes) -> None:
    engine = KokoroEngine()
    engine.synthesize("Hi.", voice="v", speed=1.0, lang="en-gb")
    assert fakes.g2ps[0].kwargs["fallback"] == ("fallback", True)


def test_chinese_hands_english_words_to_the_english_g2p(fakes: Fakes) -> None:
    engine = KokoroEngine()
    engine.synthesize("你好 hello", voice="zf_xiaobei", speed=1.0, lang="zh")
    zh = [g for g in fakes.g2ps if g.kind == "zh"][0]
    assert zh.kwargs["en_callable"]("hello") == "ph:hello"


def test_a_g2p_is_built_once_per_language_not_per_call(fakes: Fakes) -> None:
    engine = KokoroEngine()
    for _ in range(3):
        engine.synthesize("Hi.", voice="af_heart", speed=1.0, lang="en")
    engine.synthesize("Bonjour.", voice="ff_siwis", speed=1.0, lang="fr")
    assert [g.kind for g in fakes.g2ps] == ["en", "espeak"]


def test_one_model_serves_every_language(fakes: Fakes) -> None:
    engine = KokoroEngine()
    engine.synthesize("Hi.", voice="af_heart", speed=1.0, lang="en")
    engine.synthesize("Bonjour.", voice="ff_siwis", speed=1.0, lang="fr")
    assert len(FakeKokoroModel.instances) == 1
    assert len(model_of().calls) == 2


@pytest.mark.parametrize("lang", ["ja", "zh"])
def test_a_missing_ja_or_zh_extra_is_unsupported_language(
    fakes: Fakes, monkeypatch: pytest.MonkeyPatch, lang: str
) -> None:
    monkeypatch.setitem(sys.modules, f"misaki.{lang}", None)
    monkeypatch.delattr(sys.modules["misaki"], lang)
    engine = KokoroEngine()
    with pytest.raises(UnsupportedLanguage) as excinfo:
        engine.synthesize("x", voice="v", speed=1.0, lang=lang)
    assert excinfo.value.lang == lang


def test_a_language_with_no_kokoro_code_is_unsupported(fakes: Fakes) -> None:
    engine = KokoroEngine()
    with pytest.raises(UnsupportedLanguage):
        engine.synthesize("?", voice="v", speed=1.0, lang="de")


def test_phonemes_not_text_go_to_the_model_with_voice_and_speed(fakes: Fakes) -> None:
    engine = KokoroEngine()
    engine.synthesize("Hello.", voice="af_bella", speed=0.9)
    phonemes, kwargs = model_of().calls[0]
    assert phonemes == "ph:Hello."
    assert kwargs["voice"] == "af_bella"
    assert kwargs["speed"] == 0.9
    assert kwargs["is_phonemes"] is True
    # speakd's own trim is the only one: kokoro-onnx's would cut tighter and
    # add pauses the pipeline does not expect.
    assert kwargs["trim"] is False


@pytest.mark.parametrize("text", ["", "  \n\t "])
def test_empty_text_never_reaches_the_g2p_or_the_model(fakes: Fakes, text: str) -> None:
    engine = KokoroEngine()
    assert len(engine.synthesize(text, voice="v", speed=1.0)) == 0
    assert fakes.g2ps == []
    assert model_of().calls == []


def test_text_with_no_phonemes_gives_empty_audio(
    fakes: Fakes, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = KokoroEngine()

    def refuse(phonemes: str, **kwargs: Any) -> Any:
        raise ValueError("Nothing to synthesize")

    monkeypatch.setattr(model_of(), "create", refuse)
    audio = engine.synthesize("...", voice="v", speed=1.0)
    assert len(audio) == 0
    assert audio.dtype == np.float32


def test_audio_is_float32_and_scaled_by_the_gain(fakes: Fakes) -> None:
    engine = KokoroEngine()
    model_of().audio = np.full(4, 0.5, dtype=np.float64)
    audio = engine.synthesize("Hello.", voice="v", speed=1.0)
    assert audio.dtype == np.float32
    assert audio.tolist() == pytest.approx([0.5 * GAIN] * 4)


def test_the_gain_is_an_attenuation() -> None:
    # ONNX renders louder than torch did; the constant brings it back.
    assert 0.0 < GAIN < 1.0


def padded(lead: float, speech: int, trail: float) -> np.ndarray:
    rate = KokoroEngine.sample_rate
    audio = np.full(int(lead * rate) + speech + int(trail * rate), 1e-6, dtype=np.float32)
    audio[int(lead * rate) : int(lead * rate) + speech] = 0.5
    return audio


def test_the_padding_is_trimmed_off_what_the_engine_returns(fakes: Fakes) -> None:
    kept_lead = round(LEADING_SILENCE_SECONDS * KokoroEngine.sample_rate)
    kept_trail = round(TRAILING_SILENCE_SECONDS * KokoroEngine.sample_rate)
    engine = KokoroEngine()
    model_of().audio = padded(lead=0.28, speech=2400, trail=0.47)
    audio = engine.synthesize("Hello there.", voice="af_heart", speed=1.1)
    assert len(audio) == kept_lead + 2400 + kept_trail


def test_a_silent_result_keeps_its_length_rather_than_becoming_empty(fakes: Fakes) -> None:
    # Emptying it would collide with the contract's meaning for zero-length
    # audio, which the pipeline and player both read as "nothing to say".
    engine = KokoroEngine()
    model_of().audio = np.full(4800, 1e-6, dtype=np.float32)
    assert len(engine.synthesize("Hello there.", voice="v", speed=1.0)) == 4800


def test_speakd_device_is_ignored_silently(
    fakes: Fakes, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # Old service overrides still set it; onnxruntime on the CPU has no such choice.
    monkeypatch.setenv("SPEAKD_DEVICE", "gpu-that-does-not-exist")
    engine = KokoroEngine()
    assert len(engine.synthesize("Hi.", voice="v", speed=1.0)) > 0
    assert capsys.readouterr().err == ""


# -- model files ------------------------------------------------------------


def test_the_cache_directory_follows_xdg_cache_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    assert kokoro_engine.default_model_dir() == tmp_path / "speakd" / "kokoro-onnx"
    monkeypatch.delenv("XDG_CACHE_HOME")
    assert kokoro_engine.default_model_dir() == Path.home() / ".cache" / "speakd" / "kokoro-onnx"


def test_missing_files_are_downloaded_atomically(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    directory = tmp_path / "models"
    seen: list[str] = []

    def fetch(url: str, destination: Path) -> None:
        seen.append(url)
        # The real file must not exist until the download is complete.
        assert destination.name not in (MODEL_FILENAME, VOICES_FILENAME)
        destination.write_bytes(b"data")

    model, voices = kokoro_engine.ensure_model_files(directory, fetch=fetch)

    assert model.read_bytes() == b"data" and voices.read_bytes() == b"data"
    assert {url.rsplit("/", 1)[1] for url in seen} == {MODEL_FILENAME, VOICES_FILENAME}
    assert all(url.startswith(kokoro_engine.RELEASE_URL) for url in seen)
    assert sorted(p.name for p in directory.iterdir()) == [MODEL_FILENAME, VOICES_FILENAME]
    err = capsys.readouterr().err
    assert str(directory) in err and err.count("\n") == 1


def test_present_files_are_not_downloaded(tmp_path: Path) -> None:
    (tmp_path / MODEL_FILENAME).write_bytes(b"m")
    (tmp_path / VOICES_FILENAME).write_bytes(b"v")

    def fetch(url: str, destination: Path) -> None:
        raise AssertionError("downloaded a file that is there")

    kokoro_engine.ensure_model_files(tmp_path, fetch=fetch)


def test_a_failed_download_says_what_to_do_and_leaves_no_partial_file(tmp_path: Path) -> None:
    def fetch(url: str, destination: Path) -> None:
        destination.write_bytes(b"half")
        raise OSError("connection reset")

    with pytest.raises(RuntimeError) as excinfo:
        kokoro_engine.ensure_model_files(tmp_path, fetch=fetch)
    message = str(excinfo.value)
    assert "\n" not in message
    assert MODEL_FILENAME in message and str(tmp_path) in message
    assert "connection reset" in message
    assert list(tmp_path.iterdir()) == []


# -- real model -------------------------------------------------------------


def test_the_real_engine_speaks_citations_and_years() -> None:
    pytest.importorskip("kokoro_onnx", reason="requires the 'kokoro' extra")
    pytest.importorskip("misaki", reason="requires the 'kokoro' extra")
    directory = kokoro_engine.default_model_dir()
    if not (directory / MODEL_FILENAME).is_file() or not (directory / VOICES_FILENAME).is_file():
        pytest.skip(f"kokoro-onnx model files not in {directory}")
    engine = KokoroEngine()
    audio = engine.synthesize("Stiegler argued in 1994, pp. 34-38.", voice="af_heart", speed=1.1)
    assert audio.dtype == np.float32
    assert len(audio) > engine.sample_rate  # more than a second of speech
    assert float(np.abs(audio).max()) > 0.05
