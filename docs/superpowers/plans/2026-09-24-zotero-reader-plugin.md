# Zotero Reader, Phase 2: the Plugin — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans or subagent-driven-development. Steps use checkbox (`- [ ]`) syntax.

**Goal:** A Zotero 10 plugin in `clients/zotero/`. It reads the selection, or reads from here, through speakd, with Zotero's own sentence highlight and follow-scroll showing what is being spoken, and a small control bar.

**Architecture:** Zotero's Read Aloud manager builds a playback *controller* for every start or jump, holding the full segment list and the start index. The plugin supplies that controller for its own voice.

- **What the controller does:** it plays nothing. It turns the segments into speakd jobs (sections of about a page) and relays skip, pause and stop to speakd. When speakd's event stream says a sentence is playing, it fires `ActiveSegmentChange`, and Zotero draws the highlight and follows it itself.
- **Where the logic lives:** everything that touches Zotero internals is in `reader-takeover.ts`, and is probed at reader open. Everything else is pure TypeScript with vitest tests.

**Tech stack:** TypeScript, `zotero-plugin-scaffold` (build to `.xpi`), `zotero-types`, vitest. Node 26 is available through linuxbrew.

**Spec:** `docs/design/2026-09-24-zotero-reader-design.md`, including its *Revision, 2026-09-24*. The Zotero internals are in `docs/design/2026-09-24-zotero-10-read-aloud-internals.md`, which is cited below as "internals §N".

## Global Constraints

- **Target:** Zotero 10 only, with `strict_max_version: "10.0.*"`. The manifest's `applications.zotero` must contain `id`, `update_url` and `strict_max_version`.
- **License:** MIT, and no code copied from Zotero-TTS or ZoTTS (both AGPL).
- **Sandbox:** `fetch` is available, but not `AbortController`, `WebSocket`, `EventSource`, `Event`, `EventTarget` or `AudioContext`. Take those from `Zotero.getMainWindow()`, or from the reader iframe window (internals §1.3/§6).
- **Talking to speakd:**
  - HTTP at `http://127.0.0.1:<port>/v1/<verb>`, with `Authorization: Bearer <token>` on every request.
  - Events come from `GET /v1/events`, consumed with `fetch` and a stream reader.
  - The channel is `zotero:<itemKey>`, with `set_label` sent on every read start and `profile: "pdf"` on every enqueue.
- **Controls act on the plugin's own channel only:** ←/→ and pause are enabled only while the speaking channel is this reader's. Stop is `hush` on its own channel, and speed is the global `set_speed`.
- **Failure is loud:** a missing internal refuses that reader, logs it and says so, and never mis-highlights. The factory hooks never throw into Zotero; a throw kills the tab (internals §8.1).

## Review Focus

- A daemon sentence that spans two Zotero segments should highlight the first, never neither. Covered by the Task 2 mapping tests.
- A position event for a section that was superseded (the user jumped) must not move the highlight. Covered by Task 2's section-generation tests.
- A token or port that's wrong should produce a "no daemon"/"bad token" state rather than a hang. Covered by the Task 2 client tests.
- A document whose first segment is huge (a very long sentence) should still give section 1 no more than 2 segments. Covered by Task 2.
- Closing the reader tab mid-read should hush the channel exactly once. Covered live in Task 4.

### Task 1: scaffold and build
- [ ] Create `clients/zotero/`: `package.json` (with `zotero-plugin-scaffold`, `zotero-types`, `typescript` and `vitest` as dev dependencies), a scaffold config, `tsconfig.json`, and `addon/manifest.json`. The manifest gets id `speakd-reader@speakd`, name "speakd reader", `strict_max_version` "10.0.*", `strict_min_version` "10.0", and an `update_url` pointing at a placeholder update file in the repo.
- [ ] Add `addon/bootstrap.js` wiring startup and shutdown to `src/index.ts`, and a minimal `src/index.ts` that logs "speakd reader loaded".
- [ ] `npm run build` should produce `.scaffold/build/speakd-reader.xpi` (or the scaffold's equivalent), and `npm test` should run vitest.
- [ ] Add a `.gitignore` covering `node_modules` and the build output.
- [ ] Commit.

### Task 2: pure modules, all with vitest tests
- [ ] `src/sections.ts`: `buildSections(segments: {text:string}[], start: number, firstSize=2, size=chars≈1800) -> Section[]`. A `Section` is `{segmentIndices:number[], text:string, offsets:number[]}`, where `offsets[k]` is where segment k starts in `text`. Segments are joined with a single space, and section 1 holds at most `firstSize` segments.
- [ ] `src/cleanup.ts`: deterministic rules, which join `hyphen- ation` across a line break, collapse whitespace and drop stray page numbers. Each returns `{text, map}`, where `map` is an offset map.
- [ ] `src/offsetmap.ts`: a character-level diff from input to output, giving an `OffsetMap` (output index to input index). It also provides `compose(a,b)`, `verify(map, inLen, outLen)` (monotone and in bounds), and `apply` with a fallback to the identity map when verification fails.
- [ ] `src/mapping.ts`: `planHighlights(section, daemonSegments:{index,span_start,span_end}[], map) -> Map<daemonIndex, zoteroSegmentIndex>`. Each daemon sentence maps to the Zotero segment containing its start, and a sentence overlapping two maps to the first.
- [ ] `src/speakd.ts`, the client:
  - `constructor({port, token, fetch})`, `call(verb, sourceId, payload) -> Promise<Response>`, and `events(onEvent, signal)`, which parses SSE `data:` lines across chunk boundaries, ignores `:` comments, and reconnects with backoff.
  - A result type separates 401 (bad token) and network failure (no daemon) from success.
  - Tested with a stubbed `fetch` that yields chunked streams.
- [ ] `src/channel.ts`, the per-reader state machine:
  - `start(segments, startIndex)` enqueues section 1, then section 2 as soon as section 1 starts, one section ahead.
  - It tracks a `generation` per start and ignores events from older generations, and resolves `position → zoteroIndex`.
  - It exposes `skip(±1)` (a `seek {by}` while its own channel is speaking), `pause`, `resume`, `stop` (a `hush` on its own channel) and `onHighlight(cb)`.
  - Tested against a fake client that replays scripted event sequences: supersede on jump, the end of a section rolling into the next, and stop.
- [ ] Commit after each module.

### Task 3: live harness spike (the unverified compartment mechanics)
- [ ] Run Zotero 10.0.3 from the flatpak with a throwaway profile (`-P speakd-test -no-remote`, created with `-CreateProfile`) and `--marionette` if that's supported, and drive it with a small Python Marionette client, installed with `uv run --with marionette_driver`.
- [ ] If that works: install the built `.xpi`, open a test PDF through `Zotero.Reader.open`, and execute chrome JS to check each UNVERIFIED item in internals §UNVERIFIED that the takeover depends on: an exported function on a content object, `new win.EventTarget()` with manager listeners, and the expando `segment` on a content `Event`.
- [ ] If Marionette is not available in this build: record that, and fall back to a manual checklist plus a debug log the plugin writes. Never guess at the mechanics silently.
- [ ] Record the findings in the internals doc under a new "Verified live" section, then commit.

### Task 4: reader takeover (internals §8)
- [ ] `src/reader-takeover.ts`:
  - Wrap `_readers.push` and adopt each reader.
  - Provide a merged remote interface with a speakd voice.
  - After `_initPromise`, apply the iframe hooks: hide the popup with CSS (keeping it mounted), force `loadVoices(true)`, apply the `_createController` guard, and set sentence granularity.
  - Build the controller in the content compartment, connected to `channel.ts`.
  - Check the probe list from internals §7 at adopt time.
  - `takeoverWanted` stays true only for reads the plugin started. Restore the user's previous voice on stop (internals §8.4).
  - Clean up on shutdown.
- [ ] Verify live with the Task 3 harness. Checks:
  - Start at a segment and see `ActiveSegmentChange` events as speakd reports positions.
  - A jump supersedes the current section.
  - Stop clears the highlight.
  - Closing the tab hushes once.
  - No sound comes from Zotero, beyond the −64 dBFS media tone the internals doc describes.
- [ ] Commit.

### Task 5: controls and preferences
- [ ] `src/controls.ts`:
  - "Read selection" and "Read from here" through `renderTextSelectionPopup`, which call `startReadAloudAtPosition(params.annotation.position)`.
  - A small control bar through `renderToolbar`, or an injected bar, with play/pause, ←, →, speed −/+ and stop.
  - It reflects the daemon's state: its own channel speaking, another channel speaking, no daemon, or a bad token.
- [ ] A preference pane with port (default 8642) and token (pasted in; the help text points at `speakctl http-token`).
- [ ] Verify live, then commit.

### Task 6: docs and packaging
- [ ] `clients/zotero/README.md`: install from the `.xpi`, get the token, the supported Zotero version, the manual per-release checklist, and the known limits (sections, the media tone, readers opened before install).
- [ ] The main README gets a "Reading PDFs in Zotero" section covering the plugin and the daemon's HTTP transport. This completes phase 1's Task 5.
- [ ] Add a CI job that runs `npm ci && npm test && npm run build` in `clients/zotero`.
- [ ] Commit.
