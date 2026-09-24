# Briefings and a Ranked Channel List — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Agents brief the user by voice through an MCP server, sessions switch between brief and full narration, and the window's channel list is ranked by relevance with stars and a fold for muted channels.

**Architecture:** The daemon gains per-channel `last_output`, `briefs`, `mode` and `briefed_this_turn`, and gates each enqueue by its `kind` against the mode. A hand-written stdio MCP server (`speakd-mcp`) gives agents `brief`, `briefing_status` and `set_mode`, and finds its Claude Code session through process ancestry recorded by the prompt hook. Hooks backstop what an agent cannot report itself. The window ranks, stars and folds channels and shows a brief/full toggle.

**Tech Stack:** Python 3.11+ (stdlib only for the MCP server), pytest via `uv run pytest`, vanilla JS in the Tauri webview.

**Spec:** `docs/superpowers/specs/2026-09-24-briefings-and-channel-list-design.md`

## Global Constraints

- No new runtime dependencies; the MCP server is stdlib JSON-RPC over stdio.
- Kinds: `response`, `brief`, `attention`. Modes: `full`, `brief`. Mute overrides every kind.
- Mode × kind: full speaks `response` and `attention`; brief speaks `brief` and `attention`; declines are reasoned `full mode` / `brief mode`.
- `claude-code:<id>` main channels (exactly one `:`) start `briefs=True`, `mode="brief"`, `muted=True`.
- `last_output` is `time.time()` of the last enqueue attempt, spoken or not.
- Hooks import only the lean `speakd.clients.send` path, never `speakd.cli`.
- The window builds DOM with `textContent` only; `localStorage` access is wrapped in try/catch.
- Commits end with the session's attribution lines.

## Review Focus

- A malformed JSON line on the MCP server's stdin should produce JSON-RPC error -32700, and the server should keep serving. Tested in Task 5.
- The MCP server's stdin reaching EOF (the agent exits) should end the server cleanly with exit 0, not hang. Tested in Task 5.
- An unknown tool name or a `brief` without `text` should come back as a tool result with `isError: true`, not a crash or protocol error. Tested in Task 5.
- A `brief` with blank text should not be spoken, and the answer should say so rather than raise. Tested in Task 5.
- A channel that has never produced output should sort last in its group and show no time rather than "NaN" or "55 years". Tested in Task 2.

---

## Phase B — ranked channel list

### Task 1: `last_output` on channels

**Files:**
- Modify: `src/speakd/channels.py` (field), `src/speakd/daemon.py` (`_enqueue`, status, `declined`/`queued` payloads)
- Test: `tests/test_channel_activity.py` (create)

**Interfaces:**
- Produces: `Channel.last_output: float = 0.0`; `status.channels[].last_output`; `at: float` in `queued` and `declined` event data.

- [ ] **Step 1: Failing tests**

```python
"""Tests for when each channel last tried to speak."""

import time

from speakd.channels import ChannelTable
from speakd.daemon import Daemon, ProfileView
from speakd.events import Event, EventBus
from speakd.player import RecordingPlayer
from speakd.protocol import Request, Verb
from speakd.synth.fake import FakeEngine


def build() -> Daemon:
    d = Daemon(
        FakeEngine(),
        RecordingPlayer(),
        lambda name: ProfileView(voice="v", speed=1.0, interrupt_on=(), prepare=lambda p: (list(p), [])),
        bus=EventBus(),
        channels=ChannelTable(),
    )
    d.start()
    return d


def say(d: Daemon, source: str) -> None:
    d.handle(Request(verb=Verb.ENQUEUE, source_id=source, payload={"text": "Hi.", "kind": "response"}))


def channel(d: Daemon, source: str) -> dict:  # type: ignore[type-arg]
    return next(c for c in d.handle(Request(verb=Verb.STATUS, source_id="")).data["channels"] if c["source_id"] == source)


def test_speaking_records_when() -> None:
    d = build()
    try:
        before = time.time()
        say(d, "a")
        assert before <= channel(d, "a")["last_output"] <= time.time()
    finally:
        d.stop()


def test_a_muted_attempt_still_counts() -> None:
    d = build()
    try:
        d.handle(Request(verb=Verb.MUTE, source_id="a", payload={"muted": True}))
        say(d, "a")
        assert channel(d, "a")["last_output"] > 0
    finally:
        d.stop()


def test_a_channel_that_never_spoke_reports_zero() -> None:
    d = build()
    try:
        d.handle(Request(verb=Verb.SET_LABEL, source_id="quiet", payload={"label": "Quiet"}))
        assert channel(d, "quiet")["last_output"] == 0.0
    finally:
        d.stop()


def test_events_carry_the_time() -> None:
    d = build()
    seen: list[Event] = []
    d.bus.subscribe(seen.append)
    try:
        say(d, "a")
        d.handle(Request(verb=Verb.MUTE, source_id="b", payload={"muted": True}))
        say(d, "b")
        assert d.wait_idle(timeout=5.0)
        assert all(isinstance(e.data.get("at"), float) for e in seen if e.kind in ("queued", "declined"))
        assert any(e.kind == "declined" for e in seen)
    finally:
        d.stop()
```

- [ ] **Step 2: Run** `uv run pytest tests/test_channel_activity.py -q` and expect failures.
- [ ] **Step 3: Implement.**
  - `Channel` gains `last_output: float = 0.0` with a comment saying it counts attempts, not only speech.
  - `ChannelTable.touch(source_id: str) -> float` sets `time.time()` under the lock and returns it.
  - In `Daemon._enqueue`, right after `channel = self.channels.open(...)`, add `at = self.channels.touch(request.source_id)` and pass `at` to `_refusal(..., at=at)`. Every `declined` payload there gains `"at": at`.
  - `_accept(job, at=...)` adds `"at": at` to `queued`.
  - Replay (`_replay`) calls `touch` on the replayed channel too.
  - The status channel dict gains `"last_output": c.last_output`.
- [ ] **Step 4: Run** `uv run pytest tests/test_channel_activity.py tests/test_daemon.py tests/test_mute.py tests/test_replay.py -q` and expect a pass.
- [ ] **Step 5: Commit** "Record when each channel last tried to speak".

### Task 2: The window ranks, stars and folds channels

**Files:**
- Modify: `clients/gui/pinned.html` (`renderChannels`, CSS, handlers), `clients/gui/shared/speakd-source.js` (simulation channels carry `last_output`; `queued`/`declined` carry `at`)

**Interfaces:**
- Consumes: `status.channels[].last_output`, `at` on `queued`/`declined`.

- [ ] **Step 1: Simulation.**
  - The `SimulatedSource` channels gain `last_output` (fixture: now − 20 s; resem: now − 3600 s; kronikk: 0), and `muted: true` on kronikk so there is something to fold.
  - `say()` and `_speak()` set the channel's `last_output = Date.now() / 1000`.
  - `queued` and `declined` carry `at`.
- [ ] **Step 2: Controller.**
  - `handleEvent` on `queued`/`declined` with `data.at` sets that channel's `last_output` and calls `renderChannels()`.
  - Stars: `const stars = loadStars()`, a `Set` from `localStorage["speakd.stars"]` (JSON array), with try/catch returning an empty set. `saveStars()` does the same in reverse.
  - `rank(channels)`: star first (0/1), then audible before muted (`channel.muted || globalMuted`), then `last_output` descending (0 last), then label.
  - `renderChannels()` renders the ranked list. Rows that are muted and not starred go into a `<details class="fold" id="fold">` placed after the others, with the summary `N muted`; its `open` is kept in a module variable `foldOpen` across rebuilds.
- [ ] **Step 3: Row additions**, all `textContent`:
  - A star button (`☆`/`★`, `aria-pressed`, title "Keep at the top") placed before the name.
  - An `.ago` span after the name. `ago(ts)` returns `""` for 0, `"now"` under 60 s, then `"Nm"`, `"Nh"`, `"Nd"`.
  - `renderChannels` re-runs every 30 s (a `setInterval`) so the times stay honest.
- [ ] **Step 4: CSS** in the channel-row vocabulary: `.chan-row .star` borderless, faint, lit when pressed. `.chan-row .ago` mono 9px faint with `flex: none`. `.fold > summary` styled like `.upnext-head`, clickable.
- [ ] **Step 5: Browser check** (Playwright script in the scratchpad, system Chrome, `python3 -m http.server 8765 --directory clients/gui`). Open the disclosure and assert:
  - the order is fixture (20 s) and then resem (1 h);
  - kronikk is inside the closed fold, which reads "1 muted", and has an empty `.ago` (the Review Focus line);
  - starring resem moves it first and survives a reload;
  - there are no page errors.
- [ ] **Step 6: Commit** "Rank the channel list by relevance, with stars and a fold for muted ones".

## Phase A — briefings

### Task 3: Modes, kinds, capability and turn tracking in the daemon

**Files:**
- Modify: `src/speakd/channels.py`, `src/speakd/protocol.py`, `src/speakd/daemon.py`, `src/speakd/__main__.py` (`build_channels`)
- Test: `tests/test_briefing_modes.py` (create), `tests/test_channels.py` (update the default-mute test)

**Interfaces:**
- Produces:
  - `Channel.briefs: bool = False`, `Channel.mode: str = "full"`, `Channel.briefed_this_turn: bool = False`.
  - `Verb.SET_MODE = "set_mode"` with `{mode}` → `{mode, briefs}`, and `Verb.SET_CAPABILITIES = "set_capabilities"` with `{briefs}` → `{briefs, mode}`.
  - The event `mode {mode, briefs}`.
  - Status channels gain `briefs`, `mode` (effective) and `briefed_this_turn`.
  - Enqueue payload flags: `only_in_mode` and `unless_briefed`.
  - `ChannelTable(defaults: Callable[[str], dict[str, object]] | None)` replaces `muted_by_default`.
  - `build_channels()` returns the claude-code defaults.
  - `effective_mode(channel) -> str` in `channels.py`.

- [ ] **Step 1: Failing tests**

```python
"""Tests for brief and full modes, and what each lets through."""

import pytest

from speakd.channels import ChannelTable
from speakd.daemon import Daemon, ProfileView
from speakd.events import Event, EventBus
from speakd.player import RecordingPlayer
from speakd.protocol import Request, Response, Verb
from speakd.synth.fake import FakeEngine


def build() -> tuple[Daemon, RecordingPlayer, list[Event]]:
    player = RecordingPlayer()
    d = Daemon(
        FakeEngine(),
        player,
        lambda name: ProfileView(voice="v", speed=1.0, interrupt_on=(), prepare=lambda p: (list(p), [])),
        bus=EventBus(),
        channels=ChannelTable(),
    )
    seen: list[Event] = []
    d.bus.subscribe(seen.append)
    d.start()
    return d, player, seen


def req(d: Daemon, verb: Verb, source: str = "s", **payload: object) -> Response:
    return d.handle(Request(verb=verb, source_id=source, payload=dict(payload)))


def say(d: Daemon, kind: str, text: str = "Hello there.", **flags: object) -> Response:
    return req(d, Verb.ENQUEUE, text=text, kind=kind, **flags)


@pytest.mark.parametrize(
    ("mode", "kind", "spoken"),
    [
        ("full", "response", True), ("full", "brief", False), ("full", "attention", True),
        ("brief", "response", False), ("brief", "brief", True), ("brief", "attention", True),
    ],
)
def test_what_each_mode_lets_through(mode: str, kind: str, spoken: bool) -> None:
    d, _, _ = build()
    try:
        req(d, Verb.SET_CAPABILITIES, briefs=True)
        req(d, Verb.SET_MODE, mode=mode)
        response = say(d, kind)
        assert response.data["spoken"] is spoken
        if not spoken:
            assert response.data["reason"] == f"{mode} mode"
    finally:
        d.stop()


def test_a_channel_without_a_briefer_is_always_full() -> None:
    d, _, _ = build()
    try:
        assert not req(d, Verb.SET_MODE, mode="brief").ok
        assert say(d, "response").data["spoken"] is True
    finally:
        d.stop()


def test_set_mode_refuses_nonsense() -> None:
    d, _, _ = build()
    try:
        req(d, Verb.SET_CAPABILITIES, briefs=True)
        assert not req(d, Verb.SET_MODE, mode="loud").ok
        assert not req(d, Verb.SET_CAPABILITIES, briefs="yes").ok
    finally:
        d.stop()


def test_mode_changes_are_announced_and_reported() -> None:
    d, _, seen = build()
    try:
        req(d, Verb.SET_CAPABILITIES, briefs=True)
        req(d, Verb.SET_MODE, mode="brief")
        assert [e.data for e in seen if e.kind == "mode"][-1] == {"mode": "brief", "briefs": True}
        ch = req(d, Verb.STATUS, source="").data["channels"][0]
        assert (ch["briefs"], ch["mode"]) == (True, "brief")
    finally:
        d.stop()


def test_a_brief_is_spoken_with_the_label_in_front() -> None:
    d, player, seen = build()
    try:
        req(d, Verb.SET_LABEL, label="ReSem paper")
        req(d, Verb.SET_CAPABILITIES, briefs=True)
        req(d, Verb.SET_MODE, mode="brief")
        say(d, "brief", "Tests pass.")
        assert d.wait_idle(timeout=5.0)
        started = next(e for e in seen if e.kind == "started")
        assert started.data["text"] == "ReSem paper: Tests pass."
    finally:
        d.stop()


def test_a_fallback_is_silent_once_the_agent_has_briefed() -> None:
    d, _, _ = build()
    try:
        req(d, Verb.SET_CAPABILITIES, briefs=True)
        req(d, Verb.SET_MODE, mode="brief")
        assert say(d, "attention", "finished", unless_briefed=True).data["spoken"] is True
        say(d, "brief", "Done.")
        again = say(d, "attention", "finished", unless_briefed=True)
        assert again.data == {"spoken": False, "reason": "already briefed"}
        req(d, Verb.HUSH)  # the next prompt
        assert say(d, "attention", "finished", unless_briefed=True).data["spoken"] is True
    finally:
        d.stop()


def test_only_in_mode_declines_in_the_other_mode() -> None:
    d, _, _ = build()
    try:
        req(d, Verb.SET_CAPABILITIES, briefs=True)
        req(d, Verb.SET_MODE, mode="full")
        response = say(d, "attention", "finished", only_in_mode="brief")
        assert response.data == {"spoken": False, "reason": "full mode"}
    finally:
        d.stop()


def test_claude_code_sessions_start_brief_and_muted() -> None:
    from speakd.__main__ import build_channels

    table = build_channels()
    main = table.open("claude-code:abc")
    assert (main.briefs, main.mode, main.muted) == (True, "brief", True)
    other = table.open("notify:whatsapp")
    assert (other.briefs, other.mode, other.muted) == (False, "full", False)
    assert table.open("claude-code:abc:notify").briefs is False
```

  Update `tests/test_channels.py`:
  - Rewrite the three `muted_by_default` tests to use `ChannelTable(defaults=lambda s: {"muted": True} if ... else {})` with the same assertions.
  - Delete `test_the_daemon_mutes_claude_code_sessions_until_chosen`, which the new test above supersedes.
- [ ] **Step 2: Run** `uv run pytest tests/test_briefing_modes.py tests/test_channels.py -q` and expect failures.
- [ ] **Step 3: Implement.**
  - **`channels.py`:**
    - Add the three fields.
    - Add `def effective_mode(channel: Channel) -> str: return channel.mode if channel.briefs else "full"`.
    - `ChannelTable.__init__(self, defaults=None)` applies `defaults(source_id)` (a dict of field → value) with `setattr` on first open only.
    - Add `set_mode`, `set_briefs`, `mark_briefed(source, value)` helpers, each under the lock.
  - **`__main__.build_channels`:** `ChannelTable(defaults=_claude_code_defaults)` with `def _claude_code_defaults(source): return {"briefs": True, "mode": "brief", "muted": True} if source.startswith("claude-code:") and source.count(":") == 1 else {}`, and its docstring updated.
  - **`protocol.py`:** add `SET_MODE` and `SET_CAPABILITIES`.
  - **`daemon.py`:**
    - `handle` gets a `SET_MODE` branch: validate `mode in ("full", "brief")`; refuse with "this channel has no briefer" when the channel (opened) has `briefs` false; set it and publish `mode`.
    - `handle` gets a `SET_CAPABILITIES` branch: validate a bool; set it; publish `mode` with the effective mode.
    - `_enqueue`: after the mute refusal and before `decide`, call `mode_refusal = self._mode_refusal(channel, kind, payload, text, at)`. It returns a declined `Response` for:
      - kind `response` in effective brief mode ("brief mode");
      - kind `brief` in effective full mode ("full mode");
      - `only_in_mode` set and not equal to the effective mode ("<effective> mode");
      - `unless_briefed` true while `channel.briefed_this_turn` ("already briefed").
    - Accepting a `brief` sets `briefed_this_turn = True`.
    - For kinds `brief` and `attention`, when `channel.label` is non-empty, the job text becomes `f"{channel.label}: {text}"`: pass `prefix=f"{channel.label}:"` into `_Job` (the existing prefix path joins with a space).
    - HUSH/CANCEL with a non-empty source clears that channel's `briefed_this_turn`.
    - Status channels gain `"briefs": c.briefs, "mode": effective_mode(c), "briefed_this_turn": c.briefed_this_turn`.
- [ ] **Step 4: Run** `uv run pytest tests/test_briefing_modes.py tests/test_channels.py tests/test_daemon.py tests/test_mute.py tests/test_queue.py tests/test_scheduler.py -q` and expect a pass.
- [ ] **Step 5: Commit** "Give channels a brief and a full mode, and gate speech by kind".

### Task 4: `speakctl mode`

**Files:** Modify `src/speakd/cli.py`. Test `tests/test_cli.py` (append).

- [ ] **Step 1: Failing test**

```python
def test_mode_sends_set_mode(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from speakd.protocol import Response, Verb

    sent = _fake_call(monkeypatch, Response(ok=True, data={"mode": "full", "briefs": True}))
    assert main(["mode", "full", "--source", "claude-code:abc"]) == 0
    assert (sent[0].verb, sent[0].source_id, sent[0].payload) == (Verb.SET_MODE, "claude-code:abc", {"mode": "full"})
    assert main(["mode", "loud"]) != 0
```

- [ ] **Step 2: Run** and expect a failure.
- [ ] **Step 3: Implement.**
  - The parser: `mode = sub.add_parser("mode", parents=[common], help="brief or full narration for a channel")` with `mode.add_argument("mode", metavar="{brief,full}")`.
  - `_mode(args)` validates the value (a message and `_UNREACHABLE` on anything else), sends it, and prints nothing on success.
  - Route it from `main`.
- [ ] **Step 4: Run** `uv run pytest tests/test_cli.py -q` and expect a pass.
- [ ] **Step 5: Commit** "Let speakctl switch a channel between brief and full".

### Task 5: `speakd-mcp`

**Files:**
- Create: `src/speakd/clients/mcp/__init__.py` (empty docstring), `src/speakd/clients/mcp/server.py`, `src/speakd/clients/mcp/session.py`, `src/speakd/clients/mcp/guide.py`
- Modify: `pyproject.toml` (`speakd-mcp = "speakd.clients.mcp.server:main"`)
- Test: `tests/test_mcp.py`

**Interfaces:**
- Consumes: `speakd.clients.send.send(Request, socket=, timeout=) -> str | None`. It needs the response data, so `send.py` gains `call(request, *, socket=None, timeout=TIMEOUT) -> Response | str` (the Response, or an error string), and `send` is re-expressed on top of it.
- Produces:
  - `session.resolve(client_name: str, *, proc: Path = Path("/proc"), cwd: str | None = None) -> tuple[str, str | None]` returning (source_id, label or None). A registry match gives `("claude-code:<id>", None)`.
  - `guide.text(config_dir: Path | None = None) -> str`.
  - `server.Server(socket: Path | None, proc: Path)` with `.handle(message: dict) -> dict | None`, and `server.main(argv) -> int`.

- [ ] **Step 1: Failing tests**

```python
"""Tests for the MCP server agents brief through."""

import json
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from speakd.channels import ChannelTable
from speakd.clients.mcp import guide, session
from speakd.daemon import Daemon, ProfileView
from speakd.events import EventBus
from speakd.player import RecordingPlayer
from speakd.synth.fake import FakeEngine
from speakd.transport import SocketServer


@pytest.fixture
def daemon_socket(tmp_path: Path):  # type: ignore[no-untyped-def]
    d = Daemon(
        FakeEngine(), RecordingPlayer(),
        lambda n: ProfileView(voice="v", speed=1.0, interrupt_on=(), prepare=lambda p: (list(p), [])),
        bus=EventBus(), channels=ChannelTable(),
    )
    d.start()
    path = Path("/tmp") / f"speakd-mcp-test-{id(d)}.sock"  # AF_UNIX path limit
    server = SocketServer(path, d.handle, d.bus)
    server.start()
    yield path, d
    server.stop()
    d.stop()


def rpc(proc: subprocess.Popen, method: str, params: dict | None = None, id_: int = 1) -> dict:  # type: ignore[type-arg]
    proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": id_, "method": method, "params": params or {}}) + "\n")
    proc.stdin.flush()
    return json.loads(proc.stdout.readline())


def spawn(socket: Path, tmp_path: Path) -> subprocess.Popen:  # type: ignore[type-arg]
    env = {"PATH": "/usr/bin:/bin", "XDG_CONFIG_HOME": str(tmp_path / "cfg"), "SPEAKD_STATE_DIR": str(tmp_path / "st")}
    return subprocess.Popen(
        [sys.executable, "-m", "speakd.clients.mcp.server", "--socket", str(socket)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, env=env,
    )


def test_initialize_hands_over_the_guide(daemon_socket, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    socket, _ = daemon_socket
    proc = spawn(socket, tmp_path)
    try:
        result = rpc(proc, "initialize", {"protocolVersion": "2025-06-18", "clientInfo": {"name": "codex"}})["result"]
        assert result["serverInfo"]["name"] == "speakd"
        assert "brief" in result["instructions"]
        tools = rpc(proc, "tools/list", id_=2)["result"]["tools"]
        assert {t["name"] for t in tools} == {"brief", "briefing_status", "set_mode"}
    finally:
        proc.stdin.close(); proc.wait(timeout=5)


def test_brief_answers_with_the_mode(daemon_socket, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    socket, d = daemon_socket
    proc = spawn(socket, tmp_path)
    try:
        rpc(proc, "initialize", {"clientInfo": {"name": "codex"}})
        out = rpc(proc, "tools/call", {"name": "brief", "arguments": {"text": "Tests pass.", "kind": "done"}}, 2)["result"]
        assert out["isError"] is False
        assert out["structuredContent"]["mode"] == "brief"
        assert out["structuredContent"]["spoken"] is True
        channels = {c["source_id"]: c for c in d.handle_status_channels()} if hasattr(d, "handle_status_channels") else None
        status = rpc(proc, "tools/call", {"name": "briefing_status", "arguments": {}}, 3)["result"]["structuredContent"]
        assert status["briefs"] is True and "guide" in status
        switched = rpc(proc, "tools/call", {"name": "set_mode", "arguments": {"mode": "full"}}, 4)["result"]
        assert switched["structuredContent"]["mode"] == "full"
        again = rpc(proc, "tools/call", {"name": "brief", "arguments": {"text": "More.", "kind": "done"}}, 5)["result"]
        assert again["structuredContent"]["spoken"] is False
    finally:
        proc.stdin.close(); proc.wait(timeout=5)


def test_blank_unknown_and_malformed_do_not_crash(daemon_socket, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    socket, _ = daemon_socket
    proc = spawn(socket, tmp_path)
    try:
        rpc(proc, "initialize", {"clientInfo": {"name": "codex"}})
        blank = rpc(proc, "tools/call", {"name": "brief", "arguments": {"text": "  ", "kind": "done"}}, 2)["result"]
        assert blank["structuredContent"]["spoken"] is False
        assert rpc(proc, "tools/call", {"name": "nope", "arguments": {}}, 3)["result"]["isError"] is True
        assert rpc(proc, "tools/call", {"name": "brief", "arguments": {}}, 4)["result"]["isError"] is True
        proc.stdin.write("{not json\n"); proc.stdin.flush()
        assert json.loads(proc.stdout.readline())["error"]["code"] == -32700
        assert rpc(proc, "no/such/method", {}, 5)["error"]["code"] == -32601
        assert "result" in rpc(proc, "ping", {}, 6)
    finally:
        proc.stdin.close()
    assert proc.wait(timeout=5) == 0  # EOF ends it cleanly


def test_no_daemon_is_an_answer_not_an_error(tmp_path: Path) -> None:
    proc = spawn(tmp_path / "absent.sock", tmp_path)
    try:
        rpc(proc, "initialize", {"clientInfo": {"name": "codex"}})
        out = rpc(proc, "tools/call", {"name": "brief", "arguments": {"text": "Hi.", "kind": "done"}}, 2)["result"]
        assert out["isError"] is False
        assert out["structuredContent"]["spoken"] is False
        assert "not running" in out["structuredContent"]["reason"]
    finally:
        proc.stdin.close(); proc.wait(timeout=5)


def fake_proc(root: Path, chain: list[tuple[int, str, int]]) -> Path:
    """Write /proc/<pid>/stat and comm for a chain of (pid, comm, ppid)."""
    for pid, comm, ppid in chain:
        (root / str(pid)).mkdir(parents=True)
        (root / str(pid) / "stat").write_text(f"{pid} ({comm}) S {ppid} 0 0\n")
        (root / str(pid) / "comm").write_text(comm + "\n")
    (root / "self").symlink_to(root / str(chain[0][0]))
    return root


def test_a_claude_code_session_is_found_by_ancestry(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from speakd.clients.claude_code import registry

    monkeypatch.setenv("SPEAKD_STATE_DIR", str(tmp_path / "st"))
    proc = fake_proc(tmp_path / "proc", [(300, "python3", 200), (200, "claude", 100), (100, "zsh", 1)])
    registry.register("abc", Path("/t.jsonl"), "/work", claude_pid=200)
    assert session.resolve("claude-code", proc=proc) == ("claude-code:abc", None)


def test_anything_else_gets_a_channel_of_its_own(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("SPEAKD_STATE_DIR", str(tmp_path / "st"))
    proc = fake_proc(tmp_path / "proc", [(300, "python3", 250), (250, "codex", 1)])
    assert session.resolve("codex", proc=proc, cwd="/home/u/kronikk") == ("agent:codex:250", "codex · kronikk")


def test_the_guide_is_the_users_file_when_there_is_one(tmp_path: Path) -> None:
    assert "brief" in guide.text(tmp_path)
    (tmp_path / "speakd").mkdir()
    (tmp_path / "speakd" / "briefing.md").write_text("Only tell me when it is done.")
    assert guide.text(tmp_path) == "Only tell me when it is done."
```

  (Remove the unused `channels = …` line and `threading` import when writing the file. It is shown here only to flag that status is read through `briefing_status`, not the daemon directly.)
- [ ] **Step 2: Run** `uv run pytest tests/test_mcp.py -q` and expect failures.
- [ ] **Step 3: `send.call`.**
  - In `send.py`, factor the body of `send` into `call(request, *, socket=None, timeout=TIMEOUT) -> Response | str`, which returns the `Response` or the error string.
  - `send` becomes: `r = call(...)`; if `r` is a str, return it; if not `r.ok`, return the refused-message; otherwise return None.
  - Existing tests must stay green.
- [ ] **Step 4: `guide.py`**

```python
"""The standing briefing guide every agent is handed."""

from __future__ import annotations

import os
from pathlib import Path

DEFAULT = """Brief the user by voice with the `brief` tool. They are usually doing something else and listening, not reading.
- When you finish a task: one or two sentences of outcome (kind "done").
- When you are blocked, something failed, or you had to change plan: what and why ("problem").
- When you need a decision or answer from them: the question, then end your turn ("question").
- On tasks longer than about ten minutes: a sentence at real milestones ("progress"). Never narrate routine steps, file edits or tool calls.
- Under about 25 words. No markdown, no code, no paths unless essential.
- Follow what the user asks for in this conversation over this guide.
- Call `briefing_status` if unsure of the mode; in "full" mode everything is already read aloud, so do not brief."""


def text(config_dir: Path | None = None) -> str:
    """The user's guide from `<config>/speakd/briefing.md`, or the default."""
    base = config_dir or Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    try:
        found = (base / "speakd" / "briefing.md").read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return DEFAULT
    return found or DEFAULT
```

- [ ] **Step 5: `session.py`.**
  - `_ancestors(proc, start="self", limit=8) -> list[tuple[int, str]]` reads `stat` (the ppid is the field after the `)`; the comm comes from the `comm` file) and never raises.
  - `resolve(client_name, *, proc, cwd)`:
    - Build a map from `registry.live()` of `claude_pid` to session id. `Registration` gains `claude_pid: int | None`.
    - Return the first ancestor pid in that map as `(f"claude-code:{sid}", None)`.
    - Otherwise use the nearest ancestor that is not the process itself and not `python`/`python3`/`uv`/`sh`/`bash`/`node`, falling back to the direct parent. Return `(f"agent:{client_name}:{pid}", f"{client_name} · {Path(cwd or os.getcwd()).name}")`.
- [ ] **Step 6: `registry.register`** gains the keyword `claude_pid: int | None = None`, written to the file and read back by `live()`. Existing callers are unchanged.
- [ ] **Step 7: `server.py`**, stdlib only:
  - **Loop:** `main()` parses `--socket`, builds `Server`, and loops `for line in sys.stdin`. A line that is not JSON gets `{"jsonrpc":"2.0","id":null,"error":{"code":-32700,"message":"parse error"}}`. Otherwise `reply = server.handle(msg)`, which is written as one line and flushed when not None. EOF returns 0.
  - **Methods in `handle`:**
    - `initialize` stores `clientInfo.name` and answers `{"protocolVersion": params.get("protocolVersion", "2025-06-18"), "capabilities": {"tools": {}}, "serverInfo": {"name": "speakd", "version": "0.1"}, "instructions": PREAMBLE + "\n\n" + guide.text()}`.
    - `notifications/*` return None.
    - `ping` returns `{}`.
    - `tools/list` returns three tool schemas (JSON Schema; `brief` requires `text` and `kind` with enum done/problem/question/progress; `set_mode` requires `mode` with enum full/brief).
    - `tools/call` dispatches by name.
    - Anything else returns -32601.
  - **Tool results:** every tool returns `{"content":[{"type":"text","text": summary}], "structuredContent": data, "isError": bool}`. Unknown tools and missing arguments return `isError: true`, with the problem in the text.
  - **Channel:** `_channel()` resolves lazily through `session.resolve`, and retries on every call while the answer is not `claude-code:`.
  - **First contact:** on first reaching the daemon it sends `set_capabilities {briefs:true}` and, when there is a label, `set_label`.
  - **`brief`:**
    - Blank text gives `{spoken: False, reason: "nothing to say"}`.
    - Otherwise it sends ENQUEUE `{text, kind:"brief", brief_kind}` and then reads STATUS for that channel's `mode` and `muted`.
    - It answers `{spoken, mode, muted, reason?}`. An error string from `call` becomes `{spoken: False, reason: "speakd is not running"}` with `isError: False`.
  - **`briefing_status`:** STATUS for the channel, answering `{mode, muted, briefs, guide}`.
  - **`set_mode`:** SET_MODE, then answers as `briefing_status` does.
  - `PREAMBLE` is two sentences naming the tools and the rule that full mode reads everything aloud.
  - `if __name__ == "__main__": raise SystemExit(main())`.
- [ ] **Step 8: Script entry.** Add `speakd-mcp` to `[project.scripts]`, then run `uv sync`.
- [ ] **Step 9: Run** `uv run pytest tests/test_mcp.py tests/test_client_send.py tests/test_cc_registry.py tests/test_registry.py -q` and expect a pass.
- [ ] **Step 10: Commit** "Let agents brief the user through an MCP server".

### Task 6: Hooks and the plugin

**Files:**
- Modify: `src/speakd/clients/claude_code/hook.py`, `src/speakd/clients/send.py` (`enqueue` gains `kind` and `**flags`), `clients/claude-code/hooks/hooks.json` (add `Stop`)
- Create: `clients/claude-code/.mcp.json`
- Test: `tests/test_cc_hook.py` (update and append)

**Interfaces:**
- Consumes: `registry.register(..., claude_pid=)`, `send.enqueue(source, text, *, kind="response", flags=None, ...)`.

- [ ] **Step 1: Tests.**
  - Change `run`'s `fake_enqueue` to record `(source, text, kw.get("kind"), kw.get("flags"))`, and update existing asserts to match.
  - Replace `test_a_notification_speaks_on_the_notify_channel` with:

```python
def test_a_notification_is_an_attention_on_the_main_channel(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("SPEAKD_STATE_DIR", str(tmp_path / "state"))
    sent: list = []
    body = payload(hook_event_name="Notification", message="Claude needs your permission.")
    assert run(monkeypatch, body, sent) == 0
    assert sent == [("claude-code:s1", "Claude needs your permission.", "attention", None)]
```

  - Replace `test_a_prompt_hushes_both_channels` with one asserting a single hush on `claude-code:s1`.
  - Replace `test_stop_no_longer_speaks` with:

```python
def test_stop_says_finished_unless_the_agent_briefed(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("SPEAKD_STATE_DIR", str(tmp_path / "state"))
    sent: list = []
    assert run(monkeypatch, payload(hook_event_name="Stop"), sent) == 0
    assert sent == [("claude-code:s1", "finished", "attention", {"unless_briefed": True, "only_in_mode": "brief"})]


def test_a_prompt_records_the_claude_process(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("SPEAKD_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(hook, "_claude_pid", lambda: 4242)
    run(monkeypatch, payload(hook_event_name="UserPromptSubmit"), [])
    assert [r.claude_pid for r in registry.live()] == [4242]
```

- [ ] **Step 2: Run** `uv run pytest tests/test_cc_hook.py -q` and expect failures.
- [ ] **Step 3: Implement.**
  - **`send.enqueue`** gains `kind: str = "response"` and `flags: dict[str, object] | None = None`, merged into the payload.
  - **`hook.py`:**
    - `_claude_pid()` walks `/proc/self` ancestors (reusing `speakd.clients.mcp.session._ancestors`; that module imports only stdlib and registry, so the hook stays lean) to the first comm `claude`, falling back to the grandparent pid.
    - UserPromptSubmit passes `claude_pid=_claude_pid()` to `register` and hushes only the main channel.
    - Notification enqueues `kind="attention"` on the main channel.
    - Stop enqueues `"finished"` with `kind="attention", flags={"unless_briefed": True, "only_in_mode": "brief"}`.
    - `_channels` returns only the main channel, and its comment says why `:notify` went.
  - **`hooks.json`:** a `Stop` entry identical in shape to Notification's, `timeout: 5`.
  - **`.mcp.json`:** `{"mcpServers": {"speakd": {"command": "bash", "args": ["-c", "exec \"${SPEAKD_HOME:-$HOME/programing_linux/speakd}\"/.venv/bin/speakd-mcp"]}}}`. It mirrors how `speakd-hook.sh` finds the checkout; check that file's own lookup and use the same fallback it uses.
  - Run `tests/test_import_cost.py`, which must stay green (hook import budget).
- [ ] **Step 4: Run** `uv run pytest tests/test_cc_hook.py tests/test_cc_plugin_manifest.py tests/test_import_cost.py tests/test_client_send.py -q` and expect a pass.
- [ ] **Step 5: Commit** "Backstop briefings with hooks, and ship the MCP server in the plugin".

### Task 7: The window's brief/full toggle

**Files:** Modify `clients/gui/pinned.html`, `clients/gui/shared/speakd-source.js`, `clients/gui/app/src-tauri/src/bridge.rs` (`FORWARDED` gains `set_mode`).

- [ ] **Step 1: Shell.** Add `"set_mode"` to `FORWARDED`, with a doc line saying it is scoped by source like mute and changes how a channel is heard, not what it says. Then `cargo build`.
- [ ] **Step 2: Simulation.**
  - The fixture channel `briefs: true, mode: "brief"`, and `_status` carries `briefs`/`mode`.
  - `send("set_mode", {mode}, source)` validates, sets, and emits `mode` on that source.
  - `set_mode` on a channel without `briefs` is refused.
- [ ] **Step 3: Controller.**
  - `handleEvent` `case "mode"`: find the channel by `event.source_id`, set `mode`/`briefs`, then `renderChannels()`. For an unknown channel, `refreshStatus()`.
  - A row with `channel.briefs` gets a `button.modeswitch` showing `brief` or `full` (`aria-pressed` when brief, title "Brief: only what the agent chooses to tell you. Full: every response."). Clicking sends `source.send("set_mode", {mode: other}, channel.source_id)` and applies `res.data.mode`.
  - It is placed between skip and mute.
- [ ] **Step 4: CSS.** `.chan-row button.modeswitch` in the same small mono style, lit when `aria-pressed`.
- [ ] **Step 5: Browser check.** The fixture row shows `brief`; a click makes it `full` and back; rows without `briefs` show no toggle; there are no page errors.
- [ ] **Step 6: Commit** "Switch a session between brief and full from the window".

### Task 8: README, full suite, end to end

- [ ] **Step 1: README** gets a "Briefings" section:
  - the three modes and what each speaks;
  - that new Claude Code sessions start brief and muted;
  - `speakctl mode`;
  - the guide file;
  - that the plugin ships the MCP server;
  - the one-line registration for others: `codex mcp add speakd -- speakd-mcp`, or the equivalent JSON for OpenCode;
  - the channel list's ranking, stars and fold.

  Replace the "Claude Code sessions start muted" paragraph.
- [ ] **Step 2:** Run `uv run pytest -q`, `uv run ruff check src tests`, `uv run ruff format --check src tests`, `uv run mypy src`, and `cargo build`. Everything should be green.
- [ ] **Step 3: End to end.**
  - Restart the daemon and window.
  - `claude mcp add speakd -- "$PWD/.venv/bin/speakd-mcp"` (user scope), or rely on the plugin once reinstalled.
  - Start a fresh Claude Code session in a scratch dir, unmute it in the window, and ask it to do a small task.
  - Confirm `speakctl subscribe` shows `enqueue kind=brief` spoken with the label prefix, and that the session's row shows `brief` with a fresh time.
  - Switch it to `full` and confirm the next response is read in full and no briefing is spoken.
- [ ] **Step 4: Commit** "Document briefings and the ranked channel list".
