# Transport Wiring Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `pause`, `resume` and a real transport state reach the player and come back out on the event bus, so the GUI's controls do something and its indicator tells the truth.

**Architecture:** A second, narrow protocol — `Pausable` — beside the existing `Player`. The daemon probes for it rather than requiring it, so `RecordingPlayer` and every existing test keep working unchanged, and a player that cannot pause says so in one clear line instead of pretending. `StreamingPlayer` becomes the default player everywhere audio is real.

**Tech Stack:** Python 3.11–3.13, numpy, pytest, ruff, mypy strict.

**Spec:** `docs/design/2026-09-13-speakd-design.md` (Control surface, Failure handling) and `docs/design/2026-09-13-gui-milestone-design.md`.

## Why this plan exists

Three pieces were built separately and each is correct on its own:

- `StreamingPlayer` can pause, resume and be interrupted mid-segment.
- `Daemon.handle` accepts `Verb.PAUSE` and `Verb.RESUME` — and answers both with `not implemented`.
- The pinned GUI draws a paused indicator and a pause button.

Nothing joins them. `Player` is `play(audio, sample_rate)` and `stop()`, which is all the pipeline ever needed, so the daemon holds a player it cannot pause. This plan is the joint, and it is deliberately small: one new protocol, one new event, one default swapped.

**Seek stays unimplemented.** The `Timeline` can already answer `time_of(offset)` and `span_at(t)`, so the *mapping* for seeking exists — but moving playback to a point means re-synthesising or retaining audio for segments already played, which is a design question this plan does not have an answer to. `seek` keeps returning `not implemented`, and the GUI's scrubber stays a position display rather than a control. Say so in the GUI, rather than shipping a control that does nothing.

## Global Constraints

- Python `>=3.11,<3.14`; core dependencies numpy-only. No test may require an audio device or the `kokoro` extra.
- **Existing players must keep working.** `RecordingPlayer` has no `pause`; adding pause to the `Player` protocol itself would break every test in the project. The probe is the point.
- Speech never disappears silently: a refused verb returns a reason a person can act on, never a bare `False`.
- Transport state is reported, never inferred. The GUI must not have to guess paused-ness from the absence of events.
- ruff line length 100. `uv run pytest`, `uv run ruff check .`, `uv run ruff format --check .` and `uv run mypy src tests` must all pass before every commit.
- Commit after every task.

## Dependencies

This plan starts after **both** of these have merged to `main`:
- `docs/plans/2026-09-13-streaming-player.md` — gives `StreamingPlayer`, `AudioSink`, `SoundDeviceSink`, `pause()`, `resume()`, `paused`.
- `docs/plans/2026-09-13-daemon.md` — gives `Daemon.handle`, the event bus, and `speakctl`'s daemon verbs.

If either is missing, stop and report rather than reimplementing its half.

---

### Task 1: The pausable seam

**Files:**
- Modify: `src/speakd/player.py`
- Test: `tests/test_player.py`

**Interfaces:**
- Produces: `Pausable` — a `@runtime_checkable` Protocol with `pause() -> None`, `resume() -> None`, and a `paused` property returning `bool`.

`runtime_checkable` on a Protocol with a property is the subtle part: `isinstance` checks only for the *presence* of the attributes, not their types or signatures. That is exactly what is wanted here — the daemon asks "can this player pause?", and a class answering yes by accident is not a failure mode this project has.

`StreamingPlayer` already satisfies it without modification. Do not add an inheritance declaration; structural typing is the whole point, and making `StreamingPlayer` inherit would invite the next person to make `Player` a base class too.

- [ ] **Step 1: Write the failing test**

```python
def test_streaming_player_is_pausable() -> None:
    from speakd.player import FakeSink, Pausable, StreamingPlayer

    assert isinstance(StreamingPlayer(FakeSink()), Pausable)


def test_recording_player_is_not_pausable() -> None:
    from speakd.player import Pausable, RecordingPlayer

    assert not isinstance(RecordingPlayer(), Pausable)


def test_recording_player_is_still_a_player() -> None:
    """The whole point of a separate protocol: nothing existing breaks."""
    import numpy as np

    from speakd.player import RecordingPlayer

    player = RecordingPlayer()
    player.play(np.zeros(4, dtype=np.float32), 24000)
    player.stop()
    assert len(player.played) == 1
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_player.py -k pausable -v`
Expected: FAIL — `ImportError: cannot import name 'Pausable'`

- [ ] **Step 3: Write the implementation**

In `src/speakd/player.py`, beside the existing `Player` protocol:

```python
@runtime_checkable
class Pausable(Protocol):
    """A player whose playback can be suspended and taken up again.

    Deliberately separate from `Player`: the pipeline never needs this, and
    folding it into `Player` would make every test double implement two
    methods it has no use for. The daemon probes with `isinstance` and
    refuses the verb, with a reason, when the answer is no.
    """

    def pause(self) -> None: ...

    def resume(self) -> None: ...

    @property
    def paused(self) -> bool: ...
```

Add `runtime_checkable` to the existing `from typing import ...` line.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_player.py -v`
Expected: PASS, including every pre-existing test in the file.

- [ ] **Step 5: Gates and commit**

```bash
uv run pytest -q && uv run ruff check . && uv run ruff format --check . && uv run mypy src tests
git add src/speakd/player.py tests/test_player.py
git commit -m "Name the players that can pause"
```

---

### Task 2: Pause and resume reach the player

**Files:**
- Modify: `src/speakd/daemon.py`
- Test: `tests/test_daemon.py`

**Interfaces:**
- Consumes: `speakd.player.Pausable`
- Produces: `Verb.PAUSE` and `Verb.RESUME` answered for real; `Response.data["paused"]` carrying the resulting state; a `transport` event on the bus.

Three behaviours, and the third is the one the GUI actually needs:

1. A pausable player pauses, and the response says so.
2. A player that is not pausable gets `Response(ok=False, error=...)` naming what it is — `not implemented` told the caller nothing about whether to try again with a different setup.
3. **Every transport change emits an event.** The GUI subscribes; it must never have to infer paused-ness from silence. Emit on the successful change only — a pause when already paused is a no-op and emitting would make the GUI flicker.

`seek` keeps its existing `not implemented` response. Leave that branch alone.

- [ ] **Step 1: Write the failing test**

```python
def test_pause_pauses_a_pausable_player(running_daemon) -> None:  # type: ignore[no-untyped-def]
    daemon = running_daemon(player=StreamingPlayer(FakeSink()))
    response = daemon.handle(Request(verb=Verb.PAUSE, source_id="s"))
    assert response.ok
    assert response.data["paused"] is True
    assert daemon.player.paused is True


def test_resume_unpauses(running_daemon) -> None:  # type: ignore[no-untyped-def]
    daemon = running_daemon(player=StreamingPlayer(FakeSink()))
    daemon.handle(Request(verb=Verb.PAUSE, source_id="s"))
    response = daemon.handle(Request(verb=Verb.RESUME, source_id="s"))
    assert response.ok
    assert response.data["paused"] is False
    assert daemon.player.paused is False


def test_pausing_a_player_that_cannot_pause_says_why(running_daemon) -> None:  # type: ignore[no-untyped-def]
    daemon = running_daemon(player=RecordingPlayer())
    response = daemon.handle(Request(verb=Verb.PAUSE, source_id="s"))
    assert not response.ok
    assert "pause" in response.error
    assert "RecordingPlayer" in response.error


def test_a_transport_change_is_announced(running_daemon) -> None:  # type: ignore[no-untyped-def]
    daemon = running_daemon(player=StreamingPlayer(FakeSink()))
    seen: list[Event] = []
    daemon.bus.subscribe(seen.append)
    daemon.handle(Request(verb=Verb.PAUSE, source_id="s"))
    transport = [event for event in seen if event.kind == "transport"]
    assert len(transport) == 1
    assert transport[0].data["paused"] is True


def test_pausing_twice_announces_once(running_daemon) -> None:  # type: ignore[no-untyped-def]
    """A GUI that redraws on every event must not flicker on a no-op."""
    daemon = running_daemon(player=StreamingPlayer(FakeSink()))
    seen: list[Event] = []
    daemon.bus.subscribe(seen.append)
    daemon.handle(Request(verb=Verb.PAUSE, source_id="s"))
    daemon.handle(Request(verb=Verb.PAUSE, source_id="s"))
    assert len([event for event in seen if event.kind == "transport"]) == 1


def test_seek_is_still_refused(running_daemon) -> None:  # type: ignore[no-untyped-def]
    daemon = running_daemon(player=StreamingPlayer(FakeSink()))
    assert not daemon.handle(Request(verb=Verb.SEEK, source_id="s")).ok
```

Use the file's existing daemon fixture; add a `player=` parameter to it if it does not take one. Import `Event`, `FakeSink`, `StreamingPlayer` and `RecordingPlayer` at the top of the test file with the others.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_daemon.py -k "pause or resume or transport or seek" -v`
Expected: FAIL — the pause tests fail with `assert not response.ok` inverted, because the current branch returns `ok=False` for everything.

- [ ] **Step 3: Write the implementation**

Replace the `if request.verb in (Verb.PAUSE, Verb.RESUME, Verb.SEEK):` branch in `Daemon.handle` with:

```python
        if request.verb in (Verb.PAUSE, Verb.RESUME):
            return self._set_paused(request.verb is Verb.PAUSE)
        if request.verb is Verb.SEEK:
            return Response(ok=False, error=_NOT_IMPLEMENTED)
```

and add the method:

```python
    def _set_paused(self, paused: bool) -> Response:
        """Suspend or take up playback, announcing only a real change.

        A player that cannot pause is not an error in the daemon; it is a
        fact about how this daemon was constructed, and the caller is told
        which player it got so the answer is actionable.
        """
        player = self.player
        if not isinstance(player, Pausable):
            return Response(
                ok=False,
                error=f"{type(player).__name__} cannot pause",
            )
        if player.paused == paused:
            return Response(ok=True, data={"paused": paused})
        if paused:
            player.pause()
        else:
            player.resume()
        self.bus.publish(Event(kind="transport", data={"paused": paused}))
        return Response(ok=True, data={"paused": paused})
```

Import `Pausable` from `speakd.player` at the top of `daemon.py`. Match the existing `Event` construction in the file — if events there carry a `source_id` or a timestamp, carry one here too rather than inventing a second shape.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_daemon.py -v`
Expected: PASS, including every pre-existing daemon test.

- [ ] **Step 5: Gates and commit**

```bash
uv run pytest -q && uv run ruff check . && uv run ruff format --check . && uv run mypy src tests
git add src/speakd/daemon.py tests/test_daemon.py
git commit -m "Pause and resume reach the player, and say so on the bus"
```

---

### Task 3: A discarded utterance is its own event

**Files:**
- Modify: `src/speakd/daemon.py`
- Test: `tests/test_daemon.py`

**Interfaces:**
- Produces: events of kind `discarded` when `hush` clears the queue.

Hush's discards currently arrive as `error` events, so a GUI wanting to show "3 dropped" has to match on message text. Matching on prose is a bug waiting for the day someone rewords the message.

This is small, and it is the last cheap moment: once a GUI ships against `error`, changing the kind breaks it.

**Keep speech-never-disappears intact.** A discarded utterance must still be *visible* — this changes which kind carries it, not whether it is reported. If any existing test asserts on the `error` event for a discard, update it to the new kind rather than deleting it.

- [ ] **Step 1: Write the failing test**

```python
def test_hush_reports_discards_as_their_own_kind(running_daemon) -> None:  # type: ignore[no-untyped-def]
    daemon = running_daemon()
    seen: list[Event] = []
    daemon.bus.subscribe(seen.append)
    # Queue more than the worker can take, then hush.
    for index in range(3):
        daemon.handle(Request(verb=Verb.ENQUEUE, source_id="s", payload={"text": f"Item {index}."}))
    response = daemon.handle(Request(verb=Verb.HUSH, source_id="s"))
    assert response.ok

    discarded = [event for event in seen if event.kind == "discarded"]
    if response.data["discarded"]:
        assert discarded, "a discard was counted but never announced"
        assert sum(int(event.data["count"]) for event in discarded) == response.data["discarded"]


def test_a_discard_is_not_reported_as_an_error(running_daemon) -> None:  # type: ignore[no-untyped-def]
    daemon = running_daemon()
    seen: list[Event] = []
    daemon.bus.subscribe(seen.append)
    for index in range(3):
        daemon.handle(Request(verb=Verb.ENQUEUE, source_id="s", payload={"text": f"Item {index}."}))
    daemon.handle(Request(verb=Verb.HUSH, source_id="s"))
    for event in seen:
        if event.kind == "error":
            assert "discard" not in str(event.data).lower()
```

Note the conditional in the first test: how many utterances a hush discards depends on how fast the worker drained them, so asserting an exact count would be a timing test. What is asserted unconditionally is the *invariant* — a counted discard is an announced discard.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_daemon.py -k discard -v`
Expected: FAIL — no `discarded` events exist yet.

- [ ] **Step 3: Write the implementation**

In `_drain_queued` (or wherever the hush drain publishes today), change the published event's kind from `error` to `discarded` and give it `data={"count": <n>}`. Publish once for the whole drain, not once per item: a hush of forty queued utterances should not put forty events on the bus.

Keep the `finally` that guards the pending counter exactly as it is — it was added in Task 7's consolidation for a reason, and this change must not move it.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_daemon.py -v`
Expected: PASS. Fix any pre-existing test that asserted on the old `error` kind by updating its expected kind; do not delete it.

- [ ] **Step 5: Gates and commit**

```bash
uv run pytest -q && uv run ruff check . && uv run ruff format --check . && uv run mypy src tests
git add src/speakd/daemon.py tests/test_daemon.py
git commit -m "Announce discards as discards, not as errors"
```

---

### Task 4: StreamingPlayer becomes the default

**Files:**
- Modify: `src/speakd/__main__.py`, `src/speakd/cli.py`
- Test: `tests/test_cli_verbs.py`

**Interfaces:**
- Consumes: `speakd.player.StreamingPlayer`, `SoundDeviceSink`
- Produces: no new names — a swapped construction.

`SoundDevicePlayer` blocks for the whole segment and cannot be interrupted, so with it in place a hush waits up to four seconds for the current sentence to end. Every piece needed to fix that now exists; this is the line that turns it on.

Keep `SoundDevicePlayer` in the codebase. It is the simplest thing that plays audio, it is what the tests of the sink-free path use, and deleting it buys nothing.

The sample rate matters: `SoundDeviceSink` takes one at construction, while `play()` also receives one per segment. Use the engine's rate — read it from the engine rather than hardcoding 24000, and if the engine does not expose one, hardcode Kokoro's 24000 with a comment naming why.

- [ ] **Step 1: Write the failing test**

```python
def test_the_daemon_entry_point_builds_a_pausable_player() -> None:
    """A daemon whose player cannot pause makes the GUI's controls dead."""
    from speakd.__main__ import build_player
    from speakd.player import Pausable

    assert isinstance(build_player(sample_rate=24000, fake=True), Pausable)


def test_pause_works_end_to_end_through_the_socket(running) -> None:  # type: ignore[no-untyped-def]
    address, daemon, _player = running
    assert main(["pause", "--source", "s", "--socket", str(address)]) == 0
    assert main(["resume", "--source", "s", "--socket", str(address)]) == 0
```

The second test needs the fixture's daemon to hold a pausable player; update the fixture in `tests/test_cli_verbs.py` to construct `StreamingPlayer(FakeSink())` instead of `RecordingPlayer()` if the assertions it already makes still hold — `FakeSink.blocks` gives the same evidence `RecordingPlayer.played` did. If any existing assertion cannot be expressed against `FakeSink`, keep that test on `RecordingPlayer` and add a second fixture rather than weakening it.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_cli_verbs.py -k "pausable or end_to_end" -v`
Expected: FAIL — `ImportError: cannot import name 'build_player'`

- [ ] **Step 3: Write the implementation**

Add to `src/speakd/__main__.py`:

```python
def build_player(*, sample_rate: int, fake: bool = False) -> Player:
    """The player the daemon runs with.

    `StreamingPlayer` rather than `SoundDevicePlayer` because a hush that
    waits for the current sentence to end is not a hush. `fake=True` is for
    tests, which must never open an audio device.
    """
    if fake:
        from speakd.player import FakeSink, StreamingPlayer

        return StreamingPlayer(FakeSink())
    from speakd.player import SoundDeviceSink, StreamingPlayer

    return StreamingPlayer(SoundDeviceSink(sample_rate))
```

and use it where `__main__` constructs the daemon's player today. Add `pause` and `resume` subcommands to `speakctl` alongside the other daemon verbs, following the shape the existing verbs use — same `--source`, same `--socket`, same one-line failure message.

`speakctl say` keeps `SoundDevicePlayer`: it is a single blocking utterance with nothing to interrupt it, and changing it would alter behaviour this plan has no reason to touch.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest -v`
Expected: PASS, whole suite.

- [ ] **Step 5: Check it by hand**

```bash
uv run speakd &
uv run speakctl enqueue "One. Two. Three. Four. Five. Six." --source manual
# while it is speaking:
uv run speakctl pause --source manual      # audio should stop within about a second
uv run speakctl resume --source manual     # and take up where it left off
uv run speakctl hush --source manual       # and cut off mid-sentence, not at the end of one
```

Record in the report what actually happened, especially how long the hush took to take effect. If it waits for the sentence to finish, the swap did not take — say so rather than reporting success.

- [ ] **Step 6: Gates and commit**

```bash
uv run pytest -q && uv run ruff check . && uv run ruff format --check . && uv run mypy src tests
git add src/speakd/__main__.py src/speakd/cli.py tests/test_cli_verbs.py
git commit -m "Run the daemon on the interruptible player"
```

---

## What this plan leaves for later

- **Seek.** The mapping exists; moving playback does not. It needs a decision about retaining or re-synthesising audio for segments already played, which is a design question, not an implementation one.
- **Per-channel pause.** Pause here is global, because the player is. Pausing one channel while another speaks means one player per channel, which is a larger change than the GUI currently asks for.
- **Transport state on subscribe.** A GUI connecting mid-utterance learns the transport state only at the next change. A `state` snapshot in the subscribe response would fix it, and belongs with the HTTP/SSE transport plan, where the GUI's connection story is settled.
