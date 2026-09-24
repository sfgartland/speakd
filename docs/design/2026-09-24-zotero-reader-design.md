# speakd Zotero reader — design

**Date:** 2026-09-24
**Status:** approved
**Author:** Severin Gartland (designed with Kimi K3)

## Problem

The original design deferred "reading a vault note or a PDF by ear" to its own
milestone. This is the PDF half of that milestone, with a twist: instead of
building a standalone document reader, the reading environment is Zotero — the
user's library already lives there, and Zotero 10 ships a Read Aloud feature
whose machinery (segmentation, source positions, highlight rendering,
follow-scroll) can be reused rather than rebuilt.

The reader must be a speakd client, not a separate TTS integration: the daemon
owns the model, the queue, arbitration and the audio. A paper being read is a
channel like any other — arbitrated with agent sessions, silenced by `hush`,
visible in the window, labeled by its title.

## Decisions

Settled with the owner, in the order they were made:

1. **speakd owns the audio.** The Zotero plugin is a client. Zotero's own
   player never makes a sound; the daemon synthesises, queues, arbitrates and
   plays. The alternative — speakd as a synthesise-and-return-audio backend for
   Zotero's player — was rejected: it would bypass channels, arbitration, the
   event bus and the GUI.
2. **v1 scope: read the selection, and read from here to the end of the
   document.** Controls: play/pause, one sentence back/forward, speed, stop.
   Whole-document position persistence and navigation are later.
3. **Reuse Zotero 10's Read Aloud machinery** for segmentation and highlight
   rendering, hijacked the way the Zotero-TTS plugin does it, rather than
   extracting text and drawing highlights ourselves. This buys reading order
   (including two-column), header/footer/citation skipping and native
   follow-scroll at the price of depending on internal APIs that change with
   Zotero's 6–10-week release cycle. Accepted, with mitigations (§ Fragility).
4. **Citations and footnotes are skipped silently.** Narrative citations
   ("Stiegler (1994) argues") are prose and are read.
5. **Text cleanup — deterministic rules plus an LLM pass over an
   OpenAI-compatible endpoint — runs in the plugin**, streamed a section at a
   time, with a character offset map composed through every stage. It does not
   run in the daemon: a job-scoped LLM transform would sit between enqueue and
   synthesis and would break the time-to-first-audio contract on long
   documents, and daemon-side streaming transforms do not exist.

## Architecture

```
Zotero 10 reader (plugin, clients/zotero/)
│
│  1. user selects text / clicks "Read from here"
│  2. hijacked Read Aloud machinery → segments (text + PDF source positions;
│     reading order, headers, footnotes and citations already handled)
│  3. cleanup per section: deterministic rules, then LLM;
│     a diff of original vs cleaned keeps the offset map honest
│  4. enqueue section N ──────────────┐
│                                     ▼
│                          speakd daemon (new: HTTP+SSE transport)
│                             channel "zotero:<itemKey>", label = item title
│                             transforms → segmenter → engine → player
│                                     │
│  5. SSE events ◀────────────────────┘
│     position(span_start) → section map → segment → source position
│  6. push the Read Aloud view's active position → NATIVE highlight and
│     follow-scroll, sentence granularity
│  7. control bar → POST pause/resume/seek/speed/hush
```

**Sections, not one job.** A from-here read is N sequential jobs on one
channel, each about a page. Time-to-first-audio stays at two to four seconds:
section 1 enqueues as soon as its cleanup lands, section 2 is cleaned while
section 1 speaks. Arbitration stays natural — a notification can interrupt
between sections, a hushed agent session does not stall the paper. The cost,
stated plainly: seeking back clamps at the current section's start, and
`replay` covers only the last section. Cross-section seek is v2.

**Play after stop.** `hush` drops the queue, so a stopped read forgets its
sections. The plugin remembers the last position event's segment; pressing
play re-cleans and re-enqueues from that segment. This is the v1 resume.

## Daemon changes

**One is required: an HTTP+SSE transport** (`src/speakd/transport_http.py`,
standard library only — the design doc already promises HTTP, and the
dependency rule stands). It is an adapter over the same `Request`/`Response`
handler the Unix socket serves:

| endpoint | maps to |
|---|---|
| `POST /v1/enqueue` | `enqueue` |
| `POST /v1/pause`, `/v1/resume`, `/v1/hush`, `/v1/seek`, `/v1/set_speed`, `/v1/set_label`, `/v1/status` | the same verbs |
| `GET /v1/events` | `subscribe` as a server-sent-events stream |
| `GET /v1/health` | cheap liveness for the control bar's "no daemon" state |

Bound to `127.0.0.1`, port in config (default 8642). **Every request carries a
token** — see *Revision, 2026-09-24* below: loopback HTTP is not the Unix
socket's trust boundary, because any web page the user opens can reach it. The plugin consumes SSE with `fetch` + `ReadableStream`, not
`EventSource` — `fetch` is on Zotero's plugin-sandbox whitelist; `EventSource`
is not known to be.

**No transform surgery in v1.** The `pdf` profile binds no rewriting
transforms. Highlight spans must stay sentence-exact end to end, and a
rewriting transform marks its pieces inexact, dropping every segment's span to
the whole piece. What the pronunciation table would have done is covered by
the LLM cleanup's abbreviation handling instead. (Deferred, valuable, and not
v1: sub-span offset maps for rewritten pieces — a `difflib` alignment between
`spoken` and source per piece would keep sentence-exact spans under *any*
transform chain, and would fix the GUI's paragraph-level highlighting too.)

The channel is `zotero:<itemKey>`, labeled with the item's title via
`set_label`, so the window shows what is being read and per-channel mute,
hush and arbitration work unchanged. Two open PDFs are two channels.

## The plugin (`clients/zotero/`)

A fresh MIT plugin, TypeScript, built with the community scaffold
(`zotero-plugin-scaffold`), targeting Zotero 10 only
(`strict_max_version: "10.0.*"`). Prior art — Zotero-TTS and ZoTTS, both AGPL —
is **documentation only**: Zotero-TTS's engineering notes enumerate the
internals being hooked and are the reason this design is possible; no code is
copied.

Modules, with everything fragile in exactly one of them:

- `reader-takeover.ts` — **the only module that touches internals.**
  Intercepts reader creation, replaces the Read Aloud remote interface, hooks
  `setSegments` to capture segments with their source positions, puppeteers
  the controller, and shadows the view's active position. Keeps the native
  popup mounted-but-hidden — it owns OS media keys and gates highlight
  rendering. Every hooked member is probed for existence at reader open (§
  Fragility).
- `segments.ts` — builds sections (about a page each) from captured segments;
  the section index maps every enqueued character back to its segment.
- `cleanup.ts` — deterministic stage: hyphenation joins, whitespace collapse,
  leftover page numbers and markers. Pure functions.
- `offsetmap.ts` — diff (character-level Myers) between one stage's input and
  output, composition of maps, verification (monotonic, in-bounds), and the
  fallback: any inconsistency and the stage's input is used unchanged.
- `llm.ts` — the LLM stage. One chat-completions call per section against a
  configured OpenAI-compatible endpoint; the prompt demands minimal edits
  only: drop residual citation/footnote noise, expand abbreviations (Fig., et
  al., vs.), never rephrase. Timeout, HTTP error, empty answer or diff
  failure → the deterministic text is spoken, the failure is logged, and the
  control bar marks it once. Reading never waits on the network beyond the
  first section's budget.
- `speakd.ts` — HTTP verb POSTs, the SSE subscription with reconnect, and
  channel bookkeeping.
- `highlight.ts` — position event → composed offset map → segment → source
  position → pushed into the view. Highlight granularity is sentence-level
  (speakd's timeline is segment-granular; no word karaoke). Pauses need no
  handling: no position events, the highlight simply stays.
- `controls.ts` — the UI. Selection-popup buttons via the official
  `renderTextSelectionPopup` hook: "Read selection", "Read from here". A
  minimal control bar in the reader: play/pause, back one sentence
  (`seek --by -1`), forward one sentence (`seek --by 1`), speed down/up,
  stop. The bar reflects daemon events, never local guesses; daemon
  unreachable → "no daemon" state, actions disabled.
- Preference pane: endpoint, API key, model, LLM on/off (rules-only mode),
  speed and voice defaults.

Tab lifecycle: closing the PDF's tab hushes its channel. Zotero's own
"Read Aloud from Here" is left alone — the plugin adds its own entry points
rather than re-skinning Zotero's.

## Fragility

The accepted risk, and how it is contained:

- Everything hooked lives in `reader-takeover.ts`, enumerated in one list,
  each entry probed at reader open. A Zotero update that moves the machinery
  fails **loudly**: the plugin refuses that reader, logs the missing member,
  and reading actions say so. What it must never do is mis-highlight
  silently — a wrong highlight is the one failure worse than none.
- `strict_max_version: "10.0.*"`; the supported version is stated in the
  plugin's README with the per-release manual checklist (§ Testing).
- Zotero 9 and older get nothing: the machinery does not exist there, and the
  plugin declines to load rather than emulating it.

## Failure handling

- **No daemon** — control bar shows it, actions disabled, nothing queued for
  later. A daemon that appears is noticed on the next health poll.
- **LLM failure** — deterministic text is spoken; logged; marked once in the
  bar. Consistent with "speech never disappears silently": the failure is
  visible, the speech continues.
- **Cleanup/diff inconsistency** — the stage's input passes through
  unchanged, as the transform chain already does for failing transforms.
- **Internals mismatch** — loud refusal, above.
- **Mis-mapping defence** — before highlighting, the segment's expected text
  is checked against the resolved source position's text. Mismatch → no
  highlight for that segment, logged. Never a wrong highlight.

## Testing

- **Daemon**: HTTP+SSE transport tests in pytest, mirroring the existing
  Unix-socket transport suite — same verbs, same events, over the wire.
- **Plugin**: vitest over the pure logic — `offsetmap` (diff, composition,
  verification, fallback), `segments`, `cleanup`, `llm`'s failure paths with a
  stubbed endpoint. The reader-integration layer is not unit-testable
  meaningfully and does not pretend to be.
- **Manual checklist**, run per Zotero release: a single-column article, a
  two-column paper, a footnote-heavy philosophy paper; read selection, read
  from here, pause/resume, seek, speed, stop, daemon-kill mid-read.

## Staging

- **Stage A — working reader, no LLM.** Transport, scaffold, takeover,
  sections, deterministic cleanup, highlight, controls. Fully useful alone.
- **Stage B — LLM cleanup.** `llm.ts`, the diff-map stage it plugs into
  (present from A), the preference pane. Additive; nothing in A is revised.

## Non-goals (v1)

- Word-level karaoke highlighting.
- Cross-section seek; `replay` beyond the last section.
- Position persistence across sessions (Zotero saves *its* read-aloud
  position; wiring it to resume is v2).
- EPUB and snapshot reading (the machinery exists; PDFs first).
- Sub-span offset maps in the daemon (deferred, above).
- Reading-order guarantees beyond what Zotero's segmentation already provides.

## Risks

- **Zotero's release cadence** against internal APIs. Mitigated by the single
  fragile module, probes and loud failure — but the maintenance is real and
  per-release.
- **Flatpak networking.** Zotero here is a flatpak; loopback HTTP works from
  it (the browser connector depends on the same path), verified on first run
  of Stage A, not assumed.
- **LLM latency on section 1.** Bounded by a timeout with the deterministic
  fallback; the user sees the bar's mark rather than silence.

## References

- Zotero-TTS (AGPL) and its `notes/` — documentation of the hooked internals.
- ZoTTS (AGPL) — official-hook usage patterns (`renderTextSelectionPopup`,
  toolbar).
- `docs/design/2026-09-13-speakd-design.md` — the deferred reader milestone,
  the span-to-time map, the HTTP transport promise.
- Zotero plugin dev docs: reader event handlers, preference panes,
  `strict_max_version` requirement.

## Revision, 2026-09-24: review before planning

Reviewed against the daemon as it now stands (briefings, channel modes,
replay, pruning). Seven changes, all binding on the plan:

1. **The HTTP transport requires a token, from v1.** The Unix socket's trust
   model does not carry over: a browser tab can reach `127.0.0.1:8642`. A
   plain-text `POST /v1/enqueue` needs no CORS preflight, so any page could
   make the machine speak, hush or mute it; DNS rebinding could read
   `/v1/events`, which carries the text of every agent session. So:
   - The daemon creates `$XDG_CONFIG_HOME/speakd/http-token` (random,
     mode 0600) on first start, and every request must carry
     `Authorization: Bearer <token>`; a missing or wrong token is 401.
   - The `Host` header must be `127.0.0.1:<port>` or `localhost:<port>`
     (defeats DNS rebinding); anything else is 421.
   - No CORS headers are ever sent, so no page can read a response even by
     accident.
   - The plugin reads the token from its preference pane (the user pastes it
     once; `speakctl http-token` prints it). Reading the file directly is not
     assumed possible from inside the Zotero flatpak.
   - `set_capabilities` is not exposed over HTTP: nothing on this path
     briefs.
2. **The bar's controls act only on the plugin's own channel.** `pause`,
   `resume` and `seek` are global — they move whatever is speaking — so the
   bar enables ←/→ and pause only while the speaking channel (from the event
   stream) is this reader's `zotero:<itemKey>`, and shows "another channel is
   speaking" otherwise. Stop is `hush` scoped to its own channel. Speed is the
   listener's single global speed (the window's and `speakctl speed`'s); the
   plugin has no speed default of its own, and its −/+ change the same
   setting.
3. **A built-in `pdf` profile ships in `profiles.py`**, with no transforms, and
   the plugin enqueues with `profile: "pdf"`. The default profile runs
   markdown and pronunciation, both rewriting, which would drop every span to
   its whole piece and the highlight from sentence to page.
4. **Highlight by index, not arithmetic.** `started.segments` lists every
   daemon sentence with its span; `position.index` says which is playing.
   The plugin maps each daemon sentence's span to the Zotero segment(s) it
   overlaps once, at `started`, and looks positions up by index. A daemon
   sentence spanning two Zotero segments highlights the first.
5. **Time to first audio is protected explicitly.** Section 1 is short (the
   first two Zotero segments) and skips the LLM stage; the LLM cleans from
   section 2 on, while section 1 speaks. Measured on the reference laptop a
   first sentence still takes seconds to synthesise at RTF 0.75, so the
   "two to four seconds" above is a target, not a promise.
6. **The label is set on every read start**, not once: the daemon forgets
   channels idle for twelve hours, label and all.
7. **The LLM stage is off until an endpoint is configured**, and the
   preference pane says in one line that enabled, it sends the paper's text
   to that endpoint.

Unchanged: speakd owns the audio; Zotero's Read Aloud machinery supplies
segments and draws the highlight; sections as jobs; the fragility containment.
The Zotero-internals details the takeover module is written from live in
`2026-09-24-zotero-10-read-aloud-internals.md`, verified against 10.0.3's
source.
