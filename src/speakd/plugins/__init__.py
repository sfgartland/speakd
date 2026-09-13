"""Plugin host.

Two invariants, borrowed from arXiv:2608.25512 (spatiotemporal composability),
implemented minimally rather than by adopting Cordis itself:

  temporal — every registration returns a disposable, so unloading a plugin
  mechanically undoes its side effects, in reverse order;

  spatial — plugins declare the services they require, so they activate only
  when a provider exists and deactivate when it disappears.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol


class Disposable(Protocol):
    def dispose(self) -> None: ...


class Disposer:
    """Runs a teardown function exactly once."""

    def __init__(self, fn: Callable[[], None]) -> None:
        self._fn = fn
        self._disposed = False

    def dispose(self) -> None:
        if self._disposed:
            return
        self._disposed = True
        self._fn()


class DisposableGroup:
    """Disposes its members in reverse order, mirroring setup.

    One member raising must not strand the others: teardown that gives up
    halfway is how a half-unloaded plugin leaves registrations behind.
    """

    def __init__(self) -> None:
        self._members: list[Disposable] = []
        self._disposed = False

    def add(self, disposable: Disposable) -> None:
        self._members.append(disposable)

    def dispose(self) -> list[str]:
        """Dispose every member, returning the failures rather than raising."""
        if self._disposed:
            return []
        self._disposed = True
        errors: list[str] = []
        for member in reversed(self._members):
            try:
                member.dispose()
            except Exception as exc:
                errors.append(f"{type(member).__name__}: {exc}")
        self._members.clear()
        return errors
