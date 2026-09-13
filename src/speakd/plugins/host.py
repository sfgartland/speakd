"""Plugin host: activation gated on declared services, teardown by disposal."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from functools import partial

from speakd.model import Piece
from speakd.plugins import Disposable, DisposableGroup, Disposer
from speakd.plugins.registry import ServiceRegistry

TransformFn = Callable[[Sequence[Piece]], Sequence[Piece]]


@dataclass(frozen=True)
class RegisteredTransform:
    name: str
    fn: TransformFn
    scope: str
    plugin: str


class PluginContext:
    """A plugin's handle on the host. Every registration is undoable."""

    def __init__(self, host: PluginHost, plugin: str, group: DisposableGroup) -> None:
        self._host = host
        self._plugin = plugin
        self._group = group

    def require(self, name: str) -> object:
        """The service this plugin declared. Present, because activation waited."""
        value = self._host.registry.get(name)
        if value is None:
            raise LookupError(f"service {name!r} is not available")
        return value

    def _reject_if_spent(self, action: str) -> None:
        """Refuse to touch the host once this context's group has been disposed.

        A context outlives the plugin that was handed it -- a captured `ctx`, a
        callback that fires late -- and a registration made through a spent one
        can never be undone: the group that would have owned the disposer is
        gone. Registering into the host first and adding the disposer second is
        what let a spent context inject a live transform, so the guard comes
        before the host is touched at all. It raises rather than no-ops because
        a registration that quietly does nothing is exactly the silent failure
        this host exists to prevent.
        """
        if self._group.disposed:
            raise RuntimeError(
                f"plugin {self._plugin!r}: cannot {action} after the plugin was unloaded"
            )

    def provide(self, name: str, value: object) -> None:
        self._reject_if_spent(f"provide service {name!r}")
        self._group.add(self._host.registry.provide(name, value))

    def transform(self, name: str, fn: TransformFn, scope: str = "piece") -> None:
        self._reject_if_spent(f"register transform {name!r}")
        if scope not in ("piece", "job"):
            raise ValueError(f"scope must be 'piece' or 'job', got {scope!r}")
        registered = RegisteredTransform(name=name, fn=fn, scope=scope, plugin=self._plugin)
        self._host._transforms.append(registered)
        self._group.add(Disposer(lambda: self._host._transforms.remove(registered)))

    def on_dispose(self, fn: Callable[[], None]) -> None:
        self._group.add(Disposer(fn))


@dataclass
class _Plugin:
    name: str
    setup: Callable[[PluginContext], None]
    requires: tuple[str, ...]
    group: DisposableGroup | None = None
    watches: tuple[Disposable, ...] = ()


class PluginHost:
    """Loads plugins, and keeps them active exactly while their services exist."""

    def __init__(self, registry: ServiceRegistry) -> None:
        self.registry = registry
        self._plugins: dict[str, _Plugin] = {}
        self._transforms: list[RegisteredTransform] = []
        self._errors: list[str] = []

    def transforms(self) -> list[RegisteredTransform]:
        return list(self._transforms)

    def active(self) -> set[str]:
        return {name for name, p in self._plugins.items() if p.group is not None}

    def errors(self) -> list[str]:
        return list(self._errors)

    def register(
        self,
        name: str,
        setup: Callable[[PluginContext], None],
        requires: Sequence[str] = (),
    ) -> None:
        # Replacing a name must unload what it held. Overwriting the entry
        # dropped the old plugin's group and watches on the floor, leaving its
        # transforms live in the host but owned by nobody: unreachable by
        # unregister, still resolvable by a profile.
        self.unregister(name)
        plugin = _Plugin(name=name, setup=setup, requires=tuple(requires))
        self._plugins[name] = plugin
        # Watching before the first evaluation means a provider appearing later
        # activates the plugin without anyone re-running registration.
        plugin.watches = tuple(
            self.registry.watch(service, partial(self._on_service_change, name))
            for service in plugin.requires
        )
        if not plugin.requires:
            self._activate(plugin)

    def unregister(self, name: str) -> None:
        plugin = self._plugins.pop(name, None)
        if plugin is None:
            return
        self._deactivate(plugin)
        for watch in plugin.watches:
            watch.dispose()

    def _on_service_change(self, name: str, _value: object | None) -> None:
        self._reevaluate(name)

    def _reevaluate(self, name: str) -> None:
        plugin = self._plugins.get(name)
        if plugin is None:
            return
        satisfied = all(self.registry.get(s) is not None for s in plugin.requires)
        if satisfied and plugin.group is None:
            self._activate(plugin)
        elif not satisfied and plugin.group is not None:
            self._deactivate(plugin)

    def _activate(self, plugin: _Plugin) -> None:
        group = DisposableGroup()
        context = PluginContext(self, plugin.name, group)
        try:
            plugin.setup(context)
        except Exception as exc:
            # A half-registered plugin is worse than an absent one: undo it.
            # A teardown failure during that rollback must not vanish either --
            # mirror _deactivate and record it too, instead of the bare call.
            self._errors.extend(f"{plugin.name}: {e}" for e in group.dispose())
            self._errors.append(f"{plugin.name}: {exc}")
            return
        plugin.group = group

    def _deactivate(self, plugin: _Plugin) -> None:
        if plugin.group is None:
            return
        self._errors.extend(f"{plugin.name}: {e}" for e in plugin.group.dispose())
        plugin.group = None
