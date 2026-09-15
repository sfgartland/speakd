# Narration: the transcript follower — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Speak a Claude Code message when it lands on disk instead of when the tool after it finishes, and stop charging the user ~250 ms of Python startup on every single tool call.

**Architecture:** A `speakd-claude-follow` process, spawned and supervised by the daemon, polls the registration files the hook already writes and tails each live transcript. `Stop` and `PostToolUse` hooks are deleted outright; `UserPromptSubmit` and `Notification` survive, once per turn rather than once per tool call.

**Tech Stack:** Python 3.11–3.13, pytest, ruff, mypy (strict). No new dependencies — **polling, not inotify**.

**Spec:** `docs/design/2026-09-15-streaming-narration-design.md` (§4)

## Global Constraints

- **Never run `uv sync`, in any form.** Always `uv run --no-sync ...`. This venv holds a hand-installed CPU-only torch that any sync prunes.
- Tests: `uv run --no-sync pytest`. Lint: `ruff check .`, `ruff format --check .`. Types: `mypy src tests` (strict).
- `line-length = 100`. `requires-python = ">=3.11,<3.14"`.
- Branch: `feat/streaming-narration`. **Depends on Plan A Task 1** (the import diet) for its benefit, not its correctness.
- **Poll, never inotify.** The transcript is append-only, so `os.path.getsize()` on a timer is portable and dependency-free. inotify would tie the follower to Linux for no gain.
- No test may spawn a real daemon, open an audio device, or reach a real socket.

---

## Measured facts this plan rests on

Re-derive these if anything looks wrong; do not take them on faith.

- Claude Code writes an assistant message's content blocks to the transcript **together, when the message completes**. Sampling at 150 ms during generation showed `thinking`, `text:97` and `tool_use` for one message landing 2 ms apart after ~16 s of generation. **Sub-message streaming is impossible; do not attempt it.**
- Each content block is nonetheless its own JSONL line, with its own `uuid`.
- A tool result is a record of type `"user"`. This is what breaks `_from_start` (Task 4).
- `ai-title` records carry a human-readable session name and are rewritten as the session evolves: `{"type": "ai-title", "aiTitle": "...", "sessionId": "..."}`.
- Records carry `cwd`, `sessionId`, `isSidechain`.

---

## File Structure

| File | Responsibility |
|---|---|
| `src/speakd/clients/claude_code/registry.py` | **new.** Which sessions are live, and where their transcripts are. Pure functions over a directory. |
| `src/speakd/clients/claude_code/follow.py` | **new.** The poll loop: tail, enqueue, label, advance the watermark. |
| `src/speakd/clients/claude_code/hook.py` | loses `Stop`/`PostToolUse` speaking; `UserPromptSubmit` registers |
| `src/speakd/clients/claude_code/transcript.py` | `ai_title()` helper |
| `src/speakd/clients/claude_code/reader.py` | `_from_start` bug; start-at-EOF |
| `src/speakd/daemon.py` or `__main__.py` | spawns and supervises the follower child |
| `clients/claude-code/hooks/hooks.json` | four events become two |
| `clients/claude-code/README.md`, `README.md` | the new shape |

---

### Task 1: The session registry

**Files:**
- Create: `src/speakd/clients/claude_code/registry.py`, `tests/test_cc_registry.py`

**Interfaces:**
- Consumes: `speakd.clients.claude_code.watermark.state_dir`.
- Produces:
  - `Registration` — frozen dataclass: `session_id: str`, `transcript: Path`, `cwd: str`, `touched: float`
  - `register(session_id: str, transcript: Path, cwd: str) -> None`
  - `live(max_idle_seconds: float = 1800.0) -> list[Registration]`

- [ ] **Step 1: Write the failing test**

```python
"""Tests for which Claude Code sessions are worth following."""

import time
from pathlib import Path

from speakd.clients.claude_code import registry


def test_a_registered_session_is_live(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("SPEAKD_STATE_DIR", str(tmp_path))
    registry.register("s1", Path("/tmp/a.jsonl"), "/home/me/project")
    live = registry.live()
    assert [r.session_id for r in live] == ["s1"]
    assert live[0].transcript == Path("/tmp/a.jsonl")
    assert live[0].cwd == "/home/me/project"


def test_registering_again_refreshes_rather_than_duplicates(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("SPEAKD_STATE_DIR", str(tmp_path))
    registry.register("s1", Path("/tmp/a.jsonl"), "/p")
    registry.register("s1", Path("/tmp/a.jsonl"), "/p")
    assert len(registry.live()) == 1


def test_a_stale_session_drops_out(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("SPEAKD_STATE_DIR", str(tmp_path))
    registry.register("s1", Path("/tmp/a.jsonl"), "/p")
    assert registry.live(max_idle_seconds=-1.0) == []


def test_an_unreadable_registration_is_skipped_not_fatal(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    # One corrupt file must not stop every other session being spoken.
    monkeypatch.setenv("SPEAKD_STATE_DIR", str(tmp_path))
    registry.register("good", Path("/tmp/a.jsonl"), "/p")
    bad = registry._registration_path("bad")
    bad.write_text("{not json")
    assert [r.session_id for r in registry.live()] == ["good"]


def test_touched_is_recent(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("SPEAKD_STATE_DIR", str(tmp_path))
    registry.register("s1", Path("/tmp/a.jsonl"), "/p")
    assert time.time() - registry.live()[0].touched < 5.0
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run --no-sync pytest tests/test_cc_registry.py -v`
Expected: FAIL — no module named `registry`.

- [ ] **Step 3: Implement**

```python
"""Which Claude Code sessions are live, and where their transcripts are.

The hook writes one small file per session; the follower reads them. A file
rather than a control verb because the hook already writes to this directory
for the watermark, and a registration verb would be a second source of truth
for one fact -- with the added cost of a socket round trip on the one hook
that is still inside the user's critical path.

Nothing here raises. A hook that fails is a hook that breaks someone's editor,
and a follower that dies on one corrupt file stops speaking for every session.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path

from speakd.clients.claude_code.watermark import state_dir

# Half an hour of no prompt is a session someone has walked away from. Long
# enough that a lunch break does not cost the follower the session; short
# enough that yesterday's twenty windows are not polled today.
DEFAULT_MAX_IDLE_SECONDS = 1800.0


@dataclass(frozen=True)
class Registration:
    session_id: str
    transcript: Path
    cwd: str
    touched: float


def _registration_path(session_id: str) -> Path:
    directory = state_dir()
    directory.mkdir(parents=True, exist_ok=True)
    return directory / f"{session_id}.session.json"


def register(session_id: str, transcript: Path, cwd: str) -> None:
    """Record that this session is live. Never raises."""
    try:
        _registration_path(session_id).write_text(
            json.dumps(
                {
                    "session_id": session_id,
                    "transcript": str(transcript),
                    "cwd": cwd,
                    "touched": time.time(),
                }
            ),
            encoding="utf-8",
        )
    except OSError:
        pass


def live(max_idle_seconds: float = DEFAULT_MAX_IDLE_SECONDS) -> list[Registration]:
    """Every session registered recently enough to be worth polling."""
    directory = state_dir()
    try:
        candidates = sorted(directory.glob("*.session.json"))
    except OSError:
        return []
    now = time.time()
    found: list[Registration] = []
    for path in candidates:
        try:
            body = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            continue
        if not isinstance(body, dict):
            continue
        touched = body.get("touched")
        transcript = body.get("transcript")
        session_id = body.get("session_id")
        if not isinstance(touched, int | float) or not isinstance(transcript, str):
            continue
        if not isinstance(session_id, str) or now - touched > max_idle_seconds:
            continue
        found.append(
            Registration(
                session_id=session_id,
                transcript=Path(transcript),
                cwd=str(body.get("cwd") or ""),
                touched=float(touched),
            )
        )
    return found
```

- [ ] **Step 4: Run, lint, type-check, commit**

```bash
uv run --no-sync pytest tests/test_cc_registry.py -v
uv run --no-sync ruff check . && uv run --no-sync mypy src tests
git add src/speakd/clients/claude_code/registry.py tests/test_cc_registry.py
git commit -m "Record which sessions are live, in the directory that already knows"
```

---

### Task 2: `ai_title`, for a channel label worth reading

**Files:**
- Modify: `src/speakd/clients/claude_code/transcript.py`
- Test: `tests/test_cc_transcript.py`

**Interfaces:**
- Produces: `ai_title(records: Sequence[Record]) -> str | None`, and `Record` gains `title: str | None`.

- [ ] **Step 1: Write the failing test**

```python
def test_an_ai_title_record_yields_the_session_name() -> None:
    line = b'{"type": "ai-title", "aiTitle": "Claude code feature enablement", "sessionId": "x"}\n'
    records, _ = parse(line)
    assert ai_title(records) == "Claude code feature enablement"


def test_the_latest_title_wins() -> None:
    chunk = (
        b'{"type": "ai-title", "aiTitle": "First guess", "sessionId": "x"}\n'
        b'{"type": "ai-title", "aiTitle": "Better name", "sessionId": "x"}\n'
    )
    records, _ = parse(chunk)
    assert ai_title(records) == "Better name"


def test_no_title_record_yields_none() -> None:
    records, _ = parse(b'{"type": "assistant", "uuid": "u", "message": {"content": []}}\n')
    assert ai_title(records) is None
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run --no-sync pytest tests/test_cc_transcript.py -k title -v`
Expected: FAIL — `ai_title` is not defined.

**Note:** an `ai-title` record has **no `uuid`**, so `_record_of` currently returns `None` for it and it never reaches `speakable`. Carry the title on `Record` with a synthetic uuid rather than loosening the uuid check, which several tests pin.

- [ ] **Step 3: Implement**

Add `title: str | None = None` to `Record`. In `_record_of`, before the uuid check:

```python
    if kind == "ai-title":
        title = body.get("aiTitle")
        if isinstance(title, str) and title.strip():
            # No uuid on these records, and no need of one: nothing walks
            # back to a title, and `speakable` skips every kind but
            # "assistant" anyway.
            return Record(uuid="", kind="ai-title", is_sidechain=False, text="", title=title)
        return None
```

and at module level:

```python
def ai_title(records: Sequence[Record]) -> str | None:
    """The session's latest human-readable name, if it has one yet.

    Claude Code rewrites this as a session develops, so the last one wins.
    """
    for record in reversed(records):
        if record.kind == "ai-title" and record.title:
            return record.title
    return None
```

- [ ] **Step 4: Run, lint, type-check, commit**

```bash
uv run --no-sync pytest tests/test_cc_transcript.py -v
uv run --no-sync ruff check . && uv run --no-sync mypy src tests
git add src/speakd/clients/claude_code/transcript.py tests/test_cc_transcript.py
git commit -m "Read the name Claude Code gives a session"
```

---

### Task 3: The follower

**Files:**
- Create: `src/speakd/clients/claude_code/follow.py`, `tests/test_cc_follow.py`
- Modify: `pyproject.toml` (`[project.scripts]`)

**Interfaces:**
- Consumes: `registry.live`, `reader.new_text`, `transcript.ai_title`, `watermark.load`/`save`, `send.enqueue`, and a `send_request` callable for `SET_LABEL`.
- Produces:
  - `Follower(poll_seconds: float = 0.1, send=..., now=...)` with `.tick() -> None` and `.run(stop: threading.Event) -> None`
  - console script `speakd-claude-follow = "speakd.clients.claude_code.follow:main"`

- [ ] **Step 1: Write the failing test**

Drive `tick()` directly — never `run()` — so the tests carry no sleeps.

```python
"""Tests for the loop that speaks a transcript as it grows."""

import json
from pathlib import Path

from speakd.clients.claude_code import registry
from speakd.clients.claude_code.follow import Follower


def assistant(uuid: str, text: str) -> bytes:
    return json.dumps(
        {
            "type": "assistant",
            "uuid": uuid,
            "isSidechain": False,
            "message": {"content": [{"type": "text", "text": text}]},
        }
    ).encode() + b"\n"


class Spy:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str, dict]] = []

    def __call__(self, verb: str, source: str, payload: dict) -> None:
        self.sent.append((verb, source, payload))

    def spoken(self) -> list[str]:
        return [p["text"] for v, _s, p in self.sent if v == "enqueue"]


def follower(tmp_path, monkeypatch, spy) -> tuple[Follower, Path]:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("SPEAKD_STATE_DIR", str(tmp_path / "state"))
    transcript = tmp_path / "t.jsonl"
    transcript.write_bytes(b"")
    registry.register("s1", transcript, "/home/me/My Project")
    return Follower(send=spy), transcript


def test_new_text_is_spoken(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    spy = Spy()
    f, transcript = follower(tmp_path, monkeypatch, spy)
    f.tick()  # registers at end of file; nothing to say yet
    with transcript.open("ab") as handle:
        handle.write(assistant("u1", "Hello there."))
    f.tick()
    assert spy.spoken() == ["Hello there."]


def test_text_is_never_spoken_twice(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    spy = Spy()
    f, transcript = follower(tmp_path, monkeypatch, spy)
    f.tick()
    with transcript.open("ab") as handle:
        handle.write(assistant("u1", "Once."))
    f.tick()
    f.tick()
    assert spy.spoken() == ["Once."]


def test_a_partial_line_waits_for_the_rest(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    spy = Spy()
    f, transcript = follower(tmp_path, monkeypatch, spy)
    f.tick()
    whole = assistant("u1", "Complete.")
    with transcript.open("ab") as handle:
        handle.write(whole[:20])
    f.tick()
    assert spy.spoken() == []
    with transcript.open("ab") as handle:
        handle.write(whole[20:])
    f.tick()
    assert spy.spoken() == ["Complete."]


def test_a_new_session_starts_at_end_of_file(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    # Registering must not read an hour of history aloud.
    monkeypatch.setenv("SPEAKD_STATE_DIR", str(tmp_path / "state"))
    transcript = tmp_path / "t.jsonl"
    transcript.write_bytes(assistant("old", "Ancient history."))
    registry.register("s1", transcript, "/p")
    spy = Spy()
    f = Follower(send=spy)
    f.tick()
    assert spy.spoken() == []


def test_the_channel_is_labelled_from_the_working_directory(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    spy = Spy()
    f, _transcript = follower(tmp_path, monkeypatch, spy)
    f.tick()
    labels = [p["label"] for v, _s, p in spy.sent if v == "set_label"]
    assert labels == ["Claude Code · My Project"]


def test_an_ai_title_renames_the_channel(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    spy = Spy()
    f, transcript = follower(tmp_path, monkeypatch, spy)
    f.tick()
    with transcript.open("ab") as handle:
        handle.write(
            json.dumps({"type": "ai-title", "aiTitle": "Real name", "sessionId": "s1"}).encode()
            + b"\n"
        )
    f.tick()
    labels = [p["label"] for v, _s, p in spy.sent if v == "set_label"]
    assert labels[-1] == "Claude Code · Real name"


def test_the_label_is_not_resent_when_it_has_not_changed(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    spy = Spy()
    f, _transcript = follower(tmp_path, monkeypatch, spy)
    f.tick()
    f.tick()
    labels = [p for v, _s, p in spy.sent if v == "set_label"]
    assert len(labels) == 1


def test_a_sidechain_message_is_not_spoken(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    spy = Spy()
    f, transcript = follower(tmp_path, monkeypatch, spy)
    f.tick()
    record = json.loads(assistant("u1", "Subagent output."))
    record["isSidechain"] = True
    with transcript.open("ab") as handle:
        handle.write(json.dumps(record).encode() + b"\n")
    f.tick()
    assert spy.spoken() == []


def test_a_vanished_transcript_does_not_stop_the_loop(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    spy = Spy()
    f, transcript = follower(tmp_path, monkeypatch, spy)
    f.tick()
    transcript.unlink()
    f.tick()  # must not raise
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run --no-sync pytest tests/test_cc_follow.py -v`
Expected: FAIL — no module named `follow`.

- [ ] **Step 3: Implement**

Create `src/speakd/clients/claude_code/follow.py`:

```python
"""Speak a Claude Code transcript as it grows.

Claude Code flushes an assistant message's content blocks together, when the
message completes -- measured, not assumed -- so there is no text to be had
mid-sentence and this loop does not pretend otherwise. What it buys over the
hooks it replaces is the gap between a message landing on disk and the tool
after it finishing: `PostToolUse` could not fire until the tool was done, and
on a slow tool that was seconds of silence for text already written.

Polling, not inotify. The transcript only ever grows, so a size check on a
timer is portable and needs no dependency; inotify would tie this to Linux for
nothing a listener could hear.

Nothing raises out of `tick`. One corrupt transcript must not stop every other
session being spoken.
"""

from __future__ import annotations

import argparse
import signal
import threading
from collections.abc import Callable, Sequence
from pathlib import Path
from types import FrameType

from speakd.clients.claude_code import registry
from speakd.clients.claude_code.hook import _log
from speakd.clients.claude_code.reader import new_text
from speakd.clients.claude_code.send import send as send_request
from speakd.clients.claude_code.transcript import ai_title, parse
from speakd.clients.claude_code.watermark import Watermark, load, save
from speakd.protocol import Request, Verb

DEFAULT_POLL_SECONDS = 0.1

# How far back to look for the session's name. `ai-title` is rewritten as the
# session develops, so only the tail matters, and a bounded read keeps this
# cheap enough to do whenever new text arrives.
TITLE_TAIL_BYTES = 64 * 1024

Send = Callable[[str, str, dict[str, object]], str | None]


def _default_send(verb: str, source: str, payload: dict[str, object]) -> str | None:
    return send_request(Request(verb=Verb(verb), source_id=source, payload=payload))


def _latest_title(path: Path) -> str | None:
    """The session's name, from a bounded read of the transcript's tail."""
    try:
        with path.open("rb") as handle:
            handle.seek(0, 2)
            start = max(0, handle.tell() - TITLE_TAIL_BYTES)
            handle.seek(start)
            chunk = handle.read()
    except OSError:
        return None
    # Drop a leading partial line: a seek into the middle of one yields bytes
    # that are not JSON, and `parse` would count them as consumed.
    if start and b"\n" in chunk:
        chunk = chunk.split(b"\n", 1)[1]
    records, _ = parse(chunk)
    return ai_title(records)


def _label_for(reg: registry.Registration, title: str | None) -> str:
    """What the GUI should show for this session.

    `ai-title` does not appear until Claude Code has named the session, so the
    working directory carries the label until it does, and a short session id
    carries it if even that is missing.
    """
    if title:
        return f"Claude Code \u00b7 {title}"
    name = Path(reg.cwd).name if reg.cwd else ""
    if name:
        return f"Claude Code \u00b7 {name}"
    return f"Claude Code \u00b7 {reg.session_id[:8]}"


class Follower:
    def __init__(
        self,
        *,
        poll_seconds: float = DEFAULT_POLL_SECONDS,
        send: Send | None = None,
    ) -> None:
        self.poll_seconds = poll_seconds
        self._send: Send = send if send is not None else _default_send
        # Last label sent per session. A label re-sent ten times a second is
        # ten needless round trips saying the same thing.
        self._labels: dict[str, str] = {}

    def tick(self) -> None:
        for reg in registry.live():
            try:
                self._follow(reg)
            except Exception as exc:
                _log(f"follower: session {reg.session_id} failed: {exc!r}")

    def run(self, stop: threading.Event) -> None:
        while not stop.is_set():
            self.tick()
            stop.wait(self.poll_seconds)

    def _follow(self, reg: registry.Registration) -> None:
        channel = f"claude-code:{reg.session_id}"
        mark = load(reg.session_id)
        if mark is None or mark.path != str(reg.transcript):
            self._start_at_end(reg)
            self._relabel(reg, channel)
            return
        text, new_mark = new_text(reg.transcript, mark)
        if new_mark is None:
            return
        if text.strip():
            reason = self._send("enqueue", channel, {"text": text})
            if reason is not None:
                # Deliberately not saved. The hook this replaces advanced its
                # watermark whether or not the daemon took the text, so a
                # daemon that was briefly unreachable lost that speech for
                # good. Leaving the mark where it is costs a repeat at worst.
                _log(f"follower: {reason}")
                return
        save(reg.session_id, new_mark)
        self._relabel(reg, channel)

    def _start_at_end(self, reg: registry.Registration) -> None:
        """Begin at end of file, so registering speaks no history."""
        try:
            size = reg.transcript.stat().st_size
        except OSError:
            return
        save(reg.session_id, Watermark(path=str(reg.transcript), offset=size, uuid=""))

    def _relabel(self, reg: registry.Registration, channel: str) -> None:
        label = _label_for(reg, _latest_title(reg.transcript))
        if self._labels.get(reg.session_id) == label:
            return
        reason = self._send("set_label", channel, {"label": label})
        if reason is None:
            self._labels[reg.session_id] = label


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="speakd-claude-follow", description=__doc__)
    parser.add_argument("--poll-seconds", type=float, default=DEFAULT_POLL_SECONDS)
    args = parser.parse_args(argv)

    stop = threading.Event()

    def on_signal(_signum: int, _frame: FrameType | None) -> None:
        stop.set()

    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGTERM, on_signal)
    Follower(poll_seconds=args.poll_seconds).run(stop)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

Add to `pyproject.toml` under `[project.scripts]`:

```toml
speakd-claude-follow = "speakd.clients.claude_code.follow:main"
```

- [ ] **Step 4: Run the tests**

Run: `uv run --no-sync pytest tests/test_cc_follow.py -v`
Expected: PASS, and the run should take well under a second — a slow run means a `sleep` crept into `tick()`.

- [ ] **Step 5: Lint, type-check, commit**

```bash
uv run --no-sync ruff check . && uv run --no-sync ruff format --check .
uv run --no-sync mypy src tests
git add src/speakd/clients/claude_code/follow.py tests/test_cc_follow.py pyproject.toml
git commit -m "Speak a transcript as it grows, instead of when a hook says so"
```

---

### Task 4: Shrink the hooks to two events

**Files:**
- Modify: `src/speakd/clients/claude_code/hook.py`, `src/speakd/clients/claude_code/reader.py`, `clients/claude-code/hooks/hooks.json`, `clients/claude-code/README.md`
- Test: `tests/test_cc_hook.py`, `tests/test_cc_reader.py`, `tests/test_cc_plugin_manifest.py`

**Interfaces:**
- `hook._dispatch` handles `UserPromptSubmit` and `Notification` only.
- `reader.new_text` gains `start_at_end: bool = False`.

- [ ] **Step 1: Write the failing tests**

In `tests/test_cc_hook.py`:

```python
def test_post_tool_use_no_longer_speaks(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    # The follower sees the message land before the tool finishes, so a hook
    # here would only duplicate it -- 250ms inside the user's turn, once per
    # tool call.
    sent: list[tuple[str, str]] = []
    monkeypatch.setattr(
        hook, "enqueue", lambda source, text, **kw: sent.append((source, text)) or None
    )
    transcript = tmp_path / "t.jsonl"
    transcript.write_bytes(
        b'{"type":"assistant","uuid":"u","isSidechain":false,'
        b'"message":{"content":[{"type":"text","text":"Hello."}]}}\n'
    )
    hook._dispatch(
        {
            "session_id": "s",
            "hook_event_name": "PostToolUse",
            "transcript_path": str(transcript),
        }
    )
    assert sent == []


def test_stop_no_longer_speaks(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    sent: list[tuple[str, str]] = []
    monkeypatch.setattr(
        hook, "enqueue", lambda source, text, **kw: sent.append((source, text)) or None
    )
    hook._dispatch(
        {
            "session_id": "s",
            "hook_event_name": "Stop",
            "transcript_path": str(tmp_path / "t.jsonl"),
        }
    )
    assert sent == []


def test_a_prompt_registers_the_session(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("SPEAKD_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(hook, "hush", lambda channel, **kw: None)
    hook._dispatch(
        {
            "session_id": "s",
            "hook_event_name": "UserPromptSubmit",
            "transcript_path": str(tmp_path / "t.jsonl"),
            "cwd": "/home/me/p",
        }
    )
    found = registry.live()
    assert [r.session_id for r in found] == ["s"]
    assert found[0].cwd == "/home/me/p"


def test_a_prompt_still_hushes_both_channels(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("SPEAKD_STATE_DIR", str(tmp_path))
    hushed: list[str] = []
    monkeypatch.setattr(hook, "hush", lambda channel, **kw: hushed.append(channel) or None)
    hook._dispatch(
        {
            "session_id": "s",
            "hook_event_name": "UserPromptSubmit",
            "transcript_path": str(tmp_path / "t.jsonl"),
            "cwd": "/p",
        }
    )
    assert hushed == ["claude-code:s", "claude-code:s:notify"]


def test_registration_survives_a_missing_transcript_path(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    # A hook payload without one must still hush; it simply registers nothing.
    monkeypatch.setenv("SPEAKD_STATE_DIR", str(tmp_path))
    hushed: list[str] = []
    monkeypatch.setattr(hook, "hush", lambda channel, **kw: hushed.append(channel) or None)
    hook._dispatch({"session_id": "s", "hook_event_name": "UserPromptSubmit"})
    assert len(hushed) == 2
```

In `tests/test_cc_reader.py`:

```python
def test_a_tool_result_is_not_the_turn_boundary() -> None:
    # Tool results are records of type "user", so scanning back for the last
    # one dropped every prose block before it -- which is most of a turn that
    # used any tools at all.
    chunk = (
        b'{"type":"user","uuid":"u0","message":{"content":[{"type":"text","text":"go"}]}}\n'
        b'{"type":"assistant","uuid":"a1","isSidechain":false,'
        b'"message":{"content":[{"type":"text","text":"First."}]}}\n'
        b'{"type":"user","uuid":"u1","message":{"content":[{"type":"tool_result"}]}}\n'
        b'{"type":"assistant","uuid":"a2","isSidechain":false,'
        b'"message":{"content":[{"type":"text","text":"Second."}]}}\n'
    )
    path = Path(tempfile.mkstemp(suffix=".jsonl")[1])
    path.write_bytes(chunk)
    text, mark = new_text(path, None)
    assert "First." in text
    assert "Second." in text
```

The manifest test must assert the file now declares exactly `UserPromptSubmit` and `Notification`.

- [ ] **Step 2: Run to verify they fail**

Run: `uv run --no-sync pytest tests/test_cc_hook.py -v`
Expected: the new tests FAIL; the hush test still PASSES.

- [ ] **Step 3: Implement**

In `hook._dispatch`, delete the `Stop`/`PostToolUse` branch entirely along with `_speak_new`, and extend the prompt branch:

```python
    if event == "UserPromptSubmit":
        transcript_path = body.get("transcript_path")
        if isinstance(transcript_path, str) and transcript_path:
            # Before the hushes, and never over the socket: this is the one
            # hook still inside the user's critical path, and a local write
            # costs microseconds where a round trip costs milliseconds.
            registry.register(session_id, Path(transcript_path), str(body.get("cwd") or ""))
        for channel in (response, notify):
            reason = hush(channel, timeout=HUSH_TIMEOUT)
            if reason is not None:
                _log(reason)
        return
```

In `reader.py`, add `start_at_end` and fix `_from_start`. The honest fix is to stop using it: with the follower registering at end of file, the "no usable watermark" case no longer means "read the current turn" but "start here". Keep the function only if a test still needs it, and note in the docstring that a tool result is a `user` record — the reason it was wrong.

`hooks.json`: delete the `Stop` and `PostToolUse` entries. Keep `UserPromptSubmit` at `timeout: 3` and `Notification` at `5`.

Update both READMEs: the "Known limitation" paragraph about a long tool-free answer is now **obsolete** — the follower speaks it when it lands. Say what replaced it, and that the daemon supervises the follower.

- [ ] **Step 4: Run the tests**

Run: `uv run --no-sync pytest tests/test_cc_hook.py tests/test_cc_reader.py tests/test_cc_plugin_manifest.py -v`
Expected: PASS.

Run: `uv run --no-sync pytest`
Expected: PASS.

- [ ] **Step 5: Measure the turn cost**

```bash
PAYLOAD='{"session_id":"BENCH","hook_event_name":"UserPromptSubmit","transcript_path":"/dev/null","cwd":"/tmp"}'
for i in 1 2 3; do
  /usr/bin/time -f "%e s" bash clients/claude-code/hooks/speakd-hook.sh <<< "$PAYLOAD" 2>&1 | tail -1
done
rm -f ~/.local/state/speakd/claude-code/BENCH*
```

Expected: ~0.07 s, once per turn. Before this plan it was ~0.25 s per tool call. Record both in the commit message.

- [ ] **Step 6: Lint, type-check, commit**

```bash
uv run --no-sync ruff check . && uv run --no-sync ruff format --check .
uv run --no-sync mypy src tests
git add src/speakd clients/claude-code tests/ README.md
git commit -m "Take Stop and PostToolUse out of the turn"
```

---

### Task 5: The daemon supervises the follower

**Files:**
- Create: `src/speakd/supervise.py`, `tests/test_supervise.py`
- Modify: `src/speakd/__main__.py`

**Interfaces:**
- Produces: `Supervisor(argv: list[str], backoff: Sequence[float] = (1, 2, 5, 10, 30))` with `.start()`, `.stop()`, and a `_spawn` seam tests replace.

- [ ] **Step 1: Write the failing test**

Replace the spawn seam; never launch a real process.

```python
"""Tests for keeping the follower alive without risking the daemon."""

import threading

from speakd.supervise import Supervisor


class FakeChild:
    def __init__(self) -> None:
        self.alive = True
        self.terminated = False

    def poll(self) -> int | None:
        return None if self.alive else 1

    def terminate(self) -> None:
        self.terminated = True
        self.alive = False

    def wait(self, timeout: float | None = None) -> int:
        return 0


def supervisor(children: list[FakeChild], fail_first: int = 0) -> Supervisor:
    attempts = {"n": 0}

    def spawn() -> FakeChild:
        attempts["n"] += 1
        if attempts["n"] <= fail_first:
            raise OSError("no such file")
        child = FakeChild()
        children.append(child)
        return child

    s = Supervisor(["speakd-claude-follow"], backoff=(0.0,))
    s._spawn = spawn  # type: ignore[method-assign]
    return s


def test_it_spawns_once_on_start() -> None:
    children: list[FakeChild] = []
    s = supervisor(children)
    s.start()
    try:
        assert s.wait_for_children(1, timeout=2.0)
    finally:
        s.stop()


def test_a_child_that_dies_is_restarted() -> None:
    children: list[FakeChild] = []
    s = supervisor(children)
    s.start()
    try:
        assert s.wait_for_children(1, timeout=2.0)
        children[0].alive = False
        assert s.wait_for_children(2, timeout=2.0)
    finally:
        s.stop()


def test_stop_terminates_the_child_and_stops_restarting() -> None:
    children: list[FakeChild] = []
    s = supervisor(children)
    s.start()
    assert s.wait_for_children(1, timeout=2.0)
    s.stop()
    assert children[0].terminated
    assert not s.wait_for_children(2, timeout=0.3)


def test_a_spawn_that_fails_is_retried_rather_than_fatal() -> None:
    # A follower that cannot start must never stop the daemon speaking for
    # every other client.
    children: list[FakeChild] = []
    s = supervisor(children, fail_first=2)
    s.start()
    try:
        assert s.wait_for_children(1, timeout=3.0)
    finally:
        s.stop()


def test_stop_is_safe_before_start_and_twice() -> None:
    s = supervisor([])
    s.stop()
    s.start()
    s.stop()
    s.stop()
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run --no-sync pytest tests/test_supervise.py -v`
Expected: FAIL — no module named `speakd.supervise`.

- [ ] **Step 3: Implement**

Create `src/speakd/supervise.py`:

```python
"""Keep a child process alive, without letting it take this one down.

The follower is a child of the daemon rather than a second service because
packaging is already the least portable part of this project: a second unit
would have to be written for systemd, launchd and Windows alike, where a child
is written once.

Every failure here is survivable by design. A follower that will not start
means a session is not spoken aloud; it must never mean the daemon stops
answering the socket for everyone else.
"""

from __future__ import annotations

import subprocess
import threading
from collections.abc import Sequence

# Starts short, so restarting a wedged follower is not a visible wait, and
# caps low enough that a daemon left running overnight beside a permanently
# broken follower is not spawning in a tight loop.
DEFAULT_BACKOFF: tuple[float, ...] = (1.0, 2.0, 5.0, 10.0, 30.0)


class Supervisor:
    def __init__(
        self,
        argv: Sequence[str],
        *,
        backoff: Sequence[float] = DEFAULT_BACKOFF,
    ) -> None:
        self._argv = list(argv)
        self._backoff = tuple(backoff) or (1.0,)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._child: subprocess.Popen[bytes] | None = None
        self._started = 0
        self._changed = threading.Condition()

    def _spawn(self) -> subprocess.Popen[bytes]:
        """The seam tests replace. On POSIX the child gets its own session so
        a Ctrl-C in the daemon's terminal does not reach it directly; the
        daemon terminates it deliberately in `stop`.

        A Windows branch would pass
        `creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP` here. It is
        not written, because `transport.py` is AF_UNIX-only and a branch here
        would pretend the port is closer than it is.
        """
        return subprocess.Popen(self._argv, start_new_session=True)

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        thread = threading.Thread(target=self._run, name="speakd-supervisor", daemon=True)
        self._thread = thread
        thread.start()

    def _run(self) -> None:
        attempt = 0
        while not self._stop.is_set():
            try:
                child = self._spawn()
            except OSError:
                delay = self._backoff[min(attempt, len(self._backoff) - 1)]
                attempt += 1
                self._stop.wait(delay)
                continue
            attempt = 0
            with self._changed:
                self._child = child
                self._started += 1
                self._changed.notify_all()
            while not self._stop.is_set() and child.poll() is None:
                self._stop.wait(0.1)
            if self._stop.is_set():
                return
            self._stop.wait(self._backoff[0])

    def wait_for_children(self, count: int, timeout: float) -> bool:
        """Block until `count` children have been spawned. Tests use it."""
        with self._changed:
            return self._changed.wait_for(lambda: self._started >= count, timeout)

    def stop(self) -> None:
        self._stop.set()
        child = self._child
        if child is not None and child.poll() is None:
            try:
                child.terminate()
                child.wait(timeout=5.0)
            except Exception:
                # Best effort: the OS reclaims it when this process exits, and
                # raising here would turn a clean shutdown into a traceback.
                pass
        thread = self._thread
        if thread is not None:
            thread.join(timeout=5.0)
        self._thread = None
```

In `__main__.py`, after `server.start()`:

```python
    supervisor: Supervisor | None = None
    # Suppressed by SPEAKD_NO_FOLLOWER=1: the only callers are the test suite
    # and someone debugging the follower by hand.
    if not os.environ.get("SPEAKD_NO_FOLLOWER"):
        supervisor = Supervisor([sys.executable, "-m", "speakd.clients.claude_code.follow"])
        supervisor.start()
```

and in the shutdown sequence, before `daemon.silence()`:

```python
    if supervisor is not None:
        supervisor.stop()
```

- [ ] **Step 4: Run the tests, then try it for real**

```bash
uv run --no-sync pytest tests/test_supervise.py -v
systemctl --user restart speakd
sleep 5
pgrep -af "claude_code.follow" || echo "FOLLOWER NOT RUNNING"
pkill -f "claude_code.follow"; sleep 3
pgrep -af "claude_code.follow" || echo "NOT RESTARTED — supervision is broken"
```

Expected: running after restart, and running again within a few seconds of being killed.

- [ ] **Step 5: Lint, type-check, commit**

```bash
uv run --no-sync ruff check . && uv run --no-sync ruff format --check .
uv run --no-sync mypy src tests
git add src/speakd tests/test_supervise.py
git commit -m "Keep the follower alive, and let it die without taking audio with it"
```

---

## Done when

- `uv run --no-sync pytest`, `ruff check`, `ruff format --check`, `mypy src tests` all pass.
- A real Claude Code session speaks its prose **while the next tool is still running**, not after it.
- `hooks.json` declares two events; the hook costs ~0.07 s and runs once per turn.
- Killing the follower gets it restarted within seconds; killing it repeatedly does not take the daemon down.
- Both READMEs describe the current shape, and the obsolete "Known limitation" paragraph is gone.
