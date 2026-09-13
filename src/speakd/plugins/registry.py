"""Service registry — the spatial half of composability.

A plugin declares what it needs rather than reaching for it, so the host can
activate it when a provider appears and deactivate it when one goes away.
"""

from __future__ import annotations

from collections.abc import Callable

from speakd.plugins import Disposable, Disposer

Watcher = Callable[[object | None], None]


class ServiceRegistry:
    """Named services, and watchers notified when they come and go."""

    def __init__(self) -> None:
        self._services: dict[str, object] = {}
        self._watchers: dict[str, list[Watcher]] = {}
        # A raising watcher must not stop the others being told, but it must not
        # vanish either -- a plugin that fails to activate is reported, never
        # silently dropped. Failures collect here until someone drains them;
        # PluginHost folds them into its own errors.
        self.errors: list[str] = []

    def take_errors(self) -> list[str]:
        """Return the watcher failures collected since the last call, and forget them."""
        drained = self.errors[:]
        self.errors.clear()
        return drained

    def get(self, name: str) -> object | None:
        return self._services.get(name)

    def provide(self, name: str, value: object) -> Disposable:
        """Register `value` under `name` until the returned handle is disposed."""
        if name in self._services:
            raise ValueError(f"service {name!r} is already provided")
        self._services[name] = value
        self._notify(name, value)

        def revoke() -> None:
            if self._services.get(name) is value:
                del self._services[name]
                self._notify(name, None)

        return Disposer(revoke)

    def watch(self, name: str, callback: Watcher) -> Disposable:
        """Call `callback` with the current value now, and on every change."""
        self._watchers.setdefault(name, []).append(callback)
        try:
            callback(self._services.get(name))
        except Exception as exc:
            self.errors.append(f"watcher for service {name!r} failed on registration: {exc}")

        def unwatch() -> None:
            watchers = self._watchers.get(name)
            if watchers is not None:
                self._watchers[name] = [w for w in watchers if w is not callback]

        return Disposer(unwatch)

    def _notify(self, name: str, value: object | None) -> None:
        # A failing watcher must not stop the others from being told.
        for callback in list(self._watchers.get(name, ())):
            try:
                callback(value)
            except Exception as exc:
                self.errors.append(f"watcher for service {name!r} failed: {exc}")
                continue
