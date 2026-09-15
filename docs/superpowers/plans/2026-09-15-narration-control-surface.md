# Narration: labels, mute and disable — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the daemon an off switch at two levels — an instant mute that stops speech, and a disable that unloads the model and gives back three gigabytes — plus the channel labels that make a per-session mute usable.

**Architecture:** Three new verbs on the existing protocol (`SET_LABEL`, `MUTE`, `SET_ENGINE`), a small persisted state file, and a `LazyEngine` wrapper that lets the daemon hold a `Synthesizer` it has not loaded. Mute drops at `_enqueue`, so a muted channel never reaches the worker and costs no CPU. Disable additionally drops the model.

**Tech Stack:** Python 3.11–3.13, pytest, ruff, mypy (strict). No new dependencies.

**Spec:** `docs/design/2026-09-15-streaming-narration-design.md` (§3, §5, §6)

## Global Constraints

- **Never run `uv sync`, in any form, including `uv sync --group dev`.** This venv holds a hand-installed CPU-only torch (`2.14.0+cpu`) plus kokoro, sounddevice, scipy. Any sync prunes them. **Always `uv run --no-sync ...`.**
- Tests: `uv run --no-sync pytest`. Lint: `uv run --no-sync ruff check .` and `ruff format --check .`. Types: `uv run --no-sync mypy src tests` (strict).
- `line-length = 100`. `requires-python = ">=3.11,<3.14"`.
- Branch: `feat/streaming-narration`.
- **No test may open an audio device or construct `KokoroEngine`.** Use `FakeEngine` and `RecordingPlayer`, as `tests/test_daemon.py` does.
- Comments say *why*, never *what*. Match the surrounding prose.

---

## File Structure

| File | Responsibility |
|---|---|
| `src/speakd/protocol.py` | three new `Verb` members |
| `src/speakd/channels.py` | `Channel.muted`; `set_label`, `set_muted` |
| `src/speakd/state.py` | **new.** Reads and writes the persisted mute/disabled flags. A leaf, like `paths.py`. |
| `src/speakd/synth/lazy.py` | **new.** `LazyEngine`: a `Synthesizer` that can be loaded and unloaded. |
| `src/speakd/daemon.py` | handles the three verbs; drops muted enqueues; owns the engine's loaded state |
| `src/speakd/__main__.py` | reads the disabled flag *before* building the engine |
| `src/speakd/cli.py` | `mute`, `unmute`, `enable`, `disable`, `label` subcommands |
| `tests/test_mute.py`, `tests/test_lazy_engine.py`, `tests/test_state.py` | **new** |

---

### Task 1: `SET_LABEL`, so a channel can say what it is

`Channel.label` exists and `STATUS` already returns it, but nothing sets it and no verb can. Without it the GUI's channel list is a column of UUIDs, and a per-channel mute is unusable.

**Files:**
- Modify: `src/speakd/protocol.py:15-25`, `src/speakd/channels.py`, `src/speakd/daemon.py` (near the `SET_ROLE` branch, ~line 415), `src/speakd/cli.py`
- Test: `tests/test_channels.py`, `tests/test_cli_verbs.py`, `tests/test_daemon.py`

**Interfaces:**
- Consumes: `ChannelTable.open`, `Verb`, `Request`, `Response`.
- Produces: `Verb.SET_LABEL = "set_label"`; `ChannelTable.set_label(source_id: str, label: str) -> None`; `speakctl label --source X "text"`.

- [ ] **Step 1: Write the failing tests**

In `tests/test_channels.py`:

```python
def test_set_label_names_a_channel() -> None:
    table = ChannelTable()
    table.set_label("s", "Claude Code · a session")
    channel = table.get("s")
    assert channel is not None
    assert channel.label == "Claude Code · a session"


def test_set_label_on_an_unknown_source_opens_it() -> None:
    # Consistent with set_role and set_priority: a source may name itself
    # before it has said anything.
    table = ChannelTable()
    table.set_label("new", "named")
    assert table.get("new") is not None
```

In `tests/test_daemon.py`:

```python
def test_set_label_reaches_status(daemon) -> None:  # type: ignore[no-untyped-def]
    d, _player, _bus = daemon
    d.handle(Request(verb=Verb.SET_LABEL, source_id="s", payload={"label": "named"}))
    status = d.handle(Request(verb=Verb.STATUS, source_id=""))
    channels = status.data["channels"]
    assert [c["label"] for c in channels if c["source_id"] == "s"] == ["named"]


def test_set_label_without_a_label_is_refused() -> None:
    table = ChannelTable()
    d = Daemon(FakeEngine(), RecordingPlayer(), profile_for, bus=EventBus(), channels=table)
    response = d.handle(Request(verb=Verb.SET_LABEL, source_id="s", payload={}))
    assert not response.ok
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run --no-sync pytest tests/test_channels.py tests/test_daemon.py -k label -v`
Expected: FAIL — `Verb.SET_LABEL` does not exist.

- [ ] **Step 3: Implement**

In `src/speakd/protocol.py`, add to `Verb` after `SET_PRIORITY`:

```python
    SET_LABEL = "set_label"
```

In `src/speakd/channels.py`, beside `set_role` and `set_priority`:

```python
    def set_label(self, source_id: str, label: str) -> None:
        self.open(source_id, label=label)
```

In `src/speakd/daemon.py`, beside the `SET_PRIORITY` branch in `handle`:

```python
        if request.verb is Verb.SET_LABEL:
            label = request.payload.get("label")
            if not isinstance(label, str) or not label.strip():
                return Response(ok=False, error="set_label needs a non-empty label")
            self.channels.set_label(request.source_id, label)
            return Response(ok=True, data={"label": label})
```

In `src/speakd/cli.py`, beside the `priority` subparser:

```python
    label = sub.add_parser("label", parents=[common], help="name a channel for display")
    label.add_argument("label", help="what to show for this channel")
```

and a `_label` handler following the shape of `_priority` in the same file.

- [ ] **Step 4: Run the tests**

Run: `uv run --no-sync pytest tests/test_channels.py tests/test_daemon.py tests/test_cli_verbs.py -v`
Expected: PASS.

- [ ] **Step 5: Lint, type-check, commit**

```bash
uv run --no-sync ruff check . && uv run --no-sync ruff format --check .
uv run --no-sync mypy src tests
git add src/speakd/protocol.py src/speakd/channels.py src/speakd/daemon.py src/speakd/cli.py tests/
git commit -m "Let a channel say what it is"
```

---

### Task 2: The persisted state file

Mute and disable both survive a restart, and the disabled flag has to be readable *before* the engine is constructed — otherwise a daemon that starts disabled loads three gigabytes for the sole purpose of freeing them. That means a leaf module, like `paths.py`.

**Files:**
- Create: `src/speakd/state.py`, `tests/test_state.py`

**Interfaces:**
- Produces:
  - `speakd.state.state_path() -> Path` — `$XDG_STATE_HOME/speakd/state.json`, else `~/.local/state/speakd/state.json`
  - `speakd.state.DaemonState` — frozen dataclass, `muted: bool = False`, `disabled: bool = False`
  - `speakd.state.load() -> DaemonState`
  - `speakd.state.save(state: DaemonState) -> None`

- [ ] **Step 1: Write the failing test**

```python
"""Tests for the flags that outlive a daemon."""

import json

from speakd.state import DaemonState, load, save, state_path


def test_state_path_follows_xdg(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("XDG_STATE_HOME", "/tmp/xdg")
    assert state_path() == __import__("pathlib").Path("/tmp/xdg/speakd/state.json")


def test_a_missing_file_is_the_default_state(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    assert load() == DaemonState(muted=False, disabled=False)


def test_what_is_saved_is_what_loads(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    save(DaemonState(muted=True, disabled=True))
    assert load() == DaemonState(muted=True, disabled=True)


def test_unreadable_state_is_the_default_rather_than_a_crash(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    # A daemon that will not start because a state file got truncated is a
    # worse failure than one that starts audible.
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    path = state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json")
    assert load() == DaemonState(muted=False, disabled=False)


def test_unknown_keys_are_ignored(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    path = state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"muted": True, "from_a_later_version": 1}))
    assert load() == DaemonState(muted=True, disabled=False)
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run --no-sync pytest tests/test_state.py -v`
Expected: FAIL — no module named `speakd.state`.

- [ ] **Step 3: Implement**

```python
"""Flags that outlive one run of the daemon.

A leaf, like `paths`: `__main__` reads `disabled` *before* it constructs the
engine, because a daemon that starts disabled must not load three gigabytes for
the sole purpose of freeing them. Importing anything heavy here would put that
cost back.

Every failure resolves to the default state rather than an exception. A daemon
that will not start because a JSON file was truncated is a worse failure than
one that starts audible.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass(frozen=True)
class DaemonState:
    muted: bool = False
    disabled: bool = False


def state_path() -> Path:
    xdg = os.environ.get("XDG_STATE_HOME")
    base = Path(xdg) if xdg else Path.home() / ".local" / "state"
    return base / "speakd" / "state.json"


def load() -> DaemonState:
    try:
        body = json.loads(state_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return DaemonState()
    if not isinstance(body, dict):
        return DaemonState()
    return DaemonState(
        muted=bool(body.get("muted", False)),
        disabled=bool(body.get("disabled", False)),
    )


def save(state: DaemonState) -> None:
    """Write the flags, or give up quietly.

    Quietly because the caller is a control verb: failing `mute` because the
    state directory is read-only would refuse an action that did in fact take
    effect in memory.
    """
    path = state_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(state)), encoding="utf-8")
    except OSError:
        pass
```

- [ ] **Step 4: Run, lint, type-check, commit**

```bash
uv run --no-sync pytest tests/test_state.py -v
uv run --no-sync ruff check . && uv run --no-sync mypy src tests
git add src/speakd/state.py tests/test_state.py
git commit -m "Remember the flags that should outlive a restart"
```

---

### Task 3: Mute

**Files:**
- Modify: `src/speakd/protocol.py`, `src/speakd/channels.py`, `src/speakd/daemon.py:433+` (`_enqueue`), `src/speakd/daemon.py` (`handle`, `STATUS` at 392-410)
- Test: `tests/test_mute.py` (create)

**Interfaces:**
- Consumes: `speakd.state.DaemonState`, `load`, `save` from Task 2; `Verb.SET_LABEL` pattern from Task 1.
- Produces: `Verb.MUTE = "mute"`; `Channel.muted: bool = False`; `Daemon.muted: bool`; `STATUS` data gains top-level `"muted": bool` and `"muted"` on each channel entry; bus event `kind="mute"`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_mute.py`:

```python
"""Tests for the off switch that keeps the model loaded."""

from collections.abc import Sequence

from speakd.channels import ChannelTable
from speakd.daemon import Daemon, ProfileView
from speakd.events import Event, EventBus
from speakd.model import Piece
from speakd.player import RecordingPlayer
from speakd.protocol import Request, Verb
from speakd.synth.fake import FakeEngine


def passthrough(pieces: Sequence[Piece]) -> tuple[list[Piece], list[str]]:
    return list(pieces), []


def profile_for(name: str) -> ProfileView:
    return ProfileView(voice="af_heart", speed=1.1, interrupt_on=(), prepare=passthrough)


class CountingEngine(FakeEngine):
    """A FakeEngine that says how many times it was asked to synthesise."""

    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    def synthesize(self, text: str, voice: str, speed: float):  # type: ignore[no-untyped-def]
        self.calls += 1
        return super().synthesize(text, voice, speed)


def build() -> tuple[Daemon, CountingEngine, list[Event]]:
    engine = CountingEngine()
    bus = EventBus()
    seen: list[Event] = []
    bus.subscribe(seen.append)
    daemon = Daemon(engine, RecordingPlayer(), profile_for, bus=bus, channels=ChannelTable())
    daemon.start()
    return daemon, engine, seen


def test_a_muted_channel_never_reaches_the_engine() -> None:
    daemon, engine, _ = build()
    try:
        daemon.handle(Request(verb=Verb.MUTE, source_id="s", payload={"muted": True}))
        daemon.handle(Request(verb=Verb.ENQUEUE, source_id="s", payload={"text": "Hello."}))
        daemon.wait_idle(2.0)
        assert engine.calls == 0
    finally:
        daemon.stop()


def test_an_unmuted_channel_still_speaks() -> None:
    daemon, engine, _ = build()
    try:
        daemon.handle(Request(verb=Verb.ENQUEUE, source_id="s", payload={"text": "Hello."}))
        daemon.wait_idle(2.0)
        assert engine.calls > 0
    finally:
        daemon.stop()


def test_global_mute_silences_a_channel_that_is_not_itself_muted() -> None:
    daemon, engine, _ = build()
    try:
        daemon.handle(Request(verb=Verb.MUTE, source_id="", payload={"muted": True}))
        daemon.handle(Request(verb=Verb.ENQUEUE, source_id="s", payload={"text": "Hello."}))
        daemon.wait_idle(2.0)
        assert engine.calls == 0
    finally:
        daemon.stop()


def test_clearing_the_global_mute_restores_what_each_channel_had() -> None:
    # The two flags are independent state, not one setting written twice.
    daemon, engine, _ = build()
    try:
        daemon.handle(Request(verb=Verb.MUTE, source_id="s", payload={"muted": True}))
        daemon.handle(Request(verb=Verb.MUTE, source_id="", payload={"muted": True}))
        daemon.handle(Request(verb=Verb.MUTE, source_id="", payload={"muted": False}))
        daemon.handle(Request(verb=Verb.ENQUEUE, source_id="s", payload={"text": "Hello."}))
        daemon.wait_idle(2.0)
        assert engine.calls == 0
    finally:
        daemon.stop()


def test_muting_announces_itself_on_the_bus() -> None:
    daemon, _engine, seen = build()
    try:
        daemon.handle(Request(verb=Verb.MUTE, source_id="", payload={"muted": True}))
        mutes = [e for e in seen if e.kind == "mute"]
        assert mutes and mutes[-1].data == {"muted": True, "scope": "global"}
    finally:
        daemon.stop()


def test_status_reports_both_levels() -> None:
    daemon, _engine, _ = build()
    try:
        daemon.handle(Request(verb=Verb.MUTE, source_id="s", payload={"muted": True}))
        status = daemon.handle(Request(verb=Verb.STATUS, source_id=""))
        assert status.data["muted"] is False
        entry = [c for c in status.data["channels"] if c["source_id"] == "s"][0]
        assert entry["muted"] is True
    finally:
        daemon.stop()


def test_asking_the_status_does_not_open_a_channel_for_the_asker() -> None:
    # The same care STATUS already takes: a global mute names no source, and
    # must not leave a phantom channel behind.
    daemon, _engine, _ = build()
    try:
        daemon.handle(Request(verb=Verb.MUTE, source_id="", payload={"muted": True}))
        status = daemon.handle(Request(verb=Verb.STATUS, source_id=""))
        assert status.data["channels"] == []
    finally:
        daemon.stop()
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run --no-sync pytest tests/test_mute.py -v`
Expected: FAIL — `Verb.MUTE` does not exist.

- [ ] **Step 3: Implement**

`protocol.py`: add `MUTE = "mute"` to `Verb`.

`channels.py`: add `muted: bool = False` to `Channel`, a `muted` keyword to `ChannelTable.open` handled exactly as `role` and `profile` are, and:

```python
    def set_muted(self, source_id: str, muted: bool) -> None:
        self.open(source_id, muted=muted)
```

`daemon.py`, in `__init__`, after `self.channels`:

```python
        # Loaded rather than defaulted: after a reboot, surprising silence is a
        # smaller failure than surprising speech.
        self.muted = state.load().muted
```

with `from speakd import state` at the top.

In `handle`, beside the other verbs:

```python
        if request.verb is Verb.MUTE:
            wanted = request.payload.get("muted")
            if not isinstance(wanted, bool):
                return Response(ok=False, error="mute needs a boolean 'muted'")
            if request.source_id:
                self.channels.set_muted(request.source_id, wanted)
                scope = "channel"
            else:
                # Global: named no source, so it must open no channel, for the
                # same reason STATUS does not.
                self.muted = wanted
                state.save(state.DaemonState(muted=wanted, disabled=state.load().disabled))
                scope = "global"
            if wanted:
                # An off switch that lets the current paragraph finish is not
                # an off switch.
                self._silence_for_mute(request.source_id)
            self._publish("mute", request.source_id, {"muted": wanted, "scope": scope})
            return Response(ok=True, data={"muted": wanted, "scope": scope})
```

`_silence_for_mute` reuses the existing hush path: call whatever `handle` calls for `Verb.HUSH`, restricted to the named source (or to everything, when global). Read lines 360-391 of `daemon.py` and factor the body into a helper both branches call rather than duplicating it.

In `_enqueue`, immediately after the `self._running` guard and before `decide(...)`:

```python
        channel = self.channels.open(request.source_id)
        if self.muted or channel.muted:
            # Dropped at the door, not held: unmuting must not release ten
            # minutes of backlog. Nothing reaches the worker, so a muted
            # channel costs no synthesis at all.
            self._publish(
                "declined",
                request.source_id,
                {"text": text, "kind": kind, "reason": "muted"},
            )
            return Response(ok=True, data={"spoken": False, "reason": "muted"})
```

Note the existing `channel = self.channels.open(request.source_id)` further down becomes redundant — move it up rather than calling `open` twice.

In the `STATUS` branch, add `"muted": self.muted` alongside `"channels"`, and `"muted": c.muted` to each channel dict.

- [ ] **Step 4: Run the tests**

Run: `uv run --no-sync pytest tests/test_mute.py -v`
Expected: PASS.

Run: `uv run --no-sync pytest`
Expected: PASS. If `test_daemon.py`'s STATUS assertions compare whole dicts, they now see a `muted` key — update them.

- [ ] **Step 5: Lint, type-check, commit**

```bash
uv run --no-sync ruff check . && uv run --no-sync ruff format --check .
uv run --no-sync mypy src tests
git add src/speakd tests/test_mute.py tests/test_daemon.py
git commit -m "Add an off switch that stops speech without unloading anything"
```

---

### Task 4: `LazyEngine`

**Files:**
- Create: `src/speakd/synth/lazy.py`, `tests/test_lazy_engine.py`

**Interfaces:**
- Consumes: `Synthesizer` protocol from `speakd.synth`.
- Produces: `LazyEngine(factory: Callable[[], Synthesizer], name: str, sample_rate: int)` with `.loaded: bool`, `.loading: bool`, `.load() -> None`, `.unload() -> None`, and `synthesize(text, voice, speed) -> np.ndarray` which **returns an empty array when unloaded and never loads implicitly**.

- [ ] **Step 1: Write the failing test**

```python
"""Tests for an engine the daemon can put down and pick up again."""

import numpy as np

from speakd.synth.fake import FakeEngine
from speakd.synth.lazy import LazyEngine


def build(counter: list[int]) -> LazyEngine:
    def factory() -> FakeEngine:
        counter.append(1)
        return FakeEngine()

    return LazyEngine(factory, name="lazy", sample_rate=24000)


def test_it_starts_unloaded_and_builds_nothing() -> None:
    built: list[int] = []
    engine = build(built)
    assert not engine.loaded
    assert built == []


def test_sample_rate_is_known_while_unloaded() -> None:
    # build_player() reads it to open the sink, and must not need a model.
    assert build([]).sample_rate == 24000


def test_synthesising_while_unloaded_returns_silence_and_loads_nothing() -> None:
    # A stray enqueue must not undo a decision the user made deliberately,
    # nor pay thirty seconds to do it.
    built: list[int] = []
    engine = build(built)
    assert engine.synthesize("Hello.", "af_heart", 1.0).size == 0
    assert built == []


def test_loading_then_synthesising_produces_audio() -> None:
    built: list[int] = []
    engine = build(built)
    engine.load()
    assert engine.loaded
    assert built == [1]
    assert engine.synthesize("Hello.", "af_heart", 1.0).size > 0


def test_loading_twice_builds_once() -> None:
    built: list[int] = []
    engine = build(built)
    engine.load()
    engine.load()
    assert built == [1]


def test_unloading_drops_it_and_reloading_builds_again() -> None:
    built: list[int] = []
    engine = build(built)
    engine.load()
    engine.unload()
    assert not engine.loaded
    assert engine.synthesize("Hello.", "af_heart", 1.0).size == 0
    engine.load()
    assert built == [1, 1]


def test_unloading_when_never_loaded_is_not_an_error() -> None:
    build([]).unload()
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run --no-sync pytest tests/test_lazy_engine.py -v`
Expected: FAIL — no module named `speakd.synth.lazy`.

- [ ] **Step 3: Implement**

```python
"""A Synthesizer the daemon can put down and pick up again.

`KokoroEngine.__init__` *is* the model load, and the loaded model is some three
gigabytes resident. This wrapper separates "which engine" from "is it loaded",
so a daemon can be disabled without exiting, and can start disabled without
loading three gigabytes for the sole purpose of freeing them.

`sample_rate` is given at construction rather than read off the engine, because
`build_player()` needs it to open the sink while nothing is loaded. On
`KokoroEngine` it is a class attribute, so the caller has it without an
instance.

`synthesize` on an unloaded engine returns silence and does **not** load.
Loading implicitly would let a stray enqueue undo a decision the user made
deliberately, and spend thirty seconds doing it.
"""

from __future__ import annotations

import gc
import threading
from collections.abc import Callable

import numpy as np

from speakd.synth import Synthesizer


class LazyEngine:
    name: str
    sample_rate: int

    def __init__(
        self,
        factory: Callable[[], Synthesizer],
        *,
        name: str = "lazy",
        sample_rate: int = 24000,
    ) -> None:
        self._factory = factory
        self.name = name
        self.sample_rate = sample_rate
        self._engine: Synthesizer | None = None
        self._loading = False
        # Load and unload arrive on a control thread while the speech worker
        # is reading `_engine`; the flag pair and the reference move together
        # or a worker sees `loaded` true with nothing behind it.
        self._lock = threading.Lock()

    @property
    def loaded(self) -> bool:
        with self._lock:
            return self._engine is not None

    @property
    def loading(self) -> bool:
        with self._lock:
            return self._loading

    def load(self) -> None:
        with self._lock:
            if self._engine is not None or self._loading:
                return
            self._loading = True
        try:
            engine = self._factory()
        finally:
            with self._lock:
                self._loading = False
        with self._lock:
            # Checked again: `unload()` may have run while the factory was
            # working, and a load that lands after it would resurrect a model
            # the user just asked to be rid of.
            if self._engine is None:
                self._engine = engine

    def unload(self) -> None:
        with self._lock:
            self._engine = None
        # The tensors are freed to Python here; whether the resident set
        # returns to the OS is the allocator's business, and is measured
        # rather than assumed.
        gc.collect()

    def synthesize(self, text: str, voice: str, speed: float) -> np.ndarray:
        with self._lock:
            engine = self._engine
        if engine is None:
            return np.zeros(0, dtype=np.float32)
        return engine.synthesize(text, voice, speed)
```

- [ ] **Step 4: Run, lint, type-check, commit**

```bash
uv run --no-sync pytest tests/test_lazy_engine.py -v
uv run --no-sync ruff check . && uv run --no-sync mypy src tests
git add src/speakd/synth/lazy.py tests/test_lazy_engine.py
git commit -m "Make the engine something the daemon can put down"
```

---

### Task 5: `SET_ENGINE`, and a daemon that can start disabled

**Files:**
- Modify: `src/speakd/protocol.py`, `src/speakd/daemon.py`, `src/speakd/__main__.py:147-177`, `src/speakd/cli.py`
- Test: `tests/test_mute.py` (extend), `tests/test_daemon.py`

**Interfaces:**
- Consumes: `LazyEngine` (Task 4), `speakd.state` (Task 2).
- Produces: `Verb.SET_ENGINE = "set_engine"`, payload `{"loaded": bool}`; `STATUS` gains `"engine": {"loaded": bool, "loading": bool}`; bus event `kind="engine"` with `data={"state": "loading"|"ready"|"unloaded"}`; `speakctl enable` / `speakctl disable`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_mute.py`:

```python
def test_disabling_unloads_and_enqueues_are_dropped() -> None:
    from speakd.synth.lazy import LazyEngine

    built: list[int] = []
    engine = LazyEngine(lambda: (built.append(1), FakeEngine())[1], sample_rate=24000)
    engine.load()
    bus = EventBus()
    daemon = Daemon(engine, RecordingPlayer(), profile_for, bus=bus, channels=ChannelTable())
    daemon.start()
    try:
        daemon.handle(Request(verb=Verb.SET_ENGINE, source_id="", payload={"loaded": False}))
        assert not engine.loaded
        response = daemon.handle(
            Request(verb=Verb.ENQUEUE, source_id="s", payload={"text": "Hello."})
        )
        assert response.data["spoken"] is False
        assert response.data["reason"] == "disabled"
    finally:
        daemon.stop()


def test_enabling_loads_off_the_request_thread() -> None:
    from speakd.synth.lazy import LazyEngine

    engine = LazyEngine(FakeEngine, sample_rate=24000)
    daemon = Daemon(engine, RecordingPlayer(), profile_for, bus=EventBus(), channels=ChannelTable())
    daemon.start()
    try:
        response = daemon.handle(
            Request(verb=Verb.SET_ENGINE, source_id="", payload={"loaded": True})
        )
        # The verb returns at once; readiness is announced on the bus.
        assert response.ok
        deadline = __import__("time").monotonic() + 5.0
        while not engine.loaded and __import__("time").monotonic() < deadline:
            __import__("time").sleep(0.01)
        assert engine.loaded
    finally:
        daemon.stop()


def test_status_reports_the_engine() -> None:
    from speakd.synth.lazy import LazyEngine

    engine = LazyEngine(FakeEngine, sample_rate=24000)
    daemon = Daemon(engine, RecordingPlayer(), profile_for, bus=EventBus(), channels=ChannelTable())
    daemon.start()
    try:
        status = daemon.handle(Request(verb=Verb.STATUS, source_id=""))
        assert status.data["engine"] == {"loaded": False, "loading": False}
    finally:
        daemon.stop()
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run --no-sync pytest tests/test_mute.py -k "disabl or enabl or engine" -v`
Expected: FAIL — `Verb.SET_ENGINE` does not exist.

- [ ] **Step 3: Implement**

`protocol.py`: `SET_ENGINE = "set_engine"`.

`daemon.py`, in `handle`:

```python
        if request.verb is Verb.SET_ENGINE:
            wanted = request.payload.get("loaded")
            if not isinstance(wanted, bool):
                return Response(ok=False, error="set_engine needs a boolean 'loaded'")
            return self._set_engine_loaded(wanted)
```

and the method:

```python
    def _set_engine_loaded(self, wanted: bool) -> Response:
        """Load or unload the model, without ever blocking the socket.

        Loading takes tens of seconds. Doing it on the request thread would
        stall every other client for the duration, so the verb returns at once
        and readiness is announced on the bus instead.
        """
        loader = getattr(self.engine, "load", None)
        unloader = getattr(self.engine, "unload", None)
        if loader is None or unloader is None:
            return Response(ok=False, error="this engine cannot be loaded or unloaded")
        state.save(state.DaemonState(muted=state.load().muted, disabled=not wanted))
        if not wanted:
            self._silence_for_mute("")
            unloader()
            self._publish("engine", "", {"state": "unloaded"})
            return Response(ok=True, data={"loaded": False})
        self._publish("engine", "", {"state": "loading"})

        def run() -> None:
            loader()
            self._publish("engine", "", {"state": "ready"})

        threading.Thread(target=run, name="speakd-engine-load", daemon=True).start()
        return Response(ok=True, data={"loading": True})
```

In `_enqueue`, extend the drop guard from Task 3 so an unloaded engine drops too, with `reason="disabled"`:

```python
loaded = getattr(self.engine, "loaded", True)
if not loaded:
    self._publish("declined", request.source_id, {"text": text, "kind": kind, "reason": "disabled"})
    return Response(ok=True, data={"spoken": False, "reason": "disabled"})
```

In the `STATUS` branch add:

```python
                    "engine": {
                        "loaded": bool(getattr(self.engine, "loaded", True)),
                        "loading": bool(getattr(self.engine, "loading", False)),
                    },
```

`__main__.py`: replace the eager construction at lines 157-172 with a lazy one that reads the flag first:

```python
from speakd import state
from speakd.synth.kokoro_engine import KokoroEngine
from speakd.synth.lazy import LazyEngine

# The class attribute, not an instance: the player needs the rate to open
# the sink, and asking an instance for it would mean loading the model
# this whole path exists to avoid loading.
engine = LazyEngine(KokoroEngine, name=KokoroEngine.name, sample_rate=KokoroEngine.sample_rate)
if not state.load().disabled:
    # On a thread, so the socket appears at once rather than thirty
    # seconds later. The README already tells users the socket appearing
    # is the readiness signal; this is the first release where that is
    # true.
    threading.Thread(target=engine.load, name="speakd-engine-load", daemon=True).start()
```

The `ModuleNotFoundError` guard that used to wrap `KokoroEngine()` moves inside the factory: wrap `engine.load` in a function that catches it and writes the same message to stderr, since the constructor no longer runs here.

`cli.py`: add `enable` and `disable` subparsers on `common`, both sending `SET_ENGINE` with the appropriate boolean, and `mute` / `unmute` sending `MUTE`. `--source` defaults to `cli`; for these four the default must be `""` (global) instead — give them their own parent parser whose `--source` defaults to `""`.

- [ ] **Step 4: Run the tests**

Run: `uv run --no-sync pytest tests/test_mute.py tests/test_cli_verbs.py -v`
Expected: PASS.

Run: `uv run --no-sync pytest`
Expected: PASS.

- [ ] **Step 5: Measure the memory, and record what actually happened**

The spec promises a measurement, not a number. With the daemon running under systemd:

```bash
P=$(pgrep -f "\.venv/bin/speakd$" | head -1)
ps -o rss= -p "$P" | awk '{printf "before: %.2f GiB\n", $1/1048576}'
uv run --no-sync speakctl disable
sleep 3
ps -o rss= -p "$P" | awk '{printf "after:  %.2f GiB\n", $1/1048576}'
```

Put both figures in the commit message. **If the resident set barely moves, say so** — torch's allocator and CPython's arenas both retain freed memory, and an honest "it fell by 0.4 GiB" is the finding. Do not restate the spec's 3.05 GiB as though it were the saving.

- [ ] **Step 6: Lint, type-check, commit**

```bash
uv run --no-sync ruff check . && uv run --no-sync ruff format --check .
uv run --no-sync mypy src tests
git add src/speakd tests/
git commit -m "Let the daemon put the model down, and start without picking it up"
```

---

## Done when

- `uv run --no-sync pytest`, `ruff check`, `ruff format --check` and `mypy src tests` all pass.
- `speakctl mute` stops speech instantly; `speakctl unmute` restores it; both survive a daemon restart.
- `speakctl mute --source X` silences one channel while others speak.
- `speakctl disable` unloads the model, and a daemon started while disabled never constructs it — check with `pgrep` and `ps -o rss=` that it starts small and fast.
- The measured RSS delta from disabling is recorded in a commit message.
