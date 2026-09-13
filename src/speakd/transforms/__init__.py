"""In-process transform tier.

Transforms run on the critical path to first audio, so they must be fast.
Each declares a scope:

  piece — applied as pieces flow, so the job starts speaking as soon as the
  first piece clears the chain;

  job — gates the whole job. The summariser is job-scoped: a summary cannot be
  spoken before it exists.

A transform that raises is skipped and its input passes through unchanged.
Speech never disappears silently.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Literal, Protocol

from speakd.model import Piece

Scope = Literal["piece", "job"]


class Transform(Protocol):
    """Rewrites pieces into something worth speaking, preserving provenance."""

    name: str
    scope: Scope

    def apply(self, pieces: Sequence[Piece]) -> Sequence[Piece]: ...
