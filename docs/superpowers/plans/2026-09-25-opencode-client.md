# OpenCode Client — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** An OpenCode plugin speaks responses, permission alerts and end-of-turn backstops through the speakd daemon, on per-session `opencode:<id>` channels that start brief and muted and are found by `speakd-mcp` through process ancestry — everything the Claude Code client has.

**Architecture:** A single dependency-free JS plugin, loaded by OpenCode, talks the daemon's JSON-lines protocol over the Unix socket and writes the same registration files the Claude hook writes. The daemon side is generalised, not copied: the session registry moves out of the Claude client and gains a `client` field, channel defaults recognise `opencode:` as well as `claude-code:`, and MCP ancestry resolution matches any registered ancestor pid. The plugin targets the installed OpenCode 1.18.x (v1) plugin API, and uses only signals that exist in the v2 event union — the v2 port is an entry-shim-only follow-up (Phase D).

**Tech Stack:** Python 3.11+ for the daemon side (pytest via `uv run pytest`, ruff, mypy); vanilla JS (ESM, `node:net`) for the plugin, tested with vitest under Node; python3 for JSON editing in the installer.

**Spec:** `docs/superpowers/specs/2026-09-25-opencode-client-design.md`

## Global Constraints

- No new Python runtime dependencies; no JS runtime dependencies in the plugin (only the vitest dev dependency in `clients/opencode/package.json`).
- Wire format: JSON lines — `{"verb": <str>, "source_id": <str>, "payload": {...}}` in, `{"ok": <bool>, "data": {...}, "error": <str>}` out (see `speakd/protocol.py`).
- Channel ids: `claude-code:<id>` and `opencode:<id>`, exactly one `:`, start `briefs=True`, `mode="brief"`, `muted=True`.
- Registration file: `<state root>/<client>/<slug(session_id)>.session.json`, JSON object `{"session_id", "transcript", "cwd", "touched", "client", "agent_pid"}`, where `touched` is `time.time()` and `agent_pid` may be `null`. `transcript` is `""` for OpenCode.
- The plugin must never break OpenCode: every handler and every socket call is guarded; socket calls are bounded at `SOCKET_TIMEOUT_MS = 2000`; a missing daemon is silence.
- Plugin signals, verified against OpenCode 1.18.32 and the v2 event union: `chat.message` hook (prompt), `permission.asked` event (permission — the `permission.ask` hook is dead code in v1, never invoked), `session.status` with `status.type === "idle"` (turn end — `session.idle` is deprecated), `message.updated` / `message.part.updated` (speech). Event payloads are read defensively: `part.sessionID ?? properties.sessionID`.
- Plugin state directory rule mirrors `speakd.paths.client_state_dir`: `$SPEAKD_STATE_DIR/<client>` wins, else `$XDG_STATE_HOME/speakd/<client>`, else `~/.local/state/speakd/<client>`.
- Socket path mirrors `speakd.paths.default_socket_path`: `$XDG_RUNTIME_DIR/speakd/speakd.sock`, falling back to `$TMPDIR` or `/tmp`.
- Python tests use `SPEAKD_STATE_DIR` and fake `/proc` trees (see `tests/test_mcp.py::fake_proc`); JS tests use vitest's node environment and never touch a real socket path.
- Commits end with the repo's attribution lines (`Co-Authored-By:` and session line), as in `git log`.

## Review Focus

- The Claude follower must ignore OpenCode registrations and vice versa — `follow.py` filters on `client`, tested in Task 2.
- Old registration files without a `client` field are skipped quietly, so an in-flight upgrade never mis-speaks; the next prompt rewrites them.
- `session.resolve` for an unregistered agent keeps today's `agent:<name>:<pid>` channel with its label — OpenCode without the plugin behaves exactly as it does today.
- A `session.status` event with `status.type === "idle"` flushes buffered parts *before* the "finished" backstop, so no text is left behind when a turn ends; a `busy` or `retry` status does nothing.
- A `permission.asked` event is spoken as a fixed attention sentence on its session's channel; a payload without a `sessionID` is skipped, not thrown.
- A part is spoken at most once, even when `message.updated` re-fires; sub-agent and `ignored` parts are never spoken.
- The installer must not destroy an existing `opencode.json`: keys outside `mcp.speakd` survive untouched, and a missing file is created.

---

## Phase A — daemon side (Python)

### Task 1: `opencode:` channels start brief and muted

**Files:**
- Modify: `src/speakd/__main__.py:92-114` (`_claude_code_defaults` → `_agent_defaults`, `build_channels` docstring)
- Test: `tests/test_briefing_modes.py` (add one test)

**Interfaces:**
- Produces: `_agent_defaults(source_id: str) -> dict[str, object]` returning `{"briefs": True, "muted": True}` for ids with exactly one `:` whose prefix is `claude-code` or `opencode`, else `{}`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_briefing_modes.py`:

```python
def test_opencode_sessions_start_brief_and_muted() -> None:
    from speakd.__main__ import build_channels
    from speakd.channels import effective_mode

    table = build_channels()
    main = table.open("opencode:ses_abc123")
    assert (main.briefs, effective_mode(main), main.muted) == (True, "brief", True)
    assert table.open("opencode:ses_abc123:extra").briefs is False
    other = table.open("notify:whatsapp")
    assert (other.briefs, effective_mode(other), other.muted) == (False, "full", False)
```

- [ ] **Step 2: Run it to see it fail**

Run: `uv run pytest tests/test_briefing_modes.py::test_opencode_sessions_start_brief_and_muted -q`
Expected: FAIL — `main.briefs` is `False`.

- [ ] **Step 3: Implement**

In `src/speakd/__main__.py`, replace `_claude_code_defaults` (lines 92–101) with:

```python
def _agent_defaults(source_id: str) -> dict[str, object]:
    """How an agent session's channel starts: able to brief, and muted.

    Both agents that ship an integration -- `claude-code:<id>` and
    `opencode:<id>`, exactly one colon -- start silent until chosen, and can
    brief because each ships the MCP server. Several sessions run at once and
    hearing all of them is noise, so each is silent until picked.
    """
    if source_id.count(":") == 1 and source_id.split(":", 1)[0] in ("claude-code", "opencode"):
        return {"briefs": True, "muted": True}
    return {}
```

In `build_channels` (lines 104–114), use `_agent_defaults` and update the docstring's first sentence:

```python
def build_channels() -> ChannelTable:
    """The channel table, with agent sessions starting brief and muted.

    A session is silent until it is chosen -- unmuted in the window's channel
    list or with `speakctl unmute --source` -- and then speaks the briefings
    its agent chooses to give, not every response, until switched to full.
    Everything else, the paste box and notifications included, starts audible
    as before. Held for the daemon's lifetime: after a restart every session
    starts this way again.
    """
    return ChannelTable(defaults=_agent_defaults)
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_briefing_modes.py -q`
Expected: PASS — the new test and `test_claude_code_sessions_start_brief_and_muted` both pass (the renamed function still covers `claude-code:`).

- [ ] **Step 5: Lint, typecheck, commit**

```bash
uv run ruff check src/speakd/__main__.py tests/test_briefing_modes.py && uv run ruff format --check src/speakd/__main__.py tests/test_briefing_modes.py && uv run mypy src tests
git add src/speakd/__main__.py tests/test_briefing_modes.py
git commit -m "Start opencode channels brief and muted, like claude-code"
```

### Task 2: The session registry moves out of the Claude client and gains a client

**Files:**
- Create: `src/speakd/clients/registry.py`
- Delete: `src/speakd/clients/claude_code/registry.py`
- Modify: `src/speakd/clients/claude_code/watermark.py` (`_slug` import), `src/speakd/clients/claude_code/hook.py` (import + `register` call), `src/speakd/clients/claude_code/follow.py` (import + filter), `src/speakd/clients/mcp/session.py` (import, `agent_pid` rename — the resolve logic itself is Task 3), `src/speakd/daemon.py:1590` (comment path)
- Test: `tests/test_client_registry.py` (create), update `tests/test_mcp.py` (imports, `claude_pid` → `agent_pid`, `client=` kwarg), `tests/test_cc_hook.py` (import, `r.claude_pid` → `r.agent_pid`)

**Interfaces:**
- Produces: `speakd.clients.registry.Registration(client, session_id, transcript, cwd, touched, agent_pid=None)`; `register(session_id, transcript, cwd, *, client, agent_pid=None) -> None`; `live(max_idle_seconds=1800.0) -> list[Registration]`; `state_dir(client) -> Path`; `_slug(session_id) -> str` (imported by `watermark.py`).
- Consumes: `speakd.paths.client_state_dir(name) -> Path`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_client_registry.py`:

```python
"""Tests for the shared session registry both agent clients use."""

import json
import time
from pathlib import Path

from speakd.clients import registry
from speakd.paths import client_state_dir


def test_registrations_land_in_a_directory_per_client(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("SPEAKD_STATE_DIR", str(tmp_path / "st"))
    registry.register("abc", Path("/t.jsonl"), "/work", client="claude-code", agent_pid=200)
    registry.register("ses_1", Path(""), "/work", client="opencode", agent_pid=700)
    claude_files = list(client_state_dir("claude-code").glob("*.session.json"))
    opencode_files = list(client_state_dir("opencode").glob("*.session.json"))
    assert len(claude_files) == 1 and len(opencode_files) == 1
    body = json.loads(opencode_files[0].read_text(encoding="utf-8"))
    assert body["client"] == "opencode"
    assert body["agent_pid"] == 700
    assert body["transcript"] == ""
    assert isinstance(body["touched"], float)


def test_live_reads_every_client_and_round_trips(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("SPEAKD_STATE_DIR", str(tmp_path / "st"))
    registry.register("abc", Path("/t.jsonl"), "/work", client="claude-code", agent_pid=200)
    registry.register("ses_1", Path(""), "/work", client="opencode", agent_pid=700)
    found = {(r.client, r.session_id, r.agent_pid, r.cwd) for r in registry.live()}
    assert found == {
        ("claude-code", "abc", 200, "/work"),
        ("opencode", "ses_1", 700, "/work"),
    }


def test_an_idle_registration_is_not_live(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("SPEAKD_STATE_DIR", str(tmp_path / "st"))
    registry.register("abc", Path("/t.jsonl"), "/work", client="claude-code", agent_pid=200)
    assert registry.live(max_idle_seconds=0.0) == []


def test_old_files_without_a_client_are_skipped(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("SPEAKD_STATE_DIR", str(tmp_path / "st"))
    directory = registry.state_dir("claude-code")
    directory.mkdir(parents=True)
    (directory / "stale-000000000000.session.json").write_text(
        json.dumps(
            {
                "session_id": "stale",
                "transcript": "/t.jsonl",
                "cwd": "/w",
                "touched": time.time(),
                "claude_pid": 9,
            }
        ),
        encoding="utf-8",
    )
    assert registry.live() == []


def test_a_path_shaped_session_id_stays_in_the_directory(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("SPEAKD_STATE_DIR", str(tmp_path / "st"))
    registry.register("../evil", Path(""), "/w", client="opencode")
    assert len(list(registry.state_dir("opencode").glob("*.session.json"))) == 1
```

- [ ] **Step 2: Run them to see them fail**

Run: `uv run pytest tests/test_client_registry.py -q`
Expected: FAIL — `speakd.clients.registry` does not exist.

- [ ] **Step 3: Write the module**

Create `src/speakd/clients/registry.py`:

```python
"""Which agent sessions are live, and where their transcripts are.

Shared by the agent clients: the Claude Code hook and the OpenCode plugin
each write one small file per session; the Claude follower and `speakd-mcp`'s
session resolver read them. A file rather than a control verb because the
hook already writes to this directory for the watermark, and a registration
verb would be a second source of truth for one fact -- with the added cost
of a socket round trip on the one hook that is still inside the user's
critical path.

Nothing here raises. A hook that fails is a hook that breaks someone's
editor, and a follower that dies on one corrupt file stops speaking for
every session.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass
from pathlib import Path

from speakd.paths import client_state_dir

# Half an hour of no prompt is a session someone has walked away from. Long
# enough that a lunch break does not cost the follower the session; short
# enough that yesterday's twenty windows are not polled today.
DEFAULT_MAX_IDLE_SECONDS = 1800.0

_SAFE = re.compile(r"[^A-Za-z0-9_.-]")


@dataclass(frozen=True)
class Registration:
    client: str
    session_id: str
    transcript: Path
    cwd: str
    touched: float
    # The agent process the session runs in, so an MCP server it started can
    # find which session it serves. None for registrations written before
    # this was recorded.
    agent_pid: int | None = None


def _slug(session_id: str) -> str:
    """A filename that cannot leave the state directory.

    Session ids come from a hook payload or a plugin event. Sanitising is not
    paranoia about the agents; it is that a path-shaped id would otherwise
    write state wherever it pointed.
    """
    safe = _SAFE.sub("_", session_id)[:80]
    digest = hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:12]
    return f"{safe}-{digest}"


def state_dir(client: str) -> Path:
    """The directory holding one client's registration files."""
    return client_state_dir(client)


def _registration_path(client: str, session_id: str) -> Path:
    directory = state_dir(client)
    directory.mkdir(parents=True, exist_ok=True)
    return directory / f"{_slug(session_id)}.session.json"


def register(
    session_id: str,
    transcript: Path,
    cwd: str,
    *,
    client: str,
    agent_pid: int | None = None,
) -> None:
    """Record that this session is live. Never raises."""
    try:
        _registration_path(client, session_id).write_text(
            json.dumps(
                {
                    "session_id": session_id,
                    "transcript": str(transcript),
                    "cwd": cwd,
                    "touched": time.time(),
                    "client": client,
                    "agent_pid": agent_pid,
                }
            ),
            encoding="utf-8",
        )
    except OSError:
        pass


def live(max_idle_seconds: float = DEFAULT_MAX_IDLE_SECONDS) -> list[Registration]:
    """Every session of every client registered recently enough to be polled."""
    base = client_state_dir("")
    try:
        directories = [path for path in base.iterdir() if path.is_dir()]
    except OSError:
        return []
    now = time.time()
    found: list[Registration] = []
    for directory in sorted(directories):
        try:
            candidates = sorted(directory.glob("*.session.json"))
        except OSError:
            continue
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
            client = body.get("client")
            if not isinstance(touched, int | float) or not isinstance(transcript, str):
                continue
            if not isinstance(session_id, str) or not isinstance(client, str):
                continue
            if now - touched > max_idle_seconds:
                continue
            found.append(
                Registration(
                    client=client,
                    session_id=session_id,
                    transcript=Path(transcript),
                    cwd=str(body.get("cwd") or ""),
                    touched=float(touched),
                    agent_pid=pid if isinstance(pid := body.get("agent_pid"), int) else None,
                )
            )
    return found
```

- [ ] **Step 4: Move the callers onto the new module**

In `src/speakd/clients/claude_code/watermark.py`: delete the `_SAFE` regex and the whole `_slug` function, and add to the imports:

```python
from speakd.clients.registry import _slug
from speakd.paths import client_state_dir
```

(keep `state_dir`, `_path_for`, `load`, `save`, `locked` as they are).

In `src/speakd/clients/claude_code/hook.py`: change the import to `from speakd.clients import registry` and the `register` call to:

```python
            registry.register(
                session_id,
                Path(transcript_path),
                str(body.get("cwd") or ""),
                client="claude-code",
                agent_pid=_claude_pid(),
            )
```

In `src/speakd/clients/claude_code/follow.py`: change the import to `from speakd.clients import registry`, and in `tick()` filter by client:

```python
    def tick(self) -> None:
        for reg in registry.live():
            if reg.client != "claude-code":
                continue
            try:
                self._follow(reg)
            except Exception as exc:
                _log(f"follower: session {reg.session_id} failed: {exc!r}")
```

In `src/speakd/clients/mcp/session.py`: change the import to `from speakd.clients import registry`, and rename the field in `resolve` (the loop at lines 83–85 — the full rewrite of `resolve` is Task 3, this keeps Task 2's tests green):

```python
    sessions: dict[int, str] = {}
    for reg in sorted(registry.live(), key=lambda r: r.touched):
        if reg.agent_pid:
            sessions[reg.agent_pid] = reg.session_id
```

Delete `src/speakd/clients/claude_code/registry.py`.

In `src/speakd/daemon.py:1590`, change the comment to reference the new path:

```python
        a sentinel. It is the argument `clients/registry` makes
```

- [ ] **Step 5: Update the existing tests**

In `tests/test_mcp.py` and `tests/test_cc_hook.py`: replace `from speakd.clients.claude_code import registry` with `from speakd.clients import registry`; change every `claude_pid=` keyword to `agent_pid=`, adding `client="claude-code"` to each `registry.register(...)` call; in `tests/test_cc_hook.py::test_a_prompt_records_the_claude_process` change `r.claude_pid` to `r.agent_pid` (line 136).

- [ ] **Step 6: Run everything**

Run: `uv run pytest tests/test_client_registry.py tests/test_mcp.py tests/test_cc_hook.py -q`
Expected: PASS. (`test_a_prompt_without_a_transcript_path_still_hushes` still passes: no registration is written, `live()` is empty.)
Then the full suite: `uv run pytest -q` — expected PASS everywhere.

- [ ] **Step 7: Lint, typecheck, commit**

```bash
uv run ruff check . && uv run ruff format --check . && uv run mypy src tests
git add src/speakd/clients/registry.py src/speakd/clients/claude_code tests/test_client_registry.py tests/test_mcp.py tests/test_cc_hook.py src/speakd/clients/mcp/session.py src/speakd/daemon.py
git rm src/speakd/clients/claude_code/registry.py
git commit -m "Share the session registry between agent clients"
```

### Task 3: MCP resolution finds OpenCode sessions by ancestry

**Files:**
- Modify: `src/speakd/clients/mcp/session.py` (`resolve`, module docstring)
- Test: `tests/test_mcp.py` (add two tests)

**Interfaces:**
- Produces: `resolve(client_name: str, *, proc: Path = Path("/proc"), cwd: str | None = None) -> tuple[str, str | None]` — first registered ancestor pid yields `("<client>:<session_id>", None)`; otherwise the unchanged `agent:<name>:<pid>` fallback.
- Consumes: `registry.live()`, `ancestors(proc)`, `agent_pid(chain)`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_mcp.py`:

```python
def test_an_opencode_session_is_found_by_ancestry(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from speakd.clients import registry

    monkeypatch.setenv("SPEAKD_STATE_DIR", str(tmp_path / "st"))
    proc = fake_proc(
        tmp_path / "proc", [(300, "python3", 290), (290, "bash", 700), (700, "opencode", 1)]
    )
    registry.register("ses_abc123", Path(""), "/work", client="opencode", agent_pid=700)
    assert session.resolve("opencode", proc=proc) == ("opencode:ses_abc123", None)


def test_a_registered_ancestor_is_found_through_any_launcher_chain(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    from speakd.clients import registry

    monkeypatch.setenv("SPEAKD_STATE_DIR", str(tmp_path / "st"))
    registry.register("abc", Path("/t.jsonl"), "/work", client="claude-code", agent_pid=200)
    proc = fake_proc(
        tmp_path / "proc",
        [(300, "python3", 290), (290, "bash", 250), (250, "env", 200), (200, "node", 1)],
    )
    assert session.resolve("claude-code", proc=proc) == ("claude-code:abc", None)
```

- [ ] **Step 2: Run them to see them fail**

Run: `uv run pytest tests/test_mcp.py::test_an_opencode_session_is_found_by_ancestry -q`
Expected: FAIL — returns `("agent:opencode:700", ...)`, not `("opencode:ses_abc123", None)`.

- [ ] **Step 3: Rewrite `resolve`**

Replace `resolve` in `src/speakd/clients/mcp/session.py` (lines 74–94) with:

```python
def resolve(
    client_name: str, *, proc: Path = Path("/proc"), cwd: str | None = None
) -> tuple[str, str | None]:
    """(source_id, label) for this server. A label of None means the channel has one.

    The session is whichever registered process appears first in this
    process's ancestry -- any level, not just the nearest non-launcher, so a
    launcher between the agent and what it starts cannot hide the match. The
    newest registration for each pid wins: `/clear` and `/new` start a fresh
    session in the same agent process, and the old one stays live in the
    registry until it idles out.
    """
    chain = ancestors(proc)
    sessions: dict[int, tuple[str, str]] = {}
    for reg in sorted(registry.live(), key=lambda r: r.touched):
        if reg.agent_pid:
            sessions[reg.agent_pid] = (reg.client, reg.session_id)
    for pid, _comm in chain:
        if pid in sessions:
            client, session_id = sessions[pid]
            return f"{client}:{session_id}", None
    # Not a registered agent session: a channel of its own, named for the
    # agent's pid so two agents in one directory stay apart.
    agent = agent_pid(chain)
    if agent is None:
        agent = chain[1][0] if len(chain) > 1 else os.getppid()
    directory = Path(cwd or os.getcwd()).name or "/"
    return f"agent:{client_name}:{agent}", f"{client_name} · {directory}"
```

Update the module docstring's second paragraph to say registrations are shared between the agents (the plugin records OpenCode's pid just as the hook records Claude Code's).

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_mcp.py -q`
Expected: PASS — including `test_a_claude_code_session_is_found_by_ancestry`, `test_the_newest_registration_for_a_process_wins`, `test_a_terminal_shared_with_a_claude_session_does_not_capture_another_agent` and `test_hook_and_server_meet_at_the_agent_whatever_it_is_called` (they hold because the shared terminal's pid is never registered, and newest-wins still applies per pid).

- [ ] **Step 5: Lint, typecheck, commit**

```bash
uv run ruff check . && uv run ruff format --check . && uv run mypy src tests
git add src/speakd/clients/mcp/session.py tests/test_mcp.py
git commit -m "Resolve an MCP server's session through any registered ancestor"
```

---

## Phase B — the plugin (JS)

All plugin code lives in one file, `clients/opencode/plugin/speakd.js`, grown task by task. Tests sit in `clients/opencode/test/` and run with vitest under Node (the plugin imports only `node:` modules, which both Bun and Node provide).

### Task 4: Scaffold, and the socket client

**Files:**
- Create: `clients/opencode/package.json`, `clients/opencode/vitest.config.js`, `clients/opencode/.gitignore`, `clients/opencode/plugin/speakd.js`, `clients/opencode/test/socket.test.js`

**Interfaces:**
- Produces: `socketPath(env?) -> string`; `request(line, {socket, timeoutMs}) -> Promise<{ok: true, body}|{ok: false, error}>`; `sendRequest(verb, source, payload, options) -> Promise<string|null>` (null on success, one-line reason otherwise); `enqueueText(source, text, {kind, flags, ...}) -> Promise<string|null>`; `hushChannel(source, {new_turn, ...}) -> Promise<string|null>`; `setLabelChannel(source, label, ...) -> Promise<string|null>`; `SOCKET_TIMEOUT_MS = 2000`.

- [ ] **Step 1: Scaffold**

`clients/opencode/package.json`:

```json
{
  "name": "speakd-opencode",
  "version": "0.1.0",
  "description": "Speak OpenCode's responses, or its briefings, through the speakd daemon",
  "private": true,
  "license": "MIT",
  "type": "module",
  "scripts": {
    "test": "vitest run"
  },
  "devDependencies": {
    "vitest": "^5.0.1"
  }
}
```

`clients/opencode/vitest.config.js` (mirrors `clients/zotero/vitest.config.ts`):

```js
import { defineConfig } from "vitest/config";

export default defineConfig({
  test: {
    include: ["test/**/*.test.js"],
    environment: "node",
  },
});
```

`clients/opencode/.gitignore` (mirrors `clients/zotero/.gitignore`):

```
node_modules/
```

- [ ] **Step 2: Write the failing tests**

`clients/opencode/test/socket.test.js`:

```js
import net from "node:net";
import os from "node:os";
import path from "node:path";
import { afterAll, beforeAll, describe, expect, it } from "vitest";

import { enqueueText, hushChannel, request } from "../plugin/speakd.js";

function listening(server, socket) {
  return new Promise((resolve) => server.listen(socket, resolve));
}

function closed(server) {
  return new Promise((resolve) => server.close(resolve));
}

describe("the socket client", () => {
  let sockets = [];

  afterAll(async () => {
    await Promise.all(sockets.map(closed));
    sockets = [];
  });

  it("sends one JSON line and resolves the reply", async () => {
    const socket = path.join(os.tmpdir(), `speakd-opencode-${process.pid}-${Math.random()}.sock`);
    const server = net.createServer((conn) => {
      conn.on("data", () => {
        conn.write(JSON.stringify({ ok: true, data: { spoken: true }, error: "" }) + "\n");
      });
    });
    sockets.push(server);
    await listening(server, socket);
    const reply = await request(JSON.stringify({ verb: "status", source_id: "", payload: {} }), {
      socket,
    });
    expect(reply).toEqual({ ok: true, body: { ok: true, data: { spoken: true }, error: "" } });
  });

  it("reports no daemon when nothing listens", async () => {
    const reply = await request(JSON.stringify({ verb: "status", source_id: "", payload: {} }), {
      socket: path.join(os.tmpdir(), "speakd-opencode-absent.sock"),
      timeoutMs: 500,
    });
    expect(reply.ok).toBe(false);
    expect(reply.error).toMatch(/no daemon/);
  });

  it("enqueueText puts text, kind and flags in the payload", async () => {
    const socket = path.join(os.tmpdir(), `speakd-opencode-${process.pid}-${Math.random()}.sock`);
    let raw = "";
    const server = net.createServer((conn) => {
      conn.on("data", (chunk) => {
        raw += chunk;
      });
    });
    sockets.push(server);
    await listening(server, socket);
    await enqueueText("opencode:ses_1", "Hello.", {
      kind: "attention",
      flags: { unless_briefed: true },
      socket,
      timeoutMs: 100,
    });
    expect(JSON.parse(raw.split("\n")[0])).toEqual({
      verb: "enqueue",
      source_id: "opencode:ses_1",
      payload: { text: "Hello.", kind: "attention", unless_briefed: true },
    });
  });

  it("blank text is never sent", async () => {
    const reason = await enqueueText("opencode:ses_1", "   ", {
      socket: path.join(os.tmpdir(), "speakd-opencode-absent.sock"),
      timeoutMs: 200,
    });
    expect(reason).toBeNull();
  });

  it("hushChannel sends new_turn only when asked", async () => {
    const socket = path.join(os.tmpdir(), `speakd-opencode-${process.pid}-${Math.random()}.sock`);
    let raw = "";
    const server = net.createServer((conn) => {
      conn.on("data", (chunk) => {
        raw += chunk;
      });
    });
    sockets.push(server);
    await listening(server, socket);
    await hushChannel("opencode:ses_1", { new_turn: true, socket, timeoutMs: 100 });
    expect(JSON.parse(raw.split("\n")[0])).toEqual({
      verb: "hush",
      source_id: "opencode:ses_1",
      payload: { new_turn: true },
    });
  });
});
```

- [ ] **Step 3: Run them to see them fail**

Run: `npm install` then `npm test` in `clients/opencode/`
Expected: FAIL — `../plugin/speakd.js` does not exist.

- [ ] **Step 4: Implement**

`clients/opencode/plugin/speakd.js`:

```js
// Speak an OpenCode session through the speakd daemon.
//
// One dependency-free file, loaded by OpenCode as a plugin. It talks the
// daemon's JSON-lines protocol over the Unix socket, writes the registration
// files `speakd-mcp` reads to find its session, and reacts to OpenCode's
// hooks and events. Nothing here may break OpenCode: every handler is
// guarded, every socket call bounded, and a missing daemon is silence.

// ---- the socket ----

import net from "node:net";
import path from "node:path";

export const SOCKET_TIMEOUT_MS = 2000;

export function socketPath(env = process.env) {
  // Mirrors speakd.paths.default_socket_path.
  const base = env.XDG_RUNTIME_DIR || env.TMPDIR || "/tmp";
  return path.join(base, "speakd", "speakd.sock");
}

export function request(line, { socket = socketPath(), timeoutMs = SOCKET_TIMEOUT_MS } = {}) {
  return new Promise((resolve) => {
    const client = net.createConnection({ path: socket });
    let settled = false;
    const settle = (reply) => {
      if (settled) return;
      settled = true;
      client.destroy();
      resolve(reply);
    };
    client.setTimeout(timeoutMs);
    client.on("connect", () => client.write(line + "\n"));
    client.on("timeout", () => settle({ ok: false, error: `timed out after ${timeoutMs}ms` }));
    client.on("error", (err) =>
      settle({ ok: false, error: `no daemon at ${socket} (${err.code ?? err.message})` }),
    );
    let buffer = "";
    client.on("data", (chunk) => {
      buffer += chunk.toString("utf-8");
      const end = buffer.indexOf("\n");
      if (end === -1) return;
      const raw = buffer.slice(0, end);
      buffer = buffer.slice(end + 1);
      try {
        settle({ ok: true, body: JSON.parse(raw) });
      } catch {
        settle({ ok: false, error: `unreadable reply: ${raw.slice(0, 80)}` });
      }
    });
    client.on("end", () => settle({ ok: false, error: "daemon closed the connection" }));
  });
}

export function sendRequest(verb, source, payload, options = {}) {
  return request(JSON.stringify({ verb, source_id: source, payload }), options).then((reply) => {
    if (reply.ok === false) return reply.error;
    if (reply.body && reply.body.ok === false) return reply.body.error || "refused";
    return null;
  });
}

export function enqueueText(source, text, { kind = "response", flags = {}, ...options } = {}) {
  if (!String(text).trim()) return Promise.resolve(null);
  return sendRequest("enqueue", source, { text, kind, ...flags }, options);
}

export function hushChannel(source, { new_turn = false, ...options } = {}) {
  return sendRequest("hush", source, new_turn ? { new_turn: true } : {}, options);
}

export function setLabelChannel(source, label, options = {}) {
  return sendRequest("set_label", source, { label }, options);
}
```

- [ ] **Step 5: Run the tests**

Run: `npm test` in `clients/opencode/`
Expected: 5 PASS.

- [ ] **Step 6: Commit**

```bash
git add clients/opencode
git commit -m "Add the opencode plugin scaffold and socket client"
```

### Task 5: The plugin writes registrations the MCP server can read

**Files:**
- Modify: `clients/opencode/plugin/speakd.js` (append the registration section)
- Test: `clients/opencode/test/state.test.js` (create)

**Interfaces:**
- Produces: `stateDir(env?) -> string`; `register(sessionID, {cwd, agentPid, env}) -> void` (never throws), writing the file shape from the Global Constraints.
- Consumes: nothing beyond `node:fs`, `node:crypto`, `node:path`.

- [ ] **Step 1: Write the failing tests**

`clients/opencode/test/state.test.js`:

```js
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { describe, expect, it } from "vitest";

import { register, stateDir } from "../plugin/speakd.js";

function tempState() {
  return fs.mkdtempSync(path.join(os.tmpdir(), "speakd-opencode-state-"));
}

describe("registration files", () => {
  it("writes the schema the Python registry reads", () => {
    const root = tempState();
    const env = { ...process.env, SPEAKD_STATE_DIR: root };
    register("ses_abc123", { cwd: "/home/me/p", agentPid: 4242, env });
    const files = fs.readdirSync(stateDir(env));
    expect(files).toHaveLength(1);
    const body = JSON.parse(
      fs.readFileSync(path.join(stateDir(env), files[0]), "utf-8"),
    );
    expect(body.session_id).toBe("ses_abc123");
    expect(body.transcript).toBe("");
    expect(body.cwd).toBe("/home/me/p");
    expect(body.client).toBe("opencode");
    expect(body.agent_pid).toBe(4242);
    expect(typeof body.touched).toBe("number");
  });

  it("a path-shaped session id stays inside the directory", () => {
    const root = tempState();
    const env = { ...process.env, SPEAKD_STATE_DIR: root };
    register("../../evil", { cwd: "/w", agentPid: 1, env });
    const files = fs.readdirSync(stateDir(env));
    expect(files).toHaveLength(1);
    expect(files[0].startsWith(".._.._evil-")).toBe(true);
    expect(fs.existsSync(path.join(root, "evil.session.json"))).toBe(false);
  });

  it("never throws, even into an unwritable directory", () => {
    const env = { SPEAKD_STATE_DIR: "/proc/forbidden/speakd" };
    expect(() => register("ses_1", { cwd: "/w", env })).not.toThrow();
  });
});
```

- [ ] **Step 2: Run them to see them fail**

Run: `npm test` in `clients/opencode/`
Expected: FAIL — `stateDir`/`register` are not exported.

- [ ] **Step 3: Implement**

Append to `clients/opencode/plugin/speakd.js`:

```js
// ---- the registration file ----
//
// The same file `speakd.clients.registry` writes and reads, in the same
// state directory scheme, so `speakd-mcp` can find this session by process
// ancestry. The slug must match Python's `_slug` exactly.

import fs from "node:fs";
import crypto from "node:crypto";

export function stateDir(env = process.env) {
  // Mirrors speakd.paths.client_state_dir for client "opencode".
  const override = env.SPEAKD_STATE_DIR;
  if (override) return path.join(override, "opencode");
  const xdg = env.XDG_STATE_HOME;
  const base = xdg
    ? path.join(xdg, "speakd")
    : path.join(env.HOME ?? "/tmp", ".local", "state", "speakd");
  return path.join(base, "opencode");
}

export function slug(sessionID) {
  const safe = sessionID.replace(/[^A-Za-z0-9_.-]/g, "_").slice(0, 80);
  const digest = crypto.createHash("sha256").update(sessionID, "utf-8").digest("hex").slice(0, 12);
  return `${safe}-${digest}`;
}

export function register(sessionID, { cwd = "", agentPid = process.pid, env = process.env } = {}) {
  try {
    const directory = stateDir(env);
    fs.mkdirSync(directory, { recursive: true });
    const body = {
      session_id: sessionID,
      transcript: "",
      cwd: cwd || process.cwd(),
      touched: Date.now() / 1000,
      client: "opencode",
      agent_pid: agentPid,
    };
    fs.writeFileSync(path.join(directory, `${slug(sessionID)}.session.json`), JSON.stringify(body));
  } catch {
    // A registration that cannot be written costs silence, nothing more.
  }
}
```

- [ ] **Step 4: Run the tests**

Run: `npm test` in `clients/opencode/`
Expected: 8 PASS (5 socket + 3 state).

- [ ] **Step 5: Cross-check the two slugs agree**

Run: `uv run pytest tests/test_client_registry.py -q` and a parity check of the two slug implementations:

```bash
uv run python -c "
from speakd.clients.registry import _slug
print(_slug('ses_abc123'))
print(_slug('../../evil'))" > /tmp/py-slugs.txt
node -e "
import('./clients/opencode/plugin/speakd.js').then((m) => {
  console.log(m.slug('ses_abc123'))
  console.log(m.slug('../../evil'))
})" > /tmp/js-slugs.txt
diff /tmp/py-slugs.txt /tmp/js-slugs.txt && echo "slugs agree"
```

Expected: `slugs agree`. If they differ, the regex, truncation or encoding differs — fix `slug` in the plugin (the Python side is shipped and tested), then re-run.

- [ ] **Step 6: Commit**

```bash
git add clients/opencode
git commit -m "Have the opencode plugin write session registrations"
```

### Task 6: The speaker — buffer parts, speak them whole, once

**Files:**
- Modify: `clients/opencode/plugin/speakd.js` (append the speaker section)
- Test: `clients/opencode/test/speech.test.js` (create)

**Interfaces:**
- Produces: `class Speaker` with constructor `({send, log})` and methods `onChatMessage(sessionID, agent)`, `onMessageUpdated(info)`, `onPartUpdated(sessionID, part)`, `onPartRemoved(sessionID, part)`, `onSessionIdle(sessionID)`. `send` is called as `send({verb, source, payload})` and returns a promise of a reason string or null.
- Consumes: none outside the file.

- [ ] **Step 1: Write the failing tests**

`clients/opencode/test/speech.test.js`:

```js
import { describe, expect, it } from "vitest";

import { Speaker } from "../plugin/speakd.js";

function build() {
  const sent = [];
  const speaker = new Speaker({
    send: (request) => {
      sent.push(request);
      return Promise.resolve(null);
    },
  });
  return { speaker, sent };
}

function text(sessionID, messageID, partID, value, extra = {}) {
  return {
    id: partID,
    sessionID,
    messageID,
    type: "text",
    text: value,
    ...extra,
  };
}

describe("the speaker", () => {
  it("speaks a text part whole when it finishes", async () => {
    const { speaker, sent } = build();
    speaker.onChatMessage("ses_1", "build");
    speaker.onMessageUpdated({
      id: "m1", sessionID: "ses_1", role: "assistant", agent: "build",
      parentID: "", time: { created: 1 },
    });
    speaker.onPartUpdated("ses_1", text("ses_1", "m1", "p1", "Hello."));
    speaker.onPartUpdated("ses_1", text("ses_1", "m1", "p1", "Hello. World.", { time: { start: 0, end: 12 } }));
    expect(sent).toEqual([
      { verb: "enqueue", source: "opencode:ses_1", payload: { text: "Hello. World.", kind: "response" } },
    ]);
  });

  it("never speaks a part twice", async () => {
    const { speaker, sent } = build();
    speaker.onChatMessage("ses_1", "build");
    speaker.onMessageUpdated({
      id: "m1", sessionID: "ses_1", role: "assistant", agent: "build",
      parentID: "", time: { created: 1, completed: 2 },
    });
    speaker.onPartUpdated("ses_1", text("ses_1", "m1", "p1", "Done.", { time: { start: 0, end: 5 } }));
    speaker.onPartUpdated("ses_1", text("ses_1", "m1", "p1", "Done.", { time: { start: 0, end: 5 } }));
    speaker.onMessageUpdated({
      id: "m1", sessionID: "ses_1", role: "assistant", agent: "build",
      parentID: "", time: { created: 1, completed: 2 },
    });
    expect(sent).toHaveLength(1);
  });

  it("buffers before the message is known and flushes after classification", async () => {
    const { speaker, sent } = build();
    speaker.onChatMessage("ses_1", "build");
    speaker.onPartUpdated("ses_1", text("ses_1", "m1", "p1", "Early.", { time: { start: 0, end: 6 } }));
    expect(sent).toHaveLength(0);
    speaker.onMessageUpdated({
      id: "m1", sessionID: "ses_1", role: "assistant", agent: "build",
      parentID: "", time: { created: 1, completed: 2 },
    });
    expect(sent).toEqual([
      { verb: "enqueue", source: "opencode:ses_1", payload: { text: "Early.", kind: "response" } },
    ]);
  });

  it("skips sub-agent messages and dropped ignored parts", async () => {
    const { speaker, sent } = build();
    speaker.onChatMessage("ses_1", "build");
    speaker.onMessageUpdated({
      id: "m1", sessionID: "ses_1", role: "assistant", agent: "general",
      parentID: "", time: { created: 1, completed: 2 },
    });
    speaker.onPartUpdated("ses_1", text("ses_1", "m1", "p1", "Sub-agent noise."));
    speaker.onMessageUpdated({
      id: "m2", sessionID: "ses_1", role: "assistant", agent: "build",
      parentID: "", time: { created: 1, completed: 2 },
    });
    speaker.onPartUpdated("ses_1", { ...text("ses_1", "m2", "p2", "Skip me."), ignored: true });
    expect(sent).toHaveLength(0);
  });

  it("idle flushes what remains and then sends the finished backstop", async () => {
    const { speaker, sent } = build();
    speaker.onChatMessage("ses_1", "build");
    speaker.onMessageUpdated({
      id: "m1", sessionID: "ses_1", role: "assistant", agent: "build",
      parentID: "", time: { created: 1 },
    });
    speaker.onPartUpdated("ses_1", text("ses_1", "m1", "p1", "Left over."));
    speaker.onSessionIdle("ses_1");
    expect(sent).toEqual([
      { verb: "enqueue", source: "opencode:ses_1", payload: { text: "Left over.", kind: "response" } },
      {
        verb: "enqueue", source: "opencode:ses_1",
        payload: { text: "finished", kind: "attention", unless_briefed: true, only_in_mode: "brief" },
      },
    ]);
  });
});
```

- [ ] **Step 2: Run them to see them fail**

Run: `npm test` in `clients/opencode/`
Expected: FAIL — `Speaker` is not exported.

- [ ] **Step 3: Implement**

Append to `clients/opencode/plugin/speakd.js`:

```js
// ---- what is spoken ----
//
// OpenCode streams a text part's growth through `message.part.updated`; each
// event carries the part's accumulated text. Speaking each delta would cut
// words in half, so parts are buffered and enqueued whole when they finish
// -- a text block's end, which is usually before the tool calls that follow
// it. A part is spoken at most once; sub-agent output and `ignored` parts
// are never spoken.

class SessionState {
  constructor() {
    this.mainAgent = null;
    this.userAgents = new Map(); // messageID -> agent, for user messages
    this.messages = new Map(); // messageID -> { agent, parts, spoken }
  }
}

export class Speaker {
  constructor({ send, log = () => {} }) {
    this.send = send; // ({verb, source, payload}) -> Promise<reason|null>
    this.log = log;
    this.sessions = new Map(); // sessionID -> SessionState
  }

  _session(sessionID) {
    let state = this.sessions.get(sessionID);
    if (!state) {
      state = new SessionState();
      this.sessions.set(sessionID, state);
    }
    return state;
  }

  _main(state) {
    return state.mainAgent ?? "build";
  }

  _entry(state, messageID) {
    let entry = state.messages.get(messageID);
    if (!entry) {
      entry = { agent: null, parts: new Map(), spoken: new Set() };
      state.messages.set(messageID, entry);
    }
    return entry;
  }

  onChatMessage(sessionID, agent) {
    // The first `chat.message` for a session names its main agent; user
    // prompts routed to sub-agents do not come through this hook.
    const state = this._session(sessionID);
    if (!state.mainAgent && agent) state.mainAgent = agent;
  }

  onMessageUpdated(info) {
    if (info.role === "user") {
      const state = this._session(info.sessionID);
      state.userAgents.set(info.id, info.agent);
      return;
    }
    const state = this._session(info.sessionID);
    const entry = this._entry(state, info.id);
    // `agent` is present in OpenCode's stored messages though absent from the
    // SDK's published types; the parent user message is the fallback.
    entry.agent = info.agent ?? state.userAgents.get(info.parentID) ?? state.mainAgent;
    if (entry.agent !== this._main(state)) {
      entry.parts.clear();
      return;
    }
    if (info.time && info.time.completed != null) {
      this._flushAll(state, info.sessionID, info.id, entry);
    }
  }

  onPartUpdated(sessionID, part) {
    if (part.type !== "text") return;
    const state = this._session(sessionID);
    const entry = this._entry(state, part.messageID);
    if (!entry.parts.has(part.id) && entry.spoken.has(part.id)) return;
    if (part.ignored) {
      entry.parts.delete(part.id);
      return;
    }
    if (entry.agent !== null && entry.agent !== this._main(state)) return;
    entry.parts.set(part.id, part.text);
    if (part.time && part.time.end != null && entry.agent !== null) {
      this._flush(state, sessionID, part.messageID, entry, part.id);
    }
  }

  onPartRemoved(sessionID, part) {
    if (part.type !== "text") return;
    const state = this._session(sessionID);
    const entry = state.messages.get(part.messageID);
    if (entry) entry.parts.delete(part.id);
  }

  onSessionIdle(sessionID) {
    const state = this._session(sessionID);
    for (const [messageID, entry] of state.messages) {
      if (entry.agent !== null && entry.agent === this._main(state)) {
        this._flushAll(state, sessionID, messageID, entry);
      }
    }
    this.send({
      verb: "enqueue",
      source: `opencode:${sessionID}`,
      payload: { text: "finished", kind: "attention", unless_briefed: true, only_in_mode: "brief" },
    });
  }

  _flush(state, sessionID, messageID, entry, partID) {
    const text = entry.parts.get(partID);
    if (text == null) return;
    entry.parts.delete(partID);
    entry.spoken.add(partID);
    const body = text.trim();
    if (body) {
      this.send({
        verb: "enqueue",
        source: `opencode:${sessionID}`,
        payload: { text: body, kind: "response" },
      });
    }
  }

  _flushAll(state, sessionID, messageID, entry) {
    for (const partID of [...entry.parts.keys()]) {
      this._flush(state, sessionID, messageID, entry, partID);
    }
  }
}
```

- [ ] **Step 4: Run the tests**

Run: `npm test` in `clients/opencode/`
Expected: 13 PASS.

- [ ] **Step 5: Commit**

```bash
git add clients/opencode
git commit -m "Speak opencode text parts whole, once, when they finish"
```

### Task 7: Hush, permission alerts, and labels

**Files:**
- Modify: `clients/opencode/plugin/speakd.js` (append the signals section)
- Test: `clients/opencode/test/signals.test.js` (create)

**Interfaces:**
- Produces: `class Signals` with constructor `({send, register, log})` and methods `onChatMessage(sessionID)`, `onPermissionAsked(event)`, `onSession(info)`. `register(sessionID, {cwd})` is the plugin's own `register`.
- Consumes: `register` from Task 5, `path` (already imported).

- [ ] **Step 1: Write the failing tests**

`clients/opencode/test/signals.test.js`:

```js
import { describe, expect, it } from "vitest";

import { Signals } from "../plugin/speakd.js";

function build() {
  const sent = [];
  const registrations = [];
  const signals = new Signals({
    send: (request) => {
      sent.push(request);
      return Promise.resolve(null);
    },
    register: (sessionID, options) => registrations.push([sessionID, options]),
  });
  return { signals, sent, registrations };
}

describe("signals", () => {
  it("a prompt registers the session and hushes it with a new turn", async () => {
    const { signals, sent, registrations } = build();
    signals.onSession({ id: "ses_1", title: "", directory: "/home/me/p" });
    signals.onChatMessage("ses_1");
    expect(registrations).toEqual([["ses_1", { cwd: "/home/me/p" }]]);
    expect(sent).toEqual([
      { verb: "hush", source: "opencode:ses_1", payload: { new_turn: true } },
    ]);
  });

  it("a permission ask is spoken on the session's channel in every mode", async () => {
    const { signals, sent } = build();
    signals.onPermissionAsked({
      type: "permission.asked",
      properties: { sessionID: "ses_1", permission: "bash" },
    });
    expect(sent).toEqual([
      {
        verb: "enqueue",
        source: "opencode:ses_1",
        payload: { text: "OpenCode needs your permission.", kind: "attention" },
      },
    ]);
  });

  it("a permission event without a session is skipped, not thrown", async () => {
    const { signals, sent } = build();
    expect(() => signals.onPermissionAsked({ type: "permission.asked", properties: {} })).not.toThrow();
    expect(sent).toHaveLength(0);
  });

  it("the label is sent once per change, title first", async () => {
    const { signals, sent } = build();
    signals.onSession({ id: "ses_1", title: "", directory: "/home/me/repo" });
    expect(sent).toEqual([
      { verb: "set_label", source: "opencode:ses_1", payload: { label: "OpenCode · repo" } },
    ]);
    sent.length = 0;
    signals.onSession({ id: "ses_1", title: "", directory: "/home/me/repo" });
    expect(sent).toHaveLength(0);
    signals.onSession({ id: "ses_1", title: "Fix the build", directory: "/home/me/repo" });
    expect(sent).toEqual([
      { verb: "set_label", source: "opencode:ses_1", payload: { label: "OpenCode · Fix the build" } },
    ]);
  });
});
```

- [ ] **Step 2: Run them to see them fail**

Run: `npm test` in `clients/opencode/`
Expected: FAIL — `Signals` is not exported.

- [ ] **Step 3: Implement**

Append to `clients/opencode/plugin/speakd.js`:

```js
// ---- control signals and labels ----
//
// What only OpenCode itself can know, mirrored from the Claude hooks: a
// prompt was submitted (hush, register), the user's permission is needed
// (attention, spoken in every mode), and what the session is called.

export class Signals {
  constructor({ send, register, log = () => {} }) {
    this.send = send;
    this.register = register;
    this.log = log;
    this.sessions = new Map(); // sessionID -> { title, directory }
    this.labels = new Map(); // sessionID -> last label sent
  }

  _session(sessionID) {
    let state = this.sessions.get(sessionID);
    if (!state) {
      state = { title: null, directory: "" };
      this.sessions.set(sessionID, state);
    }
    return state;
  }

  onChatMessage(sessionID) {
    const state = this._session(sessionID);
    this.register(sessionID, { cwd: state.directory });
    this.send({ verb: "hush", source: `opencode:${sessionID}`, payload: { new_turn: true } });
  }

  onPermissionAsked(event) {
    // An event, not the `permission.ask` hook: that hook is declared in the
    // plugin types but never invoked in OpenCode 1.x. Only the session id is
    // read, which every delivered payload shape carries.
    const properties = event.properties ?? {};
    const sessionID = properties.sessionID;
    if (!sessionID) return;
    this.send({
      verb: "enqueue",
      source: `opencode:${sessionID}`,
      payload: { text: "OpenCode needs your permission.", kind: "attention" },
    });
  }

  onSession(info) {
    const state = this._session(info.id);
    state.title = info.title || null;
    state.directory = info.directory || "";
    const label = info.title
      ? `OpenCode · ${info.title}`
      : info.directory
        ? `OpenCode · ${path.basename(info.directory)}`
        : "OpenCode";
    if (this.labels.get(info.id) === label) return;
    this.send({
      verb: "set_label",
      source: `opencode:${info.id}`,
      payload: { label },
    }).then((reason) => {
      if (reason == null) this.labels.set(info.id, label);
    });
  }
}
```

- [ ] **Step 4: Run the tests**

Run: `npm test` in `clients/opencode/`
Expected: 17 PASS.

- [ ] **Step 5: Commit**

```bash
git add clients/opencode
git commit -m "Hush, alert and label opencode sessions from the plugin"
```

### Task 8: The plugin entry — wire hooks and events to the pieces

**Files:**
- Modify: `clients/opencode/plugin/speakd.js` (append the entry section)
- Test: `clients/opencode/test/plugin.test.js` (create)

**Interfaces:**
- Produces: `buildPlugin({client, send, registerFn}) -> hooks object` with `"chat.message"` and `event` handlers; `SpeakdPlugin(input) -> Promise<hooks>` — OpenCode's plugin entry, delegating to `buildPlugin`.

- [ ] **Step 1: Write the failing tests**

`clients/opencode/test/plugin.test.js`:

```js
import { describe, expect, it } from "vitest";

import { buildPlugin } from "../plugin/speakd.js";

function build() {
  const sent = [];
  const registrations = [];
  const logs = [];
  const plugin = buildPlugin({
    client: { app: { log: async (line) => logs.push(line) } },
    send: (request) => {
      sent.push(request);
      return Promise.resolve(null);
    },
    registerFn: (sessionID, options) => registrations.push([sessionID, options]),
  });
  return { plugin, sent, registrations, logs };
}

describe("the plugin entry", () => {
  it("a chat.message hushes, registers and sets the main agent", async () => {
    const { plugin, sent, registrations } = build();
    await plugin["chat.message"]({ sessionID: "ses_1", agent: "build" });
    await plugin.event({ event: { type: "message.part.updated", properties: { part: { id: "p1", sessionID: "ses_1", messageID: "m1", type: "text", text: "Hi.", time: { start: 0, end: 3 } } } } });
    expect(registrations).toEqual([["ses_1", { cwd: "" }]]);
    expect(sent).toEqual([
      { verb: "hush", source: "opencode:ses_1", payload: { new_turn: true } },
      { verb: "enqueue", source: "opencode:ses_1", payload: { text: "Hi.", kind: "response" } },
    ]);
  });

  it("a session status of idle sends the finished backstop", async () => {
    const { plugin, sent } = build();
    await plugin.event({ event: { type: "session.status", properties: { sessionID: "ses_1", status: { type: "idle" } } } });
    expect(sent).toEqual([
      {
        verb: "enqueue",
        source: "opencode:ses_1",
        payload: { text: "finished", kind: "attention", unless_briefed: true, only_in_mode: "brief" },
      },
    ]);
  });

  it("a busy session status does nothing", async () => {
    const { plugin, sent } = build();
    await plugin.event({ event: { type: "session.status", properties: { sessionID: "ses_1", status: { type: "busy" } } } });
    expect(sent).toHaveLength(0);
  });

  it("a permission.asked event is spoken as attention", async () => {
    const { plugin, sent } = build();
    await plugin.event({ event: { type: "permission.asked", properties: { sessionID: "ses_1", permission: "bash" } } });
    expect(sent).toEqual([
      {
        verb: "enqueue",
        source: "opencode:ses_1",
        payload: { text: "OpenCode needs your permission.", kind: "attention" },
      },
    ]);
  });

  it("malformed events are logged, never thrown", async () => {
    const { plugin, logs } = build();
    await expect(plugin.event({ event: { type: "session.status", properties: null } })).resolves.toBeUndefined();
    await expect(plugin.event({ event: null })).resolves.toBeUndefined();
    expect(logs.length).toBeGreaterThan(0);
  });
});
```

- [ ] **Step 2: Run them to see them fail**

Run: `npm test` in `clients/opencode/`
Expected: FAIL — `buildPlugin` is not exported.

- [ ] **Step 3: Implement**

Append to `clients/opencode/plugin/speakd.js`:

```js
// ---- the plugin ----

export function buildPlugin({ client, send, registerFn = register } = {}) {
  const log = async (line) => {
    try {
      if (client && client.app && client.app.log) {
        await client.app.log({ body: { service: "speakd", level: "warn", message: String(line) } });
      }
    } catch {
      // A log that cannot be written is not worth an error in the host.
    }
  };

  const realSend =
    send ??
    (({ verb, source, payload }) =>
      sendRequest(verb, source, payload, { timeoutMs: SOCKET_TIMEOUT_MS }).then((reason) => {
        if (reason != null) log(reason);
        return reason;
      }));

  const speaker = new Speaker({ send: realSend, log });
  const signals = new Signals({ send: realSend, register: registerFn, log });

  return {
    "chat.message": async (input) => {
      try {
        speaker.onChatMessage(input.sessionID, input.agent);
        signals.onChatMessage(input.sessionID);
      } catch (err) {
        log(`chat.message failed: ${err}`);
      }
    },
    event: async ({ event }) => {
      try {
        switch (event.type) {
          case "message.updated":
            speaker.onMessageUpdated(event.properties.info);
            break;
          case "message.part.updated": {
            const part = event.properties.part;
            if (!part) break;
            speaker.onPartUpdated(part.sessionID ?? event.properties.sessionID, part);
            break;
          }
          case "message.part.removed": {
            const part = event.properties.part;
            if (!part) break;
            speaker.onPartRemoved(part.sessionID ?? event.properties.sessionID, part);
            break;
          }
          case "session.status":
            // `session.idle` is deprecated; a status of idle is the same
            // signal, and it is the spelling that survives into v2.
            if (event.properties.status && event.properties.status.type === "idle") {
              speaker.onSessionIdle(event.properties.sessionID);
            }
            break;
          case "permission.asked":
            signals.onPermissionAsked(event);
            break;
          case "session.created":
          case "session.updated":
            signals.onSession(event.properties.info);
            break;
        }
      } catch (err) {
        log(`event ${event && event.type} failed: ${err}`);
      }
    },
  };
}

export const SpeakdPlugin = async ({ client } = {}) => buildPlugin({ client });
```

- [ ] **Step 4: Run the tests**

Run: `npm test` in `clients/opencode/`
Expected: 22 PASS (17 + 5 new).

- [ ] **Step 5: Syntax-check under both runtimes, then commit**

```bash
node --check plugin/speakd.js
bun --check plugin/speakd.js 2>/dev/null || echo "bun not installed; node check suffices"
git add clients/opencode
git commit -m "Wire the opencode plugin hooks and events to the speaker and signals"
```

---

## Phase C — install and docs

### Task 9: Installer, MCP wrapper, and documentation

**Files:**
- Create: `clients/opencode/speakd-mcp.sh`, `clients/opencode/install.sh`, `clients/opencode/README.md`
- Modify: `README.md` (Clients + Briefings sections), `docs/HANDOFF.md` (OpenCode row)

- [ ] **Step 1: The MCP wrapper**

`clients/opencode/speakd-mcp.sh` — the same lookup the Claude Code wrapper makes, pointed at this checkout:

```bash
#!/usr/bin/env bash
# Find speakd-mcp and run it in this process's place, so OpenCode talks to
# the server directly over stdin and stdout.
#
# The installer writes this file's absolute path into opencode.json, so an
# installed config points back at the checkout through it -- and
# $SPEAKD_HOME carries it across a moved checkout, as with Claude Code.
#
# Unlike a hook, this may fail loudly: an MCP server that cannot start is
# reported by OpenCode as one, which is the right place for it.
set -u
home_dir=~

if command -v speakd-mcp >/dev/null 2>&1; then
  exec speakd-mcp "$@"
fi

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
for candidate in \
  "${SPEAKD_HOME:-}/.venv/bin/speakd-mcp" \
  "$root/.venv/bin/speakd-mcp" \
  "$home_dir/.local/bin/speakd-mcp"; do
  if [ -x "$candidate" ]; then
    exec "$candidate" "$@"
  fi
done

echo "speakd-mcp: not on PATH, and no executable at \${SPEAKD_HOME}/.venv/bin \
(SPEAKD_HOME=${SPEAKD_HOME:-unset}), $root/.venv/bin, or $home_dir/.local/bin -- \
set SPEAKD_HOME to your speakd checkout and run \`uv sync\` there" >&2
exit 1
```

Make it executable: `chmod +x clients/opencode/speakd-mcp.sh clients/opencode/install.sh` (after writing both).

- [ ] **Step 2: The installer**

`clients/opencode/install.sh`:

```bash
#!/usr/bin/env bash
# Install the plugin and the MCP server into OpenCode's global config.
#
# The plugin file is copied into ~/.config/opencode/plugins/, which OpenCode
# loads at startup; the MCP entry is merged into opencode.json (created if
# absent, every other key preserved) with this wrapper's absolute path, so
# the installed config finds the checkout whatever OpenCode's own cwd is.
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
config_dir="${XDG_CONFIG_HOME:-$HOME/.config}/opencode"
plugins_dir="$config_dir/plugins"
config_file="$config_dir/opencode.json"

mkdir -p "$plugins_dir"
cp "$root/plugin/speakd.js" "$plugins_dir/speakd.js"

python3 - "$config_file" "$root/speakd-mcp.sh" <<'PY'
import json
import sys
from pathlib import Path

config_path = Path(sys.argv[1])
wrapper = str(Path(sys.argv[2]).resolve())
body: dict = {}
if config_path.exists():
    body = json.loads(config_path.read_text(encoding="utf-8"))
body.setdefault("mcp", {})
body["mcp"]["speakd"] = {
    "type": "local",
    "command": ["bash", wrapper],
    "enabled": True,
}
config_path.write_text(json.dumps(body, indent=2) + "\n", encoding="utf-8")
print(f"wrote {config_path}")
PY

printf 'speakd for OpenCode installed:\n'
printf '  plugin:  %s/speakd.js\n' "$plugins_dir"
printf '  mcp:     speakd in %s\n' "$config_file"
printf 'Restart OpenCode for the plugin to load. The daemon itself: \n'
printf '  systemctl --user enable --now speakd   (or: uv run speakd &)\n'
```

- [ ] **Step 3: Verify the installer on a scratch config**

Run (with a scratch `HOME`, so your real config is untouched):

```bash
tmp=$(mktemp -d) && XDG_CONFIG_HOME="$tmp/cfg" bash clients/opencode/install.sh && \
python3 -c "import json,sys; print(json.load(open('$tmp/cfg/opencode/opencode.json')))" && \
ls "$tmp/cfg/opencode/plugins" && rm -r "$tmp"
```

Expected: the printed JSON has `mcp.speakd` as `{"type": "local", "command": ["bash", "<abs>/clients/opencode/speakd-mcp.sh"], "enabled": true}` and the plugins dir holds exactly `speakd.js`.
Then confirm a pre-existing config survives: repeat with `printf '{"theme": "dark"}' > "$tmp/cfg/opencode/opencode.json"` first, and check `theme` is still there afterwards.

- [ ] **Step 4: The client README**

`clients/opencode/README.md`:

```markdown
# speakd for OpenCode

Speaks OpenCode's prose responses aloud while the session runs: each text
block is enqueued the moment it finishes, a new prompt stops the speech, and
permission prompts are read aloud. It also gives each session a way to
**brief** you instead — the same `speakd-mcp` server Claude Code uses, whose
`brief` tool the agent calls when it has finished, is stuck, or has a
question. Whether a session reads every response (full) or only its
briefings (brief) is chosen per session — see the main README's *Briefings*.

One plugin file does the work OpenCode's own hooks would do in Claude Code:

- `chat.message` stops the speech, starts a new turn and tells the daemon
  this session is live.
- `permission.ask` speaks "OpenCode needs your permission."
- `session.idle` says "finished" when a turn ended in brief mode without a
  briefing.

## Install, once

```bash
clients/opencode/install.sh
```

This copies the plugin into `~/.config/opencode/plugins/` and merges the
`speakd-mcp` entry into `~/.config/opencode/opencode.json`. Restart
OpenCode; a session started afterwards has both. Code changes to the plugin
need only a restart — the installed copy is a plain file; if you would
rather not copy it, symlink it instead:

```bash
ln -sf "$PWD/clients/opencode/plugin/speakd.js" ~/.config/opencode/plugins/speakd.js
```

The installer writes OpenCode v1's config shape (`mcp.speakd` with
`enabled`). On OpenCode v2 the same entry lives under `mcp.servers.speakd`
and `enabled` becomes `disabled: false` — see the main README and the
spec's *V2 readiness*; the plugin file itself needs no change.

## Start the daemon

```bash
speakd &
```

Nothing breaks if you don't: with no daemon listening, every send fails
quietly inside the plugin's two-second budget, and the session runs on in
silence — which is also what you get by simply not starting it on a day you
don't want speech. What the plugin could not say is written to OpenCode's
plugin log (`client.app.log`), not to OpenCode's transcript.

## What gets spoken, and when

Every `text` part the main agent writes, enqueued whole when the part
finishes — earlier than Claude Code, where a whole message flushes at once.
Parts are never re-spoken, sub-agent output is never spoken, and parts
OpenCode itself ignores (outside its context display) are skipped.
```

- [ ] **Step 5: The main README and handoff**

In `README.md`:

1. In the **Clients** section, after the Claude Code paragraph (line ~499), add:

```markdown
**OpenCode** — [`clients/opencode/`](clients/opencode/) is a plugin that
speaks a session's text blocks as they finish, stops the speech on a new
prompt, and reads the permission prompts aloud — the same three jobs the
Claude Code hooks do, from inside OpenCode's own plugin runtime. Install it
once with `clients/opencode/install.sh`; the `speakd-mcp` server comes with
it, so sessions brief and switch modes exactly as Claude Code's do. See its
[README](clients/opencode/README.md).
```

2. In the **Briefings** section, where other agents are told to register the MCP server manually (lines ~188–196), adjust to:

```markdown
The Claude Code plugin ships the MCP server (`clients/claude-code/.mcp.json`),
and the OpenCode installer writes the equivalent entry for OpenCode. For
other agents, register it once:
```

3. In the **Briefings** section, "New Claude Code sessions start brief and
muted" — extend to cover OpenCode, adding one example line:

```markdown
**New agent sessions start brief and muted** — Claude Code and OpenCode
alike. Unmute the ones you want to hear; switch one to full when you are
following it closely:

```bash
uv run speakctl mode full --source claude-code:<session>   # every response
uv run speakctl mode full --source opencode:<session>      # same, for OpenCode
```

In `docs/HANDOFF.md`, the table row for OpenCode — currently "For Codex and
OpenCode, the README documents the one-line MCP registration" — gains its
own row: `OpenCode | plugin in ~/.config/opencode/plugins/ + mcp entry in
opencode.json, via clients/opencode/install.sh; sessions start brief and
muted like Claude Code | speakctl mode full --source opencode:<id>`.

- [ ] **Step 6: Full suite, lint, typecheck**

```bash
uv run pytest -q && uv run ruff check . && uv run ruff format --check . && uv run mypy src tests
cd clients/opencode && npm test
```

Expected: everything passes.

- [ ] **Step 7: Commit**

```bash
git add clients/opencode README.md docs/HANDOFF.md
git commit -m "Install opencode support with a script, and document it"
```

---

## End-to-end check (manual, after all tasks)

```bash
./packaging/systemd/install-service.sh   # or: uv run speakd &
clients/opencode/install.sh
opencode            # start a session in a scratch directory
```

- Ask the session to do something small; hear the response as its text blocks
  finish, and a hush the moment the next prompt is submitted.
- Unmute the channel in the window (or `uv run speakctl unmute --source
  opencode:<id>`), and ask it to brief you: `brief` arrives over MCP.
- Trigger a permission prompt (a Bash tool with ask-permissions on): hear
  "OpenCode needs your permission."
- Confirm `uv run speakctl status` shows the channel as `opencode:<id>`,
  brief and muted by default, with a label from the session title.
- `tail -f ~/.local/state/speakd/opencode/` shows one `.session.json` per
  session, rewritten on every prompt.

## Phase D — OpenCode v2 (notes only; do not implement until the upgrade)

OpenCode v2 rewrote the plugin system: v1 plugin implementations do not run
in v2. Everything below is verified against the published v2 docs and SDK
types as of 2026-09-25; the v2 plugin API is still churning, so port then,
not now.

- [ ] **The entry shim only.** The `Speaker`, `Signals`, socket and
      registration code port unchanged. The v2 entry is a default export
      with an `id` and a `setup(ctx)` that registers:
  - `event` → `ctx.event.subscribe()`, same event names (`message.updated`,
    `message.part.updated`, `permission.asked`, `session.status`,
    `session.created`/`updated`), re-read payloads defensively (`part` now
    arrives as `{sessionID, part, time}`).
  - `chat.message` → `ctx.session.hook("prompt", …)` (fires before durable
    prompt admission — the hush/register point).
- [ ] **Config:** move the MCP entry to `mcp.servers.speakd` with
      `disabled: false` (or nothing) instead of `enabled: true`; plugin
      discovery under `~/.config/opencode/plugins/` is unchanged, and the v1
      `plugin` config key auto-migrates to `plugins` if one is ever used.
- [ ] **Verify** `permission.asked` vs `permission.v2.asked` and
      `question.asked` payloads on the target v2 release before relying on
      them; `speakd-mcp`'s 2025-06-18 handshake is within v2's `legacy`
      range, no server change expected.
- [ ] **Speak questions too (optional):** v2 adds `question.asked`
      (`{sessionID, questions[]}`) — the natural extension of the
      permission alert, same `attention` kind.
