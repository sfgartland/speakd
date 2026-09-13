"""Applying a chain of transforms, one bad plugin at a time.

A transform that raises — or returns something that is not a sequence of
pieces — is skipped, and the pieces it was given continue down the chain
unchanged. Losing a transform's contribution is a cost worth paying; losing
the speech is not.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from speakd.model import Piece
from speakd.plugins.host import RegisteredTransform


@dataclass
class ChainResult:
    pieces: list[Piece]
    errors: list[str] = field(default_factory=list)


def apply_chain(
    pieces: Sequence[Piece],
    transforms: Sequence[RegisteredTransform],
) -> ChainResult:
    """Run `pieces` through `transforms` in order, surviving individual failures."""
    current = list(pieces)
    errors: list[str] = []
    for transform in transforms:
        try:
            produced = transform.fn(list(current))
            candidate = list(produced)
        except Exception as exc:
            errors.append(f"{transform.name}: {exc}")
            continue
        if any(not isinstance(p, Piece) for p in candidate):
            errors.append(f"{transform.name}: returned something other than pieces")
            continue
        current = candidate
    return ChainResult(pieces=current, errors=errors)
