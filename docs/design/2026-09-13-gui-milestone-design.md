# Milestone 2 — the desktop GUI

**Date:** 2026-09-13
**Status:** scoping, not yet approved
**Depends on:** the daemon milestone (channels, event bus, control verbs)

## What this is

A window showing the text being spoken, with the current sentence highlighted, and
controls to pause, resume and skip. Click a word to jump there. It runs as a Tauri
shell around a web frontend, and the same frontend is servable as a plain
`localhost` page for anything that would rather use a browser.

## Why it is a milestone rather than a feature

Because three of its four parts do not exist, and one of them changes code that
three finished plans already depend on.

```
streaming player   (rewrite in the core — blocks everything else)
        │
        ├──▶ transport controls become real (pause / resume / seek)
        │
HTTP + event stream transport   (new, alongside the Unix socket)
        │
        └──▶ web frontend  ──▶  Tauri shell
```

## Part 1 — the streaming player

**This is the hard part and it is not optional.**

`Player.play()` blocks until the audio finishes. That was a deliberate, good choice
for the pipeline: playback becomes the clock, and a producer thread can run exactly
one segment ahead. But it makes three things impossible:

- **Pause and resume.** There is no handle on audio already handed to the device.
- **Seek.** Same.
- **Hush that actually interrupts.** Cancellation is only noticed *between*
  segments, so hushing waits for the current one to finish — up to four seconds at
  the default segment length, on the single most common path there is, since a user
  keystroke is a hush.

The replacement is a `sounddevice.OutputStream` with a callback pulling from a
ring buffer, per player rather than sounddevice's process-wide stream. That last
point is not cosmetic: the current `SoundDevicePlayer` drives a global stream, so
with several channels one session's `stop()` silences another's audio.

What it buys beyond the controls: the callback knows how many frames have actually
reached the device, which is a true playback position rather than the
`time.monotonic()` stamp taken at segment start. The timeline's two clocks were
designed for exactly this — `played_at` becomes measured rather than approximated.

**Risk.** This touches `player.py` and `pipeline.py`, which the speaking pipeline,
the plugin host's tests and the daemon all sit on. The pipeline's producer/consumer
loop currently uses "play returns" as its rate limit; with a streaming player it
must instead wait on buffer space. Getting that wrong reintroduces either the
thread leak or the unbounded-buffer problem the depth-one queue exists to prevent.

## Part 2 — transport controls

With a streaming player, `pause`, `resume` and `seek` stop returning
"not implemented" and become real. `seek` needs one addition the daemon does not
have: `speak()` always starts at unit 0, so resuming mid-utterance needs a start
index. The data is already there — `Timeline.segments[k].span` — but `SpeechResult`
does not retain the input pieces, so either re-segmentation must be deterministic
or the units must be kept.

## Part 3 — HTTP and the event stream

The daemon speaks line-delimited JSON over a Unix socket. A browser cannot.

**Recommendation: HTTP for the verbs, Server-Sent Events for the stream** — not
WebSocket. SSE is one-directional, which is exactly the shape of an event stream,
and it is implementable on `http.server` with no dependency. WebSocket would mean
either a dependency or hand-rolling a framing protocol, to buy bidirectionality the
design does not use: controls already have a perfectly good channel in POST.

This also serves `opencode-voice-plugin`, a third-party OpenCode plugin that
already expects "any TTS server with a simple REST API" — a client we get without
writing one.

## Part 4 — the frontend, and the shell

The page renders the source text, subscribes to position events, and wraps the
current span in a `<mark>`. Clicking a word sends `seek` with that offset, which
the daemon resolves through `Timeline.time_of` — the reason that map is
bidirectional rather than one-way.

The Tauri shell adds what a browser tab cannot:

- **Global hotkeys** — pause from inside your editor without switching windows.
  For a narrator this is the single most valuable thing here.
- **System tray** with current state.
- A real window, and autostart.
- **Supervising the daemon**, so launching the app starts speech if it is not running.

The shell owns no state. Queue, timeline and channels stay in the daemon; the page
talks to it over the same transport agent clients use. It cannot drift from the
daemon because it owns nothing.

Cost, stated honestly: Tauri renders in each platform's own webview — WebKitGTK,
WKWebView, WebView2 — so "cross-platform" means three engines to test rather than
Electron's one bundled Chromium. For a page that renders text, highlights a range
and draws four buttons, that risk is small. The gain is a 3–10 MB bundle against
Electron's 120–200 MB.

## Settled: now-playing only

The window shows the text currently being spoken and nothing else. It does not open
documents.

That was put to the owner and confirmed: "the ui should only show the actual
speaking text." It keeps this milestone complete on its own and roughly halves the
frontend. Reading a vault note or a PDF by ear — with position persistence, chapter
navigation and the corpus features that go with them — gets its own milestone rather
than riding along inside this one, where it would double the scope of the part that
is already gated behind a rewrite.

## Settled: the window is pinned, small, and shows four numbers

The primary form is a **384px always-on-top monitor** meant to sit beside an editor,
not a reading view. That width is the constraint everything else answers to: the
passage does not fit, so the text area shows three lines — previous, current, next —
with the spoken one lit and centred. You keep your place without scrolling, and it
is still only the text being spoken.

Four metrics earn a place, chosen because each predicts or explains a failure this
project actually has, rather than because they are the numbers dashboards usually
show:

- **rtf** — synthesis speed against realtime, the *leading* indicator. Kokoro runs
  at 0.75x on the reference machine: only 33% faster than realtime, so anything else
  loading the CPU makes synthesis fall behind playback and audio stutter. Colour-coded,
  with the spare headroom stated rather than left to be computed.
- **drift** — the gap between the nominal audio clock and wall time, the *lagging*
  indicator of the same thing. Both are shown because one predicts and one confirms.
- **mem** — resident memory against total, with a proportion bar. Argued against
  initially on the grounds that it barely moves; conceded, because on a machine that
  sits near its limit "is this the thing eating it" is a real question a constant
  number still answers.
- **queue depth** — how many utterances are behind this one, which is what tells you
  whether a hush silences one thing or five.

Deliberately **not** shown: GPU, which would read 0% forever since the reference
machine's card is below the compute capability modern cuDNN supports and the project
runs CPU-only by design; and raw CPU percentage, whose meaningful form here is rtf.

Also in the window: the **other channels** as pills with their roles, because a
monitor showing one channel on a machine running three lies by omission; and an
**error line that appears only when there is one**, since transform failures and
missing transforms currently reach stderr where a GUI user never sees them.

Mockups: `clients/gui/pinned.html` (the primary form) and `clients/gui/prototype.html`
(the wider reading view). Both run against a simulated event stream and are approved
as the reference for the real frontend.

## Explicitly not in this milestone

Multi-machine or remote access; authentication; theming or a design system beyond
legibility; mobile; the document-reader milestone's corpus features (bookmarks,
reading history, annotations) beyond whatever the open decision above settles.

## Sequencing

The streaming player is a prerequisite for everything else and touches shared code,
so it lands as its own plan with its own review, before any GUI work begins. HTTP
and SSE come next and are independently testable. The frontend and the shell are
last and can overlap.

## Revision, 2026-09-13: the shell reads the socket, not HTTP

Part 3 put HTTP and SSE on the GUI's critical path. Running the daemon for the
first time showed that it does not belong there.

`speakctl subscribe` already streams exactly what the monitor needs, over the
Unix socket, one JSON object per line. Observed verbatim from a real run:

```json
{"event": "started",  "source_id": "smoke", "data": {"text": "One. Two. Three. Four. Five."}}
{"event": "position", "source_id": "smoke", "data": {"text": "One.", "span_start": 0, "span_end": 4,
                                                     "audio_offset": 0.0, "played_at": 89731.960452604}}
{"event": "finished", "source_id": "smoke", "data": {"cancelled": false, "aborted": false}}
```

**The Tauri shell is a native process, so it can read that socket directly.** It
does not need an HTTP server, an SSE framing layer, or a second copy of the wire
format. What HTTP buys is browser clients and `opencode-voice-plugin` — both
real, neither on this milestone's path.

So Part 3 is **no longer a prerequisite for Part 4**. It becomes its own plan,
sequenced after the GUI rather than before it, and the GUI's event source reads
the socket through a Tauri command.

### Consequence for `clients/gui/shared/speakd-source.js`

`DaemonSource` was written against a guessed SSE shape before the daemon existed,
and the guess was close: its normalised `{kind, data}` differs from the wire only
in that the wire calls the field `event` and carries `source_id` alongside it. It
is replaced by a `SocketSource` with the same interface — `subscribe(handler)`
returning an unsubscribe — reading lines relayed from Rust. `SimulatedSource`
stays exactly as it is; it remains how the window is developed with no daemon
running, and how `pinned.html` opens in a plain browser.

The mapping is one line, `kind = wire.event`, and `source_id` becomes available
to the window for free — which the channel label in the title bar wants anyway.

### What this does not change

The event *content* is unchanged, so the four metrics, the position highlight and
the transport controls are all unaffected. This is a decision about which pipe the
same bytes arrive through.

### Sequencing, revised

The streaming player lands first (done). Transport wiring — the `Pausable` seam
and the pause/resume verbs — comes next, because without it the window's controls
have nothing to call. Then the frontend and the shell. HTTP and SSE last, for the
clients that actually need them.
