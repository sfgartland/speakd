"""Tests for an engine the daemon can put down and pick up again."""

import threading

from speakd.synth.fake import FakeEngine
from speakd.synth.lazy import LazyEngine


def build(counter: list[int]) -> LazyEngine:
    def factory() -> FakeEngine:
        counter.append(1)
        return FakeEngine()

    return LazyEngine(factory, name="lazy", sample_rate=24000)


def test_it_starts_unloaded_and_builds_nothing() -> None:
    built: list[int] = []
    engine = build(built)
    assert not engine.loaded
    assert built == []


def test_sample_rate_is_known_while_unloaded() -> None:
    # build_player() reads it to open the sink, and must not need a model.
    assert build([]).sample_rate == 24000


def test_synthesising_while_unloaded_returns_silence_and_loads_nothing() -> None:
    # A stray enqueue must not undo a decision the user made deliberately,
    # nor pay thirty seconds to do it.
    built: list[int] = []
    engine = build(built)
    assert engine.synthesize("Hello.", "af_heart", 1.0).size == 0
    assert built == []


def test_loading_then_synthesising_produces_audio() -> None:
    built: list[int] = []
    engine = build(built)
    engine.load()
    assert engine.loaded
    assert built == [1]
    assert engine.synthesize("Hello.", "af_heart", 1.0).size > 0


def test_loading_twice_builds_once() -> None:
    built: list[int] = []
    engine = build(built)
    engine.load()
    engine.load()
    assert built == [1]


def test_unloading_drops_it_and_reloading_builds_again() -> None:
    built: list[int] = []
    engine = build(built)
    engine.load()
    engine.unload()
    assert not engine.loaded
    assert engine.synthesize("Hello.", "af_heart", 1.0).size == 0
    engine.load()
    assert built == [1, 1]


def test_unloading_when_never_loaded_is_not_an_error() -> None:
    build([]).unload()


def test_an_unload_during_a_load_is_not_undone_when_the_factory_returns() -> None:
    # The two decisions are ordered, and the user's is the later one: a
    # factory that has been working for thirty seconds must not outvote a
    # disable that arrived while it worked.
    building = threading.Event()
    release = threading.Event()

    def factory() -> FakeEngine:
        building.set()
        release.wait(5.0)
        return FakeEngine()

    engine = LazyEngine(factory, name="lazy", sample_rate=24000)
    loader = threading.Thread(target=engine.load, name="test-load")
    loader.start()
    try:
        assert building.wait(5.0)
        engine.unload()
    finally:
        release.set()
        loader.join(5.0)
    assert not engine.loaded
