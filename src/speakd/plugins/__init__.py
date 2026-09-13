"""Plugin host.

Two invariants, borrowed from arXiv:2608.25512 (spatiotemporal composability):

  temporal — every registration returns a disposable, so unloading a plugin
  mechanically undoes its side effects;

  spatial — plugins declare the services they require, so they activate only
  when a provider exists and deactivate when it disappears.
"""

from __future__ import annotations

from typing import Protocol


class Disposable(Protocol):
    def dispose(self) -> None: ...
