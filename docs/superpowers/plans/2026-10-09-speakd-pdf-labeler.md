# Speakd PDF Component Labeler / Ground-Truth Tool — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development. TDD throughout.

**Goal:** A human-in-the-loop component labeler for speakd's PDF narration, in the Zotero plugin: show, inside the Zotero reader, what the pipeline does with each captured segment (its predicted category), let the owner re-categorize any segment with one click/keypress, persist the labels per item, apply them live over the heuristics, and export labeled documents as ground-truth fixtures (with a capture-gap report) that score the deterministic rules and pin regressions.

**Spec (binding):** `docs/superpowers/specs/2026-10-09-speakd-pdf-labeler-design.md` (revised 2026-10-09, second pass — the revision list at its top, items 1–8, is part of the spec). Cited below as "spec §N". The cleanup architecture it builds on: `docs/design/2026-10-08-pdf-text-cleanup-proposal-glm.md` ("proposal §N"); the Zotero internals reference: `docs/design/2026-09-24-zotero-10-read-aloud-internals.md` ("internals §N").

**Workspace:** worktree `../speakd-worktrees/pdf-text-cleanup`, branch **`feat/pdf-labeler`** (cut from `feat/pdf-text-cleanup`, whose shipped filters — `segmentfilter.ts`, `footnotes.ts` — the labeler builds on; do not rebase over unrelated work).
- Zotero plugin: `cd clients/zotero && npm ci`, then `npm test` and `npm run build` (vitest + `tsc --noEmit`; `npm run build` runs both).
- Root gates stay green (the labeler adds no Python, but the skill corpus tests are part of them): `uv sync`, then `uv run pytest`, `uv run mypy src tests`, `uv run ruff check .`, `uv run ruff format --check .`.

## Global Constraints

- **Never mis-highlight; segment-level only (the offset-map contract).** A label attaches to a *whole captured segment* and applies only at the seams where whole-segment decisions are already made: `footnotes.ts` `tagSegments` (its overrides parameter) and its callers — `channel.ts` `planned()` for the live read (channel.ts:259–261), `export/parts.ts` `buildParts` (parts.ts:117) for the export. Highlights resolve by segment index (`sections.ts` `Section.segmentIndices`), so a labeled-away segment never appears in a section and can never be mis-highlighted; a deferred one keeps its true index and highlights its own rect. **A label never touches characters** — `cleanup.ts`, `llm.ts`, `mapping.ts`, `offsetmap.ts` are untouched; the worst a label can do is drop or keep speech, the same blast radius the filters already have.
- **Time-to-first-audio stays 2–4 s.** The override map is resolved **once per capture** (pure matching over the segment list, microseconds) and consulted at `planned()`/`buildParts` time as one map lookup. The label *file* is loaded asynchronously at reader adoption and cached — never awaited in the read path; a file not yet loaded means no overrides (fail-open), and the next read has them. Nothing in a live read awaits label I/O or the pane.
- **No daemon changes.** Nothing under `src/` (the daemon) changes; the labeler is entirely client-side. Label mode never enqueues anything to speakd (its controller is a `captureOnly` controller, session.ts:188, 311–313).
- **`reader-takeover.ts` stays the only internals module; the probe list is unchanged (internals §7, spec §14).** The only new mechanism inside it: four `ReaderHandle` entry points (`enterLabelMode`, `exitLabelMode`, `labelSegment`, `exportFixture`) and the pane, injected into the reader iframe's `win.document` at the existing `STYLE_ID` site (reader-takeover.ts:269–273) under the existing `guard()` wrapper (reader-takeover.ts:824). No new probes. The gap check's pdf.js reach is the `readStructure` reach (`export/structure.ts:65–69`), used best-effort inside `try`/`catch` — a failure costs the gap report, never the reader.
- **Fail-open everywhere (spec §10).** Unparsable JSON or an unknown `schema` → ignored, one warning, the read proceeds as if unlabeled. Unmatched locators → inert, listed in the pane, never applied. Pane injection failure → guard warns, label mode reports "unavailable", reading is exactly as it was, and stored labels still apply through `planned()` (label *application* and label *making* fail independently). Capture timeout (`CAPTURE_WAIT_MS`, session.ts:121) → the pane says why, label mode exits, the read path untouched. `Esc` abandons the capture *wait* (the existing give-up path), never Zotero's build.
- **Labels feed detection, never the footnote policy.** A `footnote` label makes a segment a footnote *candidate*; the existing policy pref (`classify` | `drop_all` | `defer_all`) then decides drop/defer/classify exactly as for any detected footnote. The policy stays one knob, document-independent. A `body` label is a forced keep (the running-head filter and footnote detection both skip it). `running-head` / `footer` / `page-number` / `junk` → dropped, with the same one-line audit log the filters write (`DroppedSegment`, why `"label"`). `heading` → informational in v1: spoken as body.
- **In-copyright fixture bound (spec §8.5, as corrected).** Derived artifacts only; the PDF is never checked in (its `sha256` + owner path in the fixture README). At most **8 pages and ~20 KB of *text content*** (whitespace-collapsed readable prose, summed over the fixture's derived artifacts, however encoded) per in-copyright work without the owner's sign-off. Geometry bytes do not count (`kohler-p36.xml`: five pages, ~75 KB of file, ~9 KB of text, inside the bound). When the source PDF *can* be checked in, regenerate derived encodings at test time instead of checking them in (the `tests/fixtures/corpus/README.md` precedent).
- **Never touch the user's real setup.** No writes to `~/Zotero`, the real profile, the running daemon, its socket or port 8642. Live Zotero work uses only `clients/zotero/test-live/` (its own profile under `~/.cache/speakd-zotero-test`, fake-engine daemon on HTTP 8743): `setup-profile.sh`, `start-fake-daemon.sh &`, `run-zotero.sh`, `zeval.sh`. Kill harness processes with `pkill -f "[s]peakd-zotero-test/profile"`.
- **Repo style gates hold:** ruff line length 100, mypy strict (root), vitest + `tsc --noEmit` (`clients/zotero`). Commit messages say why, not what.

## Task ordering

Task 1 (the pure store and matching) lands before anything reads it; Task 2 (the `tagSegments` overrides) before any caller passes one; Task 3 wires the two pipelines; Task 4 is the UI (it rides on 1–3, breaks nothing if it fails); Task 5–6 are the ground-truth outputs; Task 7 is live verification and the first real fixtures; Task 8 is docs and the full gate. Each task is independently testable and leaves both suites green.

---

### Task 1: `clients/zotero/src/labelstore.ts` — the label file, locators, matching, atomic save (spec §7)

New pure module (Zotero APIs no further than paths/IO, the `debugdump.ts` pattern of `declare const IOUtils: any`):

- **Types:** `LabelCategory = "body" | "footnote" | "heading" | "running-head" | "footer" | "page-number" | "junk"`; `LabelLocator { page: number; rectY: number | null; anchor: string | null; prefix: string; textSha256: string }` (prefix = first 48 chars, whitespace-collapsed; `textSha256` = sha256 of the full collapsed text — pure JS `crypto.subtle.digest` or a small sync helper if the environment lacks it, decided by what `npm test` runs under); `LabelRecord { locator; category; labelledAt; note }`; `LabelFile { schema: 1; attachmentKey; documentSha256; capture: { zoteroVersion; segments; labelledAt }; labels: LabelRecord[] }` — the exact JSON of spec §7.1.
- **Locator building:** `locatorFor(segment: CapturedSegment): LabelLocator` from `pageIndex` / `firstRectY` / `anchor` / `text` (session.ts:67 `CapturedSegment`).
- **Matching (spec §7.2), pure:** `matchLabels(file: LabelFile, segments: readonly CapturedSegment[]): { overrides: ReadonlyMap<number, LabelCategory>; unmatched: readonly LabelRecord[] }` — (1) same page + same `textSha256` → exact hit; (2) same page + same `prefix` + same `anchor` → hit; (3) anything else → unmatched. **No fuzzy matching below exact prefix.** First rule wins; a segment already matched is not re-matched.
- **Load/save:** `parseLabelFile(json: string): { ok: true; file: LabelFile } | { ok: false; why: string }` (unparsable or `schema !== 1` → `{ ok: false }`, the caller logs one warning and stands down — fail-open); `readLabelFile(path)` / `writeLabelFile(path, file)` on `PathUtils.join(PathUtils.profileDir, "speakd-reader", "labels", "<attachmentKey>.json")` — the `stagingPath()` pattern verbatim (`export/host.ts:26–27`), atomic write (temp + rename, the `IOUtils` pattern `debugdump.ts` uses). Pure string variants exported for the tests; the IOUtils wrappers stay thin.

**Tests first** (`clients/zotero/test/labelstore.test.ts`): locator shape from a segment with/without rects; exact hash match; prefix+anchor match across a synthetic re-segmentation that moved boundaries around a surviving text; near-miss prefix → unmatched and inert; a label whose page drifted → unmatched; `parseLabelFile` on the spec §7.1 example round-trips; unparsable JSON → `{ok:false}`; `schema: 2` → `{ok:false}`; save/load round-trip (string variants). **Verification:** `cd clients/zotero && npm test && npm run build`.

---

### Task 2: `clients/zotero/src/footnotes.ts` — the `tagSegments` overrides parameter (spec §8.2)

Extend `tagSegments` (footnotes.ts, signature `tagSegments<S extends PlanSegment>(segments, policy)`) with a third optional parameter `overrides?: ReadonlyMap<number, LabelCategory>` (import the type from `labelstore.ts`; existing callers pass none and are unchanged — `classificationResidue`, `channel.ts`, `export/parts.ts` compile untouched until Task 3 opts in). Overrides are keyed by **original document index** and applied to the full tagged list, after both filters have run, so index-keying survives the head filter's subsetting:

- `body` → tag `"body"` even where `filterRunningHeads` dropped it (the restore path) and even where footnote detection would have fired;
- `footnote` → the segment enters the footnote plan **as detected**: the policy decides — `classify` runs the existing `classifyShape`, `drop_all` → `"drop"`, `defer_all` → `"defer"` (exactly `planFootnotes`'s `footnote()` mapping, footnotes.ts — factor it out rather than duplicating);
- `running-head` / `footer` / `page-number` / `junk` → tag `"drop"`, and the record joins the `dropped` audit list with `why: "label"` (extend `DroppedSegment.why` with `"label"`);
- `heading` → tag `"body"` (informational in v1; spoken);
- unlabeled → the heuristics exactly as today.

**Tests first** (additions to `clients/zotero/test/footnotes.test.ts`): a `body` override restores a recurring-prose false positive (a paragraph recurring on ≥ `HEADER_SHARE` of ≥ `HEADER_MIN_PAGES` pages that the head filter drops unaided); a `footnote` override makes the OCR'd `il found recently that these words are quoted…` a candidate under **each** policy (`classify` → shape rules decide, `drop_all` → drop, `defer_all` → defer); a `running-head` override drops `i6  THE PLACE OF VALUE IN A WORLD OF FACTS` on a **3-page** input (below `HEADER_MIN_PAGES`, so the unaided filter never fires — the override is recurrence-independent); a `junk` override drops and audits `why: "label"`; a `heading` override changes nothing spoken; no overrides → byte-identical result to today (the existing footnotes/segmentfilter/corpus tests pass **unchanged** — they are the no-labels case). **Verification:** `cd clients/zotero && npm test && npm run build`.

---

### Task 3: Wire the live and export paths — the per-capture override map (spec §8.1, §8.2, §7.2)

- **`channel.ts`:** `ChannelOptions` gains `overrides?: (segments: readonly ReadSegment[]) => ReadonlyMap<number, LabelCategory> | null`. The resolver is called **once per start** (per capture — spec §7.2), its result cached on the `Read`; `planned()` (channel.ts:259–261) passes it into `tagSegments(segments, this.footnotes, map)`. Every applied override writes one audit line through the existing `log` ("label: segment N → running-head (dropped)"). Microseconds; TTFA untouched.
- **`export/parts.ts`:** `BuildPartsOptions` gains `overrides?: ReadonlyMap<number, LabelCategory>`; the `tagSegments` call at parts.ts:117 passes it. The export path resolves the map itself, after its capture completes (`export/capture.ts`) — exports are not TTFA-bound: load the item's label file (`labelstore.readLabelFile`, fail-open), match against the captured segments, pass the map in. One seam, no parallel pipeline.
- **Who supplies the live resolver:** the reader path in `reader-takeover.ts` — at adoption, kick off `readLabelFile` for the item's `attachmentKey` (async, cached by key; never awaited by a read); the resolver handed to `Channel` uses the cached file (or none, fail-open) and `matchLabels` against that start's segments. A new controller (new read, jump, re-capture, label-mode entry) re-matches and reuses nothing (spec §7.2). **A mid-read label write cannot happen on the live path** — labels are written in label mode only (Task 4), which stops the read.
- Two readers on one item share one label file; writes are whole-file and atomic (Task 1), last writer wins, the loser's stale pane list is repaired by its next matching pass.

**Tests first:** `channel.test.ts` — a fixture read where a labeled `running-head` segment never appears in any enqueued section and the audit line is logged; a labeled `body` restores a head-filter false positive into the enqueued text; a labeled `footnote` lands at its section's end under `defer_all`; no `overrides` option → identical to today (existing channel tests unchanged); the resolver is called exactly once per start (a counting resolver across two starts); a resolver returning `null` behaves as unlabeled. `export/parts.test.ts` — overrides reach the parts (a labeled junk segment absent from every part's text); no map → today's output. `labelstore`-level: a schema-2 file → `{ok:false}` and the resolver yields no overrides. **Verification:** `cd clients/zotero && npm test && npm run build`.

---

### Task 4: The pane, the labeling controller, and the four `ReaderHandle` entry points (spec §6, §14, §10)

**`clients/zotero/src/labeler.ts` (new):** the pane and the label-mode controller.

- **The pane:** a narrow column injected into the reader iframe's `win.document` at the existing `STYLE_ID` site (reader-takeover.ts:269–273) — an interactive DOM subtree with listeners, created in the plugin's compartment, **inside the takeover's `guard()`** so it can never throw into Zotero. It lists the current page's captured segments in order (the held whole-document list filtered by `pageIndex`), each with a colored category chip seeded from the pipeline's own prediction (`tagSegments` without overrides — prediction-first, so labeling is mostly confirm-or-correct). ↑/↓ walk segments and continue into the next page; a "next unlabeled" jump; unmatched labels listed ("N labels no longer match this document — re-label or discard").
- **The labeling controller:** built through the reader's `ReaderSession.createController` machinery (session.ts:470) as a `captureOnly` controller (session.ts:188 — "it holds the segments, and never starts the channel", enforced at session.ts:311–313) whose sink emits `ActiveSegmentChange` (session.ts:295) so Zotero's own Read Aloud highlight draws on the selected segment (the mechanism internals §4.1 documents) and `navigateToPosition` scrolls to it. **It never enqueues anything to speakd.** Entering label mode during a read takes the live controller's segments (`session.capture()`'s fast path, session.ts:413–415) and stops the read — one controller per manager (internals §3.2).
- **Re-categorization:** click a chip → a category picker, or press a key with the pane focused: `b` body, `f` footnote, `h` running head, `p` page number, `k` junk, `g` heading. **Keys are pane-scoped:** the `keydown` listener sits on the pane element, fires only when the pane holds focus, and `Esc` calls `stopPropagation` — the reader's own keys are never intercepted, there are no global hotkeys. `Esc` exits label mode (destroys the labeling controller, clears the highlight the way the read path clears it). Each label write goes through `labelstore` (atomic save) and updates the override map the pane shows.
- **Progress/abort (spec §6.1):** while the capture runs the pane shows a "capturing…" state (spinner, elapsed seconds, the sentence "Zotero is preparing the document; this happens once per label-mode session" — elapsed time, never a faked percentage); `Esc` abandons the wait through the existing give-up path (the SDT pass finishes in the background, its result discarded); the capture timeout (180 s) says why and exits label mode.

**`clients/zotero/src/reader-takeover.ts` (extend — the one fragile module, unchanged in kind):** `ReaderHandle` (reader-takeover.ts:91) gains exactly four entry points (spec §14): `enterLabelMode(): Promise<LabelResult>`, `exitLabelMode(): void`, `labelSegment(index: number, category: LabelCategory): Promise<LabelResult>`, `exportFixture(path: string, range?: { first: number; last: number }): Promise<FixtureResult>` — implemented on `Adopted` for real, on `Unadopted` as fail-open no-ops returning a refused result (the `dumpSegments` precedent, reader-takeover.ts:127–131). The UI and the `zeval.sh` harness bridge both call these (`Zotero.SpeakdReader.handles().find(...)`); there is no second path. **No new probes** — everything needed is already probed (`installIframeHooks`, reader-takeover.ts:246) or guarded.

**`clients/zotero/src/controls.ts`:** a "Label" toggle button in the existing `speakd-bar` (`button()` helper, controls.ts:119), disabled while the capture runs, showing the refusal reason on `Unadopted`.

**Fail-open stories (spec §10), each tested:** pane injection fails at runtime → guard warns, label mode reports "unavailable" in the control bar, reading is exactly as it was, stored labels still apply through `planned()` (Task 3); a reader that passes the probes but whose controller construction fails → the pane opens, clicking a segment reports "the highlight could not be driven", labels are still makeable from the pane's text list; a crashed pane costs the pane, never the read path.

**Tests first:** `clients/zotero/test/labeler.test.ts` — pane DOM shape from synthetic segments (chips seeded with the pipeline's prediction); page filtering by `pageIndex`; a category key writes a `LabelRecord` through a stubbed store and updates the chip; pane-scoped keys (a `keydown` on the pane element only — a listener on the reader document never sees `b`/`f`/`Esc`); `Esc` exits and clears; unmatched-label listing; "next unlabeled" jump; capture-timeout state; `captureOnly` controller never enqueues (assert zero channel starts through a stub). Additions to the takeover/controls tests — the four entry points exist on `Unadopted` and return refused results; on `Adopted` they delegate to the labeler (mocked); the Label toggle appears, is disabled during capture, surfaces the refusal reason. `session.test.ts` — entering label mode during a read stops the read. **Verification:** `cd clients/zotero && npm test && npm run build`.

---

### Task 5: `clients/zotero/src/export/gaps.ts` + `clients/zotero/src/export/fixture.ts` — capture gaps and the fixture export (spec §8.3, §8.4, §8.5)

**`export/gaps.ts` (new, pure + Zotero glue):**

- Pure: `pageGaps(rawPageLines: readonly string[], capturedTexts: readonly string[]): readonly string[]` — whitespace-collapsed raw lines of the page minus the page's captured segment text (containment after collapse); a planted dropped block is reported, an empty page reports nothing, an exact match reports nothing.
- Glue: `rawPageLines(reader, pageIndex)` reads the page's raw text from pdf.js through the **same reach `export/structure.ts readStructure` uses** (structure.ts:65–69: `reader._internalReader._primaryView._iframeWindow.PDFViewerApplication.pdfDocument` → `page.getTextContent()`), its text items grouped into lines best-effort by y. Everything inside `try`/`catch`: a reader where the reach fails or a `getTextContent` rejects → "gap check unavailable" in the README, **never blocks the export, never throws into Zotero, no new probe** (spec §8.4, §14).

**`export/fixture.ts` (new):** `exportFixture(path, range)` (driven through `ReaderHandle.exportFixture` — the pane's "Export fixture…" button and the harness bridge call the same action) writes, under the given directory (`clients/zotero/test/fixtures/pdf/<slug>/` for repo fixtures):

- `capture.jsonl` — the captured segments, the exact `dumpSegments` per-segment shape (`export/debugdump.ts` — index, text, pageIndex, anchor, rectCount, firstRectY), the format `corpus.test.ts` already parses;
- `labels.json` — the label file verbatim (**the ground truth**);
- `expected.json` — the **regression snapshot, not ground truth**: per-segment expected tags via `tagSegments` **with** overrides, plus the expected section/part text the shipped pipeline produces today;
- `README.md` — source PDF `sha256` + owner path (never the file), the harness command to reproduce, the gap report (dropped-before-capture items), and the in-copyright bound record (pages, text-content size).

**Tests first:** `clients/zotero/test/export/gaps.test.ts` — pure cases above; a stubbed reader without the pdf.js reach → "gap check unavailable" and the export still completes; `clients/zotero/test/export/fixture.test.ts` — the four files written from synthetic segments + labels; `capture.jsonl` parses with the `corpus.test.ts` shape; `expected.json` reflects overrides; the README records the sha and the bound. **Verification:** `cd clients/zotero && npm test && npm run build`.

---

### Task 6: `clients/zotero/src/scoring.ts` + corpus consumption — the tuning metric (spec §8.3, §12)

New pure module (the spec §13 sketch does not name it; spec §12 requires it as a pure function over a fixture — it is pure and small):

- `scoreFixture(segments: readonly PlanSegment[], labels: readonly { index: number; category: LabelCategory }[], unaided: readonly TaggedSegment[]): Score` where **only labeled segments count** (unlabeled segments are not asserted ground truth) and **only detection is scored** (the policy decides fate, spec §8.3):
  - **miss (recall failure):** a ground-truth `running-head`/`footer`/`page-number`/`junk` segment the unaided rules tagged `body` or `defer`/`classify` (spoken or held as body), **or** a ground-truth `footnote` the unaided rules did not tag `drop`/`defer`/`classify` (i.e. not detected as a candidate at all);
  - **over-drop (precision failure):** a segment the unaided rules tagged `drop` whose ground truth is `body`;
  - `precision` = correct drops ÷ all drops; `recall` = caught ground-truth non-body ÷ all ground-truth non-body; footnote **detection** scored in its own line (a detected footnote's drop/keep is the policy's, excluded) so a `classify` default does not silently punish a correct detection.
- Wire into `corpus.test.ts`: a checked-in fixture is consumed **twice** — unaided (documents today's failure as the characterisation) and with overrides (documents the fix) — and the unaided run's score is asserted against the fixture's known misses/over-drops (the synthetic fixture first; the real Köhler fixture lands in Task 7).

**Tests first:** `clients/zotero/test/scoring.test.ts` — a synthetic fixture with planted misses (a spoken running head, an undetected footnote) and over-drops (a dropped body paragraph) yields exactly those lists and the precision/recall numbers; unlabeled segments counted neither way; a detected-but-policy-kept footnote is not a miss; zero-disagreement fixture scores 1.0/1.0. **Verification:** `cd clients/zotero && npm test && npm run build`.

---

### Task 7: Live/harness verification + the first real fixtures (spec §12, §8.5, §9)

Using `clients/zotero/test-live/` (its README "Development"), record the findings in a new `docs/design/2026-10-09-labeler-verification.md` (the Stage 0 notes pattern):

1. **Headless labeling on the Köhler item** (`zeval.sh`, the `debugdump.ts` precedent — every call is `Zotero.SpeakdReader.handles().find(h => h.sourceId.includes("<itemKey>"))…`): `enterLabelMode()` (cold capture on a slow document: pane shows "capturing…", elapsed seconds; `Esc` abandons cleanly, the reader is as Zotero had it; a second entry within the session is instant); `labelSegment` on the three failing segments — the `il found recently…` footnote (PDF p. 35), the `i6  THE PLACE OF VALUE…` running head and the title head (p. 36); `exitLabelMode()`.
2. **The live read, labels applied:** a read of the labeled pages — the footnote deferred or dropped per the policy pref, the heads gone from every enqueued section, the labeled `body` restore audible, **no highlight ever on a labeled-away segment**, and no change in reading behaviour on unlabeled pages. **Time-to-first-audio re-measured** against the 2–4 s budget (the override map is one lookup at `planned()` time — confirm by timing, first section unchanged by construction).
3. **The fixture export and the gap report:** `exportFixture("/repo/path", { first: 34, last: 38 })` on the Köhler item; check in the derived artifacts only (`capture.jsonl`, `labels.json`, `expected.json`, `README.md` with the `sha256` and the bound record) under `clients/zotero/test/fixtures/pdf/kohler/`, **inside the §8.5 text-content bound** (pages 34–38; measure the whitespace-collapsed text across the artifacts and record the number in the README — it must be ≤ ~20 KB). Also drive the gap detector on a planted-"excluded" synthetic PDF in the harness library (a body block Zotero's SDT drops before the plugin sees it) and assert the fixture README names the block; and a reader whose pdf.js reach is broken writes "gap check unavailable" and still exports.
4. **Replay into the suites:** `corpus.test.ts` consumes the checked-in Köhler fixture with and without overrides (Task 6's wiring); the no-override run documents the failure, the with-override run the fix; `scoring` reports the unaided rules' miss/over-drop on the real fixture — the Stage 0 "conditional on the owner's sample" debts become payable numbers.
5. **Fail-open, live:** a hand-corrupted label file in the test profile → one warning, the read proceeds as if unlabeled; two reader tabs on one item → last writer wins, the stale pane repaired by its next matching pass.
6. **Root gates** stay green (nothing Python-side changed; the skill corpus's own Köhler regression — five pages, PDF 35–39, per the corrected spec §8.5 — runs untouched).

**Verification:** the whole checklist above recorded in the notes file; then `cd clients/zotero && npm test && npm run build` and `uv run pytest tests/test_audiobook_corpus.py && uv run mypy src tests && uv run ruff check . && uv run ruff format --check .`.

---

### Task 8: Docs and the full gate

- `clients/zotero/README.md`: label mode (entry, capture/abort, the pane, the keys), the label store location (`<profile>/speakd-reader/labels/<attachmentKey>.json`), the four harness entry points, the fixture export and the gap report, the limits (labels can only re-categorize what the capture produced; gaps are visible, not fixed), and the fixture bound policy.
- `docs/HANDOFF.md`: the labeler entry — built, what is ground truth vs regression snapshot, what remains open (the spec §15 questions and their defaults).
- No `SKILL.md` change: skill-path label consumption stays deferred (spec §9, §15 Q3) until a corpus wants it.
- **The gate, all green:** `cd clients/zotero && npm test && npm run build`; at the root `uv run pytest`, `uv run mypy src tests`, `uv run ruff check .`, `uv run ruff format --check .`.

---

## Conditional items (do not guess; wait for the named input)

| Item | Blocked on | Task |
|---|---|---|
| Fixture bound tighter/looser than 8 pages / ~20 KB text content | the owner (spec §15 Q5; the recommended default is already the constraint above) | 7 |
| First-corpus scope beyond the Köhler pages 34–38 | the owner (spec §15 Q4; a born-digital fixture may be unbounded by copyright) | 7 |
| `prepare_text.py --labels` (skill-path consumption) | a real corpus that wants it (spec §15 Q3; deferred) | — |
| Heading labels suppressing speech | the owner (spec §15 Q2; default: informational, spoken) | 2 |
