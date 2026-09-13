# speakd — design

**Date:** 2026-09-13
**Status:** approved for milestone 1
**Author:** Severin Gartland (designed with Claude Opus 5)

## Problem

Text-to-speech integrations for coding agents wait for the complete response,
synthesise all of it, then play. Measured on the reference machine with
`claude-code-narrator`, a 417-character passage produced no audio for 20.2
seconds. Two causes compound:

1. The daemon accumulates every audio chunk and concatenates before playing.
2. Kokoro's `split_pattern` defaults to `\n+`, and the caller flattens text to a
   single line — so the entire response is one segment and nothing can stream.

Sentence-level splitting with play-as-you-go was measured at 10.9s to first
audio on the same passage with zero playback underruns (RTF 0.75).

Beyond latency, the existing tool has no API, no session identity, no position
tracking and a 1000-character truncation. It cannot grow into document reading,
multi-session arbitration or follow-along display without replacing its spine.

## Goals

- Speak an agent's response starting in under three seconds.
- Serve several agent sessions at once without interleaving them into noise.
- Publish where in the source text playback currently is, so other software can
  follow along.
- Keep everything domain-specific out of the core and in plugins.
- Be drivable by any agent that can run a shell command or an HTTP request.

## Non-goals

- Running a language model inside the daemon. The reference machine has 1.6 GiB
  free of 15 and an i7-10510U; Kokoro already runs at RTF 0.75. A local model
  would contend for CPU and cause playback underruns.
- Deciding *what* deserves to be spoken by inference at runtime. Arbitration is
  declarative rules.
- Cloud services in the default path. Speech works offline.

## Principles

**The intelligence lives in the agents; the daemon stays dumb and fast.**
"AI-first" means designed to be driven by LLM agents — structured control verbs,
per-source policy, a CLI any agent can invoke — not that a model runs inside.

**Anything opinionated is a plugin.** Markdown handling, citation reading and
summarisation are transforms. The core knows about text, time and audio.

**Speech never disappears silently.** A failing transform is skipped with a
warning and its input passes through unchanged. Silent failure was the worst
property of the tool this replaces.

## Architecture

```
hooks/CLI ──enqueue──▶ speakd ──▶ transforms ──▶ segmenter ──▶ engine ──▶ player
                         │                                       │
                         └────── event bus ◀── span-to-time map ◀─┘
                                    │
                         out-of-proc subscribers (PDF highlighter, …)
```

One daemon owns the model, the queue, the plugin host and the API. Everything
else is a client.

## Core data model

```python
Span(start: int, end: int)                 # offsets into the job's source text
Piece(span: Span, spoken: str, exact: bool = True)   # text and its provenance
Segment(span, text, audio_offset, duration)  # a synthesised unit, placed in time
Job(source_id, text, profile, priority, segments)
```

A job's text enters as a single `Piece`. Transforms map pieces to pieces:
rewriting changes `spoken` and keeps `span`; dropping a code block removes a
piece; an insertion inherits a neighbouring span.

**A transform that rewrites `spoken` must clear `exact`.** Provenance cannot be
inferred from string length: a length-preserving rewrite — `café` to `cafe`, an
em-dash swap, much of the pronunciation table — would otherwise be mistaken for
untouched source, and the segmenter would compute sub-spans against text that no
longer matches. The flag is the contract; the length check is only a secondary
guard behind it. Because provenance survives
arbitrary rewriting, follow-along display works even under a profile that
rewrites heavily.

### Timeline

`Timeline` is the bidirectional map between source spans and playback time,
appended to as synthesis streams:

- `span_at(audio_time) -> Span | None` — for highlighting and resume
- `time_of(offset) -> float | None` — for seeking

This is the structure that is painful to retrofit, so it exists from the first
commit. Everything downstream — position events, seek, resume, karaoke-style
highlighting — is a read of it.

**Two clocks, deliberately.** `audio_offset` stays nominal, accumulated from
`len(audio) / sample_rate`, and each segment additionally records a monotonic
timestamp taken when playback actually starts. Seek and resume read the offsets,
because those describe the audio; subscribers read the stamp, because that
describes reality. Keeping them separate is what lets a stall be visible —
a single measured offset would collapse the distinction and would also break
`Timeline.append`'s monotonic check against an instantaneous test player.

## Pipeline and streaming

Transforms declare a scope:

- **piece** — applied as pieces flow; the job starts speaking as soon as the
  first piece clears the chain.
- **job** — gates the whole job. The summariser is job-scoped: a summary of text
  cannot be spoken before the text is summarised. The delay is the price of
  brevity, and profiles choose whether to pay it.

Segmentation splits on sentence boundaries, with a configurable maximum segment
length that falls back to clause boundaries so a single long sentence cannot
dominate time-to-first-audio. Playback keeps one segment of prefetch; measured
headroom (RTF 0.75) is sufficient but not generous.

## Channels, roles, profiles

Each source gets a channel with a role, a priority and a profile.

- **foreground** — read fully.
- **background** — emits only what its profile marks interrupting, each
  announced with a short spoken label.

A source changes its own standing by sending a control verb. The daemon never
deliberates. Natural-language policy, if added later, compiles to these rules
once at configuration time rather than being interpreted per utterance.

A profile binds transforms, voice, speed and interrupt rules:

```toml
[profile.philosophy]
transforms = ["markdown", "citations", "bekker"]
speed = 1.0

[profile.monitor]
transforms = ["markdown", "summarize"]
interrupt_on = ["error", "needs_approval", "done"]
```

## Control surface

`enqueue, cancel, hush, pause, resume, seek, set_role, set_priority, subscribe`

Served over a Unix socket and over local HTTP with the same verbs. HTTP is not
speculation: `opencode-voice-plugin` already expects "any TTS server with a
simple REST API", making it a client we do not have to write. Transport uses the
standard library; no web framework enters the dependency tree.

State lives under XDG paths (`~/.config/speakd`, `~/.local/state/speakd`).

## Plugin model

Two tiers, chosen by whether the code sits on the path to first audio.

**In-process transforms** (Python, discovered via entry points) run fast and
synchronously in the pipeline. **Entry-point discovery belongs to plan 3b**, which
is the plan that first needs it — the vault pack is specified as separately
installable, which is impossible without it. Until then the core registers its
built-ins directly. The loader lands in `PluginHost.register`, so it must be
written against the same re-registration guard that method already carries.

**Out-of-process effects** subscribe to the event stream and may send control
verbs. Any language; a crash cannot take audio down.

### Composability invariants

Borrowed from arXiv:2608.25512, *A Programming Paradigm for Spatiotemporal
Composability* (Peking University / DeepSeek-AI), implemented minimally rather
than by adopting Cordis itself:

- **Temporal** — every registration returns a disposable the host tracks, so
  unloading a plugin mechanically undoes its side effects, in reverse order.
- **Spatial** — plugins declare required services and activate only when a
  provider exists, deactivating when it disappears.

```python
def setup(ctx):
    bib = ctx.require("bibliography")  # activation waits for a provider
    ctx.transform("citations", scope="piece")(citation_reader(bib))
```

When the plugin providing `bibliography` unloads, the citation transform
deactivates on its own.

## Transforms shipped with the core

- **markdown** — headings, lists, code blocks, links, emphasis rendered as
  something worth hearing rather than stripped of markers. **Tables are not yet
  handled** and are read as raw pipes; agent output contains them constantly, so
  this is a known gap rather than a decision — closing it means either speaking
  the cells and dropping separator rows, or announcing "Table omitted." Indented
  four-space code blocks are deliberately read as prose: detecting them was tried
  and reverted because it swallowed loose list items, and doing it correctly needs
  list-context tracking.
- **pronunciation** — the abbreviation, symbol and acronym table. Ported from
  `claude-code-narrator`'s `speak.sh` with attribution; it is good work and
  reinventing it would be worse.
- **summarize** — job-scoped, backends in priority order:
  1. `claude -p --model haiku`, borrowing the host agent's existing auth. Needs
     a recursion guard: a hook shelling out to `claude` spawns a session whose
     own hooks fire.
  2. Any OpenAI-compatible endpoint via LiteLLM — OpenCode Go or OpenRouter.
     Free Zen-catalogue models suit this workload and spend no Claude quota.
  3. Extractive fallback — headings, first sentence, last line. Offline, no
     memory cost.

## Vault pack (`plugins/speakd-vault`)

Separately installable, developed in this repo to prove the plugin boundary
against a real domain case.

- **bibliography provider** — searches outward from the job's origin directory
  in rings: the directory, its subdirectories to depth 2, then the parent and
  its other children, and so on. Stops at the first of: a `.git` root, the
  home directory, 4 levels up, or 500 directory entries scanned. Collects
  `*.bib` and `*.csl.json`, plus paths named in config. Parsed results cached by
  path and mtime. **Nearest wins** on key collision, so a paper's local
  `references.bib` overrides the global library.
- **citations** — reads `@Stiegler-1994` as an author and year rather than a key.
- **bekker** — reads `A11/B25` and Bekker numbers as spoken locators.

## Claude Code client (`clients/claude-code`)

Hooks calling `speakctl`, carrying the session id so channels are per session.

- `Stop` — enqueue the response
- `PostToolUse` — enqueue orphaned text blocks between tool calls (this is real:
  a text block followed by a tool call reaches no other hook)
- `Notification` — approval requests and similar
- `UserPromptSubmit` — hush

## Failure handling

- A transform that raises is skipped; its pieces pass through unchanged.
- A plugin that fails to activate is reported by `speakctl status`, not silently
  dropped.
- Daemon startup failures are logged to a file the client names in its error, and
  never discarded to `/dev/null`.
- A dead subscriber is dropped from the bus without affecting playback.
- **A player failure is recorded, not raised.** A sink disappearing mid-utterance
  — a headset walking out of range — is routine for a daemon, so the failure goes
  into `SpeechResult.errors` with an `aborted` flag and the caller receives the
  timeline built so far. Discarding the position map would defeat resume in
  precisely the case where resume matters most.

## Testing

- Unit tests for `Timeline` in both directions, including boundaries and gaps.
- Transform tests as text-to-text fixtures, with span preservation asserted.
- Plugin host tests: activation on provider appearance, deactivation on removal,
  disposal ordering, failure isolation.
- A benchmark in CI asserting time-to-first-audio stays under the target on a
  fixed passage, with the engine stubbed so it measures pipeline overhead rather
  than hardware.

## Milestone 1

**In:** daemon with both transports; Kokoro engine with streaming and timeline;
channels with roles, priority and profiles; plugin host with both invariants;
markdown, pronunciation and summarize transforms; the vault pack; the Claude
Code client; `speakctl`; tests and CI.

**Out:** document reader and position persistence; engines beyond Kokoro; Codex
and OpenCode adapters; the natural-language policy compiler.

**The event bus is now IN, at plan 2.** It was cut as speculation on the grounds
that nothing consumed events. That reasoning expired when the desktop GUI was
specified: a follow-along view is a subscriber, so the fan-out has a consumer
before it is written.

### Plan structure

Milestone 1 is delivered as three plans, because each must produce working
software on its own:

1. **Speaking pipeline** — timeline, segmenter, streaming synthesis, engine,
   player, `speakctl say`. Complete.
2. **Daemon** — transports, channels, roles, profiles, control verbs, event bus.
3. **Plugins and clients** — plugin host, built-in transforms, the vault pack,
   the Claude Code client.

Plans 2 and 3a (the plugin host and transforms) are independent of each other:
transforms need only the data model, not the daemon. They can run concurrently.
The Claude Code client depends on plan 2's protocol and follows it.

### Milestone 2: the desktop GUI

A Tauri shell — the OS webview rather than a bundled Chromium, roughly 3-10 MB
against Electron's 120-200 — around a web frontend that also serves as a plain
localhost page. Transport controls, and the source text with the spoken range
highlighted.

The shell owns nothing: queue, timeline and channels stay in the daemon, and the
page talks to it over the same HTTP and WebSocket transports agent clients use.
The Rust side does only what a webview cannot — global hotkeys (pausing from
inside an editor is the thing a browser tab can never do), system tray, and
supervising the daemon process. Highlighting is `span_at`; click-to-jump is
`time_of`, which is why that map is bidirectional. The cost of the OS-webview
approach is three engines to test — WebKitGTK, WKWebView, WebView2 — rather than
one.

**Done:** speakd has replaced narrator in daily use; a typical response starts
speaking in under three seconds; the citation pack reads a real vault note
correctly.

## Risks

- **CPU headroom.** Kokoro runs at RTF 0.75 on the reference machine. Any
  CPU-heavy work during playback causes underruns. All summariser backends are
  network or extractive by design.
- **Kokoro's Python ceiling.** `misaki` requires Python < 3.14. Pinned in
  `pyproject.toml` with the reason recorded.
- **Segment-length variance.** Time-to-first-audio tracks the first sentence's
  length. The clause-boundary fallback bounds this; the CI benchmark guards it.

## Open questions

Deferred deliberately; none blocks milestone 1.

- **Dictation interaction.** Claude Code's hold-to-talk dictation has no hook at
  record start, so speech cannot duck when recording begins — only at submit. May
  need a different signal.
- **Background event taxonomy.** Which events a background channel may interrupt
  for is currently three names; real use will refine it.
- **Distribution.** PyPI name `speakd` is free. Whether to publish, and when.

## Notes for the daemon

Learned while building plan 1; each one bites at plan 2's layer, not plan 1's.

- **Never reuse a cancellation `Event` across utterances without clearing it.**
  `speak()` no longer writes to a caller-supplied Event, so the hazard is now the
  caller's alone: a channel holding one long-lived Event that is set once and not
  cleared goes permanently mute, with an empty timeline, no errors and no
  exception. Prefer a fresh Event per utterance.
- **Hush cannot interrupt a segment through cancellation.** `play()` blocks by
  design, so setting the Event is only noticed when the current segment ends —
  a two-to-four second tail at the default segment length, on the most common
  path there is. The daemon must call `player.stop()` directly from its control
  thread.
- **`SoundDevicePlayer` drives sounddevice's process-wide stream**, so with
  several channels one session's `stop()` silences another's audio. Serving
  multiple sessions needs a per-player `OutputStream`.
- **`speak()` always starts at unit 0.** Seek and resume need a start index, and
  `SpeechResult` does not retain the input pieces, so either re-segmentation must
  be deterministic or the units must be kept.
- **`Timeline` is per-call**, with offsets restarting at zero. One timeline per
  job is the right model; the event bus must not assume a single global clock.

## References

- arXiv:2608.25512 — *A Programming Paradigm for Spatiotemporal Composability*
- `claude-code-narrator` — prior art, source of the pronunciation table
- `opencode-voice-plugin` — an existing HTTP client for a TTS server
