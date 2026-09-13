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

    @property
    def disposed(self) -> bool:
        """Whether teardown has already run."""
        return self._disposed

    def add(self, disposable: Disposable) -> None:
        """Take ownership of `disposable`, disposing it now if the group is spent.

        A disposed group cannot hold anything: whatever it is handed would never
        be torn down by it. Disposing the newcomer at once keeps the temporal
        invariant exact -- every registration is still undone -- where appending
        leaks it. Raising was the alternative, and is rejected here because the
        other caller of this path is a member registering a teardown *during*
        teardown: dispose() flips the flag before walking the members, so such a
        member lands here too, and it would be turned into a second failure on
        top of whatever it was cleaning up after. It is disposed immediately
        instead of being dropped by the trailing _members.clear(); if that
        disposal raises during teardown, the exception surfaces in the caller,
        where dispose()'s own handler records it.
        """
        if self._disposed:
            disposable.dispose()
            return
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
