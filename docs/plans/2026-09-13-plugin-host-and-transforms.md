# Plugin Host and Transforms Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make everything opinionated a plugin — a host that loads and unloads transforms cleanly, and the two built-in transforms that turn agent output into something worth hearing.

**Architecture:** A service registry plus disposable registrations gives the two composability invariants: unloading a plugin mechanically undoes its side effects, and a plugin activates only while the services it declares are present. Transforms map `Piece` to `Piece`, preserving provenance; a profile names an ordered chain of them.

**Tech Stack:** Python 3.11–3.13, stdlib only (`tomllib` for profiles), pytest, ruff, mypy strict.

**Spec:** `docs/design/2026-09-13-speakd-design.md`

**Runs concurrently with:** the daemon plan. That plan owns `daemon.py`, `protocol.py`, `channels.py`, `events.py`, `cli.py`, and the plan-1 files (`model.py`, `timeline.py`, `pipeline.py`, `player.py`). **This plan must not touch any of them.** Channels reference a profile by name and resolve it through an injected callable, so the two plans share no module.

## Global Constraints

- Python `>=3.11,<3.14` — `misaki`, a kokoro dependency, does not support 3.14; `tomllib` is standard library only from 3.11.
- Core dependencies stay minimal: numpy only. Add nothing to `pyproject.toml`. Profiles use `tomllib` from the standard library.
- No test may require torch or an audio device.
- Imports of optional dependencies happen inside functions, never at module import time.
- **A transform that rewrites `spoken` must clear `exact`.** Provenance cannot be inferred from string length.
- **Speech never disappears silently:** a transform that raises is skipped, its failure recorded, and its input passed through unchanged.
- ruff line length 100. `uv run pytest`, `uv run ruff check .`, `uv run ruff format --check .` and `uv run mypy src tests` must all pass before every commit.
- Commit after every task.

---

### Task 1: Disposables

**Files:**
- Modify: `src/speakd/plugins/__init__.py`
- Test: `tests/test_disposables.py`

**Interfaces:**
- Produces: `Disposable` Protocol with `dispose() -> None`; `Disposer(fn: Callable[[], None])`; `DisposableGroup()` with `add(d: Disposable) -> None` and `dispose() -> None`

Temporal composability rests entirely on this: every registration hands back something that undoes it, and a group undoes its members in reverse order so teardown mirrors setup.

- [ ] **Step 1: Write the failing test**

```python
"""Tests for disposable registrations."""

from speakd.plugins import Disposer, DisposableGroup


def test_disposer_calls_its_function() -> None:
    calls: list[str] = []
    Disposer(lambda: calls.append("gone")).dispose()
    assert calls == ["gone"]


def test_disposer_is_idempotent() -> None:
    calls: list[str] = []
    d = Disposer(lambda: calls.append("gone"))
    d.dispose()
    d.dispose()
    assert calls == ["gone"]


def test_group_disposes_in_reverse_order() -> None:
    order: list[int] = []
    group = DisposableGroup()
    for i in range(3):
        group.add(Disposer(lambda i=i: order.append(i)))
    group.dispose()
    assert order == [2, 1, 0]


def test_group_is_idempotent() -> None:
    order: list[int] = []
    group = DisposableGroup()
    group.add(Disposer(lambda: order.append(1)))
    group.dispose()
    group.dispose()
    assert order == [1]


def test_group_disposes_the_rest_when_one_raises() -> None:
    order: list[int] = []

    def boom() -> None:
        raise RuntimeError("teardown failed")

    group = DisposableGroup()
    group.add(Disposer(lambda: order.append(0)))
    group.add(Disposer(boom))
    group.add(Disposer(lambda: order.append(2)))
    errors = group.dispose()
    assert order == [2, 0]
    assert len(errors) == 1
    assert "teardown failed" in errors[0]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_disposables.py -v`
Expected: FAIL with `ImportError: cannot import name 'Disposer'`.

- [ ] **Step 3: Write minimal implementation**

Replace `src/speakd/plugins/__init__.py` entirely:

```python
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
```

- [ ] **Step 4: Run tests and linters**

Run: `uv run pytest -q && uv run ruff check . && uv run ruff format --check . && uv run mypy src tests`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/speakd/plugins/__init__.py tests/test_disposables.py
git commit -m "Add disposables, the temporal composability primitive"
```

---

### Task 2: Service registry

**Files:**
- Create: `src/speakd/plugins/registry.py`
- Test: `tests/test_registry.py`

**Interfaces:**
- Consumes: `speakd.plugins.Disposable`, `Disposer`
- Produces: `ServiceRegistry()` with `provide(name: str, value: object) -> Disposable`, `get(name: str) -> object | None`, `watch(name: str, callback: Callable[[object | None], None]) -> Disposable`

`watch` is what makes activation reactive: it fires with the value when a provider appears and with `None` when one goes away.

- [ ] **Step 1: Write the failing test**

```python
"""Tests for the service registry."""

import pytest

from speakd.plugins.registry import ServiceRegistry


def test_provide_then_get() -> None:
    registry = ServiceRegistry()
    registry.provide("bibliography", {"key": "value"})
    assert registry.get("bibliography") == {"key": "value"}


def test_get_unknown_service_is_none() -> None:
    assert ServiceRegistry().get("nothing") is None


def test_disposing_a_provider_removes_the_service() -> None:
    registry = ServiceRegistry()
    handle = registry.provide("bibliography", object())
    handle.dispose()
    assert registry.get("bibliography") is None


def test_providing_twice_is_rejected() -> None:
    registry = ServiceRegistry()
    registry.provide("bibliography", object())
    with pytest.raises(ValueError, match="already provided"):
        registry.provide("bibliography", object())


def test_a_disposed_name_can_be_provided_again() -> None:
    registry = ServiceRegistry()
    registry.provide("bibliography", "first").dispose()
    registry.provide("bibliography", "second")
    assert registry.get("bibliography") == "second"


def test_watch_fires_when_a_provider_appears_and_disappears() -> None:
    registry = ServiceRegistry()
    seen: list[object | None] = []
    registry.watch("bibliography", seen.append)
    assert seen == [None]
    handle = registry.provide("bibliography", "value")
    assert seen == [None, "value"]
    handle.dispose()
    assert seen == [None, "value", None]


def test_watch_fires_immediately_for_an_existing_provider() -> None:
    registry = ServiceRegistry()
    registry.provide("bibliography", "value")
    seen: list[object | None] = []
    registry.watch("bibliography", seen.append)
    assert seen == ["value"]


def test_disposing_a_watch_stops_notifications() -> None:
    registry = ServiceRegistry()
    seen: list[object | None] = []
    watch = registry.watch("bibliography", seen.append)
    watch.dispose()
    registry.provide("bibliography", "value")
    assert seen == [None]


def test_a_raising_watcher_does_not_block_the_others() -> None:
    registry = ServiceRegistry()
    seen: list[object | None] = []

    def boom(value: object | None) -> None:
        raise RuntimeError("watcher failed")

    registry.watch("svc", boom)
    registry.watch("svc", seen.append)
    registry.provide("svc", "value")
    assert seen == [None, "value"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_registry.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'speakd.plugins.registry'`.

- [ ] **Step 3: Write minimal implementation**

Create `src/speakd/plugins/registry.py`:

```python
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
        callback(self._services.get(name))

        def unwatch() -> None:
            watchers = self._watchers.get(name)
            if watchers and callback in watchers:
                watchers.remove(callback)

        return Disposer(unwatch)

    def _notify(self, name: str, value: object | None) -> None:
        # A failing watcher must not stop the others from being told.
        for callback in list(self._watchers.get(name, ())):
            try:
                callback(value)
            except Exception:
                continue
```

- [ ] **Step 4: Run tests and linters**

Run: `uv run pytest -q && uv run ruff check . && uv run ruff format --check . && uv run mypy src tests`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/speakd/plugins/registry.py tests/test_registry.py
git commit -m "Add the service registry with reactive watchers"
```

---

### Task 3: Plugin host and context

**Files:**
- Create: `src/speakd/plugins/host.py`
- Test: `tests/test_plugin_host.py`

**Interfaces:**
- Consumes: `speakd.plugins.DisposableGroup`, `Disposable`, `Disposer`; `speakd.plugins.registry.ServiceRegistry`
- Produces: `PluginContext` with `require(name: str) -> object`, `provide(name: str, value: object) -> None`, `transform(name: str, fn: TransformFn, scope: str = "piece") -> None`, `on_dispose(fn: Callable[[], None]) -> None`; `PluginHost(registry: ServiceRegistry)` with `register(name: str, setup: Callable[[PluginContext], None], requires: Sequence[str] = ()) -> None`, `unregister(name: str) -> None`, `transforms() -> list[RegisteredTransform]`, `active() -> set[str]`; `RegisteredTransform(name, fn, scope, plugin)`
- `TransformFn` is `Callable[[Sequence[Piece]], Sequence[Piece]]`

A plugin is a `setup(ctx)` function. Everything it registers through `ctx` goes into a group the host disposes on deactivation, so unloading is mechanical rather than best-effort. A plugin with unmet requirements simply does not run, and starts running by itself when its provider appears.

- [ ] **Step 1: Write the failing test**

```python
"""Tests for plugin activation, deactivation and registration cleanup."""

from collections.abc import Sequence

from speakd.model import Piece, Span
from speakd.plugins.host import PluginContext, PluginHost
from speakd.plugins.registry import ServiceRegistry


def upper(pieces: Sequence[Piece]) -> Sequence[Piece]:
    return [Piece(span=p.span, spoken=p.spoken.upper(), exact=False) for p in pieces]


def test_a_plugin_with_no_requirements_activates_immediately() -> None:
    host = PluginHost(ServiceRegistry())
    host.register("shouty", lambda ctx: ctx.transform("upper", upper))
    assert host.active() == {"shouty"}
    assert [t.name for t in host.transforms()] == ["upper"]


def test_a_plugin_waits_for_its_required_service() -> None:
    registry = ServiceRegistry()
    host = PluginHost(registry)
    host.register("citations", lambda ctx: ctx.transform("cite", upper), requires=["bibliography"])
    assert host.active() == set()
    assert host.transforms() == []

    registry.provide("bibliography", {"Stiegler-1994": "Stiegler, 1994"})
    assert host.active() == {"citations"}
    assert [t.name for t in host.transforms()] == ["cite"]


def test_a_plugin_deactivates_when_its_provider_disappears() -> None:
    registry = ServiceRegistry()
    host = PluginHost(registry)
    host.register("citations", lambda ctx: ctx.transform("cite", upper), requires=["bibliography"])
    handle = registry.provide("bibliography", {})
    handle.dispose()
    assert host.active() == set()
    assert host.transforms() == []


def test_a_plugin_reactivates_when_its_provider_returns() -> None:
    registry = ServiceRegistry()
    host = PluginHost(registry)
    host.register("citations", lambda ctx: ctx.transform("cite", upper), requires=["bibliography"])
    registry.provide("bibliography", {}).dispose()
    registry.provide("bibliography", {})
    assert host.active() == {"citations"}


def test_require_hands_the_service_to_the_plugin() -> None:
    registry = ServiceRegistry()
    host = PluginHost(registry)
    seen: list[object] = []
    host.register(
        "p", lambda ctx: seen.append(ctx.require("bibliography")), requires=["bibliography"]
    )
    registry.provide("bibliography", "the-bib")
    assert seen == ["the-bib"]


def test_unregister_disposes_everything_the_plugin_registered() -> None:
    host = PluginHost(ServiceRegistry())
    torn_down: list[str] = []

    def setup(ctx: PluginContext) -> None:
        ctx.transform("upper", upper)
        ctx.on_dispose(lambda: torn_down.append("yes"))

    host.register("shouty", setup)
    host.unregister("shouty")
    assert host.transforms() == []
    assert host.active() == set()
    assert torn_down == ["yes"]


def test_a_plugin_can_provide_a_service_for_another() -> None:
    registry = ServiceRegistry()
    host = PluginHost(registry)
    host.register("vault", lambda ctx: ctx.provide("bibliography", "the-bib"))
    host.register("citations", lambda ctx: ctx.transform("cite", upper), requires=["bibliography"])
    assert host.active() == {"vault", "citations"}
    host.unregister("vault")
    assert host.active() == set()


def test_a_plugin_whose_setup_raises_does_not_activate() -> None:
    host = PluginHost(ServiceRegistry())

    def boom(ctx: PluginContext) -> None:
        ctx.transform("half", upper)
        raise RuntimeError("setup failed")

    host.register("broken", boom)
    assert host.active() == set()
    assert host.transforms() == []
    assert any("setup failed" in e for e in host.errors())


def test_transform_scope_defaults_to_piece_and_can_be_job() -> None:
    host = PluginHost(ServiceRegistry())
    host.register("p", lambda ctx: ctx.transform("summarize", upper, scope="job"))
    assert host.transforms()[0].scope == "job"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_plugin_host.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'speakd.plugins.host'`.

- [ ] **Step 3: Write minimal implementation**

Create `src/speakd/plugins/host.py`:

```python
"""Plugin host: activation gated on declared services, teardown by disposal."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass

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

    def provide(self, name: str, value: object) -> None:
        self._group.add(self._host.registry.provide(name, value))

    def transform(self, name: str, fn: TransformFn, scope: str = "piece") -> None:
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
        plugin = _Plugin(name=name, setup=setup, requires=tuple(requires))
        self._plugins[name] = plugin
        # Watching before the first evaluation means a provider appearing later
        # activates the plugin without anyone re-running registration.
        plugin.watches = tuple(
            self.registry.watch(service, lambda _value, n=name: self._reevaluate(n))
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
            group.dispose()
            self._errors.append(f"{plugin.name}: {exc}")
            return
        plugin.group = group

    def _deactivate(self, plugin: _Plugin) -> None:
        if plugin.group is None:
            return
        self._errors.extend(f"{plugin.name}: {e}" for e in plugin.group.dispose())
        plugin.group = None
```

- [ ] **Step 4: Run tests and linters**

Run: `uv run pytest -q && uv run ruff check . && uv run ruff format --check . && uv run mypy src tests`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/speakd/plugins/host.py tests/test_plugin_host.py
git commit -m "Add the plugin host with service-gated activation"
```

---

### Task 4: Transform chain

**Files:**
- Create: `src/speakd/transforms/chain.py`
- Test: `tests/test_transform_chain.py`

**Interfaces:**
- Consumes: `speakd.model.Piece`, `speakd.plugins.host.RegisteredTransform`
- Produces: `apply_chain(pieces: Sequence[Piece], transforms: Sequence[RegisteredTransform]) -> ChainResult`; `ChainResult(pieces: list[Piece], errors: list[str])`

The failure rule is the whole point of this module: a transform that raises is skipped and its input passes through untouched, so one bad plugin costs you its contribution and nothing else.

- [ ] **Step 1: Write the failing test**

```python
"""Tests for applying a chain of transforms."""

from collections.abc import Sequence

from speakd.model import Piece, Span
from speakd.plugins.host import RegisteredTransform
from speakd.transforms.chain import apply_chain


def registered(name: str, fn, scope: str = "piece") -> RegisteredTransform:  # type: ignore[no-untyped-def]
    return RegisteredTransform(name=name, fn=fn, scope=scope, plugin="test")


def upper(pieces: Sequence[Piece]) -> Sequence[Piece]:
    return [Piece(span=p.span, spoken=p.spoken.upper(), exact=False) for p in pieces]


def exclaim(pieces: Sequence[Piece]) -> Sequence[Piece]:
    return [Piece(span=p.span, spoken=p.spoken + "!", exact=False) for p in pieces]


def boom(pieces: Sequence[Piece]) -> Sequence[Piece]:
    raise RuntimeError("transform failed")


def one(text: str) -> list[Piece]:
    return [Piece(span=Span(0, len(text)), spoken=text)]


def test_an_empty_chain_returns_the_input() -> None:
    result = apply_chain(one("hello"), [])
    assert [p.spoken for p in result.pieces] == ["hello"]
    assert result.errors == []


def test_transforms_apply_in_order() -> None:
    result = apply_chain(one("hi"), [registered("u", upper), registered("e", exclaim)])
    assert [p.spoken for p in result.pieces] == ["HI!"]


def test_a_failing_transform_is_skipped_and_its_input_survives() -> None:
    chain = [registered("u", upper), registered("b", boom), registered("e", exclaim)]
    result = apply_chain(one("hi"), chain)
    assert [p.spoken for p in result.pieces] == ["HI!"]
    assert len(result.errors) == 1
    assert "b" in result.errors[0] and "transform failed" in result.errors[0]


def test_a_transform_returning_the_wrong_type_is_skipped() -> None:
    result = apply_chain(one("hi"), [registered("bad", lambda pieces: "not pieces")])
    assert [p.spoken for p in result.pieces] == ["hi"]
    assert len(result.errors) == 1


def test_a_transform_may_drop_pieces() -> None:
    result = apply_chain(one("hi"), [registered("drop", lambda pieces: [])])
    assert result.pieces == []
    assert result.errors == []


def test_spans_survive_the_chain() -> None:
    result = apply_chain(one("hello"), [registered("u", upper)])
    assert result.pieces[0].span == Span(0, 5)
    assert result.pieces[0].exact is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_transform_chain.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'speakd.transforms.chain'`.

- [ ] **Step 3: Write minimal implementation**

Create `src/speakd/transforms/chain.py`:

```python
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
            produced = transform.fn(current)
            candidate = list(produced)
        except Exception as exc:
            errors.append(f"{transform.name}: {exc}")
            continue
        if any(not isinstance(p, Piece) for p in candidate):
            errors.append(f"{transform.name}: returned something other than pieces")
            continue
        current = candidate
    return ChainResult(pieces=current, errors=errors)
```

- [ ] **Step 4: Run tests and linters**

Run: `uv run pytest -q && uv run ruff check . && uv run ruff format --check . && uv run mypy src tests`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/speakd/transforms/chain.py tests/test_transform_chain.py
git commit -m "Add transform chain with per-transform failure isolation"
```

---

### Task 5: Markdown transform

**Files:**
- Create: `src/speakd/transforms/markdown.py`
- Test: `tests/test_markdown_transform.py`

**Interfaces:**
- Consumes: `speakd.model.Piece`
- Produces: `markdown(pieces: Sequence[Piece]) -> list[Piece]`

Agent responses are markdown. Stripping the markers is not enough: a heading with no full stop runs into the next sentence, a bullet list becomes one breathless run-on, and a code block read aloud is noise. Every output piece is `exact=False`, because this rewrites.

- [ ] **Step 1: Write the failing test**

```python
"""Tests for rendering markdown as something worth hearing."""

from speakd.model import Piece, Span
from speakd.transforms.markdown import markdown


def render(text: str) -> str:
    pieces = markdown([Piece(span=Span(0, len(text)), spoken=text)])
    return pieces[0].spoken if pieces else ""


def test_headings_become_sentences() -> None:
    assert render("## Results") == "Results."


def test_a_heading_that_already_ends_in_punctuation_is_left_alone() -> None:
    assert render("## Really?") == "Really?"


def test_bullets_become_sentences() -> None:
    assert render("- first\n- second") == "first. second."


def test_ordered_items_become_sentences() -> None:
    assert render("1. first\n2. second") == "first. second."


def test_fenced_code_is_replaced_by_a_marker_in_place() -> None:
    assert render("Before.\n```python\nx = 1\n```\nAfter.") == "Before. Code block omitted. After."


def test_inline_code_keeps_its_text() -> None:
    assert render("Run `pytest` now.") == "Run pytest now."


def test_links_keep_their_text_and_drop_the_url() -> None:
    assert render("See [the spec](https://example.com/a).") == "See the spec."


def test_emphasis_markers_are_removed() -> None:
    assert render("This is **bold** and _italic_ and *starred*.") == (
        "This is bold and italic and starred."
    )


def test_blockquote_markers_are_removed() -> None:
    assert render("> quoted text.") == "quoted text."


def test_horizontal_rules_are_dropped() -> None:
    assert render("Before.\n---\nAfter.") == "Before. After."


def test_output_is_marked_inexact() -> None:
    pieces = markdown([Piece(span=Span(0, 9), spoken="## Results")])
    assert pieces[0].exact is False
    assert pieces[0].span == Span(0, 9)


def test_a_piece_that_renders_to_nothing_is_dropped() -> None:
    assert markdown([Piece(span=Span(0, 3), spoken="---")]) == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_markdown_transform.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'speakd.transforms.markdown'`.

- [ ] **Step 3: Write minimal implementation**

Create `src/speakd/transforms/markdown.py`:

```python
"""Render markdown as speech.

Headings and list items get terminal punctuation, because the segmenter splits
on sentence boundaries and a heading without one runs into whatever follows.
Code blocks are announced rather than read: reading punctuation aloud for
twenty lines is worse than saying nothing.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from speakd.model import Piece

CODE_BLOCK_MARKER = "Code block omitted."

_FENCE = re.compile(r"^\s*(```|~~~)")
_HEADING = re.compile(r"^\s*#{1,6}\s+(.*)$")
_BULLET = re.compile(r"^\s*[-*+]\s+(.*)$")
_ORDERED = re.compile(r"^\s*\d+[.)]\s+(.*)$")
_QUOTE = re.compile(r"^\s*>\s?(.*)$")
_RULE = re.compile(r"^\s*([-*_])\s*(\1\s*){2,}$")
_LINK = re.compile(r"\[([^\]]+)\]\([^)]*\)")
_INLINE_CODE = re.compile(r"`([^`]+)`")
_BOLD = re.compile(r"\*\*([^*]+)\*\*")
_ITALIC_STAR = re.compile(r"(?<!\*)\*([^*]+)\*(?!\*)")
_ITALIC_UNDER = re.compile(r"(?<![\w_])_([^_]+)_(?![\w_])")


def _inline(text: str) -> str:
    text = _LINK.sub(r"\1", text)
    text = _INLINE_CODE.sub(r"\1", text)
    text = _BOLD.sub(r"\1", text)
    text = _ITALIC_STAR.sub(r"\1", text)
    text = _ITALIC_UNDER.sub(r"\1", text)
    return text


def _terminate(text: str) -> str:
    text = text.strip()
    if text and text[-1] not in ".!?:;":
        return text + "."
    return text


def _render(text: str) -> str:
    parts: list[str] = []
    in_fence = False
    for line in text.splitlines():
        if _FENCE.match(line):
            if in_fence:
                parts.append(CODE_BLOCK_MARKER)
            in_fence = not in_fence
            continue
        if in_fence or _RULE.match(line):
            continue
        heading = _HEADING.match(line)
        if heading:
            parts.append(_terminate(_inline(heading.group(1))))
            continue
        item = _BULLET.match(line) or _ORDERED.match(line)
        if item:
            parts.append(_terminate(_inline(item.group(1))))
            continue
        quote = _QUOTE.match(line)
        if quote:
            line = quote.group(1)
        rendered = _inline(line).strip()
        if rendered:
            parts.append(rendered)
    # An unterminated fence still swallowed its content; say so rather than
    # silently dropping it.
    if in_fence:
        parts.append(CODE_BLOCK_MARKER)
    return " ".join(p for p in parts if p)


def markdown(pieces: Sequence[Piece]) -> list[Piece]:
    """Render each piece's markdown as speech, dropping pieces that render empty."""
    out: list[Piece] = []
    for piece in pieces:
        rendered = _render(piece.spoken)
        if rendered.strip():
            out.append(Piece(span=piece.span, spoken=rendered, exact=False))
    return out
```

- [ ] **Step 4: Run tests and linters**

Run: `uv run pytest -q && uv run ruff check . && uv run ruff format --check . && uv run mypy src tests`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/speakd/transforms/markdown.py tests/test_markdown_transform.py
git commit -m "Add markdown transform"
```

---

### Task 6: Pronunciation transform

**Files:**
- Create: `src/speakd/transforms/pronunciation.py`
- Test: `tests/test_pronunciation_transform.py`

**Interfaces:**
- Consumes: `speakd.model.Piece`
- Produces: `pronunciation(pieces: Sequence[Piece]) -> list[Piece]`

The substitution table is ported from `claude-code-narrator`'s `speak.sh` (MIT, Shreyas S Rao) — it is good work, hard-won against a real TTS engine, and reinventing it would be worse. Say so in the module docstring.

The filename rule matters more than it looks: `settings.json` spoken with the dot intact makes the engine treat it as a sentence boundary, which fragments a segment mid-phrase.

- [ ] **Step 1: Write the failing test**

```python
"""Tests for pronunciation substitutions."""

from speakd.model import Piece, Span
from speakd.transforms.pronunciation import pronunciation


def say(text: str) -> str:
    return pronunciation([Piece(span=Span(0, len(text)), spoken=text)])[0].spoken


def test_latin_abbreviations_are_expanded() -> None:
    assert say("e.g. this") == "for example this"
    assert say("i.e. that") == "that is that"


def test_filename_dots_become_the_word_dot() -> None:
    assert say("edit settings.json now") == "edit settings dot jason now"


def test_chained_extensions_are_handled() -> None:
    assert say("types.d.ts") == "types dot d dot ts"


def test_short_abbreviations_are_not_mangled_as_filenames() -> None:
    assert "dot" not in say("a.b")


def test_arrows_and_operators_are_spoken() -> None:
    assert say("a -> b") == "a arrow b"
    assert say("x != y") == "x not equal y"


def test_acronyms_are_spelled_out() -> None:
    assert say("the API") == "the A P I"
    assert say("a URL") == "a U R L"


def test_format_names_are_pronounced() -> None:
    assert say("JSON") == "jason"
    assert say("YAML") == "yammel"
    assert say("TOML") == "tommel"


def test_longer_acronyms_win_over_shorter_ones() -> None:
    assert say("JSONL") == "jason L"
    assert say("HTTPS") == "H T T P S"


def test_ellipsis_is_collapsed() -> None:
    assert say("wait... now") == "wait now"


def test_output_is_marked_inexact() -> None:
    out = pronunciation([Piece(span=Span(0, 4), spoken="JSON")])
    assert out[0].exact is False
    assert out[0].span == Span(0, 4)


def test_plain_text_is_untouched_apart_from_spacing() -> None:
    assert say("a plain sentence") == "a plain sentence"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_pronunciation_transform.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'speakd.transforms.pronunciation'`.

- [ ] **Step 3: Write minimal implementation**

Create `src/speakd/transforms/pronunciation.py`:

```python
"""Substitutions that make technical prose survive a TTS engine.

The table is ported from `claude-code-narrator`'s `speak.sh` (MIT licence,
Shreyas S Rao). It is good work, tuned against a real engine, and reinventing
it would have produced something worse.

The filename rule earns its place: "settings.json" spoken with the dot intact
reads as a sentence boundary, which fragments a segment mid-phrase. Two or more
characters are required before the dot so that genuine abbreviations survive.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from speakd.model import Piece

_LITERAL: tuple[tuple[str, str], ...] = (
    ("e.g.", "for example"),
    ("i.e.", "that is"),
    ("=>", " arrow "),
    ("->", " arrow "),
    ("<=", " less or equal "),
    (">=", " greater or equal "),
    ("!=", " not equal "),
    ("==", " equals "),
    ("&&", " and "),
    ("||", " or "),
    ("→", " to "),
    ("...", " "),
    ("/dev/null", "dev null"),
    ("stderr", "standard error"),
    ("stdout", "standard output"),
)

_FILENAME = re.compile(r"([A-Za-z0-9_-]{2,})\.([A-Za-z]{1,10})")
_CHAINED = re.compile(r"(dot [A-Za-z0-9_-]+)\.([A-Za-z]{1,10})")

# Longer forms first: JSONL before JSON, HTTPS before HTTP.
_WORDS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"README", re.I), "read me"),
    (re.compile(r"JSONL", re.I), "jason L"),
    (re.compile(r"JSON", re.I), "jason"),
    (re.compile(r"YAML", re.I), "yammel"),
    (re.compile(r"TOML", re.I), "tommel"),
    (re.compile(r"HTTPS"), "H T T P S"),
    (re.compile(r"HTTP"), "H T T P"),
    (re.compile(r"APIs"), "A P I s"),
    (re.compile(r"API"), "A P I"),
    (re.compile(r"CLI"), "C L I"),
    (re.compile(r"SQL", re.I), "sequel"),
    (re.compile(r"URLs"), "U R L s"),
    (re.compile(r"URL"), "U R L"),
    (re.compile(r"UUID", re.I), "you you I D"),
    (re.compile(r"PyPI"), "pie P I"),
    (re.compile(r"OAuth", re.I), "oh auth"),
    (re.compile(r"CORS"), "cores"),
    (re.compile(r"REPL"), "repple"),
)


def _apply(text: str) -> str:
    for needle, replacement in _LITERAL:
        text = text.replace(needle, replacement)
    text = _FILENAME.sub(r"\1 dot \2", text)
    text = _CHAINED.sub(r"\1 dot \2", text)
    for pattern, replacement in _WORDS:
        text = pattern.sub(replacement, text)
    return " ".join(text.split())


def pronunciation(pieces: Sequence[Piece]) -> list[Piece]:
    """Apply the substitution table, marking every result inexact."""
    out: list[Piece] = []
    for piece in pieces:
        spoken = _apply(piece.spoken)
        if spoken.strip():
            out.append(Piece(span=piece.span, spoken=spoken, exact=False))
    return out
```

- [ ] **Step 4: Run tests and linters**

Run: `uv run pytest -q && uv run ruff check . && uv run ruff format --check . && uv run mypy src tests`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/speakd/transforms/pronunciation.py tests/test_pronunciation_transform.py
git commit -m "Port the pronunciation table from claude-code-narrator"
```

---

### Task 7: Profiles, and the chain end to end

**Files:**
- Create: `src/speakd/profiles.py`
- Create: `src/speakd/plugins/builtin.py`
- Test: `tests/test_profiles.py`
- Test: `tests/test_transforms_end_to_end.py`

**Interfaces:**
- Consumes: everything above, plus `speakd.pipeline.speak`, `speakd.synth.fake.FakeEngine`, `speakd.player.RecordingPlayer`
- Produces: `Profile(name: str, transforms: tuple[str, ...], voice: str, speed: float, interrupt_on: tuple[str, ...])`; `load_profiles(path: Path) -> dict[str, Profile]`; `DEFAULT_PROFILE: Profile`; `resolve_chain(profile: Profile, host: PluginHost) -> tuple[list[RegisteredTransform], list[str]]`; `register_builtins(host: PluginHost) -> None`

A profile names transforms it wants; the host knows which exist. A named transform with no provider is reported, not silently skipped — a profile that quietly does nothing is how you spend an afternoon wondering why citations are not being read.

The daemon plan consumes profiles **by name through an injected callable** and never imports this module, which is what lets the two plans run concurrently.

- [ ] **Step 1: Write the failing test**

```python
"""Tests for profiles and chain resolution."""

from pathlib import Path

import pytest

from speakd.plugins.builtin import register_builtins
from speakd.plugins.host import PluginHost
from speakd.plugins.registry import ServiceRegistry
from speakd.profiles import DEFAULT_PROFILE, Profile, load_profiles, resolve_chain


def test_default_profile_speaks_markdown_and_pronunciation() -> None:
    assert DEFAULT_PROFILE.transforms == ("markdown", "pronunciation")
    assert DEFAULT_PROFILE.voice == "af_heart"
    assert DEFAULT_PROFILE.speed == 1.1


def test_load_profiles_reads_toml(tmp_path: Path) -> None:
    path = tmp_path / "profiles.toml"
    path.write_text(
        """
[profile.philosophy]
transforms = ["markdown", "citations"]
speed = 1.0

[profile.monitor]
transforms = ["markdown", "summarize"]
interrupt_on = ["error", "done"]
"""
    )
    profiles = load_profiles(path)
    assert profiles["philosophy"].transforms == ("markdown", "citations")
    assert profiles["philosophy"].speed == 1.0
    assert profiles["philosophy"].voice == "af_heart"
    assert profiles["monitor"].interrupt_on == ("error", "done")


def test_load_profiles_on_a_missing_file_yields_only_the_default(tmp_path: Path) -> None:
    profiles = load_profiles(tmp_path / "absent.toml")
    assert profiles == {"default": DEFAULT_PROFILE}


def test_load_profiles_rejects_a_non_positive_speed(tmp_path: Path) -> None:
    path = tmp_path / "profiles.toml"
    path.write_text("[profile.bad]\ntransforms = []\nspeed = 0\n")
    with pytest.raises(ValueError, match="speed"):
        load_profiles(path)


def test_resolve_chain_orders_transforms_as_the_profile_names_them() -> None:
    host = PluginHost(ServiceRegistry())
    register_builtins(host)
    profile = Profile(name="p", transforms=("pronunciation", "markdown"))
    chain, missing = resolve_chain(profile, host)
    assert [t.name for t in chain] == ["pronunciation", "markdown"]
    assert missing == []


def test_resolve_chain_reports_a_transform_nobody_provides() -> None:
    host = PluginHost(ServiceRegistry())
    register_builtins(host)
    profile = Profile(name="p", transforms=("markdown", "citations"))
    chain, missing = resolve_chain(profile, host)
    assert [t.name for t in chain] == ["markdown"]
    assert missing == ["citations"]
```

And `tests/test_transforms_end_to_end.py`:

```python
"""The whole path: profile to transforms to segments to spoken audio."""

from speakd.model import Piece, Span
from speakd.pipeline import speak
from speakd.player import RecordingPlayer
from speakd.plugins.builtin import register_builtins
from speakd.plugins.host import PluginHost
from speakd.plugins.registry import ServiceRegistry
from speakd.profiles import DEFAULT_PROFILE, resolve_chain
from speakd.synth.fake import FakeEngine
from speakd.transforms.chain import apply_chain

RESPONSE = """## Results

The API returned `null`.

```python
x = 1
```

- first finding
- second finding
"""


def test_a_markdown_response_becomes_speakable_segments() -> None:
    host = PluginHost(ServiceRegistry())
    register_builtins(host)
    chain, missing = resolve_chain(DEFAULT_PROFILE, host)
    assert missing == []

    source = [Piece(span=Span(0, len(RESPONSE)), spoken=RESPONSE)]
    transformed = apply_chain(source, chain)
    assert transformed.errors == []

    player = RecordingPlayer()
    result = speak(transformed.pieces, FakeEngine(), player, voice=DEFAULT_PROFILE.voice)

    spoken = " ".join(s.text for s in result.timeline.segments)
    assert "A P I" in spoken
    assert "Code block omitted." in spoken
    assert "```" not in spoken and "##" not in spoken
    assert len(player.played) == len(result.timeline)
    assert result.errors == []


def test_every_segment_keeps_a_span_inside_the_source() -> None:
    host = PluginHost(ServiceRegistry())
    register_builtins(host)
    chain, _ = resolve_chain(DEFAULT_PROFILE, host)
    source = [Piece(span=Span(0, len(RESPONSE)), spoken=RESPONSE)]
    transformed = apply_chain(source, chain)
    result = speak(transformed.pieces, FakeEngine(), RecordingPlayer())
    for segment in result.timeline.segments:
        assert 0 <= segment.span.start <= segment.span.end <= len(RESPONSE)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_profiles.py tests/test_transforms_end_to_end.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'speakd.profiles'`.

- [ ] **Step 3: Write minimal implementation**

Create `src/speakd/plugins/builtin.py`:

```python
"""Registers the transforms that ship with the core.

They are plugins like any other — the core has no privileged transform. That
is what keeps a domain pack and a built-in on equal footing.
"""

from __future__ import annotations

from speakd.plugins.host import PluginContext, PluginHost
from speakd.transforms.markdown import markdown
from speakd.transforms.pronunciation import pronunciation


def register_builtins(host: PluginHost) -> None:
    host.register("markdown", lambda ctx: ctx.transform("markdown", markdown))
    host.register("pronunciation", lambda ctx: ctx.transform("pronunciation", pronunciation))
```

Create `src/speakd/profiles.py`:

```python
"""Profiles: a named ordered chain of transforms, plus how to say them.

A profile names transforms; the plugin host knows which exist. Resolution
reports names nobody provides rather than skipping them, because a profile that
silently does nothing is an afternoon spent wondering why.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from speakd.plugins.host import PluginHost, RegisteredTransform


@dataclass(frozen=True)
class Profile:
    name: str
    transforms: tuple[str, ...] = ()
    voice: str = "af_heart"
    speed: float = 1.1
    interrupt_on: tuple[str, ...] = field(default=())


DEFAULT_PROFILE = Profile(name="default", transforms=("markdown", "pronunciation"))


def load_profiles(path: Path) -> dict[str, Profile]:
    """Read profiles from TOML, always including the default."""
    profiles: dict[str, Profile] = {"default": DEFAULT_PROFILE}
    if not path.is_file():
        return profiles
    with path.open("rb") as handle:
        data = tomllib.load(handle)
    for name, raw in data.get("profile", {}).items():
        speed = float(raw.get("speed", DEFAULT_PROFILE.speed))
        if speed <= 0:
            raise ValueError(f"profile {name!r}: speed must be greater than 0, got {speed}")
        profiles[name] = Profile(
            name=name,
            transforms=tuple(raw.get("transforms", ())),
            voice=str(raw.get("voice", DEFAULT_PROFILE.voice)),
            speed=speed,
            interrupt_on=tuple(raw.get("interrupt_on", ())),
        )
    return profiles


def resolve_chain(
    profile: Profile,
    host: PluginHost,
) -> tuple[list[RegisteredTransform], list[str]]:
    """Return the profile's transforms in order, plus the names nobody provides."""
    available = {t.name: t for t in host.transforms()}
    chain: list[RegisteredTransform] = []
    missing: list[str] = []
    for name in profile.transforms:
        transform = available.get(name)
        if transform is None:
            missing.append(name)
        else:
            chain.append(transform)
    return chain, missing
```

- [ ] **Step 4: Run tests and linters**

Run: `uv run pytest -q && uv run ruff check . && uv run ruff format --check . && uv run mypy src tests`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/speakd/profiles.py src/speakd/plugins/builtin.py tests/test_profiles.py tests/test_transforms_end_to_end.py
git commit -m "Add profiles and wire the built-in transforms end to end"
```

---

## Done when

- `uv run pytest` passes, and the end-to-end test shows a markdown response arriving as segments with acronyms spelled out, code blocks announced, and no markup.
- Unloading a plugin removes its transforms; a plugin whose required service disappears deactivates and reactivates on its own.
- A transform that raises costs its own contribution and nothing else.

## Not in this plan

The daemon, transports, channels and the event bus (the daemon plan). The
summariser transform and its three backends, the vault pack, and the Claude
Code client (plan 3b, after the daemon's protocol exists).
