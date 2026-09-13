# Claude Code Client Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Claude Code speaks its responses through `speakd`, starting audio at the first sentence of each block rather than at the end of the turn, with no text spoken twice and no hook that can break the user's session.

**Architecture:** Four Claude Code hooks call one entry point. The entry point reads the session's transcript JSONL forward from a per-session watermark, extracts the assistant text blocks it has not spoken yet, and sends one `enqueue` to the daemon. The watermark is advanced under a file lock, which is what makes `PostToolUse` and `Stop` — which overlap — idempotent with respect to each other. The client carries no opinion about how text should sound: markdown, pronunciation and summarisation all live in daemon transforms.

**Tech Stack:** Python 3.11–3.13, standard library only (no new dependencies), pytest, ruff, mypy strict.

**Spec:** `docs/design/2026-09-13-speakd-design.md` — sections "Claude Code client", "Channels, roles, profiles", "Failure handling".

## The design decision that shapes this plan

There are two obvious ways to get text out of Claude Code, and one of them is a trap.

The trap is **parsing hook payloads**. `Stop` gives you a transcript path, not a response; `PostToolUse` gives you the tool's input and output, not the prose that preceded it. Reconstructing "what the assistant said" from hook payloads means writing a different extractor per event and hoping they agree.

This plan does the other thing: **every hook reads the same transcript with the same function, and the only difference between events is when it fires.** `PostToolUse` catches the text block that a tool call orphaned, `Stop` catches everything after the last tool call, and neither knows nor cares which is which. Correctness reduces to a single property — *a record is spoken at most once* — which one lock and one watermark deliver, and which the tests can state directly.

The consequence worth accepting up front: this cannot stream *within* one prose block. Claude Code writes the transcript block-by-block, so a single 800-word answer with no tool calls arrives as one record and is enqueued once. What the user gains over the status quo is that audio begins at the first sentence of that block (the streaming pipeline's doing, not the client's), and that on tool-using turns — most of them — each block speaks while the next tool runs.

## Global Constraints

- Python `>=3.11,<3.14`. **The client adds no dependency**: standard library only. It must import and run without the `kokoro` extra.
- **A hook must never fail the user's session.** Every entry point exits `0` on every path except an unhandled crash, which is itself caught. A non-zero exit or an exception traceback reaching Claude Code is a defect, not an error report.
- **A hook must never write to stdout.** `UserPromptSubmit` stdout is injected into the model's context; the others surface in the transcript. Diagnostics go to the log file, and nowhere else.
- **Nothing is spoken twice, and nothing is silently dropped.** These are the two failure modes that make a narrator unusable; both are testable properties of the watermark.
- Sub-agent transcripts are never spoken. Records with `isSidechain: true` are skipped.
- ruff line length 100. `uv run pytest`, `uv run ruff check .`, `uv run ruff format --check .` and `uv run mypy src tests` must all pass before every commit.
- Commit after every task.

## File Structure

```
src/speakd/clients/__init__.py              # namespace only
src/speakd/clients/claude_code/__init__.py  # namespace only
src/speakd/clients/claude_code/transcript.py  # pure: JSONL bytes -> speakable text
src/speakd/clients/claude_code/watermark.py   # per-session state, atomic, locked
src/speakd/clients/claude_code/reader.py      # transcript + watermark -> (text, next watermark)
src/speakd/clients/claude_code/send.py        # one request to the daemon, or a logged miss
src/speakd/clients/claude_code/hook.py        # stdin JSON -> dispatch -> exit 0
clients/claude-code/.claude-plugin/plugin.json
clients/claude-code/hooks/hooks.json
clients/claude-code/README.md
tests/test_cc_transcript.py
tests/test_cc_watermark.py
tests/test_cc_reader.py
tests/test_cc_send.py
tests/test_cc_hook.py
```

Each module is separable on purpose: `transcript.py` is pure and takes the bulk of the test weight, `watermark.py` owns every filesystem concern, and `hook.py` owns the failure policy and nothing else.

## Interfaces this plan consumes

From the daemon wave (`docs/plans/2026-09-13-daemon.md`, Task 8):

- `speakd.cli.default_socket_path() -> Path`
- `speakd.transport.connect(path: Path) -> SocketClient` with `.request(Request, timeout: float) -> Response` and `.close()`
- `speakd.protocol.Request(verb: Verb, source_id: str, payload: dict[str, object])`, `Response(ok: bool, data: dict, error: str)`, `Verb.ENQUEUE`, `Verb.HUSH`, `Verb.SET_PRIORITY`

**If `transport.connect` does not exist under that name**, use whatever Task 8's `speakctl enqueue` uses to reach the daemon, and record the substitution in your report. Do not invent a second transport.

## Channel naming

Two channels per Claude Code session, both derived from the hook payload's `session_id`:

| channel | carries | priority |
|---|---|---|
| `claude-code:<session_id>` | response text | 0 |
| `claude-code:<session_id>:notify` | approval requests, notifications | 10 |

The notify channel outranks the response channel so an approval request is heard over a response in progress; arbitration is the daemon's, not the client's. `UserPromptSubmit` hushes both.

---

### Task 1: Reading the transcript

**Files:**
- Create: `src/speakd/clients/__init__.py`, `src/speakd/clients/claude_code/__init__.py`, `src/speakd/clients/claude_code/transcript.py`
- Test: `tests/test_cc_transcript.py`

**Interfaces:**
- Produces: `Record` (frozen dataclass: `uuid: str`, `kind: str`, `is_sidechain: bool`, `text: str`); `parse(chunk: bytes) -> tuple[list[Record], int]` returning the records and the number of bytes consumed (trailing partial line excluded); `speakable(records: Sequence[Record]) -> str`

A Claude Code transcript is JSONL, one record per line, appended as the turn proceeds. A record looks like:

```json
{"type":"assistant","uuid":"a4451275-…","parentUuid":"cd0dc57f-…","isSidechain":false,
 "timestamp":"2026-09-13T18:54:48.384Z","sessionId":"4165d781-…",
 "message":{"role":"assistant","content":[{"type":"text","text":"Hello."},
                                          {"type":"tool_use","id":"toolu_01…","name":"Bash","input":{}}]}}
```

Three filters, each of which is a bug if omitted:

1. **`type` must be `"assistant"`.** User records and system records are not speech.
2. **`isSidechain` must be false.** Sub-agent transcripts are written into the same file with this flag set. Without this filter the user hears every dispatched agent's internal monologue — in a session that runs four implementers, that is hours of audio nobody asked for.
3. **Content blocks: `type == "text"` only.** `thinking` blocks are not for speaking and `tool_use` blocks are JSON.

The file is being appended to while we read it, so the last line may be half-written. `parse` must return only whole lines and report how many bytes those lines occupied, so the caller can resume exactly there next time. A line that is complete but unparseable (a `\n` arrived inside a string, or the file is corrupt) is skipped and counted as consumed — a client that stops forever on one bad line is worse than one that loses a sentence.

- [ ] **Step 1: Write the failing test**

```python
"""Tests for reading Claude Code transcript records."""

import json

from speakd.clients.claude_code.transcript import Record, parse, speakable


def line(**fields: object) -> bytes:
    record: dict[str, object] = {
        "type": "assistant",
        "uuid": "u1",
        "isSidechain": False,
        "message": {"role": "assistant", "content": [{"type": "text", "text": "Hi."}]},
    }
    record.update(fields)
    return (json.dumps(record) + "\n").encode("utf-8")


def test_parse_reads_whole_lines_and_reports_bytes_consumed() -> None:
    chunk = line(uuid="a") + line(uuid="b")
    records, consumed = parse(chunk)
    assert [r.uuid for r in records] == ["a", "b"]
    assert consumed == len(chunk)


def test_a_trailing_partial_line_is_not_consumed() -> None:
    whole = line(uuid="a")
    chunk = whole + b'{"type":"assistant","uuid":"b"'
    records, consumed = parse(chunk)
    assert [r.uuid for r in records] == ["a"]
    assert consumed == len(whole)


def test_an_unparseable_whole_line_is_skipped_but_consumed() -> None:
    bad = b"not json at all\n"
    chunk = bad + line(uuid="b")
    records, consumed = parse(chunk)
    assert [r.uuid for r in records] == ["b"]
    assert consumed == len(chunk)


def test_user_records_are_kept_but_carry_no_text() -> None:
    chunk = line(type="user", uuid="u", message={"role": "user", "content": "hello"})
    records, _ = parse(chunk)
    assert [(r.kind, r.text) for r in records] == [("user", "")]


def test_sidechain_records_are_marked() -> None:
    records, _ = parse(line(uuid="s", isSidechain=True))
    assert records[0].is_sidechain is True


def test_thinking_and_tool_use_blocks_are_not_text() -> None:
    content = [
        {"type": "thinking", "thinking": "hmm"},
        {"type": "text", "text": "The answer is four."},
        {"type": "tool_use", "id": "t1", "name": "Bash", "input": {}},
    ]
    records, _ = parse(line(uuid="a", message={"role": "assistant", "content": content}))
    assert records[0].text == "The answer is four."


def test_several_text_blocks_in_one_record_join_as_paragraphs() -> None:
    content = [{"type": "text", "text": "One."}, {"type": "text", "text": "Two."}]
    records, _ = parse(line(uuid="a", message={"role": "assistant", "content": content}))
    assert records[0].text == "One.\n\nTwo."


def test_speakable_keeps_only_assistant_text_and_drops_sidechains() -> None:
    records = [
        Record(uuid="a", kind="assistant", is_sidechain=False, text="Spoken."),
        Record(uuid="b", kind="assistant", is_sidechain=True, text="Subagent chatter."),
        Record(uuid="c", kind="user", is_sidechain=False, text=""),
        Record(uuid="d", kind="assistant", is_sidechain=False, text="Also spoken."),
    ]
    assert speakable(records) == "Spoken.\n\nAlso spoken."


def test_speakable_of_nothing_is_empty() -> None:
    assert speakable([]) == ""
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_cc_transcript.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'speakd.clients'`

- [ ] **Step 3: Write the implementation**

Create `src/speakd/clients/__init__.py` and `src/speakd/clients/claude_code/__init__.py` as empty files (a docstring line each is fine), then `transcript.py`:

```python
"""Reading Claude Code's transcript JSONL.

Pure: bytes in, records out. Every filesystem concern lives in `watermark`
and every network concern in `send`, so the rules about what counts as
speech can be tested without either.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class Record:
    """One transcript line, reduced to what matters for speech."""

    uuid: str
    kind: str
    is_sidechain: bool
    text: str


def _text_of(message: object) -> str:
    """Join the `text` blocks of one assistant message.

    `thinking` blocks are not for speaking and `tool_use` blocks are JSON;
    both are dropped here rather than anywhere downstream.
    """
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    if not isinstance(content, list):
        return ""
    blocks: list[str] = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            text = block.get("text")
            if isinstance(text, str) and text.strip():
                blocks.append(text)
    return "\n\n".join(blocks)


def _record_of(body: dict[str, object]) -> Record | None:
    uuid = body.get("uuid")
    kind = body.get("type")
    if not isinstance(uuid, str) or not isinstance(kind, str):
        return None
    text = _text_of(body.get("message")) if kind == "assistant" else ""
    return Record(
        uuid=uuid,
        kind=kind,
        is_sidechain=bool(body.get("isSidechain")),
        text=text,
    )


def parse(chunk: bytes) -> tuple[list[Record], int]:
    """Parse whole lines from `chunk`; return the records and bytes consumed.

    The transcript is appended to while we read it, so a trailing partial
    line is normal and must not be consumed — the caller resumes at the
    returned offset and sees that line whole next time. A line that is
    complete but unparseable is skipped *and* counted: stopping forever on
    one bad byte loses every later sentence, which is the worse failure.
    """
    records: list[Record] = []
    consumed = 0
    for raw in chunk.splitlines(keepends=True):
        if not raw.endswith(b"\n"):
            break
        consumed += len(raw)
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        if not isinstance(body, dict):
            continue
        record = _record_of(body)
        if record is not None:
            records.append(record)
    return records, consumed


def speakable(records: Sequence[Record]) -> str:
    """The text a listener should hear, from records in transcript order."""
    return "\n\n".join(
        record.text
        for record in records
        if record.kind == "assistant" and not record.is_sidechain and record.text
    )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_cc_transcript.py -v`
Expected: PASS, 9 tests.

- [ ] **Step 5: Gates and commit**

```bash
uv run pytest -q && uv run ruff check . && uv run ruff format --check . && uv run mypy src tests
git add src/speakd/clients tests/test_cc_transcript.py
git commit -m "Read speakable text out of a Claude Code transcript"
```

---

### Task 2: The watermark

**Files:**
- Create: `src/speakd/clients/claude_code/watermark.py`
- Test: `tests/test_cc_watermark.py`

**Interfaces:**
- Consumes: nothing from Task 1.
- Produces: `Watermark` (frozen dataclass: `path: str`, `offset: int`, `uuid: str`); `state_dir() -> Path`; `load(session_id: str) -> Watermark | None`; `save(session_id: str, mark: Watermark) -> None`; `locked(session_id: str) -> AbstractContextManager[None]`

The watermark is what makes "spoken at most once" true. It records where in which transcript we stopped.

**Why a lock.** Claude Code runs `PostToolUse` hooks for parallel tool calls concurrently, and `Stop` can fire while one is still in flight. Two hooks that read the same watermark both enqueue the same text. The lock is held across read-enqueue-write, not just around the write, so the second hook sees the first hook's advance. `fcntl.flock` on a separate `.lock` file: locking the state file itself races with the atomic replace that rewrites it.

**Why atomic replace.** A half-written watermark read by the next hook loses the offset and re-speaks a whole turn. Write to a temporary file in the same directory, then `os.replace`.

State lives at `$XDG_STATE_HOME/speakd/claude-code/`, falling back to `~/.local/state`. `SPEAKD_STATE_DIR` overrides it, which is what the tests use — no test may write to the developer's real state directory.

- [ ] **Step 1: Write the failing test**

```python
"""Tests for the per-session transcript watermark."""

import os
import threading
from pathlib import Path

from speakd.clients.claude_code.watermark import Watermark, load, locked, save, state_dir


def test_state_dir_honours_the_override(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("SPEAKD_STATE_DIR", str(tmp_path))
    assert state_dir() == tmp_path / "claude-code"


def test_state_dir_falls_back_to_xdg(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.delenv("SPEAKD_STATE_DIR", raising=False)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    assert state_dir() == tmp_path / "speakd" / "claude-code"


def test_an_unknown_session_has_no_watermark(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("SPEAKD_STATE_DIR", str(tmp_path))
    assert load("never-seen") is None


def test_a_saved_watermark_round_trips(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("SPEAKD_STATE_DIR", str(tmp_path))
    save("s1", Watermark(path="/tmp/t.jsonl", offset=42, uuid="u9"))
    assert load("s1") == Watermark(path="/tmp/t.jsonl", offset=42, uuid="u9")


def test_a_corrupt_watermark_reads_as_absent(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("SPEAKD_STATE_DIR", str(tmp_path))
    save("s1", Watermark(path="/tmp/t.jsonl", offset=42, uuid="u9"))
    written = next(p for p in (tmp_path / "claude-code").iterdir() if p.suffix == ".json")
    written.write_text("{ not json", encoding="utf-8")
    assert load("s1") is None


def test_a_session_id_with_a_slash_cannot_escape_the_state_dir(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("SPEAKD_STATE_DIR", str(tmp_path))
    save("../../escaped", Watermark(path="/tmp/t.jsonl", offset=1, uuid="u"))
    assert load("../../escaped") == Watermark(path="/tmp/t.jsonl", offset=1, uuid="u")
    for written in (tmp_path / "claude-code").iterdir():
        assert written.parent == tmp_path / "claude-code"


def test_the_lock_serialises_two_holders(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("SPEAKD_STATE_DIR", str(tmp_path))
    order: list[str] = []
    inside = threading.Event()
    release = threading.Event()

    def first() -> None:
        with locked("s1"):
            order.append("first-in")
            inside.set()
            release.wait(timeout=5.0)
            order.append("first-out")

    def second() -> None:
        inside.wait(timeout=5.0)
        with locked("s1"):
            order.append("second-in")

    threads = [threading.Thread(target=first), threading.Thread(target=second)]
    for thread in threads:
        thread.start()
    inside.wait(timeout=5.0)
    # The second holder must still be waiting: nothing of its own has run.
    assert order == ["first-in"]
    release.set()
    for thread in threads:
        thread.join(timeout=5.0)
    assert order == ["first-in", "first-out", "second-in"]


def test_saving_replaces_rather_than_truncating(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("SPEAKD_STATE_DIR", str(tmp_path))
    save("s1", Watermark(path="/tmp/t.jsonl", offset=1, uuid="a"))
    save("s1", Watermark(path="/tmp/t.jsonl", offset=2, uuid="b"))
    files = sorted(p.name for p in (tmp_path / "claude-code").iterdir())
    assert [name for name in files if name.endswith(".tmp")] == []
    assert load("s1") == Watermark(path="/tmp/t.jsonl", offset=2, uuid="b")
    assert os.access(tmp_path / "claude-code", os.W_OK)
```

Note on `test_the_lock_serialises_two_holders`: `flock` is per file descriptor, not per process, so two threads in one process holding separate descriptors do block each other. If the implementation takes the lock once per process and caches it, this test fails — which is the point.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_cc_watermark.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'speakd.clients.claude_code.watermark'`

- [ ] **Step 3: Write the implementation**

```python
"""Where we stopped reading each session's transcript.

Correctness for the whole client reduces to one property — a transcript
record is spoken at most once — and this module is where that property
lives. Two hooks can run at the same moment (Claude Code fires PostToolUse
concurrently for parallel tool calls, and Stop can overlap one), so the
lock is held across read-enqueue-write by the caller, not merely around the
write here.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path

_SAFE = re.compile(r"[^A-Za-z0-9_.-]")


@dataclass(frozen=True)
class Watermark:
    """The transcript we were reading, and how far into it we got."""

    path: str
    offset: int
    uuid: str


def state_dir() -> Path:
    """The directory holding one state file per session.

    `SPEAKD_STATE_DIR` exists so tests never touch the real one.
    """
    override = os.environ.get("SPEAKD_STATE_DIR")
    if override:
        return Path(override) / "claude-code"
    xdg = os.environ.get("XDG_STATE_HOME")
    base = Path(xdg) if xdg else Path.home() / ".local" / "state"
    return base / "speakd" / "claude-code"


def _slug(session_id: str) -> str:
    """A filename that cannot leave the state directory.

    Session ids come from a hook payload. Sanitising is not paranoia about
    Claude Code; it is that a path-shaped id would otherwise write state
    wherever it pointed.
    """
    safe = _SAFE.sub("_", session_id)[:80]
    digest = hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:12]
    return f"{safe}-{digest}"


def _path_for(session_id: str, suffix: str) -> Path:
    directory = state_dir()
    directory.mkdir(parents=True, exist_ok=True)
    return directory / f"{_slug(session_id)}{suffix}"


def load(session_id: str) -> Watermark | None:
    """The stored watermark, or `None` if there is none we can trust."""
    try:
        body = json.loads(_path_for(session_id, ".json").read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(body, dict):
        return None
    path, offset, uuid = body.get("path"), body.get("offset"), body.get("uuid")
    if not isinstance(path, str) or not isinstance(offset, int) or not isinstance(uuid, str):
        return None
    return Watermark(path=path, offset=offset, uuid=uuid)


def save(session_id: str, mark: Watermark) -> None:
    """Store `mark`, atomically.

    A half-written watermark read by the next hook loses the offset and
    re-speaks the whole turn, so this writes beside the target and replaces.
    """
    target = _path_for(session_id, ".json")
    temporary = target.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(asdict(mark)), encoding="utf-8")
    os.replace(temporary, target)


@contextmanager
def locked(session_id: str) -> Iterator[None]:
    """Hold this session's lock for the body of the `with`.

    The lock file is separate from the state file: `flock` follows the open
    descriptor, and `save`'s atomic replace swaps the inode underneath a
    lock taken on the state file itself.
    """
    lock_path = _path_for(session_id, ".lock")
    handle = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield
    finally:
        os.close(handle)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_cc_watermark.py -v`
Expected: PASS, 8 tests.

Then run the lock test 20 times — a threading test that passes once has proved nothing:

```bash
for i in $(seq 20); do uv run pytest tests/test_cc_watermark.py -k lock -q || echo "FLAKE on run $i"; done
```

- [ ] **Step 5: Gates and commit**

```bash
uv run pytest -q && uv run ruff check . && uv run ruff format --check . && uv run mypy src tests
git add src/speakd/clients/claude_code/watermark.py tests/test_cc_watermark.py
git commit -m "Remember where each session's transcript was last read"
```

---

### Task 3: New text since last time

**Files:**
- Create: `src/speakd/clients/claude_code/reader.py`
- Test: `tests/test_cc_reader.py`

**Interfaces:**
- Consumes: `transcript.parse`, `transcript.speakable`, `watermark.Watermark`
- Produces: `new_text(transcript: Path, mark: Watermark | None) -> tuple[str, Watermark | None]`

This is where the fast path and the safe default live.

**Fast path.** When `mark` names this transcript and `mark.offset` is no greater than the file's size, seek there and read forward. `PostToolUse` fires on every tool call and a long session's transcript reaches tens of megabytes; re-reading it per tool call is waste that shows up as hook latency.

**Fallback.** When there is no mark, or it names a different transcript, or the offset is past the end (the file was rotated or replaced), read the whole file — and then **speak only what follows the last `user` record.** This is the rule that stops a first `Stop` hook on a resumed session from reading the entire history aloud. Getting this wrong is not subtle: the user hears hours of their own past conversation.

**The returned watermark is `None` when nothing was read**, which tells the caller not to write state at all. A hook that fires with nothing new should leave the filesystem untouched.

- [ ] **Step 1: Write the failing test**

```python
"""Tests for extracting the text a session has not spoken yet."""

import json
from pathlib import Path

from speakd.clients.claude_code.reader import new_text
from speakd.clients.claude_code.watermark import Watermark


def assistant(uuid: str, text: str, *, sidechain: bool = False) -> str:
    return json.dumps(
        {
            "type": "assistant",
            "uuid": uuid,
            "isSidechain": sidechain,
            "message": {"role": "assistant", "content": [{"type": "text", "text": text}]},
        }
    )


def user(uuid: str) -> str:
    return json.dumps(
        {
            "type": "user",
            "uuid": uuid,
            "isSidechain": False,
            "message": {"role": "user", "content": "go on"},
        }
    )


def write(path: Path, *lines: str) -> None:
    path.write_text("".join(line + "\n" for line in lines), encoding="utf-8")


def test_without_a_watermark_only_the_current_turn_is_spoken(tmp_path: Path) -> None:
    transcript = tmp_path / "t.jsonl"
    write(
        transcript,
        assistant("a1", "Ancient history."),
        user("u1"),
        assistant("a2", "The current answer."),
    )
    text, mark = new_text(transcript, None)
    assert text == "The current answer."
    assert mark is not None and mark.uuid == "a2"


def test_a_watermark_resumes_where_it_stopped(tmp_path: Path) -> None:
    transcript = tmp_path / "t.jsonl"
    write(transcript, assistant("a1", "First."))
    _, first = new_text(transcript, None)
    assert first is not None

    with transcript.open("a", encoding="utf-8") as handle:
        handle.write(assistant("a2", "Second.") + "\n")
    text, second = new_text(transcript, first)
    assert text == "Second."
    assert second is not None and second.uuid == "a2"


def test_nothing_new_reads_as_empty_and_no_new_watermark(tmp_path: Path) -> None:
    transcript = tmp_path / "t.jsonl"
    write(transcript, assistant("a1", "Only this."))
    _, mark = new_text(transcript, None)
    text, again = new_text(transcript, mark)
    assert text == ""
    assert again is None


def test_a_half_written_line_waits_for_its_newline(tmp_path: Path) -> None:
    transcript = tmp_path / "t.jsonl"
    write(transcript, assistant("a1", "Complete."))
    with transcript.open("a", encoding="utf-8") as handle:
        handle.write('{"type":"assistant","uuid":"a2"')
    text, mark = new_text(transcript, None)
    assert text == "Complete."
    assert mark is not None and mark.offset == len(assistant("a1", "Complete.")) + 1


def test_a_watermark_for_another_transcript_is_ignored(tmp_path: Path) -> None:
    transcript = tmp_path / "t.jsonl"
    write(transcript, user("u1"), assistant("a1", "Fresh."))
    stale = Watermark(path=str(tmp_path / "other.jsonl"), offset=999, uuid="x")
    text, mark = new_text(transcript, stale)
    assert text == "Fresh."
    assert mark is not None and mark.path == str(transcript)


def test_an_offset_past_the_end_rescans(tmp_path: Path) -> None:
    transcript = tmp_path / "t.jsonl"
    write(transcript, user("u1"), assistant("a1", "Short file now."))
    stale = Watermark(path=str(transcript), offset=10_000, uuid="x")
    text, _ = new_text(transcript, stale)
    assert text == "Short file now."


def test_sidechain_records_advance_the_watermark_without_speaking(tmp_path: Path) -> None:
    transcript = tmp_path / "t.jsonl"
    write(transcript, user("u1"), assistant("s1", "Subagent noise.", sidechain=True))
    text, mark = new_text(transcript, None)
    assert text == ""
    assert mark is not None and mark.uuid == "s1"


def test_a_missing_transcript_is_not_an_error(tmp_path: Path) -> None:
    text, mark = new_text(tmp_path / "absent.jsonl", None)
    assert text == ""
    assert mark is None
```

Note `test_sidechain_records_advance_the_watermark_without_speaking`: skipped records still move the offset. If they did not, every later hook would re-scan them, and a mark whose uuid never advances past a sidechain block would eventually re-speak.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_cc_reader.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'speakd.clients.claude_code.reader'`

- [ ] **Step 3: Write the implementation**

```python
"""Transcript plus watermark in, speech plus next watermark out."""

from __future__ import annotations

from pathlib import Path

from speakd.clients.claude_code.transcript import Record, parse, speakable
from speakd.clients.claude_code.watermark import Watermark


def _from_start(records: list[Record]) -> list[Record]:
    """Records belonging to the current turn only.

    With no usable watermark the alternative is reading the whole session
    aloud, which is what a resumed session would otherwise do on its first
    Stop hook.
    """
    for index in range(len(records) - 1, -1, -1):
        if records[index].kind == "user":
            return records[index + 1 :]
    return records


def new_text(transcript: Path, mark: Watermark | None) -> tuple[str, Watermark | None]:
    """What this session has not spoken yet, and where to resume.

    Returns `("", None)` when there is nothing new: the caller then writes
    no state at all.
    """
    try:
        size = transcript.stat().st_size
    except OSError:
        return "", None

    resumable = mark is not None and mark.path == str(transcript) and 0 <= mark.offset <= size
    start = mark.offset if resumable and mark is not None else 0

    try:
        with transcript.open("rb") as handle:
            handle.seek(start)
            chunk = handle.read()
    except OSError:
        return "", None

    records, consumed = parse(chunk)
    if not resumable:
        records = _from_start(records)
    if not records:
        return "", None

    return speakable(records), Watermark(
        path=str(transcript),
        offset=start + consumed,
        uuid=records[-1].uuid,
    )
```

Note the shape of the fallback: `consumed` counts from `start`, which is `0` on a rescan, so the offset is right either way, and `_from_start` trims what is *spoken* without disturbing what was *read*.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_cc_reader.py -v`
Expected: PASS, 8 tests.

- [ ] **Step 5: Gates and commit**

```bash
uv run pytest -q && uv run ruff check . && uv run ruff format --check . && uv run mypy src tests
git add src/speakd/clients/claude_code/reader.py tests/test_cc_reader.py
git commit -m "Read only what a session has not spoken yet"
```

---

### Task 4: Talking to the daemon

**Files:**
- Create: `src/speakd/clients/claude_code/send.py`
- Test: `tests/test_cc_send.py`

**Interfaces:**
- Consumes: `speakd.cli.default_socket_path`, `speakd.transport.connect`, `speakd.protocol.Request`, `Response`, `Verb`
- Produces: `send(request: Request, *, socket: Path | None = None, timeout: float = 2.0) -> str | None` returning `None` on success and a one-line reason on failure; `enqueue(source: str, text: str, **kw) -> str | None`; `hush(source: str, **kw) -> str | None`

Everything here exists to satisfy one constraint: **a hook must never fail the user's session.** So `send` raises nothing. A daemon that is not running, a socket that is not there, a timeout, a malformed response — each becomes a string the caller logs.

The timeout matters. Claude Code gives hooks a bounded window; a client that blocks on a wedged daemon stalls the user's turn. Two seconds is generous for a local socket round trip and short enough not to be noticed.

- [ ] **Step 1: Write the failing test**

```python
"""Tests for the client's one conversation with the daemon."""

from pathlib import Path

import pytest

from speakd.channels import ChannelTable
from speakd.clients.claude_code.send import enqueue, hush, send
from speakd.daemon import Daemon, ProfileView
from speakd.events import EventBus
from speakd.model import Piece
from speakd.player import RecordingPlayer
from speakd.protocol import Request, Verb
from speakd.synth.fake import FakeEngine
from speakd.transport import SocketServer


def passthrough(pieces: list[Piece]) -> tuple[list[Piece], list[str]]:
    return list(pieces), []


def profile_for(name: str) -> ProfileView:
    return ProfileView(voice="af_heart", speed=1.1, interrupt_on=("error",), prepare=passthrough)


@pytest.fixture
def running(tmp_path: Path):  # type: ignore[no-untyped-def]
    player = RecordingPlayer()
    daemon = Daemon(FakeEngine(), player, profile_for, bus=EventBus(), channels=ChannelTable())
    daemon.start()
    server = SocketServer(tmp_path / "speakd.sock", daemon.handle, daemon.bus)
    server.start()
    try:
        yield Path(str(server.address)), daemon, player
    finally:
        server.stop()
        daemon.stop()


def test_enqueue_reaches_the_daemon(running) -> None:  # type: ignore[no-untyped-def]
    socket, daemon, player = running
    assert enqueue("cc:s1", "One. Two.", socket=socket) is None
    assert daemon.wait_idle(timeout=5.0)
    assert len(player.played) == 2


def test_hush_reaches_the_daemon(running) -> None:  # type: ignore[no-untyped-def]
    socket, _daemon, _player = running
    assert hush("cc:s1", socket=socket) is None


def test_no_daemon_returns_a_reason_rather_than_raising(tmp_path: Path) -> None:
    absent = tmp_path / "nothing.sock"
    reason = send(Request(verb=Verb.HUSH, source_id="cc:s1"), socket=absent)
    assert reason is not None
    assert str(absent) in reason
    assert "\n" not in reason


def test_a_refused_verb_returns_the_daemon_error(running) -> None:  # type: ignore[no-untyped-def]
    socket, _daemon, _player = running
    reason = send(
        Request(verb=Verb.ENQUEUE, source_id="cc:s1", payload={"text": ""}),
        socket=socket,
    )
    assert reason is None or "\n" not in reason


def test_enqueueing_nothing_never_opens_a_connection(tmp_path: Path) -> None:
    # No socket exists, so a connection attempt would fail loudly.
    assert enqueue("cc:s1", "   ", socket=tmp_path / "nothing.sock") is None
    assert enqueue("cc:s1", "", socket=tmp_path / "nothing.sock") is None
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_cc_send.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'speakd.clients.claude_code.send'`

- [ ] **Step 3: Write the implementation**

```python
"""One request to the daemon, and never an exception.

A hook that raises fails the user's turn, so every failure here leaves as a
string the caller can log: no daemon, a timeout, a socket that went away
mid-request. The daemon not running is the normal case, not an error — the
user may simply not want speech today.
"""

from __future__ import annotations

from pathlib import Path

from speakd.cli import default_socket_path
from speakd.protocol import Request, Verb

TIMEOUT = 2.0


def send(request: Request, *, socket: Path | None = None, timeout: float = TIMEOUT) -> str | None:
    """Send one request. Return `None` on success, or a one-line reason."""
    address = socket if socket is not None else default_socket_path()
    try:
        from speakd.transport import connect

        client = connect(address)
    except OSError as exc:
        return f"no daemon at {address} ({exc.strerror or exc})"
    except Exception as exc:  # pragma: no cover - defence, not a path
        return f"could not reach {address}: {exc}"

    try:
        response = client.request(request, timeout=timeout)
    except Exception as exc:
        return f"{request.verb.value} failed: {exc}"
    finally:
        try:
            client.close()
        except Exception:  # pragma: no cover - closing is best effort
            pass

    if not response.ok:
        return f"{request.verb.value} refused: {response.error or 'no reason given'}"
    return None


def enqueue(
    source: str,
    text: str,
    *,
    socket: Path | None = None,
    timeout: float = TIMEOUT,
    priority: int | None = None,
) -> str | None:
    """Speak `text` on `source`. Blank text is a no-op, not a request."""
    if not text.strip():
        return None
    payload: dict[str, object] = {"text": text}
    if priority is not None:
        payload["priority"] = priority
    return send(
        Request(verb=Verb.ENQUEUE, source_id=source, payload=payload),
        socket=socket,
        timeout=timeout,
    )


def hush(source: str, *, socket: Path | None = None, timeout: float = TIMEOUT) -> str | None:
    """Stop `source` talking and drop what it had queued."""
    return send(Request(verb=Verb.HUSH, source_id=source), socket=socket, timeout=timeout)
```

**If Task 8 of the daemon plan named these differently** — `connect`, `request`, the enqueue payload key — adapt to what exists and say so in your report. The shape above is the contract; the spelling is the daemon's.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_cc_send.py -v`
Expected: PASS, 5 tests.

- [ ] **Step 5: Gates and commit**

```bash
uv run pytest -q && uv run ruff check . && uv run ruff format --check . && uv run mypy src tests
git add src/speakd/clients/claude_code/send.py tests/test_cc_send.py
git commit -m "Send one request to the daemon without ever raising"
```

---

### Task 5: The hook entry point

**Files:**
- Create: `src/speakd/clients/claude_code/hook.py`
- Modify: `pyproject.toml` (add the console script)
- Test: `tests/test_cc_hook.py`

**Interfaces:**
- Consumes: `reader.new_text`, `watermark.load`, `save`, `locked`, `send.enqueue`, `send.hush`
- Produces: `main(argv: Sequence[str] | None = None) -> int` always returning `0`; console script `speakd-claude-hook`

One entry point, four events. Claude Code delivers the hook payload as JSON on stdin:

```json
{"session_id":"4165d781-…","transcript_path":"/home/…/4165d781-….jsonl",
 "hook_event_name":"Stop","cwd":"/home/…"}
```

| event | what it does |
|---|---|
| `Stop` | speak everything new |
| `PostToolUse` | speak everything new — the text block the tool call orphaned |
| `Notification` | speak the payload's `message` on the notify channel |
| `UserPromptSubmit` | hush both channels; do not read the transcript |

`Stop` and `PostToolUse` are literally the same branch. That is the design, not an oversight.

**The failure policy, in full.** `main` returns `0` on every path. The top level catches `BaseException` short of `KeyboardInterrupt`/`SystemExit`, logs it, and still returns `0`. Nothing is ever written to stdout. Every diagnostic is appended to `state_dir() / "hook.log"`, and a log that cannot be written is itself swallowed — at that point there is nowhere left to report to, and failing the user's turn to announce it would be the worse trade.

- [ ] **Step 1: Write the failing test**

```python
"""Tests for the hook entry point's dispatch and failure policy."""

import json
from pathlib import Path

from speakd.clients.claude_code import hook
from speakd.clients.claude_code.watermark import load, state_dir


def payload(**fields: object) -> str:
    body: dict[str, object] = {
        "session_id": "s1",
        "transcript_path": "/nonexistent/t.jsonl",
        "hook_event_name": "Stop",
        "cwd": "/tmp",
    }
    body.update(fields)
    return json.dumps(body)


def transcript(tmp_path: Path, *texts: str) -> Path:
    path = tmp_path / "t.jsonl"
    lines = [
        json.dumps(
            {
                "type": "assistant",
                "uuid": f"a{index}",
                "isSidechain": False,
                "message": {"role": "assistant", "content": [{"type": "text", "text": text}]},
            }
        )
        for index, text in enumerate(texts)
    ]
    path.write_text("".join(line + "\n" for line in lines), encoding="utf-8")
    return path


def run(monkeypatch, stdin: str, sent: list[tuple[str, str]]) -> int:  # type: ignore[no-untyped-def]
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO(stdin))
    monkeypatch.setattr(
        hook, "enqueue", lambda source, text, **kw: sent.append((source, text)) or None
    )
    monkeypatch.setattr(hook, "hush", lambda source, **kw: sent.append((source, "<hush>")) or None)
    return hook.main([])


def test_stop_speaks_the_new_text(tmp_path: Path, monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("SPEAKD_STATE_DIR", str(tmp_path / "state"))
    path = transcript(tmp_path, "Hello there.")
    sent: list[tuple[str, str]] = []
    assert run(monkeypatch, payload(transcript_path=str(path)), sent) == 0
    assert sent == [("claude-code:s1", "Hello there.")]
    assert capsys.readouterr().out == ""


def test_the_same_text_is_not_spoken_twice(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("SPEAKD_STATE_DIR", str(tmp_path / "state"))
    path = transcript(tmp_path, "Once only.")
    sent: list[tuple[str, str]] = []
    run(monkeypatch, payload(transcript_path=str(path)), sent)
    run(monkeypatch, payload(transcript_path=str(path), hook_event_name="PostToolUse"), sent)
    assert sent == [("claude-code:s1", "Once only.")]


def test_post_tool_use_takes_the_same_path_as_stop(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("SPEAKD_STATE_DIR", str(tmp_path / "state"))
    path = transcript(tmp_path, "Between tools.")
    sent: list[tuple[str, str]] = []
    run(monkeypatch, payload(transcript_path=str(path), hook_event_name="PostToolUse"), sent)
    assert sent == [("claude-code:s1", "Between tools.")]


def test_a_notification_speaks_on_the_notify_channel(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("SPEAKD_STATE_DIR", str(tmp_path / "state"))
    sent: list[tuple[str, str]] = []
    body = payload(hook_event_name="Notification", message="Claude needs your permission.")
    assert run(monkeypatch, body, sent) == 0
    assert sent == [("claude-code:s1:notify", "Claude needs your permission.")]


def test_a_prompt_hushes_both_channels(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("SPEAKD_STATE_DIR", str(tmp_path / "state"))
    sent: list[tuple[str, str]] = []
    assert run(monkeypatch, payload(hook_event_name="UserPromptSubmit"), sent) == 0
    assert sent == [("claude-code:s1", "<hush>"), ("claude-code:s1:notify", "<hush>")]


def test_nothing_new_writes_no_state(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("SPEAKD_STATE_DIR", str(tmp_path / "state"))
    path = transcript(tmp_path)
    sent: list[tuple[str, str]] = []
    assert run(monkeypatch, payload(transcript_path=str(path)), sent) == 0
    assert sent == []
    assert load("s1") is None


def test_garbage_on_stdin_exits_zero(tmp_path: Path, monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("SPEAKD_STATE_DIR", str(tmp_path / "state"))
    sent: list[tuple[str, str]] = []
    assert run(monkeypatch, "not json", sent) == 0
    assert sent == []
    assert capsys.readouterr().out == ""
    assert "not json" in (state_dir() / "hook.log").read_text(encoding="utf-8")


def test_an_unknown_event_exits_zero_and_does_nothing(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("SPEAKD_STATE_DIR", str(tmp_path / "state"))
    sent: list[tuple[str, str]] = []
    assert run(monkeypatch, payload(hook_event_name="PreCompact"), sent) == 0
    assert sent == []


def test_an_exploding_send_still_exits_zero(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("SPEAKD_STATE_DIR", str(tmp_path / "state"))
    path = transcript(tmp_path, "Boom.")

    def explode(source: str, text: str, **kw: object) -> str | None:
        raise RuntimeError("the daemon caught fire")

    monkeypatch.setattr("sys.stdin", __import__("io").StringIO(payload(transcript_path=str(path))))
    monkeypatch.setattr(hook, "enqueue", explode)
    assert hook.main([]) == 0
    assert "caught fire" in (state_dir() / "hook.log").read_text(encoding="utf-8")
```

`test_an_exploding_send_still_exits_zero` is the one that matters most: it is the difference between a narrator that annoys you and one that breaks your editor.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_cc_hook.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'speakd.clients.claude_code.hook'`

- [ ] **Step 3: Write the implementation**

```python
"""The one command every Claude Code hook runs.

Four events, one code path for two of them. Nothing here decides how text
should sound — markdown, pronunciation and summarising are daemon
transforms — so this module is only: read stdin, work out what is new, send
it, and under no circumstances fail the user's turn.
"""

from __future__ import annotations

import json
import sys
import time
from collections.abc import Sequence
from pathlib import Path

from speakd.clients.claude_code.reader import new_text
from speakd.clients.claude_code.send import enqueue, hush
from speakd.clients.claude_code.watermark import load, locked, save, state_dir

NOTIFY_PRIORITY = 10


def _log(message: str) -> None:
    """Append one line to the hook log, or give up quietly.

    Giving up quietly is deliberate: if the log is unwritable there is
    nowhere left to report to, and failing the turn to announce it is the
    worse trade.
    """
    try:
        directory = state_dir()
        directory.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S")
        with (directory / "hook.log").open("a", encoding="utf-8") as handle:
            handle.write(f"{stamp} {message}\n")
    except Exception:
        pass


def _channels(session_id: str) -> tuple[str, str]:
    return f"claude-code:{session_id}", f"claude-code:{session_id}:notify"


def _speak_new(session_id: str, transcript_path: str) -> None:
    """Send whatever this session has not spoken, exactly once.

    The lock spans read, send and save. Claude Code fires PostToolUse
    concurrently for parallel tool calls and Stop can overlap one; two
    holders that each read the watermark before either wrote it would both
    send the same text.
    """
    response, _ = _channels(session_id)
    with locked(session_id):
        text, mark = new_text(Path(transcript_path), load(session_id))
        if mark is None:
            return
        reason = enqueue(response, text)
        if reason is not None:
            _log(reason)
        save(session_id, mark)


def _dispatch(body: dict[str, object]) -> None:
    session_id = str(body.get("session_id") or "unknown")
    event = str(body.get("hook_event_name") or "")
    response, notify = _channels(session_id)

    if event in ("Stop", "PostToolUse"):
        transcript_path = body.get("transcript_path")
        if isinstance(transcript_path, str) and transcript_path:
            _speak_new(session_id, transcript_path)
        return

    if event == "Notification":
        message = body.get("message")
        if isinstance(message, str):
            reason = enqueue(notify, message, priority=NOTIFY_PRIORITY)
            if reason is not None:
                _log(reason)
        return

    if event == "UserPromptSubmit":
        for channel in (response, notify):
            reason = hush(channel)
            if reason is not None:
                _log(reason)
        return


def main(argv: Sequence[str] | None = None) -> int:
    """Always 0. A hook that fails is a hook that breaks someone's editor."""
    try:
        raw = sys.stdin.read()
    except Exception as exc:
        _log(f"could not read stdin: {exc}")
        return 0

    try:
        body = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        _log(f"unreadable hook payload ({exc}): {raw[:200]!r}")
        return 0

    if not isinstance(body, dict):
        _log(f"hook payload was not an object: {raw[:200]!r}")
        return 0

    try:
        _dispatch(body)
    except Exception as exc:
        _log(f"{body.get('hook_event_name')} hook failed: {exc!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

Note the save-after-send ordering in `_speak_new`: the watermark advances even when the enqueue failed. The alternative — advancing only on success — means a daemon that is not running leaves the watermark parked, and the moment it starts, the whole backlog plays at once. Losing speech the user could not hear anyway is the right trade, and the reason is in the log.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_cc_hook.py -v`
Expected: PASS, 9 tests.

- [ ] **Step 5: Add the console script**

In `pyproject.toml`, under `[project.scripts]`:

```toml
speakd-claude-hook = "speakd.clients.claude_code.hook:main"
```

Then `uv sync --group dev` and confirm the command exists:

```bash
echo '{"session_id":"x","hook_event_name":"PreCompact"}' | uv run speakd-claude-hook; echo "exit=$?"
```

Expected: `exit=0`, no output.

- [ ] **Step 6: Gates and commit**

```bash
uv run pytest -q && uv run ruff check . && uv run ruff format --check . && uv run mypy src tests
git add src/speakd/clients/claude_code/hook.py tests/test_cc_hook.py pyproject.toml uv.lock
git commit -m "One entry point for every Claude Code hook"
```

---

### Task 6: Installing it

**Files:**
- Create: `clients/claude-code/.claude-plugin/plugin.json`, `clients/claude-code/hooks/hooks.json`, `clients/claude-code/README.md`
- Modify: `README.md` (a short section pointing at it)
- Test: `tests/test_cc_plugin_manifest.py`

**Interfaces:**
- Produces: a directory Claude Code can load as a plugin.

The four hooks, wired. `${CLAUDE_PLUGIN_ROOT}` is substituted by Claude Code when the plugin loads, which is what lets the same manifest work from any checkout.

The command is a small shell wrapper rather than the console script directly, because the console script lives inside the project's venv and the plugin has to find it. The wrapper resolves it once.

- [ ] **Step 1: Write the failing test**

```python
"""The plugin manifest has to stay loadable and in step with the hooks."""

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent / "clients" / "claude-code"


def test_the_plugin_manifest_parses() -> None:
    manifest = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    assert manifest["name"] == "speakd"


def test_every_hook_we_handle_is_registered() -> None:
    hooks = json.loads((ROOT / "hooks" / "hooks.json").read_text(encoding="utf-8"))["hooks"]
    assert set(hooks) == {"Stop", "PostToolUse", "Notification", "UserPromptSubmit"}


def test_every_registered_hook_runs_the_one_entry_point() -> None:
    hooks = json.loads((ROOT / "hooks" / "hooks.json").read_text(encoding="utf-8"))["hooks"]
    commands = [
        entry["command"]
        for matchers in hooks.values()
        for matcher in matchers
        for entry in matcher["hooks"]
    ]
    assert commands, "no hook commands registered"
    for command in commands:
        assert "${CLAUDE_PLUGIN_ROOT}" in command
        assert "speakd-hook.sh" in command


def test_hooks_have_a_timeout_short_enough_not_to_stall_a_turn() -> None:
    hooks = json.loads((ROOT / "hooks" / "hooks.json").read_text(encoding="utf-8"))["hooks"]
    for matchers in hooks.values():
        for matcher in matchers:
            for entry in matcher["hooks"]:
                assert entry["timeout"] <= 5
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_cc_plugin_manifest.py -v`
Expected: FAIL — `FileNotFoundError`

- [ ] **Step 3: Write the manifest and the wrapper**

`clients/claude-code/.claude-plugin/plugin.json`:

```json
{
  "name": "speakd",
  "description": "Speak Claude Code's responses through the speakd daemon",
  "version": "0.0.1",
  "author": { "name": "Severin Gartland" }
}
```

`clients/claude-code/hooks/hooks.json`:

```json
{
  "hooks": {
    "Stop": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "bash ${CLAUDE_PLUGIN_ROOT}/hooks/speakd-hook.sh",
            "timeout": 5
          }
        ]
      }
    ],
    "PostToolUse": [
      {
        "matcher": "*",
        "hooks": [
          {
            "type": "command",
            "command": "bash ${CLAUDE_PLUGIN_ROOT}/hooks/speakd-hook.sh",
            "timeout": 5
          }
        ]
      }
    ],
    "Notification": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "bash ${CLAUDE_PLUGIN_ROOT}/hooks/speakd-hook.sh",
            "timeout": 5
          }
        ]
      }
    ],
    "UserPromptSubmit": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "bash ${CLAUDE_PLUGIN_ROOT}/hooks/speakd-hook.sh",
            "timeout": 3
          }
        ]
      }
    ]
  }
}
```

`clients/claude-code/hooks/speakd-hook.sh` (mark it executable with `chmod +x`):

```bash
#!/usr/bin/env bash
# Find speakd-claude-hook and run it. Never fail: a non-zero exit here
# reaches Claude Code as a hook failure, and the user's turn is worth more
# than any diagnostic we could return.
set -u

if command -v speakd-claude-hook >/dev/null 2>&1; then
  speakd-claude-hook || true
  exit 0
fi

# The plugin usually sits inside the speakd checkout; fall back to the venv.
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
for candidate in "$root/.venv/bin/speakd-claude-hook" "$HOME/.local/bin/speakd-claude-hook"; do
  if [ -x "$candidate" ]; then
    "$candidate" || true
    exit 0
  fi
done

exit 0
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_cc_plugin_manifest.py -v`
Expected: PASS, 4 tests.

- [ ] **Step 5: Write the README**

`clients/claude-code/README.md` covers, in this order: what it does in two sentences; the one-time install (`/plugin marketplace add <path to the speakd checkout>`, then `/plugin install speakd`, or a `~/.claude/settings.json` snippet for people who would rather not use a marketplace); starting the daemon (`speakd &`); how to check it (`tail -f ~/.local/state/speakd/claude-code/hook.log`); and the one known limitation — a long answer with no tool calls arrives as one block, so audio starts at its first sentence but not before.

- [ ] **Step 6: End-to-end check by hand**

With the daemon running and the plugin installed, in a scratch Claude Code session:

1. Ask something short. Audio should begin within about a second of the response appearing.
2. Ask something that uses tools. Each prose block should speak while the next tool runs.
3. Press enter on a new prompt mid-speech. It should stop immediately.
4. `cat ~/.local/state/speakd/claude-code/hook.log` — expect it to be empty or absent.
5. Stop the daemon and repeat step 1. Nothing should break; the log should name the missing socket once per hook.

Record what actually happened in the task report, including anything spoken twice or not at all.

- [ ] **Step 7: Gates and commit**

```bash
uv run pytest -q && uv run ruff check . && uv run ruff format --check . && uv run mypy src tests
chmod +x clients/claude-code/hooks/speakd-hook.sh
git add clients/claude-code tests/test_cc_plugin_manifest.py README.md
git commit -m "Ship the Claude Code client as a loadable plugin"
```

---

## What this plan deliberately leaves out

- **Summarising long responses.** The `summarize` transform is a daemon transform with its own backends and its own recursion guard; it belongs to the transforms plan, not here. When it exists, this client gains nothing but a profile name.
- **Speaking tool activity.** "Running Bash" announcements are a profile and transform question. The client already sends nothing but prose, which is the right default.
- **Per-project enable/disable.** `speakctl` verbs and a config file cover this once they exist; a second mechanism in the client would be a second place to look.
- **Codex and OpenCode.** Both drive `speakctl` directly. If either needs transcript reading, it gets its own client module beside this one — `reader.py` is deliberately not Claude-Code-specific below `transcript.py`.
