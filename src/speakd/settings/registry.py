"""The runtime registry: declarations by owner, effective values, change events.

One `Settings` object is owned by the daemon (`speakd.daemon.Daemon`) and
reached through three verbs. It is the single place that knows every
declared setting and what it is currently worth -- the store underneath it
holds only values and raw declaration dicts, and does no validation of its
own.
"""

from __future__ import annotations

import sys
from collections.abc import Callable, Sequence
from typing import Protocol

from speakd.settings.store import SettingsStore
from speakd.settings.types import Declaration, SettingError, parse_declaration, to_json, validate


def _warn(message: str) -> None:
    print(f"speakd: {message}", file=sys.stderr)


class Disposable(Protocol):
    def dispose(self) -> None: ...


class _Subscription:
    """Handle for `on_change`. Structurally a plugin `Disposable`, like
    `speakd.events.Subscription` -- this module does not import that one,
    since settings and the event bus are two different fan-outs with
    nothing else in common."""

    def __init__(self, unsubscribe: Callable[[], None]) -> None:
        self._unsubscribe = unsubscribe
        self._disposed = False

    def dispose(self) -> None:
        if self._disposed:
            return
        self._disposed = True
        self._unsubscribe()


class Settings:
    def __init__(self, store: SettingsStore) -> None:
        self._store = store
        # owner -> name -> Declaration. Split this way, rather than one flat
        # dict keyed by the full "owner.name", so `undeclare` and `schema`
        # can act on exactly one owner without scanning every key.
        self._by_owner: dict[str, dict[str, Declaration]] = {}
        self._listeners: list[Callable[[str, object], None]] = []
        # Persisted client schemas exist before any client of this run has
        # declared anything, so an offline client's settings still show and
        # can still be edited. A `declare()` later in the same owner's name
        # replaces this wholesale, which is what "the current run overrides
        # what was persisted" means in practice.
        for owner, raws in store.load_schema().items():
            self._declare_owner(owner, raws, source=f"{owner} (persisted schema)")

    def _declare_owner(
        self, owner: str, raws: Sequence[dict[str, object]], *, source: str
    ) -> dict[str, Declaration]:
        parsed: dict[str, Declaration] = {}
        for raw in raws:
            try:
                decl = parse_declaration(owner, raw)
            except SettingError as exc:
                # One bad declaration must not cost every other one this
                # owner sent -- a plugin with nine good settings and a typo
                # in the tenth should not lose all ten.
                _warn(f"{source}: {exc}")
                continue
            _, _, name = decl.key.partition(".")
            parsed[name] = decl
        self._by_owner[owner] = parsed
        return parsed

    def declare(self, owner: str, raws: Sequence[dict[str, object]], *, persist: bool) -> int:
        """Replace `owner`'s declarations. Clients persist; core and plugins
        do not, since they redeclare at every start and persisting them would
        just be a slower way of saying the same thing every time."""
        raws = list(raws)
        parsed = self._declare_owner(owner, raws, source=owner)
        if persist:
            self._store.write_schema(owner, raws)
        return len(parsed)

    def declare_one(self, owner: str, raw: dict[str, object]) -> Declaration:
        """Add or replace a single setting of `owner`'s, keeping its others.

        `declare()` replaces an owner's whole schema at once, which is right
        for a client sending its full list in one `declare_settings` call.
        A plugin's `PluginContext.setting()` calls this once per setting as
        its `setup()` runs, and a second such call must not erase the first
        -- so this merges into `owner`'s table instead of starting it over.
        Never persisted: a plugin redeclares at every start, like core does.
        """
        decl = parse_declaration(owner, raw)
        _, _, name = decl.key.partition(".")
        self._by_owner.setdefault(owner, {})[name] = decl
        return decl

    def undeclare(self, owner: str, names: Sequence[str]) -> None:
        """Forget these settings of `owner`'s, for plugin disposal."""
        table = self._by_owner.get(owner)
        if not table:
            return
        for name in names:
            table.pop(name, None)
        if not table:
            del self._by_owner[owner]

    def _declaration(self, key: str) -> Declaration | None:
        owner, _, name = key.partition(".")
        return self._by_owner.get(owner, {}).get(name)

    def schema(self, owner: str | None = None) -> list[dict[str, object]]:
        owners = [owner] if owner is not None else sorted(self._by_owner)
        return [
            to_json(self._by_owner[o][name])
            for o in owners
            for name in sorted(self._by_owner.get(o, {}))
        ]

    def values(self, owner: str | None = None) -> dict[str, object]:
        """Effective values for every declared setting, defaults where nothing
        is stored. Re-reads the store each time, the same way `get` does, so
        a value changed by hand between two calls is seen on the next one."""
        stored = self._store.load_values()
        owners = [owner] if owner is not None else sorted(self._by_owner)
        result: dict[str, object] = {}
        for o in owners:
            for decl in self._by_owner.get(o, {}).values():
                result[decl.key] = self._effective(decl, stored)
        return result

    def _effective(self, decl: Declaration, stored: dict[str, object]) -> object:
        if decl.key not in stored:
            return decl.default
        try:
            return validate(decl, stored[decl.key])
        except SettingError as exc:
            # A value from before a redeclaration changed this key's type, or
            # a settings.toml edited by hand into something that no longer
            # fits. Either way, a warning and the default -- never a daemon
            # that fails to start over one bad value.
            _warn(f"{decl.key}: stored value no longer valid ({exc}); using the default")
            return decl.default

    def get(self, key: str) -> object:
        decl = self._declaration(key)
        if decl is None:
            raise SettingError(f"{key}: no such setting is declared")
        return self._effective(decl, self._store.load_values())

    def set(self, key: str, value: object) -> object:
        """Validate, persist, and notify -- in that order, so a value that
        fails validation is never written and never fires `on_change`."""
        decl = self._declaration(key)
        if decl is None:
            raise SettingError(f"{key}: no such setting is declared")
        validated = validate(decl, value)
        self._store.write_value(key, validated)
        for listener in list(self._listeners):
            listener(key, validated)
        return validated

    def on_change(self, callback: Callable[[str, object], None]) -> Disposable:
        self._listeners.append(callback)
        return _Subscription(lambda: self._remove_listener(callback))

    def _remove_listener(self, callback: Callable[[str, object], None]) -> None:
        try:
            self._listeners.remove(callback)
        except ValueError:
            pass
