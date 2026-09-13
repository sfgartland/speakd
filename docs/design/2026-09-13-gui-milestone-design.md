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

## Open decision

**Does the GUI also open documents?** Two readings, and they differ in size:

- **Now-playing only.** The window shows whatever the daemon is currently speaking,
  whichever channel it came from. Smaller, and complete on its own.
- **Also a reader.** You open a vault note or PDF in it, press play, and watch it
  follow along. This pulls document loading, position persistence and
  chapter/paragraph navigation forward from the document-reader milestone.

The second is more useful and roughly doubles the frontend. It also makes the
`audiobook-viewer` Expo project redundant, which may be an argument for it.

## Explicitly not in this milestone

Multi-machine or remote access; authentication; theming or a design system beyond
legibility; mobile; the document-reader milestone's corpus features (bookmarks,
reading history, annotations) beyond whatever the open decision above settles.

## Sequencing

The streaming player is a prerequisite for everything else and touches shared code,
so it lands as its own plan with its own review, before any GUI work begins. HTTP
and SSE come next and are independently testable. The frontend and the shell are
last and can overlap.
