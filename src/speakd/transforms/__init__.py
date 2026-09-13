"""In-process transform tier.

Transforms run on the critical path to first audio, so they must be fast and
must not block on the network unless they declare themselves async and let
earlier segments start playing while they work.
"""

from __future__ import annotations

from typing import Protocol


class Transform(Protocol):
    """Rewrites job text into something worth speaking."""

    name: str

    def apply(self, text: str) -> str: ...
