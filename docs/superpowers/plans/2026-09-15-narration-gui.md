# Narration: the GUI's toggles and paste box — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Put the off switches where a hand can reach them — a master mute, a per-session mute, a disable that gives back the model's memory — and add a box that speaks pasted text.

**Architecture:** The Rust bridge gains the new verbs in its forwarding allowlist plus one narrow `speakd_say` command; `pinned.html` gains a channel list with per-channel mutes, a master toggle, a disable control, and a paste box. `SimulatedSource` learns the same verbs so the page still works as a plain browser tab with no daemon.

**Tech Stack:** Tauri 2 (Rust), vanilla ES modules, no frontend framework and no bundler. `cargo` for the shell.

**Spec:** `docs/design/2026-09-15-streaming-narration-design.md` (§7, and the GUI halves of §3 and §6)

## Global Constraints

- **Depends on Plan B.** `MUTE`, `SET_ENGINE` and `SET_LABEL` must exist in the daemon before this plan can talk to them.
- **Never run `uv sync`.** Always `uv run --no-sync ...`.
- Rust: `cargo build` and `cargo clippy` from `clients/gui/app/src-tauri/`. CI does **not** build the shell (`.github/workflows/ci.yml` runs only the Python gates), so the Rust side is verified locally or not at all — build it before every Rust commit.
- No bundler, no npm, no framework: `pinned.html` is hand-written and loads `../shared/speakd-source.js` as a module. Keep it that way.
- `tauri.conf.json`'s `frontendDist` points at only what the window loads. If you add a file, point it there too.
- Branch: `feat/streaming-narration`.

---

## What this reverses, and what it does not

`bridge.rs` documents `enqueue`'s absence as deliberate:

> the window monitors speech, it never originates it … so `enqueue` is deliberately absent and a bug in the frontend cannot make the monitor start talking.

Task 4 reverses that, on the owner's request of 2026-09-15. **Do not simply add `enqueue` to `FORWARDED`** — that would give a frontend bug the run of the daemon. Add one narrow command instead, so the property that survives is "the frontend can speak user-typed text on its own channel, and nothing else".

This does **not** reopen the milestone's "Settled: now-playing only", which is about the window not opening documents. No file picker, no reader.

---

## File Structure

| File | Responsibility |
|---|---|
| `clients/gui/app/src-tauri/src/bridge.rs` | allowlist gains the new verbs; new `speakd_say` command |
| `clients/gui/app/src-tauri/src/main.rs` | registers `speakd_say` |
| `clients/gui/shared/speakd-source.js` | `SimulatedSource` answers the new verbs; `ShellSource` gains `say()` |
| `clients/gui/pinned.html` | channel list, master mute, disable control, paste box |

---

### Task 1: Forward the new verbs

**Files:**
- Modify: `clients/gui/app/src-tauri/src/bridge.rs:72`

**Interfaces:**
- Produces: `FORWARDED` gains `"mute"`, `"set_engine"`, `"status"`.

- [ ] **Step 1: Widen the allowlist**

```rust
/// `enqueue` is still absent, and deliberately: a bug in the frontend must not
/// be able to make the window speak arbitrary traffic on an arbitrary channel.
/// Pasted text goes through `speakd_say` below, which can do that one thing and
/// nothing else.
///
/// `status` joins the control verbs because the window needs to read mute and
/// engine state at startup — the event stream reports changes, but a window
/// opened against an already-muted daemon has missed them.
const FORWARDED: &[&str] = &[
    "pause", "resume", "hush", "cancel", "seek", "mute", "set_engine", "status",
];
```

- [ ] **Step 2: Build it**

```bash
cd clients/gui/app/src-tauri && cargo build 2>&1 | tail -5
```
Expected: compiles.

- [ ] **Step 3: Commit**

```bash
git add clients/gui/app/src-tauri/src/bridge.rs
git commit -m "Let the window read and set the daemon's two off switches"
```

---

### Task 2: The simulated source answers the new verbs

The page must keep working as a plain browser tab with no daemon; that is what `SimulatedSource` is for, and its `send()` returns `unknown verb` for anything it has not been taught.

**Files:**
- Modify: `clients/gui/shared/speakd-source.js` (the `send(verb, payload)` switch at ~line 201, and the snapshot it publishes at ~line 147)

**Interfaces:**
- Produces: `SimulatedSource` holds `muted`, `channelMuted: Map<string, boolean>`, `engineLoaded`; answers `mute`, `set_engine`, `status`; emits `mute` and `engine` events.

- [ ] **Step 1: Teach it the verbs**

In the `send` switch add cases mirroring the daemon's contract exactly — `{ok: true, data: {muted, scope}}` for `mute`, `{ok: true, data: {loaded}}` or `{loading: true}` for `set_engine`, and a `status` shaped like the daemon's: `{channels: [{source_id, role, priority, profile, label, muted}], muted, engine: {loaded, loading}}`.

Each mutation must also publish the matching event to subscribers, because that is how the real daemon informs the window and the simulation exists to behave like it.

- [ ] **Step 2: Check it in a browser**

```bash
python3 -m http.server 8765 --directory clients/gui &
```
Open `http://localhost:8765/pinned.html`. Expected: the window renders and the console is clean. Stop the server afterwards.

- [ ] **Step 3: Commit**

```bash
git add clients/gui/shared/speakd-source.js
git commit -m "Teach the simulation the verbs the window now sends"
```

---

### Task 3: Channel list, mute toggles and the disable control

**Files:**
- Modify: `clients/gui/pinned.html`

**Interfaces:**
- Consumes: `source.send("status")`, `source.send("mute", {...})`, `source.send("set_engine", {...})`; `mute` and `engine` events via `handleEvent`.

- [ ] **Step 1: Read what is there first**

`pinned.html` is a 384px always-on-top monitor, deliberately small. Read the `<style>` block and `render()` before adding markup: a channel list that makes the window taller than an editor's sidebar defeats its purpose. Put the channel list behind a disclosure that is collapsed by default.

- [ ] **Step 2: Add the controls**

- A master mute button beside `hush`, `aria-pressed` reflecting state.
- A disable control, **visually distinct from mute** — one is instant, the other costs tens of seconds. Label it with what it does (`Unload model`), and while `engine.state === "loading"` show it busy and disabled.
- A collapsed channel list, one row per `status.channels` entry: the `label` (falling back to `source_id`), and a mute toggle sending `mute` with that `source_id`.
- Extend `handleEvent` with `mute` and `engine` cases so a change made from `speakctl` moves the buttons.
- Call `status` once after `resolveSource()`, since a window opened against an already-muted daemon has missed the events.

- [ ] **Step 3: Verify against the real daemon**

```bash
cd clients/gui/app/src-tauri && cargo build && cargo run &
```
Then, from another shell, check the window follows the CLI:

```bash
uv run --no-sync speakctl mute            # master toggle should light up
uv run --no-sync speakctl unmute
uv run --no-sync speakctl disable         # control should show unloaded
uv run --no-sync speakctl enable          # should show busy, then ready
```

Expected: every change is reflected without touching the window. If it is not, the `handleEvent` case is missing or the event kind does not match the daemon's.

- [ ] **Step 4: Commit**

```bash
git add clients/gui/pinned.html
git commit -m "Put both off switches, and the channel list, in the window"
```

---

### Task 4: The paste box

**Files:**
- Modify: `clients/gui/app/src-tauri/src/bridge.rs`, `clients/gui/app/src-tauri/src/main.rs:98-103`, `clients/gui/shared/speakd-source.js`, `clients/gui/pinned.html`

**Interfaces:**
- Produces:
  - `#[tauri::command] pub async fn speakd_say(text: String) -> Result<Value, String>`
  - `ShellSource.say(text)` and `SimulatedSource.say(text)`
  - `SAY_SOURCE = "gui"`, `SAY_MAX_BYTES = 8192`

- [ ] **Step 1: Add the narrow command**

In `bridge.rs`:

```rust
/// The one way the window originates speech.
///
/// Not `enqueue` on the forwarding allowlist: that would let any frontend bug
/// send any payload on any channel. This can do exactly one thing — speak text
/// a person typed, on the window's own channel — so the property `FORWARDED`
/// was protecting mostly survives.
///
/// The source is a literal, never a parameter, so the pasted speech is an
/// ordinary channel: it appears in the channel list, it can be muted on its
/// own, and `hush` reaches it like anything else.
const SAY_SOURCE: &str = "gui";

/// Past this the box refuses. The segmenter will accept a novel; the person
/// who pasted one did not mean to hear it.
const SAY_MAX_BYTES: usize = 8192;

#[tauri::command]
pub async fn speakd_say(text: String) -> Result<Value, String> {
    let trimmed = text.trim().to_string();
    if trimmed.is_empty() {
        return Err("nothing to say".into());
    }
    if trimmed.len() > SAY_MAX_BYTES {
        return Err(format!("that is longer than {SAY_MAX_BYTES} bytes"));
    }
    let payload = json!({ "text": trimmed, "kind": "response" });
    tauri::async_runtime::spawn_blocking(move || request_on("enqueue", SAY_SOURCE, payload))
        .await
        .map_err(|err| format!("the speakd request thread died: {err}"))?
}
```

`request()` currently builds its own `source_id`; read it and factor out a `request_on(verb, source, payload)` that takes the source, leaving `request()` as a thin caller. Do not duplicate the socket code.

Register it in `main.rs`:

```rust
            bridge::speakd_send,
            bridge::speakd_say
```

- [ ] **Step 2: Build it**

```bash
cd clients/gui/app/src-tauri && cargo build 2>&1 | tail -5
```
Expected: compiles.

- [ ] **Step 3: Add `say()` to both sources**

In `speakd-source.js`, `ShellSource.say(text)` invokes `speakd_say`; `SimulatedSource.say(text)` fakes a `started` event so the page behaves the same in a browser tab. Both return the same `{ok, error}` shape as `send()`, so the caller has one thing to check.

- [ ] **Step 4: Add the box**

In `pinned.html`, behind the same disclosure as the channel list (the window is 384px and must not grow a permanent textarea):

- a `<textarea id="say">` with a placeholder,
- a Speak button, and Ctrl/Cmd+Enter as the shortcut,
- on submit: `const res = await source.say(el("say").value)`; on failure put `res.error` in the existing error line, which already exists for exactly this; on success clear the textarea.
- Enforce the 8 KiB cap in the frontend too, so the message is immediate rather than a round trip away.

- [ ] **Step 5: Verify, including that the safety property held**

```bash
cd clients/gui/app/src-tauri && cargo run &
```

- Paste a paragraph, press Ctrl+Enter. Expected: it speaks, and a `gui` channel appears in the channel list.
- Mute the `gui` channel, paste again. Expected: silence — it is an ordinary channel.
- In the window's devtools console, confirm the allowlist still holds:

```js
await window.__TAURI__.core.invoke("speakd_send", { verb: "enqueue", payload: { text: "x" } })
```
Expected: rejected with *the shell does not forward "enqueue" to speakd*. **If this speaks, Task 4 was done the wrong way** — revert and add the narrow command instead.

- [ ] **Step 6: Commit**

```bash
cd clients/gui/app/src-tauri && cargo clippy 2>&1 | tail -5
git add clients/gui
git commit -m "Let the window speak text a person pasted, and nothing else"
```

---

### Task 5: Write down what changed

**Files:**
- Modify: `docs/design/2026-09-13-gui-milestone-design.md`, `README.md`

- [ ] **Step 1: Amend the milestone doc**

Add a dated note under "Settled: now-playing only" recording that a paste box was added on 2026-09-15, that it does not reopen the document-reader question, and that `enqueue` is still off the allowlist. A settled decision that quietly stops being true is worse than one that is revised in writing.

- [ ] **Step 2: Update the top-level README**

The "Using it" section lists `speakctl` verbs. Add `mute`, `unmute`, `enable`, `disable`, and say what the window now does.

- [ ] **Step 3: Commit**

```bash
git add docs/design/2026-09-13-gui-milestone-design.md README.md
git commit -m "Record the paste box against the decision it revises"
```

---

## Done when

- The window shows `Claude Code · <session name>` per channel, not UUIDs.
- The master mute, the per-channel mutes and the disable control all work, and all follow changes made from `speakctl` without a reload.
- Pasted text speaks on a `gui` channel that can be muted on its own.
- `speakd_send` with `enqueue` is still refused.
- `cargo build` and `cargo clippy` are clean; the Python gates still pass.
