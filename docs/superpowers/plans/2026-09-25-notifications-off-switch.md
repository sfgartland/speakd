# Notifications Off-Switch Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `speakctl notify off` stops the notifications reader for good (surviving restarts, like mute), `speakctl notify on` brings it back, and `speakctl status` reports which state it is in.

**Architecture:** A `notify_enabled` flag joins `muted`/`disabled`/`speed` in `DaemonState`. The daemon takes ownership of its connector supervisors (`add_connector` / `stop_connectors`) so a new global `set_notify` verb can stop/start the notifications reader; `off` also hushes every `notify:` channel so already-enqueued text does not keep talking after the reader dies.

**Tech Stack:** Python 3.11+ (mypy strict), pytest, ruff. No new dependencies.

## Global Constraints

- Spec: `docs/superpowers/specs/2026-09-25-disable-notifications-design.md` (binding).
- Commands the whole suite must stay green under (this is CI's exact set): `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy src tests`, `uv run pytest -q`.
- Ruff: line length 100; select E, F, I, UP, B. `docs/` is excluded, so code blocks in this plan are not formatted by ruff.
- mypy is strict; tests are type-checked too. Test doubles must satisfy the protocols they stand in for (e.g. `Player` = `play` + `stop`; the new `Connector` protocol = `start` + `stop`).
- The `notify:` channel prefix is a convention owned by `speakd.clients.notifications.rules` (`notify:<app>`, one channel per app). The daemon may hardcode the prefix, with a comment pointing there.
- `SPEAKD_NO_NOTIFY` keeps its meaning: the connector is then not even registered, and `notify on` is refused naming the variable.
- The `set_notify` verb is NOT added to `transport_http.VERBS`; that allowlist stays as it is.
- `conftest.py` sets `SPEAKD_NO_NOTIFY=1` and `SPEAKD_NO_FOLLOWER=1` for every test. Tests must never spawn real connector children.
- Commit per task, from the worktree, in the repo's message style (imperative sentence, e.g. "Add the notify_enabled state flag").
- The off-switch is notifications only; no other client, no window switch, no scheduling (spec, out of scope).

---

### Task 1: The `notify_enabled` state flag

**Files:**
- Modify: `src/speakd/state.py` (`DaemonState`, `load`)
- Test: `tests/test_state.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: `DaemonState.notify_enabled: bool` (default `True`), round-tripped by `load`/`save`; a missing key or unreadable file reads `True`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_state.py` (the file already imports `DaemonState, load, save, state_path` at the top):

```python
def test_notify_enabled_defaults_to_true(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    assert load().notify_enabled is True


def test_notify_enabled_round_trips(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    save(DaemonState(notify_enabled=False))
    assert load().notify_enabled is False
    assert load().muted is False  # one flag written, the others carried through


def test_a_corrupt_file_leaves_the_reader_on(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    # A truncated state file must not silently take the reader away: the
    # safe failure for a speaking machine is to keep the reader.
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    path = state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json")
    assert load().notify_enabled is True
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/test_state.py -v`
Expected: the three new tests FAIL with `AttributeError: 'DaemonState' object has no attribute 'notify_enabled'`. All pre-existing tests in the file PASS.

- [ ] **Step 3: Implement**

In `src/speakd/state.py`, add the field to the dataclass and read it in `load()`:

```python
@dataclass(frozen=True)
class DaemonState:
    muted: bool = False
    disabled: bool = False
    # The listener's multiplier on every profile's speed. Clamped where it is
    # applied (`speakd.tempo`), not here: this module only has to refuse what
    # is not a speed at all.
    speed: float = 1.0
    # The notifications reader's switch, defaulting on: a daemon that cannot
    # read its state file must come back speaking, not silently mute the
    # channel it was reading aloud.
    notify_enabled: bool = True
```

and in `load()`:

```python
    return DaemonState(
        muted=bool(body.get("muted", False)),
        disabled=bool(body.get("disabled", False)),
        speed=speed,
        notify_enabled=bool(body.get("notify_enabled", True)),
    )
```

- [ ] **Step 4: Run them to verify they pass**

Run: `uv run pytest tests/test_state.py -v`
Expected: all PASS (including the untouched pre-existing ones — the new default does not change their equality assertions).

- [ ] **Step 5: Commit**

```bash
git add tests/test_state.py src/speakd/state.py
git commit -m "Add the notify_enabled state flag, defaulting on"
```

---

### Task 2: The daemon owns the connectors

**Files:**
- Modify: `src/speakd/daemon.py` (new `Connector` protocol; `__init__` gains `notify_enabled` and `_connectors`; `add_connector`, `stop_connectors`; `stop()` calls `stop_connectors()` first)
- Modify: `src/speakd/__main__.py` (`CONNECTORS` rows gain names; `_start_children(daemon)`; `_shutdown`; `serve()` uses both)
- Test: `tests/test_connectors.py` (new), `tests/test_notify_wiring.py` (rework)

**Interfaces:**
- Consumes: `DaemonState.notify_enabled` from Task 1.
- Produces:
  - `class Connector(Protocol)` in `speakd.daemon` with `start() -> None` and `stop() -> None`.
  - `Daemon.notify_enabled: bool` (read from `state.load()` in `__init__`).
  - `Daemon.add_connector(name: str, supervisor: Connector) -> None` — held by name, last registration wins.
  - `Daemon.stop_connectors() -> None` — stops every registered connector, idempotent.
  - `speakd.__main__._start_children(daemon: Daemon) -> None` — registers each connector from `CONNECTORS` unless its env var is set; skips *starting* the `notify` one when `daemon.notify_enabled` is false (it stays registered); starts the rest.
  - `speakd.__main__._shutdown(daemon: Daemon, server: SocketServer, http: HttpServer | None) -> None` — `stop_connectors()`, `silence()`, `server.stop()`, `http.stop()`, in that order.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_connectors.py`:

```python
"""The daemon owns its connectors, so the off-switch verb can stop one."""

from collections.abc import Callable, Sequence
from pathlib import Path

from speakd.channels import ChannelTable
from speakd.daemon import Daemon, ProfileView
from speakd.events import EventBus
from speakd.model import Piece
from speakd.player import RecordingPlayer
from speakd.protocol import Request, Response
from speakd.synth.fake import FakeEngine
from speakd.transport import SocketServer


def passthrough(pieces: Sequence[Piece]) -> tuple[list[Piece], list[str]]:
    return list(pieces), []


def profile_for(name: str) -> ProfileView:
    return ProfileView(voice="v", speed=1.0, interrupt_on=(), prepare=passthrough)


class FakeSupervisor:
    """The real supervisor's contract, minus the process: start() is a no-op
    while running, stop() takes it down, and each call is counted."""

    def __init__(self) -> None:
        self.running = False
        self.starts = 0
        self.stops = 0

    def start(self) -> None:
        if self.running:
            return
        self.running = True
        self.starts += 1

    def stop(self) -> None:
        self.running = False
        self.stops += 1


class LoggingSupervisor:
    def __init__(self, log: list[str]) -> None:
        self._log = log

    def start(self) -> None:
        pass

    def stop(self) -> None:
        self._log.append("connectors")


class RecordingPlayerWithOrder(RecordingPlayer):
    def __init__(self, log: list[str]) -> None:
        super().__init__()
        self._log = log

    def stop(self) -> None:
        self._log.append("silence")
        super().stop()


class RecordingServer(SocketServer):
    def __init__(
        self,
        address: Path,
        handler: Callable[[Request], Response],
        bus: EventBus,
        log: list[str],
    ) -> None:
        super().__init__(address, handler, bus)
        self._log = log

    def stop(self) -> None:
        self._log.append("server")
        super().stop()


def build() -> Daemon:
    return Daemon(
        FakeEngine(),
        RecordingPlayer(),
        profile_for,
        bus=EventBus(),
        channels=ChannelTable(),
    )


def test_a_registered_connector_is_stopped_by_stop_connectors() -> None:
    daemon = build()
    notify = FakeSupervisor()
    follower = FakeSupervisor()
    notify.start()
    daemon.add_connector("notify", notify)
    daemon.add_connector("follower", follower)
    daemon.stop_connectors()
    assert notify.stops == 1 and not notify.running
    assert follower.stops == 1


def test_stop_connectors_with_nothing_registered_is_a_no_op() -> None:
    daemon = build()
    daemon.stop_connectors()


def test_daemon_stop_also_stops_connectors() -> None:
    # A daemon stopped without going through serve() (tests, future
    # embeddings) must still take its readers down.
    daemon = build()
    supervisor = FakeSupervisor()
    supervisor.start()
    daemon.add_connector("notify", supervisor)
    daemon.stop()
    assert supervisor.stops == 1


def test_shutdown_stops_connectors_before_silencing_and_closing_the_server(
    tmp_path: Path,
) -> None:
    """serve()'s documented ordering, pinned: a reader must not enqueue into
    a daemon that is shutting down, and server.stop() must stay after
    silence() so a wedged worker can be freed."""
    from speakd import __main__ as entrypoint

    log: list[str] = []
    daemon = Daemon(
        FakeEngine(),
        RecordingPlayerWithOrder(log),
        profile_for,
        bus=EventBus(),
        channels=ChannelTable(),
    )
    daemon.add_connector("notify", LoggingSupervisor(log))
    server = RecordingServer(tmp_path / "speakd.sock", daemon.handle, daemon.bus, log)
    daemon.start()
    server.start()
    try:
        entrypoint._shutdown(daemon, server, None)
        assert log == ["connectors", "silence", "server"]
    finally:
        daemon.stop()
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/test_connectors.py -v`
Expected: FAIL — `AttributeError: 'Daemon' object has no attribute 'add_connector'` (first test); the `_shutdown` test fails with `AttributeError: module 'speakd.__main__' has no attribute '_shutdown'`.

- [ ] **Step 3: Implement the daemon half**

In `src/speakd/daemon.py`:

(a) Add the protocol after `Loadable` (around line 230):

```python
class Connector(Protocol):
    """A supervised client child, reduced to the two operations the daemon's
    off-switch needs. `Supervisor` satisfies it; tests substitute a stub.
    """

    def start(self) -> None: ...

    def stop(self) -> None: ...
```

(b) In `Daemon.__init__`, right after the `self.muted = state.load().muted` block (around line 350):

```python
        # The notifications reader's switch, read back for the same reason:
        # after a restart, the reader must come back exactly as it was left.
        self.notify_enabled = state.load().notify_enabled
        # The supervised client connectors, by name, so the off-switch verb
        # can stop and start the notifications reader. Registered by
        # `__main__` once the socket exists.
        self._connectors: dict[str, Connector] = {}
```

(c) Add the two methods directly after `__init__` ends (just before `def start(self)`):

```python
    def add_connector(self, name: str, supervisor: Connector) -> None:
        """Hold a client connector under `name`, so verbs can stop or start
        it. `stop_connectors()` stops every connector registered here."""
        self._connectors[name] = supervisor

    def stop_connectors(self) -> None:
        """Stop every supervised client. Idempotent: the shutdown path calls
        this before the daemon goes quiet, and `stop()` calls it again to
        catch any that remain."""
        for supervisor in self._connectors.values():
            supervisor.stop()
```

(d) At the top of `Daemon.stop()` (before the existing `self.render_queue.stop()` line, around line 499):

```python
        # The readers go down first, before the daemon goes quiet, so none of
        # them can enqueue into a daemon that is shutting down.
        self.stop_connectors()
```

- [ ] **Step 4: Run the daemon half's tests**

Run: `uv run pytest tests/test_connectors.py -v -k "not shutdown"`
Expected: the first three tests PASS; `test_shutdown_stops_connectors_before_silencing_and_closing_the_server` still fails (no `_shutdown` yet).

- [ ] **Step 5: Implement the `__main__` half**

In `src/speakd/__main__.py`:

(a) Replace the `CONNECTORS` table (lines 166-172):

```python
# One per client connector: its name, the environment variable that
# suppresses it, and the module to run. Data rather than a branch each,
# because the next client should be a line here and nothing else.
CONNECTORS: tuple[tuple[str, str, str], ...] = (
    ("follower", "SPEAKD_NO_FOLLOWER", "speakd.clients.claude_code.follow"),
    ("notify", "SPEAKD_NO_NOTIFY", "speakd.clients.notifications.follow"),
)
```

(b) Replace `_start_children` (lines 175-195):

```python
def _start_children(daemon: Daemon) -> None:
    """Register the client connectors with the daemon, starting those that
    should run.

    Called after the socket exists, never before: a child's first act is to
    connect to it, and one that starts into a closed socket spends its
    backoff on an absence we created.

    Each is suppressible on its own. The only callers of those variables are
    the test suite -- which must never spawn a child that outlives its
    daemon's socket and starts tailing the developer's real transcripts or
    reading their real notifications aloud -- and someone debugging one
    connector by hand.

    The notifications connector can also be off by flag: it is registered,
    so the verb can reach it, but not started -- `set_notify` is how it
    comes up. Suppressed by its variable, it is not even registered, and
    `set_notify` refuses.
    """
    for name, variable, module in CONNECTORS:
        if os.environ.get(variable):
            continue
        supervisor = Supervisor([sys.executable, "-m", module])
        daemon.add_connector(name, supervisor)
        if name == "notify" and not daemon.notify_enabled:
            continue
        supervisor.start()
```

(c) Add `_shutdown` right after `_start_children`:

```python
def _shutdown(daemon: Daemon, server: SocketServer, http: HttpServer | None) -> None:
    """Take the daemon down in the order that keeps every part safe.

    Connectors first, before the daemon goes quiet, so a client cannot
    enqueue into a daemon that is shutting down and have it discarded as
    unspoken. Silenced next, so Ctrl-C goes quiet at once. The two calls
    below keep their order: server.stop()'s shutdown() is what frees a
    speech worker wedged writing to a subscriber that stopped reading, and
    silencing first would make every Ctrl-C wait out a 5s join before
    anything could unwedge it. `daemon.stop()` itself runs after this, in
    `serve`, with the player's close behind it.
    """
    daemon.stop_connectors()
    daemon.silence()
    server.stop()
    if http is not None:
        http.stop()
```

(d) In `serve()` (lines 274-317): replace

```python
    server.start()
    http = start_http(daemon)
    children = _start_children()

    stop.wait()
    # Before the daemon goes quiet, so a client cannot enqueue into a daemon
    # that is shutting down and have it discarded as unspoken.
    for child in children:
        child.stop()
    # Silenced first, so Ctrl-C goes quiet at once. The two calls below keep
    # their order: server.stop()'s shutdown() is what frees a speech worker
    # wedged writing to a subscriber that stopped reading, and putting
    # daemon.stop() first would make every Ctrl-C wait out a 5s join before
    # anything could unwedge it.
    daemon.silence()
    server.stop()
    if http is not None:
        http.stop()
```

with

```python
    server.start()
    http = start_http(daemon)
    _start_children(daemon)

    stop.wait()
    _shutdown(daemon, server, http)
```

Keep the long comment block that precedes `if daemon.stop():` exactly as it is — it still applies to `serve()`'s own tail.

- [ ] **Step 6: Rework `tests/test_notify_wiring.py`**

The file's four `_start_children` tests take no daemon and assert a return value. Replace the `_FakeSupervisor` fixture block and those four tests (lines 80-134) with the version below. Everything above line 80 stays unchanged.

```python
class _FakeSupervisor:
    started: list[Sequence[str]] = []

    def __init__(self, argv: Sequence[str]) -> None:
        self.argv = argv

    def start(self) -> None:
        _FakeSupervisor.started.append(self.argv)

    def stop(self) -> None:
        pass


def _stub_daemon(notify_enabled: bool = True) -> Daemon:
    return Daemon(
        FakeEngine(),
        RecordingPlayer(),
        lambda name: ProfileView(
            voice="v", speed=1.0, interrupt_on=(), prepare=lambda p: (list(p), [])
        ),
        bus=EventBus(),
        channels=ChannelTable(),
    )


@pytest.fixture
def _fake_supervisor(monkeypatch: pytest.MonkeyPatch) -> None:
    _FakeSupervisor.started = []
    monkeypatch.setattr(entrypoint, "Supervisor", _FakeSupervisor)
    # conftest sets both of these for every test; this file is the one place
    # that needs them cleared to see what the daemon would really start.
    monkeypatch.delenv("SPEAKD_NO_FOLLOWER", raising=False)
    monkeypatch.delenv("SPEAKD_NO_NOTIFY", raising=False)


def _modules() -> list[str]:
    return [argv[-1] for argv in _FakeSupervisor.started]


@pytest.mark.usefixtures("_fake_supervisor")
def test_the_daemon_registers_and_starts_both_connectors() -> None:
    daemon = _stub_daemon()
    entrypoint._start_children(daemon)
    assert _modules() == [
        "speakd.clients.claude_code.follow",
        "speakd.clients.notifications.follow",
    ]
    assert [name for name, _ in daemon._connectors.items()] == ["follower", "notify"]


@pytest.mark.usefixtures("_fake_supervisor")
def test_each_connector_can_be_suppressed_on_its_own(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SPEAKD_NO_NOTIFY", "1")
    daemon = _stub_daemon()
    entrypoint._start_children(daemon)
    assert _modules() == ["speakd.clients.claude_code.follow"]
    # Suppressed means not even registered, so the verb has nothing to start.
    assert [name for name, _ in daemon._connectors.items()] == ["follower"]


@pytest.mark.usefixtures("_fake_supervisor")
def test_suppressing_notifications_does_not_suppress_claude_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SPEAKD_NO_FOLLOWER", "1")
    daemon = _stub_daemon()
    entrypoint._start_children(daemon)
    assert _modules() == ["speakd.clients.notifications.follow"]
    assert [name for name, _ in daemon._connectors.items()] == ["notify"]


@pytest.mark.usefixtures("_fake_supervisor")
def test_the_reader_stays_down_when_the_flag_is_off() -> None:
    from speakd import state

    state.save(state.DaemonState(notify_enabled=False))
    daemon = _stub_daemon()
    entrypoint._start_children(daemon)
    # Registered, so the verb can reach it -- but not started.
    assert _modules() == ["speakd.clients.claude_code.follow"]
    assert [name for name, _ in daemon._connectors.items()] == ["follower", "notify"]


@pytest.mark.usefixtures("_fake_supervisor")
def test_the_test_suite_itself_spawns_no_connector(monkeypatch: pytest.MonkeyPatch) -> None:
    """A connector escaping a test run reads the developer's own mail aloud."""
    monkeypatch.setenv("SPEAKD_NO_FOLLOWER", "1")
    monkeypatch.setenv("SPEAKD_NO_NOTIFY", "1")
    daemon = _stub_daemon()
    entrypoint._start_children(daemon)
    assert _modules() == []
    assert daemon._connectors == {}
```

Add these imports to the file's import block (top, keeping `from __future__ import annotations` first):

```python
from speakd.channels import ChannelTable
from speakd.daemon import Daemon, ProfileView
from speakd.events import EventBus
from speakd.player import RecordingPlayer
from speakd.synth.fake import FakeEngine
```

(Note: `_stub_daemon` builds a real `Daemon`, which needs `FakeEngine`/`RecordingPlayer`/`ProfileView`. `conftest.py`'s state isolation keeps `state.save(...)` in the test safe.)

- [ ] **Step 7: Run the full affected tests**

Run: `uv run pytest tests/test_connectors.py tests/test_notify_wiring.py -v`
Expected: all PASS.

- [ ] **Step 8: Commit**

```bash
git add tests/test_connectors.py tests/test_notify_wiring.py src/speakd/daemon.py src/speakd/__main__.py
git commit -m "Give the daemon its connectors, and keep the shutdown order"
```

---

### Task 3: The `set_notify` verb

**Files:**
- Modify: `src/speakd/protocol.py` (add `SET_NOTIFY` to `Verb`)
- Modify: `src/speakd/daemon.py` (handle branch; `_set_notify`; `_silence_notify_channels`; `_DISCARDED_NOTIFY`; status field)
- Test: `tests/test_set_notify.py` (new)

**Interfaces:**
- Consumes: `Daemon.notify_enabled`, `add_connector`, `Connector` (Task 2); `_stop_speaking`, `_silence_for_mute`-style error handling, `_publish` (existing daemon internals).
- Produces:
  - `Verb.SET_NOTIFY = "set_notify"`.
  - `Daemon._set_notify(raw: object) -> Response`: refuses non-bool; refuses `True` when no `notify` connector is registered (error names `SPEAKD_NO_NOTIFY`); otherwise sets `self.notify_enabled`, persists, starts/stops the supervisor, and for off hushes the `notify:` channels; publishes `notify {enabled: bool}` with empty source; answers `ok` either way.
  - `Daemon._silence_notify_channels() -> None`: per-channel hush of every channel whose `source_id` starts with `notify:`, reporting sink failures as `error` events.
  - STATUS data gains `"notify": {"enabled": self.notify_enabled}`.
  - New constant `_DISCARDED_NOTIFY` (discard reason).

- [ ] **Step 1: Write the failing tests**

Create `tests/test_set_notify.py`:

```python
"""The notifications off-switch: stop the reader, hush its channels, persist."""

import threading
from collections.abc import Sequence

import numpy as np

from speakd.channels import ChannelTable
from speakd.daemon import Daemon, ProfileView
from speakd.events import Event, EventBus
from speakd.model import Piece
from speakd.player import FakeSink, RecordingPlayer, StreamingPlayer
from speakd.protocol import Request, Verb
from speakd.synth.fake import FakeEngine


def passthrough(pieces: Sequence[Piece]) -> tuple[list[Piece], list[str]]:
    return list(pieces), []


def profile_for(name: str) -> ProfileView:
    return ProfileView(voice="v", speed=1.0, interrupt_on=(), prepare=passthrough)


class FakeSupervisor:
    """The real supervisor's contract, minus the process: start() is a no-op
    while running, stop() takes it down, and each call is counted."""

    def __init__(self) -> None:
        self.running = False
        self.starts = 0
        self.stops = 0

    def start(self) -> None:
        if self.running:
            return
        self.running = True
        self.starts += 1

    def stop(self) -> None:
        self.running = False
        self.stops += 1


class RudeHoldingPlayer:
    """Blocks inside play() until released, and refuses to stop."""

    def __init__(self) -> None:
        self.playing = threading.Event()
        self.release = threading.Event()
        self.stops = 0

    def play(self, audio: np.ndarray, sample_rate: int) -> None:
        self.playing.set()
        self.release.wait(timeout=10.0)

    def stop(self) -> None:
        self.stops += 1
        raise RuntimeError("the sink refuses")


def build(supervisor: FakeSupervisor | None = None) -> tuple[Daemon, list[Event]]:
    bus = EventBus()
    seen: list[Event] = []
    bus.subscribe(seen.append)
    daemon = Daemon(FakeEngine(), RecordingPlayer(), profile_for, bus=bus, channels=ChannelTable())
    if supervisor is not None:
        daemon.add_connector("notify", supervisor)
    return daemon, seen


def test_off_stops_the_reader_persists_and_announces() -> None:
    from speakd import state

    supervisor = FakeSupervisor()
    supervisor.start()
    daemon, seen = build(supervisor)
    try:
        response = daemon.handle(
            Request(verb=Verb.SET_NOTIFY, source_id="", payload={"enabled": False})
        )
        assert response.ok
        assert supervisor.stops == 1 and not supervisor.running
        assert state.load().notify_enabled is False
        notify = [e for e in seen if e.kind == "notify"]
        assert notify and notify[-1].data == {"enabled": False}
    finally:
        daemon.stop()


def test_on_starts_the_reader_persists_and_announces() -> None:
    from speakd import state

    supervisor = FakeSupervisor()
    daemon, seen = build(supervisor)
    try:
        response = daemon.handle(
            Request(verb=Verb.SET_NOTIFY, source_id="", payload={"enabled": True})
        )
        assert response.ok
        assert supervisor.starts == 1 and supervisor.running
        assert state.load().notify_enabled is True
        notify = [e for e in seen if e.kind == "notify"]
        assert notify and notify[-1].data == {"enabled": True}
        # Idempotent: a second on does not respawn the child.
        daemon.handle(Request(verb=Verb.SET_NOTIFY, source_id="", payload={"enabled": True}))
        assert supervisor.starts == 1
    finally:
        daemon.stop()


def test_off_hushes_the_readers_channels_and_no_others() -> None:
    daemon = Daemon(
        FakeEngine(),
        StreamingPlayer(FakeSink()),
        profile_for,
        bus=EventBus(),
        channels=ChannelTable(),
    )
    daemon.add_connector("notify", FakeSupervisor())
    daemon.start()
    try:
        daemon.handle(Request(verb=Verb.PAUSE, source_id=""))
        daemon.handle(
            Request(verb=Verb.ENQUEUE, source_id="notify:chrome", payload={"text": "Mail."})
        )
        daemon.handle(Request(verb=Verb.ENQUEUE, source_id="s", payload={"text": "Keep me."}))
        daemon.handle(Request(verb=Verb.SET_NOTIFY, source_id="", payload={"enabled": False}))
        status = daemon.handle(Request(verb=Verb.STATUS, source_id=""))
        queue = status.data["queue"]
        assert isinstance(queue, list)
        texts = [job["text"] for job in queue]
        assert "Mail." not in texts
        assert "Keep me." in texts
    finally:
        daemon.handle(Request(verb=Verb.RESUME, source_id=""))
        daemon.stop()


def test_on_with_no_reader_registered_is_refused() -> None:
    daemon, _seen = build(None)
    try:
        response = daemon.handle(
            Request(verb=Verb.SET_NOTIFY, source_id="", payload={"enabled": True})
        )
        assert not response.ok
        assert "SPEAKD_NO_NOTIFY" in response.error
    finally:
        daemon.stop()


def test_off_with_no_reader_registered_still_persists() -> None:
    from speakd import state

    daemon, _seen = build(None)
    try:
        response = daemon.handle(
            Request(verb=Verb.SET_NOTIFY, source_id="", payload={"enabled": False})
        )
        assert response.ok
        assert state.load().notify_enabled is False
    finally:
        daemon.stop()


def test_set_notify_needs_a_boolean() -> None:
    daemon, _seen = build(FakeSupervisor())
    try:
        response = daemon.handle(
            Request(verb=Verb.SET_NOTIFY, source_id="", payload={"enabled": "off"})
        )
        assert not response.ok
        assert "enabled" in response.error
    finally:
        daemon.stop()


def test_status_reports_the_switch() -> None:
    daemon, _seen = build(FakeSupervisor())
    try:
        status = daemon.handle(Request(verb=Verb.STATUS, source_id=""))
        assert status.data["notify"] == {"enabled": True}
        daemon.handle(Request(verb=Verb.SET_NOTIFY, source_id="", payload={"enabled": False}))
        status = daemon.handle(Request(verb=Verb.STATUS, source_id=""))
        assert status.data["notify"] == {"enabled": False}
    finally:
        daemon.stop()


def test_the_switch_survives_a_restart() -> None:
    first, _seen = build(FakeSupervisor())
    try:
        first.handle(Request(verb=Verb.SET_NOTIFY, source_id="", payload={"enabled": False}))
    finally:
        first.stop()
    second, _seen = build(FakeSupervisor())
    try:
        assert second.notify_enabled is False
    finally:
        second.stop()


def test_off_still_persists_when_the_player_refuses_to_stop() -> None:
    from speakd import state

    player = RudeHoldingPlayer()
    bus = EventBus()
    seen: list[Event] = []
    bus.subscribe(seen.append)
    daemon = Daemon(FakeEngine(), player, profile_for, bus=bus, channels=ChannelTable())
    daemon.add_connector("notify", FakeSupervisor())
    daemon.start()
    try:
        daemon.handle(
            Request(verb=Verb.ENQUEUE, source_id="notify:chrome", payload={"text": "One. Two."})
        )
        assert player.playing.wait(timeout=5.0), "the worker never reached the player"
        response = daemon.handle(
            Request(verb=Verb.SET_NOTIFY, source_id="", payload={"enabled": False})
        )
        assert response.ok, "a failing sink must not fail a switch that did flip"
        assert state.load().notify_enabled is False
        errors = [e for e in seen if e.kind == "error"]
        assert errors, "the failing sink must be reported on the bus"
    finally:
        player.release.set()
        daemon.stop()
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/test_set_notify.py -v`
Expected: FAIL — `ValueError: 'set_notify' is not a valid Verb` (raised from `Request(verb=Verb.SET_NOTIFY, ...)`) on every test. Also run `uv run mypy src tests` — it will fail on `Verb.SET_NOTIFY` until the enum gains it.

- [ ] **Step 3: Add the verb to the protocol**

In `src/speakd/protocol.py`, inside `class Verb`, after `MUTE = "mute"`:

```python
    SET_NOTIFY = "set_notify"
```

- [ ] **Step 4: Run again to see the daemon-side failures**

Run: `uv run pytest tests/test_set_notify.py -v`
Expected: FAIL — now with `set_notify is not handled here` responses (or `AttributeError` on the status key), since the daemon does not handle the verb yet.

- [ ] **Step 5: Implement the daemon half**

In `src/speakd/daemon.py`:

(a) After `_DISCARDED_MUTED` (around line 185):

```python
_DISCARDED_NOTIFY = (
    "discarded unspoken: the notifications reader was switched off before this was spoken"
)
```

(b) In `Daemon.handle`, directly after the MUTE branch (which ends around line 1241):

```python
        if request.verb is Verb.SET_NOTIFY:
            return self._set_notify(request.payload.get("enabled"))
```

(c) After `_silence_for_mute` (which ends around line 642), add the two methods:

```python
    def _set_notify(self, raw: object) -> Response:
        """Stop or start the notifications reader, persisting the choice.

        Global, like a global mute: the reader is one process for every
        `notify:` channel, so there is no per-channel form to honour.

        Persisted before anything can fail, as mute does: once the switch
        has flipped in memory, a sink that refuses to stop is a reported
        error beside a switch that did flip -- failing the verb instead
        would refuse an action that took effect anyway.
        """
        if not isinstance(raw, bool):
            return Response(ok=False, error="set_notify needs a boolean 'enabled'")
        if raw and self._connectors.get("notify") is None:
            return Response(
                ok=False,
                error=(
                    "the notifications reader is not registered in this daemon "
                    "(SPEAKD_NO_NOTIFY is set)"
                ),
            )
        self.notify_enabled = raw
        state.save(replace(state.load(), notify_enabled=raw))
        supervisor = self._connectors.get("notify")
        if raw:
            if supervisor is not None:
                supervisor.start()
        else:
            if supervisor is not None:
                supervisor.stop()
            self._silence_notify_channels()
        self._publish("notify", "", {"enabled": raw})
        return Response(ok=True, data={"enabled": raw})

    def _silence_notify_channels(self) -> None:
        """Hush every `notify:` channel: text the reader enqueued before it
        died must not keep talking after it.

        The prefix is the notifications rules' channel convention, one
        channel per app (`speakd.clients.notifications.rules`). Iterated
        channel by channel, like a scoped mute -- a drain that took the
        whole queue would throw away every other session's speech with the
        reader's. A sink that refuses to stop is reported, not raised: the
        switch has already flipped.
        """
        for channel in self.channels.all():
            if not channel.source_id.startswith("notify:"):
                continue
            try:
                self._stop_speaking(channel.source_id, drain=True, reason=_DISCARDED_NOTIFY)
            except Exception as exc:
                self._publish(
                    "error",
                    channel.source_id,
                    {"message": f"could not silence the player: {exc!r}"},
                )
```

(d) In `handle`'s STATUS data, directly after `"muted": self.muted,` (around line 1173):

```python
                    "notify": {"enabled": self.notify_enabled},
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `uv run pytest tests/test_set_notify.py -v`
Expected: all PASS.

- [ ] **Step 7: Commit**

```bash
git add tests/test_set_notify.py src/speakd/protocol.py src/speakd/daemon.py
git commit -m "Add the set_notify verb: stop the reader, hush its channels, persist"
```

---

### Task 4: `speakctl notify off` / `on`, and the HTTP guard

**Files:**
- Modify: `src/speakd/cli.py` (subparsers `off`/`on`; `_notify` dispatch; `_notify_switch`)
- Test: `tests/test_cli_notify.py` (new tests), `tests/test_cli_verbs.py` (parser-surface + end-to-end tests), `tests/test_transport_http.py` (extend the allowlist test)

**Interfaces:**
- Consumes: `Verb.SET_NOTIFY` (Task 3); `cli._call`, `_refused`, `_UNREACHABLE` (existing).
- Produces: `speakctl notify off|on` (each takes `--socket` only; exits 0 on ok, 1 on refusal, 2 unreachable); `_notify_switch(args, enabled)`.

- [ ] **Step 1: Write the failing tests**

(a) Append to `tests/test_cli_notify.py` (the file already imports `cli`, `Path`, `pytest`):

```python
def test_off_sends_set_notify_false(monkeypatch: pytest.MonkeyPatch) -> None:
    from speakd.protocol import Request, Response, Verb

    sent: list[Request] = []

    def fake_call(socket_path: Path, request: Request) -> Response:
        sent.append(request)
        return Response(ok=True, data={"enabled": request.payload["enabled"]})

    monkeypatch.setattr(cli, "_call", fake_call)
    assert cli.main(["notify", "off"]) == 0
    assert len(sent) == 1
    assert sent[0].verb is Verb.SET_NOTIFY
    assert sent[0].source_id == ""
    assert sent[0].payload == {"enabled": False}


def test_on_sends_set_notify_true(monkeypatch: pytest.MonkeyPatch) -> None:
    from speakd.protocol import Request, Response, Verb

    sent: list[Request] = []

    def fake_call(socket_path: Path, request: Request) -> Response:
        sent.append(request)
        return Response(ok=True, data={"enabled": request.payload["enabled"]})

    monkeypatch.setattr(cli, "_call", fake_call)
    assert cli.main(["notify", "on"]) == 0
    assert len(sent) == 1
    assert sent[0].verb is Verb.SET_NOTIFY
    assert sent[0].payload == {"enabled": True}


def test_on_reports_a_refusal(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from speakd.protocol import Request, Response

    def fake_call(socket_path: Path, request: Request) -> Response:
        return Response(
            ok=False,
            error="the notifications reader is not registered in this daemon "
            "(SPEAKD_NO_NOTIFY is set)",
        )

    monkeypatch.setattr(cli, "_call", fake_call)
    assert cli.main(["notify", "on"]) == 1
    assert "SPEAKD_NO_NOTIFY" in capsys.readouterr().err


def test_off_with_no_daemon_is_the_usual_unreachable(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_call(socket_path: Path, request: object) -> None:
        return None

    monkeypatch.setattr(cli, "_call", fake_call)
    assert cli.main(["notify", "off"]) == 2
```

(b) Append to `tests/test_cli_verbs.py` (the file already imports `main`; add `from speakd.protocol import Request, Verb` to its import block; the `running` fixture exists):

```python
def test_notify_off_and_on_take_only_the_socket() -> None:
    """The off switch is global, so there is deliberately no --source."""
    from speakd.cli import _build_parser

    parser = _build_parser()
    args = parser.parse_args(["notify", "off"])
    assert not hasattr(args, "source")
    assert args.socket is not None


def test_notify_off_and_on_through_the_socket(running) -> None:  # type: ignore[no-untyped-def]
    address, daemon, _player = running

    class Stub:  # a real daemon in this suite has no connectors (conftest)
        def start(self) -> None:
            pass

        def stop(self) -> None:
            pass

    daemon.add_connector("notify", Stub())
    assert main(["notify", "off", "--socket", str(address)]) == 0
    assert daemon.notify_enabled is False
    status = daemon.handle(Request(verb=Verb.STATUS, source_id=""))
    assert status.data["notify"] == {"enabled": False}
    assert main(["notify", "on", "--socket", str(address)]) == 0
    assert daemon.notify_enabled is True


def test_notify_on_is_refused_when_no_reader_is_registered(running, capsys) -> None:  # type: ignore[no-untyped-def]
    address, _daemon, _player = running
    assert main(["notify", "on", "--socket", str(address)]) == 1
    assert "SPEAKD_NO_NOTIFY" in capsys.readouterr().err
```

(c) In `tests/test_transport_http.py`, extend `test_only_the_readers_verbs_are_served` (line 213) with one more assertion:

```python
    assert (
        call(http, "POST", "/v1/set_notify", {"source_id": "", "payload": {"enabled": False}})[0]
        == 404
    )
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/test_cli_notify.py tests/test_cli_verbs.py tests/test_transport_http.py -v`
Expected: the new CLI tests FAIL — `cli.main(["notify", "off"])` falls through to `_notify`'s usage print and exits 2 (no `off` subparser); the parser-surface test fails with a SystemExit from argparse (unknown subcommand); the HTTP assertion FAILS only if someone added the verb (it currently passes — that is the guard doing its job).

- [ ] **Step 3: Implement**

In `src/speakd/cli.py`:

(a) In `_build_parser`, after the `test` subparser block (which ends around line 228) and before `return parser`:

```python
    # The reader's off switch is global, so these take only `--socket`:
    # there is deliberately no `--source` to mislead with, as `wide`'s
    # would be.
    notify_switch = argparse.ArgumentParser(add_help=False)
    notify_switch.add_argument(
        "--socket",
        type=Path,
        default=default_socket_path(),
        help="where the daemon listens (default: %(default)s)",
    )
    notify_sub.add_parser(
        "off", parents=[notify_switch], help="stop the notifications reader, and keep it stopped"
    )
    notify_sub.add_parser(
        "on", parents=[notify_switch], help="bring the notifications reader back"
    )
```

(b) Extend `_notify` (around line 671) so the switch is handled before the usage fallback:

```python
def _notify(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    if args.notify_command in ("off", "on"):
        return _notify_switch(args, args.notify_command == "on")
    if args.notify_command == "recent":
        return _notify_recent(args)
    if args.notify_command == "tap":
        return _notify_tap()
    if args.notify_command == "init":
        return _notify_init(args)
    if args.notify_command == "test":
        return _notify_test(args)
    parser.print_usage(sys.stderr)
    return 2
```

(c) Add `_notify_switch` right after `_notify`:

```python
def _notify_switch(args: argparse.Namespace, enabled: bool) -> int:
    """`notify off` and `notify on`: the persistent reader switch."""
    response = _call(
        args.socket,
        Request(verb=Verb.SET_NOTIFY, source_id="", payload={"enabled": enabled}),
    )
    if response is None:
        return _UNREACHABLE
    return 0 if response.ok else _refused(response)
```

- [ ] **Step 4: Run them to verify they pass**

Run: `uv run pytest tests/test_cli_notify.py tests/test_cli_verbs.py tests/test_transport_http.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add tests/test_cli_notify.py tests/test_cli_verbs.py tests/test_transport_http.py src/speakd/cli.py
git commit -m "Add speakctl notify off/on, socket-only, over the set_notify verb"
```

---

### Full-suite gate

- [ ] **Step 1: Run the whole suite**

Run: `uv run ruff check . && uv run ruff format --check . && uv run mypy src tests && uv run pytest -q`
Expected: everything green. If ruff reformats a touched file, apply `uv run ruff format <file>` and amend that file's task commit is NOT needed — just run format and re-run the gate; formatting diffs may be folded into a final small commit if any.

- [ ] **Step 2: Manual smoke (optional but recommended, on the developer machine)**

Run against the real daemon after merge:

```bash
speakctl notify off && speakctl status | grep -A1 '"notify"'
speakctl notify on && speakctl status | grep -A1 '"notify"'
```

Expected: `"notify": {"enabled": false}` then `"enabled": true`, and while off, notifications stop being read (the reader process is gone: `pgrep -f "[n]otifications.follow"` finds nothing, and comes back after `on`).
