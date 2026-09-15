# Streaming narration, with an off switch

**Date:** 2026-09-15
**Status:** approved, not yet planned
**Depends on:** the daemon milestone (channels, event bus, control verbs), the Claude Code client

## What this is

Three complaints about the Claude Code client, one of which turned out to be
three separate problems:

1. Speech lags behind the text on screen.
2. The hooks slow the session down.
3. There is no way to stop the speech without editing `~/.claude/settings.json`.
4. The daemon holds three gigabytes whether or not anyone is listening.

## What was measured

Everything below is measured on this machine, on 2026-09-15, rather than
reasoned from the code.

**The hook costs ~250 ms, and `PostToolUse` fires on every tool call.**

```
bare interpreter                 0.02 - 0.07 s
+ the hook's imports             0.20 - 0.24 s
full wrapper, end to end         0.21 - 0.29 s
```

A turn with twenty tool calls therefore pays about five seconds of hook, all of
it inside Claude Code's critical path.

**Almost all of it is one import.** `send.py` imports `speakd.cli` for
`default_socket_path()` — a function that reads an environment variable and
joins a `Path`:

```
speakd.clients.claude_code.send      165 ms cumulative
  speakd.cli                         151 ms
    speakd.pipeline                   87 ms
      numpy                           81 ms
    speakd.profiles                   25 ms
      tomllib                         21 ms
```

Every tool call loads numpy and the synthesis pipeline to build a path.

**Sub-message streaming is impossible.** Claude Code flushes an assistant
message's content blocks to the transcript together, when the message
completes — not as the model streams. Sampling the live transcript every 150 ms
during generation:

```
160.171  assistant msg_011Cf4tk2  ["thinking"]
160.172  assistant msg_011Cf4tk2  ["text:97"]
160.173  assistant msg_011Cf4tk2  ["tool_use"]
```

Three blocks, 2 ms apart, after some sixteen seconds of generation. The thinking
block was finished long before the text was; it was not written when it
finished. So no file watcher can obtain text mid-sentence, and this design does
not try.

What a watcher *can* do is speak at `160.172`, when the message lands, instead of
waiting for `PostToolUse` — which does not fire until the tool it called has
finished running. On a slow tool that is seconds of silence for text that is
already on disk.

## Non-goals

- **A summariser.** Investigated and dismissed: none exists, and the full
  response read verbatim is what is wanted.
- **Sub-message streaming.** Impossible, as measured above.
- **Interruptible playback.** `hush` still waits for the current segment. That
  is Part 1 of the GUI milestone doc and stays there.
- **Queue drop-behind.** Not a complaint; not addressed.
- **Windows and macOS support.** See "Portability" below. This design is
  constrained not to make it worse, not to deliver it.

## Portability

speakd is Unix-only today, in five places, none of them in this design:

| Blocker | Where |
|---|---|
| `AF_UNIX` sockets | `transport.py:73,147,548` |
| `import fcntl` at module scope | `watermark.py:13` |
| `XDG_RUNTIME_DIR` for the socket | `cli.py:32` |
| systemd unit | `packaging/systemd/` |
| `bash` hook wrapper | `clients/claude-code/hooks/speakd-hook.sh` |

Real Windows and macOS support is its own project. This design's only
obligation is to add no new portability debt, which shapes two decisions: the
follower is a child of the daemon rather than a second service any platform's
init system must learn to supervise, and it **polls rather than using inotify**.
The transcript is append-only, so `os.path.getsize()` on a timer is portable,
dependency-free and sufficient. A 100 ms poll interval is immaterial beside the
250 ms of hook cost being removed.

## §1 — The import diet

Move `default_socket_path()` from `cli.py` into a new leaf module,
`speakd/paths.py`, which imports nothing but `os` and `pathlib`. `cli.py`
re-exports it so no caller changes. `send.py` imports it from `paths`.

Expected: the surviving hooks drop from ~250 ms to ~70 ms.

A regression test asserts that importing `speakd.clients.claude_code.send` does
not import `numpy`. Without it this returns the first time someone adds a
convenient import to `send.py`, and nothing visible breaks when it does.

## §2 — Transform fixes

### One piece per block

`markdown()` currently renders a Piece to a single string, joining every line
with `" "`. Paragraphs, headings and list items therefore run together with no
pause, which is why a long answer sounds like one breathless run-on.

Instead **emit one Piece per block**, preserving spans. Blocks become natural
segment boundaries, so the pause arrives for free — and span fidelity improves,
which the timeline needs for follow-along highlighting.

This is the substantive change in §2; the rest is a table of rules.

### Rules

| Input | Spoken | Note |
|---|---|---|
| `speakable()` | "speakable" | strip empty parens |
| `~/.claude/settings.json` | "dot claude, settings dot jason" | drop leading `~/`, `/` becomes `, ` |
| `GUI`, `TTS`, `PDF`, `XDG`, `RTF`, `OCR`, `CSV`, `HTML`, `SSH`, `CPU`, `GPU`, `IDE`, `CI` | spelled or spoken | extends `_WORDS`, which today has `CLI` but not `GUI` |
| `" — "` | `", "` | an em dash between clauses is a prosodic break |
| `> quoted line` | "Quote, … End quote." | today the `>` is stripped and nothing marks it |
| a markdown table | "Table omitted." | follows the `CODE_BLOCK_MARKER` precedent |

**The table rule is the weak one.** A table often carries the answer, so omitting
it loses content — but reading a six-by-three table aloud is worse. Recorded as
a decision rather than an oversight; revisit if it bites.

## §3 — Mute

### State

A global `muted` flag on the daemon, and a `muted` field on `Channel`.

### Protocol

One new verb, `MUTE`, payload `{"muted": bool}`. An empty `source_id` means
global; a named one means that channel. Like `STATUS`, it must not open a
channel for the asker — `speakctl mute` should not leave a phantom `cli`
channel behind.

`STATUS` grows a top-level `muted` and a `muted` per channel.

A `mute` event goes on the bus, so a change made in the CLI reaches the GUI and
the reverse. Without it the two disagree until something else forces a poll:

```json
{"event": "mute", "source_id": "", "data": {"muted": true, "scope": "global"}}
{"event": "mute", "source_id": "claude-code:bcece4cd", "data": {"muted": true, "scope": "channel"}}
```

`speakctl unmute` is not a second verb; both subcommands send `MUTE` with the
appropriate boolean.

### Semantics

A muted channel's enqueues are **accepted and dropped, not held**. Unmuting
should not release ten minutes of backlog.

**Dropping happens at `ENQUEUE`, which means no synthesis.** Nothing reaches the
queue that `_speak()` pulls from, so a muted channel costs no CPU at all — not
a synthesised buffer that is then discarded. What mute does *not* do is free the
model's memory; that is §6.

**Global wins.** A channel that is not itself muted stays silent while the
global flag is set, and unmuting that channel does not override it. The two
flags are independent state, not one setting written in two places: clearing
the global mute restores whatever each channel had.

**Muting also hushes what is currently playing.** An off switch that lets the
current paragraph finish is not an off switch.

### Persistence

Global mute persists to `~/.local/state/speakd/mute.json` and is reloaded at
startup: after a reboot, surprising silence is a smaller failure than surprising
speech. Per-channel mute does not persist — channels are ephemeral, and a
session's id never recurs.

### Surfaces

- `speakctl mute [--source X]` and `speakctl unmute [--source X]`
- GUI: a master toggle, plus a per-channel toggle in the channel list

## §4 — The follower

### Process model

The daemon spawns `speakd-claude-follow` as a supervised child at startup,
restarting it with backoff if it dies. It connects
back over the ordinary socket as any client does: a separate process running a
separate module, which can crash without taking audio down.

It is a child rather than a second systemd unit because packaging is already the
least portable part of this project, and a second supervised service would have
to be written three times.

Spawning is suppressed by `SPEAKD_NO_FOLLOWER=1` in the daemon's environment —
an environment variable rather than a config file because the only consumers are
the test suite and someone debugging the follower by hand, and `profiles.toml`
is about how speech sounds, not about what the daemon runs.

### Discovery, with no new protocol

The hook already writes watermark files to
`~/.local/state/speakd/claude-code/`. `UserPromptSubmit` refreshes that file with
the transcript path — a small local write, no socket, no daemon round trip.

The follower polls that directory for live sessions, and polls each live
transcript's size every ~100 ms. A session whose transcript has not grown for
thirty minutes leaves the poll set.

This reuses a file that already exists for exactly this information. A
registration verb would have been a second source of truth for the same fact.

### The hooks shrink from four events to two

`Stop` and `PostToolUse` are deleted. The follower sees a message land on disk
before the tool that follows it has even finished, so neither buys anything.

What survives:

| Event | Job | Frequency |
|---|---|---|
| `UserPromptSubmit` | hush both channels, refresh registration | once per turn |
| `Notification` | speak the notification | rare |

Both are once-per-turn rather than once-per-tool-call, and with §1 they cost
~70 ms.

### The watermark moves to the follower

The follower becomes its sole owner, which removes the concurrency the current
lock exists to handle: `PostToolUse` fires concurrently for parallel tool calls
and `Stop` can overlap one. With a single reader the lock can go, though it
should go deliberately rather than by accident.

### A bug to fix on the way

`reader._from_start()` scans backwards for a `user` record to find the current
turn — but **tool results are `user` records**. After a `/clear` or a resume,
when the watermark is not resumable, it therefore drops every prose block before
the last tool result. The follower should start from end-of-file at
registration instead, which sidesteps the question.

## §5 — Channel labels

Nothing sets `Channel.label` today and no verb can. Add `SET_LABEL`, mirroring
the existing `SET_ROLE` and `SET_PRIORITY`. `STATUS` already returns `label`, so
the GUI needs no protocol change to display it.

Claude Code writes a human-readable session name into the transcript, and
updates it as the session evolves:

```json
{"type": "ai-title", "aiTitle": "Claude code feature enablement", "sessionId": "bcece4cd-..."}
```

The follower is parsing that file already, so the label is free. It sets:

```
Claude Code · <aiTitle>
```

falling back to the `cwd` basename, then to the first eight characters of the
session id, since `ai-title` does not appear until Claude Code has named the
session. When a later `ai-title` record changes the name, the follower sends
`SET_LABEL` again.

This is what makes per-channel mute usable: muting one session out of three
requires being able to tell which is which.

## §6 — Disable, which unloads the model

Mute and disable are two levels of the same intent, and the difference is
measured:

| | Synthesis | Memory | Cost to reverse |
|---|---|---|---|
| **Mute** | none | model stays resident | instant |
| **Disable** | none | model unloaded | tens of seconds to reload |

Measured on this machine, 2026-09-15: the daemon holds **3.05 GiB RSS** with the
model loaded (the `metrics` event reported 1.86 GiB earlier in the same session,
so it grows with use), and the journal shows the model still initialising some
nine seconds after start.

### A lazy engine

`KokoroEngine.__init__` *is* the model load, and `__main__.main()` constructs it
eagerly, before the daemon or the socket exists. Wrap it in a `LazyEngine` that
satisfies the same `Synthesizer` interface and owns `load()`, `unload()` and a
`loaded` flag.

`sample_rate` must stay available while unloaded, because `build_player()` reads
it to open the sink. It already is: `sample_rate = 24000` is a **class**
attribute on `KokoroEngine`, so the player is built from the class rather than
from an instance.

**Unloading drops the model only, never the player.** `_close_player`'s contract
is that `close()` is terminal — "the sink refuses every later write and start" —
so a disable that released the audio device could not reopen it. The device is
cheap; the model is not.

`unload()` drops the `KPipeline` reference and calls `gc.collect()`. No CUDA
cache call: this machine runs CPU-only torch by necessity, an MX250 being
sm_61.

### Never load implicitly

`synthesize()` on an unloaded engine must **not** load the model. A stray
enqueue would otherwise undo a decision the user made deliberately, and pay
thirty seconds to do it. While disabled, enqueues are dropped exactly as a mute
drops them.

### Protocol

`SET_ENGINE`, payload `{"loaded": bool}`. `STATUS` grows
`engine: {"loaded": bool, "loading": bool}`.

Enabling loads **on a background thread** so the socket stays answerable
throughout, publishing as it goes:

```json
{"event": "engine", "source_id": "", "data": {"state": "loading"}}
{"event": "engine", "source_id": "", "data": {"state": "ready"}}
{"event": "engine", "source_id": "", "data": {"state": "unloaded"}}
```

Disabling hushes first — stopping what is playing — and then unloads.

### Persistence, and why it must be read early

The disabled flag persists beside the mute flag. It has to be read **before the
engine is constructed**, or a daemon that starts disabled loads three gigabytes
for the sole purpose of freeing them. This inverts the current startup: the
socket appears immediately and the model loads behind it, which is an
improvement in its own right — the README already tells users that the socket
appearing is the readiness signal, and today that signal is thirty seconds late.

### Surfaces

`speakctl disable` / `speakctl enable`, and a GUI control kept visually distinct
from the mute toggle, since one is instant and the other is not. The GUI should
report the **measured** RSS it already receives in the `metrics` event rather
than advertising a saving.

### Risk: the memory may not come back

Dropping the reference frees the tensors to Python; it does not guarantee the
resident set returns to the OS, because torch's allocator and CPython's arenas
both retain freed memory. The implementation must **measure RSS before and
after** and record the real delta. If it turns out to be small, that is a
finding to report, not a number to quietly restate from this document.

## §7 — The GUI speaks pasted text

A box in the window: paste text, press a key, hear it. Asked for on 2026-09-15.

### What this reverses, and what it does not

`bridge.rs` keeps an allowlist of the verbs the shell forwards, and `enqueue` is
deliberately not among them:

> the window monitors speech, it never originates it … so `enqueue` is
> deliberately absent and a bug in the frontend cannot make the monitor start
> talking.

This feature reverses **that** property, and it is worth being clear that it is
a reversal rather than a gap.

It does **not** reopen the milestone's "Settled: now-playing only", which is
about the window not opening documents. There is still no file picker and no
reader; a paste box is a line of text, not a corpus.

### Keeping most of the safety property

`enqueue` does not join `FORWARDED`. Instead the bridge gains one narrow
command, `speakd_say(text: String)`, which enqueues on a fixed source and
nothing else. A frontend bug then still cannot send arbitrary verbs or speak on
another session's channel — it can only do the one thing the box exists to do.

The source is a literal `"gui"`, not a caller-supplied id, so the pasted speech
is an ordinary channel: it shows in the channel list, it can be muted on its
own, and `hush` reaches it like anything else.

Text is capped at 8 KiB. Beyond that the box refuses and says so, rather than
handing the segmenter a novel.

## §8 — Testing

- **Transforms** are pure functions: unit tests per rule, plus one that a
  multi-paragraph input yields multiple Pieces with correct spans.
- **Import diet**: assert `numpy` is absent from `sys.modules` after importing
  `send`.
- **Mute**: on the fake engine, a muted channel synthesises nothing; muting
  mid-utterance stops it; global mute silences a channel that is not itself
  muted; the flag survives a restart.
- **Follower**: a synthetic transcript grown under it, against a fake socket,
  asserting what it enqueues and that it never re-speaks across a restart.
- **Labels**: an `ai-title` arriving late renames the channel.
- **Paste to speak**: `speakd_say` reaches the daemon, `enqueue` sent directly
  through `speakd_send` is still refused by the allowlist, and text over the cap
  is refused in the frontend rather than sent.
- **Lazy engine**: a fake engine records `load`/`unload`; enqueues while
  disabled synthesise nothing and do not trigger a load; enabling loads off the
  request thread; a daemon started from a persisted disabled flag never
  constructs the model at all.

## Risk

**Deleting `Stop` and `PostToolUse` removes the fallback.** Today a dead daemon
means silence and a line in the log; afterwards, a dead *follower* means the
same, and the follower is a new thing that can die. The daemon supervising it
with backoff is the mitigation, and the follower must log where the hooks log,
so the README's "tail this file" instruction still finds the failure.

## Order of work

§1 and §2 touch nothing else and should land first — they are useful on their
own, and §2 is the change you will hear most. §5 is a prerequisite for the
per-channel half of §3 only: without a readable label, a per-channel toggle is a
list of UUIDs. §4 depends on §1 for its benefit, not its correctness, and does
not depend on §3 at all.

```
§1 import diet ──────▶ (§4's benefit)
§2 transforms ───────▶ (independent)
§5 SET_LABEL ────────▶ §3 mute, per-channel half
§3 mute ─────────────▶ §6 disable (shares the state file and the drop path)
§4 follower ─────────▶ (independent of §3 and §6)
```

§7 depends only on the GUI existing on `main`, which it now does: the Tauri
shell was merged on 2026-09-15 (`3dc68ad`), having sat eight commits ahead and
a hundred and seven behind without touching anything outside `clients/gui`.

That makes four plans rather than one:

| Plan | Sections | Deliverable |
|---|---|---|
| A | §1, §2 | the hook stops loading numpy; speech gets its pauses back |
| B | §5, §3, §6 | labels, mute and disable, in the daemon and `speakctl` |
| C | §4 | the follower, and hooks that shrink from four events to two |
| D | §7, GUI half of §3 and §6 | the window gets its toggles and its paste box |
