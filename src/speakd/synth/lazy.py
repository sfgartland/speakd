"""A Synthesizer the daemon can put down and pick up again.

`KokoroEngine.__init__` *is* the model load, and the loaded model is some three
gigabytes resident. This wrapper separates "which engine" from "is it loaded",
so a daemon can be disabled without exiting, and can start disabled without
loading three gigabytes for the sole purpose of freeing them.

`sample_rate` is given at construction rather than read off the engine, because
`build_player()` needs it to open the sink while nothing is loaded. On
`KokoroEngine` it is a class attribute, so the caller has it without an
instance.

`synthesize` on an unloaded engine returns silence and does **not** load.
Loading implicitly would let a stray enqueue undo a decision the user made
deliberately, and spend thirty seconds doing it.
"""

from __future__ import annotations

import gc
import threading
from collections.abc import Callable

import numpy as np

from speakd.synth import Synthesizer


class LazyEngine:
    name: str
    sample_rate: int

    def __init__(
        self,
        factory: Callable[[], Synthesizer],
        *,
        name: str = "lazy",
        sample_rate: int = 24000,
    ) -> None:
        self._factory = factory
        self.name = name
        self.sample_rate = sample_rate
        self._engine: Synthesizer | None = None
        self._loading = False
        # Counted rather than flagged, so a load can tell "nothing happened
        # while I worked" from "an unload happened while I worked" -- the two
        # leave `_engine` looking identical.
        self._unloads = 0
        # Load and unload arrive on a control thread while the speech worker
        # is reading `_engine`; the flag pair and the reference move together
        # or a worker sees `loaded` true with nothing behind it.
        self._lock = threading.Lock()

    @property
    def loaded(self) -> bool:
        with self._lock:
            return self._engine is not None

    @property
    def loading(self) -> bool:
        with self._lock:
            return self._loading

    def load(self) -> None:
        with self._lock:
            if self._engine is not None or self._loading:
                return
            self._loading = True
            unloads = self._unloads
        try:
            engine = self._factory()
        finally:
            with self._lock:
                self._loading = False
        with self._lock:
            # An `unload()` that ran while the factory was working is the later
            # decision, so it wins: assigning here would resurrect a model the
            # user just asked to be rid of, thirty seconds after they asked.
            # `_engine is None` cannot see this, being equally true of the
            # ordinary case; the count can.
            if self._unloads != unloads:
                return
            if self._engine is None:
                self._engine = engine

    def unload(self) -> None:
        with self._lock:
            self._engine = None
            self._unloads += 1
        # The tensors are freed to Python here; whether the resident set
        # returns to the OS is the allocator's business, and is measured
        # rather than assumed.
        gc.collect()

    def synthesize(self, text: str, voice: str, speed: float) -> np.ndarray:
        with self._lock:
            engine = self._engine
        if engine is None:
            return np.zeros(0, dtype=np.float32)
        return engine.synthesize(text, voice, speed)
