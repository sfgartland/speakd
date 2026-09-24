"""Tests for choosing where Kokoro runs: the CPU, CUDA, or whichever works."""

import sys
import types

import pytest

from speakd.synth import kokoro_engine


class FakeKokoro(types.ModuleType):
    """Stands in for `kokoro`, recording the device each pipeline was asked for."""

    def __init__(self, cuda_fails: bool) -> None:
        super().__init__("kokoro")
        self.devices: list[str | None] = []
        outer = self

        class KPipeline:
            def __init__(self, lang_code: str, repo_id: str, device: str | None = None) -> None:
                outer.devices.append(device)
                if device == "cuda" and cuda_fails:
                    raise RuntimeError(
                        "cuDNN version 92400 is not compatible with devices with SM < 7.5"
                    )

        self.KPipeline = KPipeline


def install(monkeypatch, *, cuda_available: bool, cuda_fails: bool) -> FakeKokoro:  # type: ignore[no-untyped-def]
    fake = FakeKokoro(cuda_fails)
    torch = types.ModuleType("torch")
    torch.cuda = types.SimpleNamespace(is_available=lambda: cuda_available)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "kokoro", fake)
    monkeypatch.setitem(sys.modules, "torch", torch)
    return fake


def test_auto_falls_back_to_the_cpu_when_cuda_will_not_start(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.delenv("SPEAKD_DEVICE", raising=False)
    fake = install(monkeypatch, cuda_available=True, cuda_fails=True)
    engine = kokoro_engine.KokoroEngine()
    assert fake.devices == ["cuda", "cpu"]
    assert engine.device == "cpu"


def test_auto_uses_a_gpu_that_works(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.delenv("SPEAKD_DEVICE", raising=False)
    fake = install(monkeypatch, cuda_available=True, cuda_fails=False)
    assert kokoro_engine.KokoroEngine().device == "cuda"
    assert fake.devices == ["cuda"]


def test_auto_without_a_gpu_never_tries_cuda(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.delenv("SPEAKD_DEVICE", raising=False)
    fake = install(monkeypatch, cuda_available=False, cuda_fails=True)
    assert kokoro_engine.KokoroEngine().device == "cpu"
    assert fake.devices == ["cpu"]


def test_cpu_is_honoured_even_with_a_working_gpu(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("SPEAKD_DEVICE", "cpu")
    fake = install(monkeypatch, cuda_available=True, cuda_fails=False)
    assert kokoro_engine.KokoroEngine().device == "cpu"
    assert fake.devices == ["cpu"]


def test_cuda_asked_for_by_name_is_not_quietly_replaced(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("SPEAKD_DEVICE", "cuda")
    install(monkeypatch, cuda_available=True, cuda_fails=True)
    with pytest.raises(RuntimeError, match="cuDNN"):
        kokoro_engine.KokoroEngine()


def test_an_unknown_setting_says_what_is_accepted(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("SPEAKD_DEVICE", "gpu")
    install(monkeypatch, cuda_available=True, cuda_fails=False)
    with pytest.raises(ValueError, match="cpu, cuda or auto"):
        kokoro_engine.KokoroEngine()
