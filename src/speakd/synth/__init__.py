"""Synthesiser tier.

Engines yield audio incrementally together with the source span each chunk
covers — the streaming contract that makes the timeline possible. Kokoro is
the only implementation for now; the interface exists so adding Piper or a
hosted engine is an adapter rather than a refactor.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any, Protocol

from speakd.model import Segment


class Synthesizer(Protocol):
    name: str

    def stream(self, text: str, voice: str, speed: float) -> Iterator[tuple[Segment, Any]]:
        """Yield (segment, audio) pairs as they are produced."""
        ...
