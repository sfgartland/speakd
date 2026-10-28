# Wiring the pinned monitor to the real daemon

Branch `feat/gui-tauri-shell`, worktree `speakd-worktrees/gui-tauri`.
Written 2026-09-14, against the daemon running from `main` (`c313abc`).

---

## What was built

### 1. `clients/gui/app/src-tauri/src/bridge.rs` (new)

The socket bridge. No new Rust dependency: `std::os::unix::net::UnixStream`
plus `serde_json`, which was already in `Cargo.toml`.

- **Event relay.** One long-lived connection, opened on its own std thread from
  `setup()`. Sends `{"verb":"subscribe","source_id":"","payload":{}}` exactly as
  specified, reads the `Response` ack, then relays every subsequent line to the
  window as a `speakd://event` Tauri event — the parsed JSON object, whole, keys
  unchanged. The relay does not interpret the daemon's vocabulary at all;
  `event` → `kind` and segment numbering happen once, in `ShellSource`.
- **Reconnect.** On EOF or any error the loop reports the link down and retries,
  backing off 0.5s → 5s and resetting on a clean end of stream. Verified by
  stopping and starting the daemon under a running window (screenshots below).
- **Link state.** A `speakd://link` event carries `{connected, detail}`, emitted
  only on change so a window left open beside a stopped daemon is not woken
  every five seconds. A `speakd_link` command returns the current state, which
  the frontend asks for once after registering its listeners — a Tauri event
  emitted before the page has a listener is simply lost, and the relay connects
  within milliseconds of process start.
- **Control.** `speakd_send(verb, payload)` — `async` so Tauri runs it off the
  main thread, with the blocking socket work inside `spawn_blocking`. One
  connection per request, per the task. Returns the daemon's own `Response`
  object; `Err` is reserved for not reaching the daemon at all, so the window
  can tell "speakd refused this" from "speakd is not there".
- **Socket path** mirrors `speakd.cli.default_socket_path`, including treating an
  empty `XDG_RUNTIME_DIR` as unset (Python's `if runtime` is falsey for `""`).

### 2. `clients/gui/shared/speakd-source.js`

`DaemonSource` (the HTTP/SSE stub) is gone, replaced by `ShellSource`.
`SimulatedSource` is **byte-for-byte unchanged** — verified by diffing the block
against `git show HEAD:`. `resolveSource()` is now a one-line feature-detect:
`window.__TAURI__` → `ShellSource`, otherwise `SimulatedSource`.

The header comment was rewritten against the real vocabulary — `started` with
its `text` and no `segments`, `position` with no `index` and no `duration`,
plus `transport`, `discarded` and `declined`, which the old comment did not
mention and which the daemon does emit.

### 3. The four metrics

`metrics` did not exist in the daemon when this work started — `grep -rn
"metrics" src/speakd/*.py` returned nothing on `main` at `c313abc`, which
settled the contract in docs only. So the window was built and verified for the
state that actually existed: no metrics event has ever arrived, all four read
`—`. To make that honest, the markup's hard-coded starting values had to go:
the page shipped with `0.75×`, `0.00s`, `1.52 GB` and `2` baked into the HTML,
and `mem` not wired to anything at all. They are now `—`, and `render()` is
called once at startup so the page never shows them as though they were
readings.

**Then the event landed mid-task** (`501214f Merge the metrics event`), and both
paths are now verified against the real daemon. Observed on the wire:

```
{"event": "metrics", "source_id": "", "data": {"rtf": 0.854808935725872, "drift": 0.0, "mem_bytes": 2054651904, "queue": 1}}
{"event": "metrics", "source_id": "", "data": {"rtf": 0.6667432884063127, "drift": -0.08662218599347393, "mem_bytes": 2162384896, "queue": 1}}
```

and rendered: **RTF 0.67× in green with "33% spare", DRIFT -0.08s, MEM 2.01 GiB,
QUEUE 0 with "clear"** — updating about once a second while speaking, and
settling when it goes idle.

Note for whoever owns the mockup: `drift` can be **negative** (observed -0.087),
and the static sub-label under it reads "behind clock", which a negative value
contradicts — it means ahead. The number is shown truthfully; only the label is
now wrong in one direction.

---

## The real traffic, verbatim

Captured with `speakctl subscribe`, timestamps prefixed by the capture shell.

```
15:39:40.501 {"event": "started", "source_id": "gui-test", "data": {"text": "One. Two. Three. Four. Five."}}
15:39:48.515 {"event": "position", "source_id": "gui-test", "data": {"text": "One.", "span_start": 0, "span_end": 4, "audio_offset": 0.0, "played_at": 150637.691525546}}
15:39:48.519 {"event": "position", ... "Two.",   "audio_offset": 1.15, "played_at": 150638.723217903}
15:39:48.522 {"event": "position", ... "Three.", "audio_offset": 2.3,  "played_at": 150639.874969934}
15:39:48.526 {"event": "position", ... "Four.",  "audio_offset": 3.5,  "played_at": 150641.112051257}
15:39:48.530 {"event": "position", ... "Five.",  "audio_offset": 4.7,  "played_at": 150642.307073664}
15:39:48.533 {"event": "finished", "source_id": "gui-test", "data": {"cancelled": false, "aborted": false}}
```

Also observed live: `transport` (`{"paused": true}` / `{"paused": false}`) and
`discarded` (`{"count": 1, "reason": "...", "sources": ["gui-test"]}`).

### Finding: `position` events arrive in a burst *after* the utterance is over

Read the timestamps. `started` at 15:39:40.5. The five `position` events land
at 15:39:48.51–.53 — **all five within 30 milliseconds, eight seconds after the
utterance began, after every word has already been spoken.** `finished` follows
4ms later. The `played_at` values inside them (150637.69 … 150642.31) show the
segments really were played 1.2s apart; the daemon simply does not publish them
until afterwards.

The cause is in `Daemon._speak` (`src/speakd/daemon.py:596`): `pipeline.speak()`
plays the whole utterance synchronously and returns a `Timeline`, and only then
does the daemon loop over `result.timeline.segments` publishing positions. There
is no per-segment callback.

**This is the single biggest gap between this window and the design's intent.**
The monitor cannot follow speech word by word, because the daemon does not
report speech word by word. What it can do — and does — is show the whole
utterance from `started` the moment it begins, then flip through the segments
when they arrive. Without that, the window would sit blank for the entire
duration of every utterance and then jump straight to the last segment.

This is a daemon change, not a GUI one, and it is not in this task's scope. It
is worth its own plan: publishing `position` from inside `pipeline.speak`'s play
loop, where `started` is already stamped.

---

## Bug found by running it: `hush` during playback wedges the daemon

Reproduced three times, twice on a freshly restarted daemon.

Sequence: `speakctl enqueue ...` then `speakctl hush` while audio is playing.
The hush command returns successfully. Then:

- no `finished` event is ever published — the GUI is never told speech ended;
- no `position` events for the played part are published either;
- **every subsequent utterance is accepted (`enqueue` returns 0) and never
  spoken.** `started` is never published again. The daemon looks alive to
  `speakctl status` and is silent forever.

`py-spy dump` on the wedged process, twice, same place:

```
Thread ... (idle): "speakd-speech"
    _raw_write (sounddevice.py:1317)
    write (sounddevice.py:1583)
    write (speakd/player.py:397)
    play (speakd/player.py:697)
    speak (speakd/pipeline.py:105)
    _speak (speakd/daemon.py:578)
    _consume (speakd/daemon.py:457)
```

The speech worker is blocked forever inside `sounddevice`'s blocking write.
`Daemon.handle`'s `player.stop()` returns, the queue drains, the hush is
answered `ok` — and the worker never comes back. The producer thread is parked
on the depth-one queue behind it.

The first time I hit this, it followed a `pause` → `resume` → `hush` sequence and
the stack was `player.py:280`/`player.py:436` (the other sink class in that
file); later reproductions with a plain `hush` landed on `player.py:397`/`697`.

**The window behaves correctly throughout**: it goes on showing the utterance as
playing, because that is the last thing the daemon told it. A monitor cannot
report a `finished` it was never sent. But it means the task's own verification
recipe (`enqueue` then `hush`) cannot be used to demonstrate the hushed state —
the daemon never produces it. Restarting the daemon clears it, and the window
recovers on its own.

---

## What the screenshots showed

All taken with `import -window 0x00800003` against the real daemon. Window
manager geometry is physical pixels at a 2× scale factor, so 768×1184 is
384×592 logical.

1. **Idle, connected** — channel label from the markup, `nothing speaking` in
   faint italic (no lit highlight), all four metrics `—`, no error line. No
   fabricated numbers anywhere.
2. **Speaking** (2s after `enqueue "One. Two. Three. Four. Five."`) — the pip is
   lit and breathing, the channel label has become **`gui-test`**, read from the
   wire's `source_id`; the full utterance sits in the highlighted line; the
   button reads **Pause**. Metrics still `—`.
3. **After `finished`** — `Four.` above, `Five.` in the now-line with the paused
   dashed underline, the button back to **Play**, the pip hollow, and the
   scrubber showing 5 ticks: four spent, the fifth lit-outlined.
4. **Daemon stopped** — error line reads
   `⚠ speakd is not listening on /run/user/1000/speakd/speakd.sock: No such file or directory (os error 2)`.
   Metrics reset to `—`. The stale text stays, which is the brief's own
   preference (stale over frozen), and the banner explains it.
5. **Daemon restarted** — the error line clears on its own within a few seconds,
   with no restart of the shell. A subsequent `enqueue --source gui-after-restart`
   drove the window again, label and all.
6. **Metrics live** (after `501214f` landed) — the row reads
   `RTF 0.67× / 33% spare`, `DRIFT -0.08s`, `MEM 2.01 GiB`, `QUEUE 0 / clear`,
   ticking once a second while speaking. Same window, same code that showed four
   dashes an hour earlier.

---

## What I could not verify

- **The buttons, by clicking them.** `xdotool` cannot drive this window. Clicks
  at correct absolute coordinates did not toggle even the purely local pin
  button, and `ctrl+alt+p` did not fire the registered global shortcut;
  `xdotool getmouselocation` produced no output at all. Synthetic XTEST input
  does not reach this XWayland surface here. Instead I verified the command path
  one layer down, with a temporary probe calling the same `request()` the
  command calls, against the live daemon — then removed it:

  ```
  PROBE pause  -> Ok({"ok": true,  "data": {"paused": true}})
  PROBE resume -> Ok({"ok": true,  "data": {"paused": false}})
  PROBE hush   -> Ok({"ok": true,  "data": {"discarded": 0}})
  PROBE cancel -> Ok({"ok": true,  "data": {"discarded": 0}})
  PROBE seek   -> Ok({"ok": false, "error": "seek is not implemented: ..."})
  ```

  So the JS→Rust→socket→daemon path is proven end to end for every forwarded
  verb, including that a refusal comes back as the daemon's own words. What is
  *not* proven is the last few pixels: that a real mouse press on the Hush
  button reaches `el("hush")`. A human at the keyboard can settle that in two
  seconds; I could not.
- **A real `declined` event.** The code path is a two-line case; no arbitration
  refusal occurred in testing.
- **The plain-browser fallback, in a browser.** I checked it in Node with a
  minimal global stub: with no `window.__TAURI__`, `resolveSource()` returns
  `SimulatedSource` and its synchronous `started`/`position`/`metrics` snapshot
  still fires. The module parses as ES module and `pinned.html`'s script block
  compiles. I did not open it in an actual browser tab.
- **Reconnect after a dropped-subscriber shutdown.** I verified reconnect after
  `systemctl stop`/`start`. I did not force the daemon's 256-event backlog drop.

---

## Judgement calls

1. **`ShellSource` numbers the segments.** The wire has no segment index and
   `pinned.html` needs one. It counts `position` events since the last `started`,
   resetting also when a position arrives for a different `source_id` than the
   last `started` named. Documented in the header comment.
2. **`started`'s text is shown in the now-line.** Given the burst finding above,
   the alternative was a blank window for the whole of every utterance. The
   text shown is exactly what the daemon said it started speaking — it is a
   claim about *what*, not about *where*, and the per-segment text replaces it
   the moment positions arrive.
3. **`started` clears the previous timeline.** Without it, a short utterance
   after a long one plays under a tail of ticks belonging to speech that is
   over. Side effect on the simulation: its "Again" replay now rebuilds the
   scrubber segment by segment instead of showing all five up front — which is
   how the real daemon behaves anyway.
4. **`finished` now sets `playing = false`.** It previously only did so for a
   cancelled finish, so with a real daemon the pip would breathe forever after
   normal completion.
5. **`SimulatedSource` left exactly as it is**, per the brief — so it emits no
   `mem_bytes`, and the mem cell reads `—` in a browser tab where the mockup
   used to read `1.52 GB`. That number was fabricated markup; a dash is the
   truthful replacement. Say the word and I will add `mem_bytes` to the fixture.
6. **The mem meter bar is gone**, replaced by a `resident` sub-label matching
   the other three metrics. There is no total-memory figure on the wire to
   scale a bar against, and I would not invent a denominator. A permanently
   empty 2px track reads as broken.
7. **`seek` is forwarded** even though the daemon refuses it. The window already
   has ← and → buttons; the daemon's own "seek is not implemented: the timeline
   can map an offset to a time, but moving playback needs the audio for segments
   already played…" is a better thing to show than a refusal the bridge invented.
   `enqueue` is *not* forwarded — this window monitors, it never originates.
8. **Daemon messages are escaped before reaching the DOM.** `err-msg` was set via
   `innerHTML` because `SimulatedSource`'s fixture carries `<code>` markup. Real
   daemon messages are arbitrary — `f"synthesis failed: {exc!r}"` can embed text
   an agent asked to have spoken — and this page has `window.__TAURI__` in reach.
   `messageHtml()` escapes everything and restores only `<code>`, so the mockup
   keeps its look and the injection path closes.
9. **Control requests use `source_id: "gui"`.** The daemon ignores `source_id`
   for all of pause/resume/hush/cancel/seek (they are global, as from
   `speakctl`), so this is a label, not routing. The *subscribe* line uses the
   empty `source_id` the task specified, verbatim.
10. **The title bar's `foreground` role pill and the "also" channel row are still
    static mockup content.** The daemon's `status` verb has that information, but
    wiring it is a second feature, not part of reading the event stream. Worth
    flagging: the pill makes a claim the window is not currently checking. Same
    for the default channel label `PhD articulation`, which stands until the
    first event names a real channel.

---

## Layout work (the coordinator's addition)

### Root cause of the clipping: an `i32::MAX` size hint

The window was opening 384×340 logical and scrolling internally, and it was not
a CSS problem. `tao`'s `set_size_constraints` sends a GDK geometry hint whose
maximum comes from `max_height.unwrap_or(PixelUnit::MAX)` — `i32::MAX` when
`maxHeight` is unset. This window manager answers a maximum that large by
sizing the window to its **minimum** instead. Measured, changing one thing at a
time and nothing else:

| config | resulting window height (logical) |
|---|---|
| `minHeight: 340`, no `maxHeight` | 340 |
| `minHeight: 300`, no `maxHeight` | 300 |
| `minHeight: 100`, no `maxHeight` | 100 |
| no `minHeight`, no `maxHeight`, `maxWidth: 800` present | **1** |
| no constraints at all | ~560 (correct) |
| `minHeight: 300`, **`maxHeight: 2000`** | **560 (correct)** |

The configured `height` never applied; the width was right every time, because
`maxWidth` was set and so its hint was finite. So the original `maxWidth: 384`
with no `maxHeight` is what had been collapsing the window all along.

The fix is one line — `"maxHeight": 2000` — and it is documented in `main.rs`
beside the window-state plugin, since JSON has no comments. Before finding this
I built a Rust workaround (constraints at `RunEvent::Ready`, then a one-shot
size correction hooked to the first `Resized`); all of it is removed, because
none of it was needed and none of it worked.

### The three requirements

1. **Everything visible at the default size.** Yes. Two things were adding
   height: only the *first* caption was hidden inside the shell, so the second
   ("rtf is the leading indicator…") was still laid out below the window, and
   the page kept the mockup's `26px 20px` body padding and 20px gap. Both are
   now dropped under a `.shell` class set on `<html>` (before first paint, not
   on `DOMContentLoaded`), and `.win` fills the viewport.
2. **Resizable on both axes.** `minWidth: 320`, `maxWidth: 800`, `minHeight: 300`,
   `maxHeight: 2000`, default 384×560. Verified against the window manager:
   asking for 280×200 logical was refused and left it at 320×300; asking for
   900 wide gave exactly 800.
3. **Only the text takes the slack.** `.bar, .scrub, .ctls, .metrics, .chans, .err
   { flex: none }`, `.text { flex: 1 1 auto; overflow-y: auto }` with
   `min-height: 0` under `.shell`. The three lines are centred by
   `margin-block: auto` on a new `.text-inner` wrapper rather than
   `justify-content: center` — centred overflow puts the top of the text above
   the scroll container's origin where no scrollbar can reach it.

### Verified at three sizes

- **320×300 (the minimum)** — bar, three clamped text lines, all four buttons,
  all four metric columns, and the channel pills all present. No page
  scrollbar, no clipping, no button collision or wrapping. Tightest spot is the
  metrics row, where `behind clock` at 8.5px nearly fills its column; it fits.
- **384×592 (the default)** — everything visible, nothing clipped.
- **800×600 (dragged large)** — everything visible, chrome unchanged, the text
  region absorbed all the extra height.

### Also fixed while in there: the scrubber was invisible

`.scrub` was `height: 5px; padding: 0 12px 10px` under `* { box-sizing:
border-box }`, so the 10px of padding consumed the whole 5px box and the ticks
were allocated zero height. The pinned window has never drawn a scrubber. Now
`height: 15px` — a 5px strip with 10px beneath, which is what the mockup drew —
and the ticks appear (visible in the screenshots, spent/lit/paused states and
all).

### Things that still look off, reported rather than fixed

- **There is a large empty band above and below the text at the default height.**
  The three lines are `-webkit-line-clamp`ed at 2/3/2, so the text region tops
  out around 183px however much room it is given; the chrome takes about 176px.
  At 560 that leaves roughly 190px of white space the text can never fill. A
  default nearer 420 would make it a compact strip with no dead band. I left 560
  alone because it is your number and you only authorised growing it — but if
  you want a tighter default, that is the value to change, and nothing else.
- The `foreground` pill and the "also / ReSem paper / Kronikk" row are still
  mockup fiction, as above.
