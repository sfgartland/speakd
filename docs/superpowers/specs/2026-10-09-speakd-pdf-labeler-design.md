# PDF component labeler / ground-truth tool

**Date:** 2026-10-09
**Status:** proposal for review — not a decision; no code changed
**Author:** assistant (GLM), per the brief in
`docs/design/2026-10-08-pdf-text-cleanup-request.md` and the architecture it
rests on
**Depends on:** `docs/design/2026-10-08-pdf-text-cleanup-proposal-glm.md`
(the binding cleanup spec — the segment-level filter, `segmentfilter.ts` /
`footnotes.ts`, shipped), its implementation plan
`docs/superpowers/plans/2026-10-08-pdf-text-cleanup.md`, the capture boundary
and offset-map contract of the reader design
(`docs/design/2026-09-24-zotero-reader-design.md`), and the Stage 0
characterisation (`docs/design/2026-10-08-pdf-text-cleanup-stage0.md`)

Section references: "§N" is this document; "internals §N" is
`docs/design/2026-09-24-zotero-10-read-aloud-internals.md`; "proposal §N" is
`docs/design/2026-10-08-pdf-text-cleanup-proposal-glm.md`; "plan" is
`docs/superpowers/plans/2026-10-08-pdf-text-cleanup.md`.

## 1. Revision, 2026-10-09 (after review)

Eight corrections, folded into the body below rather than kept as an errata
list:

1. **The gap detector is JS, not Python** (§8.4). Fixture export runs inside
   the plugin, which cannot run Python. The diff now runs in JS via pdf.js
   `getTextContent`, through the same `pdfDocument` reach
   `export/structure.ts readStructure` already uses; the module is named
   (`export/gaps.ts`) and the harness invocation is stated.
2. **`expected.json` is a regression snapshot, not ground truth** (§8.3). The
   ground truth is `labels.json`; the tuning metric (miss / over-drop,
   precision / recall) is now defined and is computed from the unaided rules
   against `labels.json`.
3. **Copyright is bounded by a concrete policy** (§8.5), no longer an open
   question: derived artifacts only, a hard page/size bound per in-copyright
   work, labels keyed by hashes and prefixes, never the PDF.
4. **Label-mode capture is whole-document, with an explicit progress/abort
   story** (§6): the capture boundary is document-granular, the pane says
   what it is doing, and `Esc` abandons the wait.
5. **The new mechanism inside `reader-takeover.ts` is pinned** (§6, §14): the
   exact `ReaderHandle` entry points, the probe-list decision (no new
   probes), what the pane is injected into, and the fail-open story when
   injection or the highlight machinery is absent.
6. **Storage follows the `stagingPath` pattern verbatim** (§7): the flatpak
   reasoning from `export/host.ts` is cited, not paraphrased.
7. **A standalone HTML review report is weighed and rejected as the primary
   surface** (§11): the native highlight is the tool's value; the report is
   kept as a documented fallback, and the label format is
   reader-independent, so the fallback costs nothing to keep open.
8. **The §8.5 fixture-size claim was wrong about the shipped fixture and is
   corrected** (second revision pass): `kohler-p36.xml` is not "one page,
   one failure mode" — it is **five pages, PDF 35–39**, because the running-
   head recurrence rule needs `HEADER_MIN_PAGES = 5` pages and a one-page
   trim would break the recurrence regression the fixture pins. And the
   ~20 KB bound is now defined as a bound on **text content** — the
   readable prose a fixture exposes, however encoded — not on encoded file
   size: the stext XML is ~75 KB of mostly geometry carrying ~9 KB of text,
   well inside the bound. Stage 0's notes
   (`docs/design/2026-10-08-pdf-text-cleanup-stage0.md:164–166`) already
   stated the true shape; §8.5 is aligned to them.

Also fixed: the broken "Depends on" citation (the cleanup design is the
proposal at `docs/design/…proposal-glm.md`, not a spec under
`docs/superpowers/specs/`); the fixture path is the real one
(`clients/zotero/test/fixtures/`, where `corpus.test.ts` reads its dumps);
the earlier §-references, which never matched the heading count, are now
numbered and consistent; and the minor points — pane-key scoping, two readers
on one item, and the one-old-schema-reader rule — are stated (§6, §7).

## 2. The failure that motivates it

The owner's failing sample has arrived: the Köhler scan
(`~/zotfile/Uniemner/KU Leuven/ReSem Phenomenology -- Husserl, the life world
and technology/Köhler_1939_The place of value in a world of facts.pdf`, a
linked Zotero attachment, printed p. 16 = PDF p. 36). Verified against the
file's own OCR layer (`pdftotext`, pages 35–36), it is the model case the
labeler exists for:

- **the footnote is read inline.** The bottom of PDF p. 35 ends with
  `il found recently that these words aie quoted from A. N. Whitehead, who
  does not of course share the opinion which they express.` — OCR of
  `1 I found recently that these words are quoted…`. The marker OCR'd to
  `il`, so the marker rule in `footnotes.ts` (`MARKER`, superscripts/digits/
  `n\d`/`\*`/`†`) never fires; the shape classifier never sees it, because
  detection happens *before* classification.
- **the running head is not dropped.** Page 36 opens with
  `THE PLACE OF VALUE IN A WORLD OF FACTS` / `i6` — the page number OCR'd to
  `i6`. On the skill path `strip_pages` misses it (`is_page_number` fails on
  `i6`; the head itself may not recur across ≥ 40 % of pages if the scan
  alternates recto/verso heads). On the Zotero path whether
  `flowClass === 'excluded'` dropped it is unknown until a capture runs —
  Stage 0 characterised *generated* fixtures, never this scan.
- **the sentence split across the page break** — `…an abstract and an
  enigmatic scheme` (p. 35) continues `to which we cannot transfer the rules of
  spatial and of temporal experience.` (p. 36). On the Zotero path SDT part
  chains are supposed to join exactly this (internals §3.1); whether they do
  on an OCR'd scan is uncharacterised.
- **a suspected pre-capture drop.** The owner suspects Zotero's SDT worker
  itself dropped a body block as `excluded`. If that is so, no plugin-side
  filter can ever fix it: there is no segment to filter.

Every one of these is either a case the *deterministic* rules cannot decide
(OCR-degraded markers and heads are judgment calls a threshold can't make),
or a case where a *threshold* (recurrence 40 % / ≥ 5 pages, bottom band 25 %,
OCR gate 6.0 — all marked provisional, conditional on the owner's sample in
the Stage 0 notes) must now be tuned against real documents, or a case
outside our capture boundary entirely. Tuning and regression-testing both
need one thing the repo does not have: **ground truth**.

## 3. Problem

Three gaps, one tool:

1. **The thresholds cannot be tuned without ground truth.** `segmentfilter.ts`
   and `footnotes.ts` ship provisional constants tuned on generated fixtures
   (`docs/design/2026-10-08-pdf-text-cleanup-stage0.md` says so explicitly:
   every threshold is provisional where the plan says so). The owner's sample
   is now here; what is missing is a way to record, per segment, what the
   *right* answer is — so a threshold change can be scored
   (precision/recall over the labeled corpus) instead of eyeballed on one
   reading.
2. **The per-document judgment calls cannot be rules.** `i6  THE PLACE OF
   VALUE…` as a head and `il found recently…` as a footnote are decisions a
   human makes in a second and a threshold cannot make at all. The pipeline
   needs a per-document override channel: a way to say *this segment, in
   this document, is not body text* — once, and have the live read and the
   export both honour it.
3. **The Stage B classifier has no eval set.** `llm.ts`'s footnote
   classification (`citation` vs `substantive`) is deployed with no corpus to
   measure it against. The same ground-truth artifacts feed it.

And the honest boundary, stated up front: **the Zotero path is not ours below
the capture boundary** (`reader-takeover.ts readSegments`). A label can only
re-categorize what the capture produced. If Zotero's SDT worker drops a body
block as `excluded` before the plugin sees it, there is nothing to label; if
SDT splits a sentence across a page boundary, merging segments is a
character-level operation the offset-map contract forbids us. The labeler
must make these cases *visible* (they are fixtures and bug reports), not
pretend to fix them. §8.4 says how.

## 4. Goals

1. In the Zotero reader, show what the pipeline currently does with each
   captured segment (its predicted category: body / footnote / running-head /
   footer / page-number / junk), let the owner correct it by clicking or one
   keypress, and persist the correction per item.
2. Persist labels so they survive recaptures, Zotero restarts, and plugin
   updates, keyed to the item and robust to segment drift between Zotero
   versions.
3. Apply labels live: a labeled item reads (and exports) by its labels, over
   the heuristics — the segment-level filters and (transitively) the Stage B
   classifier.
4. Export a labeled document (or page range) as a **ground-truth fixture** in
   the shared corpus format, so both surfaces' regressions and threshold
   tuning run against real documents.
5. Detect and report capture gaps — page text that never became a segment —
   at fixture-export time.

## 5. Non-goals (cut on purpose)

- **Not a general PDF editor.** No character-level labels, ever: a label
  never edits text, it categorizes a captured segment. Character repair
  stays in `cleanup.ts` stages and the LLM's diff-verified pass, exactly
  because of the never-mis-highlight contract.
- **No reading-order work.** Labels cannot reorder or merge segments; order
  is Zotero's SDT (live) or the extractor's (skill). A fixture *records* a
  split or interleave as ground truth; the fix, if any, belongs elsewhere.
- **No resurrecting dropped text.** What the capture never produced cannot be
  labeled, kept, or spoken on the Zotero path. Gap detection reports; §8.4
  says what the report is for.
- **No EPUB/snapshot labeling, no cross-document rules, no auto-labeling by
  LLM.** The labeler is a human-in-the-loop tool; the LLM's guesses may
  *seed* the pane (the heuristics already do), never write labels.
- **No new storage engine.** One JSON file per item on disk; no database.

## 6. User experience

**Where it appears: inside the Zotero reader.** The labeler is a mode of the
reader the owner already reads in — not a separate window, not an item-pane
list — because labeling is looking at the page and deciding what the thing
you are looking at *is*. (An alternative that needs no reader internals is
weighed and rejected in §11.)

### 6.1 Entry, capture, progress, abort

- **Entry: a "Label" toggle in the existing control bar** (`controls.ts`
  pattern: a `speakd-bar` button, official reader hooks, no internals beyond
  `reader-takeover.ts`). Entering it captures the document through the
  existing no-playback path (`ReaderHandle.capture()`, the same mechanism the
  export uses — `export/capture.ts`), so the pane and the labels always
  describe exactly what the pipeline would see.
- **Capture is whole-document, up front, by construction.** The capture
  boundary is document-granular: Zotero's SDT walk segments the whole
  document and the no-playback controller receives all of it at once
  (`session.ts capture()`, `CAPTURE_WAIT_MS` = 180 s). There is no
  page-granular capture API to be lazy with — faking one would mean reading a
  half-built segment array that Zotero may replace wholesale (internals §3.2:
  the array is rebuilt on every recomputation). So the pane *waits for the
  whole capture once*, and every page after that is instant, local filtering
  of the held list. On a several-hundred-page scan the first capture can
  take tens of seconds up to the 180 s timeout; that is the honest worst
  case, it is one-off per label-mode session (a fresh entry re-captures,
  below), and only the *first* entry of a session pays it.
- **The fast path: a read under way already holds the segments.**
  `session.ts capture()` returns the live controller's segments immediately
  when a read is active. Entering label mode during a read takes that list
  at once and stops the read (one controller per manager, internals §3.2);
  the pane is instant. After a read ends the controller's segments are gone
  (`session.ts` clears `current` on finish), so a *later* cold entry is the
  slow capture path again — never a stale list.
- **While the capture runs, the pane shows a "capturing…" state** — spinner,
  elapsed seconds, and the sentence "Zotero is preparing the document; this
  happens once per label-mode session." Zotero exposes no progress events
  for the SDT build, so the pane shows elapsed time, never a percentage — it
  will not fake one. The export's `busy()` progress window (`export/host.ts`)
  may accompany it; the Label button in the control bar stays disabled until
  the capture settles.
- **Abort is `Esc`.** It abandons the *wait*, not Zotero's build: the SDT
  pass is Zotero's and cannot be torn down mid-build — it finishes in the
  background and its result is discarded. Aborting leaves the reader exactly
  as Zotero had it (the existing `giveUp` path), the pane closes, and nothing
  was written. A capture that times out (the 180 s story) fails the same
  way: the pane says why, label mode exits, the read path is untouched.

### 6.2 The pane, the highlight, re-categorization

- **A pane in the reader**, injected into the reader iframe's document
  (`win.document`) through the same injection site `reader-takeover.ts`
  writes its `<style>` to (`STYLE_ID`, reader-takeover.ts:269–273): a narrow
  column listing **the current page's** captured segments in order — the
  whole-document capture filtered by `pageIndex` — each with a colored chip
  for its category (the heuristics' prediction first: `tagSegments`' tags,
  the running-head filter's drops, so labeling is mostly *confirm or
  correct*, one keypress per segment). The pane follows the reader's visible
  page; ↑/↓ walk the page's segments and continue into the next page, and a
  "next unlabeled" jump goes straight to the first unconfirmed segment.
- **The native highlight does the showing.** Clicking (or arrow-walking) a
  segment in the pane scrolls to it (`navigateToPosition` on its rects, the
  mechanism internals §4.1 documents) and makes Zotero's own Read Aloud
  highlight draw on it, by driving a **labeling controller**: the same
  `session.ts` controller machinery live reading uses
  (`createController(infos, back, forward, sink, list)`), configured to
  dispatch `ActiveSegmentChange` events and **never enqueue anything to
  speakd**. The owner sees the exact sentence the segment is, in the page,
  on Zotero's own highlight. No overlay code, no new rect drawing, and the
  highlight is the ground truth of *what the segment is* — on an OCR'd scan,
  where the text alone is unreliable, this is the whole value of embedding
  the pane in the reader rather than labeling from a list.
- **Re-categorization is one action.** Click a chip → a category picker, or
  press a key with the pane focused: `b` body, `f` footnote, `h` running
  head, `p` page number, `k` junk, `g` heading. `↑/↓` walk segments; `Esc`
  exits label mode (destroying the labeling controller and clearing the
  highlight the way the read path clears it). **Keys are pane-scoped, not
  window-scoped**: the key handler is a `keydown` listener on the pane
  element itself, so it fires only when the pane holds focus and the reader's
  own keys (page navigation, search, its own Read Aloud shortcuts) are never
  intercepted; `Esc` is handled only when the pane is focused and calls
  `stopPropagation` so the reader does not also act on it. There are no
  global hotkeys.
- **Out of the way of reading.** Label mode is an explicit toggle; a read
  under way is stopped by entering it (the manager owns one controller at a
  time — internals §3.2 — so the two cannot share it). Out of label mode,
  nothing shows: labels apply silently via the existing `planned()` seam
  (§8.2). The pane never covers the page text; it is a sidebar the reader
  tab accommodates, closable to an icon.
- **Prediction-first, so the cost is one page per minute.** The pane seeds
  with the filters' answers; on the Köhler page 35/36 the owner corrects
  exactly three segments (the `il found…` footnote, the `i6` head, the title
  head) and moves on.

### 6.3 The dev/harness surface

The same `zeval.sh` bridge that drives `dumpSegments` drives the labeler —
`ReaderHandle` gains exactly four new entry points (§14 names where they
live): `enterLabelMode()`, `exitLabelMode()`,
`labelSegment(index, category)` and `exportFixture(path, range)`, driven as
`Zotero.SpeakdReader.handles().find(...)` calls, so all of it is testable
headless — the `debugdump.ts` precedent. `exportFixture` is what invokes the
gap detector (§8.4).

## 7. Data model and storage

### 7.1 What a label is

One record per labeled segment, in one file per attachment:

```json
{
  "schema": 1,
  "attachmentKey": "<Zotero item key of the PDF attachment>",
  "documentSha256": "<sha256 of the PDF file at label time>",
  "capture": { "zoteroVersion": "10.0.6", "segments": 812, "labelledAt": 1760000000 },
  "labels": [
    {
      "locator": {
        "page": 34,                      // pageIndex (captured, predecessor-inherited)
        "rectY": 101.4,                  // firstRectY, null when the segment had no rects
        "anchor": "paragraphStart",      // or null
        "prefix": "il found recently that these words",   // first 48 chars, whitespace-collapsed
        "textSha256": "…"                // sha256 of the full collapsed text
      },
      "category": "footnote",            // body | footnote | heading | running-head | footer | page-number | junk
      "labelledAt": 1760000000,
      "note": ""                         // free text; where capture-gap suspicions are recorded
      }
  ]
}
```

**Keying — item + segment identity, deliberately not array position.**
Zotero rebuilds the segment array on every recomputation (internals §3.2),
and a Zotero upgrade can re-segment the same file. A locator therefore
carries the segment's *content and place*, not its index: page, anchor,
first-rect y, a text prefix, and the text hash — a **reference by hash and
prefix, not stored full text**, which is also what keeps a label file small
and copyright-benign (§8.5). The `attachmentKey` (not `itemID`, which is
library-instance-local) is the outer key; `documentSha256` is a hint, not a
gate — a re-OCR'd or re-saved file warns, it does not invalidate.

### 7.2 Matching, and when it runs

Matching runs in `labelstore.ts` (pure), **once per capture**, and yields an
index→category map that is cached with that capture instance:

1. Same page + same `textSha256` → exact hit.
2. Same page + same `prefix` + same `anchor` → hit (the text survived a
   re-segmentation that moved boundaries around it).
3. Anything else → **unmatched**. No fuzzy matching below exact prefix: a
   wrong fuzzy hit would silently drop body speech, which is the one failure
   worse than a stale label. Unmatched labels are listed in the pane ("3
   labels no longer match this document — re-label or discard") and never
   applied.

**When a capture changes.** The read's segment array is fixed once its
controller is built — arrays are rebuilt only on *recomputation*, which is a
new controller (internals §3.2) — so **a capture cannot change under a read
in flight**. Every new controller (a new read, a jump, a re-capture, a
label-mode entry) re-runs matching against its own segments and reuses
nothing from the previous map. A mid-read label write cannot happen on the
live path: labels are written in label mode only, and label mode stops the
read. The dev bridge can write labels headlessly between captures; the next
capture re-matches and the new map wins.

### 7.3 On disk, and where

One file per item at
`PathUtils.join(PathUtils.profileDir, "speakd-reader", "labels", "<attachmentKey>.json")`
— the `stagingPath()` pattern verbatim (`export/host.ts`):
`PathUtils.profileDir` is in the home directory, seen alike from inside the
flatpak sandbox and out, and nothing clears it at startup, so the label store
works under flatpak and survives Zotero restarts. Files are written atomically
(temp + rename, the `IOUtils` pattern `debugdump.ts` uses). Considered and
rejected for v1:

- *A sidecar in the attachment's storage folder* (`storage/<key>/…`): would
  follow the item through Zotero file sync, but it writes into Zotero's
  storage tree (sync junk, conflict files, a folder Zotero owns), and ground
  truth for *this repo's* regressions wants to be in the repo, not in the
  library. The travelling artifact is the exported fixture (§8.3), not the
  label file.
- *Zotero annotations/notes*: mixes test data into the owner's scholarly
  record and couples the labeler to annotation APIs for no benefit.

**Two readers on one item.** Two reader tabs on the same attachment share
one label file; writes are whole-file and atomic, so there is no torn state —
the last writer wins and the loser's stale pane list is repaired by the next
matching pass (per-capture, §7.2). One owner, at most two tabs, and a wrong
merge here costs a stale chip in a pane, never the read path (labels apply
per-capture; the reading tab re-matches on its next controller).

### 7.4 Versioning and updates

`schema: 1`; a file with an unknown schema is ignored with one warning and the
heuristics stand (fail-open, §10). Bumping the schema keeps **exactly one**
old-schema reader (schema N−1 only, never a chain) — a migration either
converts or it does not, and an unconverted file fails open. Plugin updates
never touch the directory; Zotero updates can only change the segments, which
the matcher reports (§7.2).

## 8. Integration with the pipeline

### 8.1 Segment-level, never character-level — the invariant restated

Labels attach to whole captured segments and apply at the one place where
whole-segment decisions are already made: the read's `planned()` seam
(`channel.ts`, which runs `tagSegments` before `buildSections`) and the
export's equivalent (`export/parts.ts buildParts` → `tagSegments`). Dropping
or deferring a *whole segment* is map-honest by construction — highlights
resolve by segment index (`Section.segmentIndices`), so a labeled-away
segment never appears in a section and can never be mis-highlighted; a
deferred one keeps its true index and highlights its own rect. A label never
touches characters, so the offset-map chain (`cleanup.ts` stages, `llm.ts`'s
diff-verified pass, `mapping.ts`'s evidence defence) is untouched by this
tool. **A label cannot mis-highlight; the worst it can do is drop or keep
speech** — the same blast radius the deterministic filters already have, now
under human control.

### 8.2 Live override (and export override — one seam)

`tagSegments` gains an overrides parameter, sourced from the item's loaded
labels (matched per capture, §7.2):

- `body` → forced keep: the running-head filter and footnote detection skip
  it. This is the restore path for a false positive (a prose line that
  recurs, as the Stage 0 corpus already surfaced on generated fixtures).
- `footnote` → forced candidate: it enters the footnote plan as detected, and
  the footnote *policy* then decides drop/defer/classify as for any detected
  footnote. A label feeds the detector, not the classifier — the policy
  setting stays one knob, document-independent.
- `running-head` / `footer` / `page-number` / `junk` → dropped (with the same
  one-line audit log the filters write; every override says so).
- `heading` → informational in v1: spoken as body (§15 asks whether headings
  should ever be treated otherwise).
- Unlabeled segments → the heuristics, exactly as today.

The override list is a plain map by segment index, resolved once per capture
and consulted at `planned()` time — microseconds, so the **2–4 s
time-to-first-audio budget is untouched** (the labels load at reader open or
first read, never awaited in the read path). Exports get the same overrides
in `buildParts` for free: one seam, no parallel pipeline.

### 8.3 The ground-truth corpus (the primary output)

From the labeler pane: **"Export fixture…"** with a page-range choice (default:
the labeled range). It writes, per fixture, under
`clients/zotero/test/fixtures/pdf/<slug>/` (the directory `corpus.test.ts`
already reads its dumps from):

- `capture.jsonl` — the captured segments, the exact `dumpSegments` shape
  (`index, text, pageIndex, anchor, rectCount, firstRectY`), the format
  `corpus.test.ts` already parses;
- `labels.json` — the label file verbatim. **This is the ground truth.**
- `expected.json` — a **regression snapshot, not ground truth**: the outcome
  the *shipped pipeline with the labels applied* produces today (per-segment
  expected tags via `tagSegments` with overrides, plus the expected section
  text). It exists so a future change to `buildSections`, the filters or the
  cleanup rules is caught when it alters what a labeled document produces;
  it says nothing about whether the *rules* are good, and it is never scored
  against. Its circularity is the point: it records the pipeline, the
  labels record the truth, and the two are only ever compared by a human
  reading the diff of a regression failure.
- `README.md` — source PDF path/fingerprint (`sha256`, not the file — §8.5),
  the harness command to reproduce, and the gap report (§8.4).

**Scoring the rules (what tuning actually runs).** To tune the thresholds,
the harness runs the heuristics **without overrides** over `capture.jsonl`
and scores the outcome against `labels.json`, at segment granularity,
policy-independent — the deterministic rules decide *detection*, the policy
decides fate, so only detection is scored:

- **A miss (recall failure):** a segment whose ground-truth category is
  running-head / footer / page-number / junk that the unaided rules *spoke
  as body*, or a ground-truth footnote the marker/shape rules did not detect
  as a footnote candidate at all.
- **An over-drop (precision failure):** a segment the unaided rules removed
  from the body flow (running-head drop, or footnote-plan drop) whose
  ground-truth category is `body`.
- **Precision** = drops that are ground-truth non-body ÷ all drops.
  **Recall** = ground-truth non-body segments the rules caught ÷ all
  ground-truth non-body, with footnote *detection* scored in its own line
  (a detected footnote's drop/keep is the policy's, not the rules', and is
  excluded) so a `classify` default does not silently punish a correct
  detection.

Unlabeled segments are not counted either way (they are not asserted
ground truth). With this, the Stage 0 "final value conditional on the
owner's sample" debts become payable: change a threshold, re-run the score,
see the two numbers move.

### 8.4 Capture gaps — visible, not fixed

At fixture export, a **JS gap check inside the plugin** diffs each page's raw
text against the page's captured segment text, and reports lines present in
the page but absent from the capture — the "SDT dropped a body block" class.

- **The module:** `export/gaps.ts`. Pure part: `pageGaps(rawPageLines,
  capturedTexts)` — whitespace-collapsed lines of the raw page minus the
  page's captured segment text. Zotero glue: reads the raw page text from
  pdf.js through the **same reach `export/structure.ts readStructure` already
  uses** — `reader._initPromise` → `reader._internalReader._primaryView` →
  `view._iframeWindow.PDFViewerApplication.pdfDocument`, then
  `page.getTextContent()` per page in the exported range, its text items
  assembled into lines best-effort (grouped by y, the same tolerance the
  skill's extractor family uses) before the pure diff. No Python: the
  plugin runs in Zotero's JS, and a skill-side script cannot be reached from
  the export. (A stdlib-CLI variant was rejected for exactly that
  round-trip: the plugin would have to write `capture.jsonl` to a path and
  the harness would need a second process at export time — more moving
  parts for no added truth, since pdf.js reads the same text layer
  `pdftotext` would.)
- **The invocation:** everything goes through `ReaderHandle.exportFixture(path,
  range)` — the UI button and the harness bridge call the same action, and
  the report lands in the fixture's `README.md` as *dropped-before-capture*
  items. The harness line is the Stage 0 shape:
  `Zotero.SpeakdReader.handles().find(...).exportFixture("/repo/path", {first: 34, last: 38})`.
- **Tolerance:** the pdf.js reach is best-effort inside a `try`/`catch` — a
  reader where it fails (the `readStructure` reach changed, a page's
  `getTextContent` rejected) writes "gap check unavailable" into the README
  and **never blocks the fixture export and never throws into Zotero**. No
  new probe is added for it (§14); a failure is a missing report, not a
  refused reader.

The reported items are unlabelable by construction, and their remedies are
(a) a skill-path audiobook, whose extractor reads the whole page, and (b) a
Zotero SDT issue with the fixture as evidence. In the live reader there is
no raw page text to diff against, so gap detection lives at export time only
— and that is stated in the pane ("gaps are reported when you export a
fixture").

### 8.5 Copyright policy for the fixture repo (a decision, not a question)

The Köhler is in copyright. The policy is concrete:

- **The PDF is never checked in.** The fixture `README.md` carries its
  `sha256` and the path on the owner's machine; the harness regenerates
  anything derived from the owner's copy.
- **Labels never store full text** — `prefix` (48 chars) + `textSha256`
  only, by the locator format above (§7.1).
- **Each fixture is bounded to the minimal excerpt per failure mode**: the
  page range the failures live on, never a whole document. A hard bound: **a
  fixture may carry at most 8 pages and ~20 KB of *text content* per
  in-copyright work without the owner's explicit sign-off**, recorded in the
  README. Text content is the whitespace-collapsed readable prose the
  fixture exposes, summed over its derived artifacts (`expected.txt`,
  `capture.jsonl`'s text fields, the text inside any stext XML), however
  encoded — the bound bounds *exposure*, not file size: a geometry-carrying
  encoding (stext XML, JSONL with rects) is mostly coordinates and markup,
  and counting its encoded bytes would miscount markup as text. The first
  real fixtures are the Köhler pages 34–38 (5 pages) — the
  footnote-inline, running-head, cross-page-split and suspected-gap cases,
  one page each beyond the failing spread for context.
- **Existing precedent, kept, and counted by text content:** the repo
  already carries `tests/fixtures/corpus/stext/kohler-p36.xml` — the mutool
  stext of **five pages, PDF 35–39 = printed pp. 15–19**
  (`tests/fixtures/corpus/README.md` and `tests/test_audiobook_corpus.py`
  both say so), `<char>` quads stripped — plus `kohler-p36.expected.txt`.
  Its text content is ~9 KB (the `expected.txt` is 9,154 bytes; the XML's
  `<line>` text collapses to about the same), well inside the ~20 KB text
  bound; its ~75 KB file size is geometry and markup, not text. Five
  pages, not one, and deliberately: the running-head recurrence rule needs
  `HEADER_MIN_PAGES = 5` pages (`prepare_text.py:26`, mirrored in
  `segmentfilter.ts`), and the regression the fixture pins asserts the
  heads recur across all five — a one-page trim would break recurrence
  detection and the regression with it. The name `kohler-p36` is the
  failing printed p. 16 the fixture was cut around. It stays: derived
  artifacts only, regenerable from the owner's copy (the PDF itself is
  never checked in), four failure modes, inside the text bound. What the
  ~75 KB *does* mean: an encoding this heavy is checked in only when the
  source cannot be (the corpus README regenerates the multi-megabyte
  two-column/footnote stext by running `mutool` over checked-in PDFs
  instead); when the source *can* be in the repo, regenerate at test time
  rather than check the encoding in. Plugin fixtures follow the same
  counting: `capture.jsonl` for pages 34–38 only, its text fields inside
  the text bound.
- A fixture that would exceed the bound is exported to the owner's machine
  (any path the dialog offers), not the repo, and the README records why.

## 9. Both surfaces

The labeler exists on one surface only: the **Zotero reader**, because that
is where a human sees the components. The skill path gets the *output*:

- the fixtures above are the shared corpus the skill's tests already consume
  (`tests/test_audiobook_corpus.py` over `tests/fixtures/corpus/`, the
  `kohler-p36` stext regression among them), and real-document fixtures are
  the thing that corpus has lacked. The plugin fixture directory and the
  skill corpus are siblings, not one directory (different shapes: JSONL
  captures vs stext XML), but the *ground-truth `labels.json` format* is
  reader-independent (locators are page/anchor/rect/prefix/hash), so a
  skill-side consumer needs no translation;
- a future `--labels` flag on `prepare_text.py`, reading an exported
  `labels.json` to override its own heuristics per document, is *conceivable*
  — the format is plain JSON and documented — but is deferred until a
  corpus exists that wants it (§15). The skill's semantic stage is the agent
  following SKILL.md, as ever; duplicating a labeling UI there is YAGNI.

## 10. Safety and failure

- **A label never writes the source.** The PDF, the item, the library record,
  and any annotation are untouched; the only write is the plugin's own label
  file. A wrong label costs a misheard or unsaid sentence, never corrupted
  data — the same guarantee the LLM stage gives (proposal §7).
- **Stale labels never apply.** Unmatched locators are inert and listed
  (§7.2); a document whose capture drifted badly (a re-OCR, a new Zotero)
  yields mostly-unmatched labels, the pane says so, and the heuristics stand
  until re-labeled.
- **Corrupt/unknown files fail open.** Unparsable JSON or an unknown schema
  → ignored, one warning in the debug log, the read proceeds as if unlabeled.
- **The wrong-drop blast radius is the filter's own, and smaller in
  practice.** A `junk` label on a body paragraph drops its speech — the
  mirror of the filters' silent-drop risk — but every label is an explicit
  act the owner performed while looking at the highlighted sentence, every
  override writes the same audit line the filters write, and the pane shows
  the drop counts. A labeled `body` *restores* speech the filter took —
  strictly a safety improvement.
- **Label mode never speaks and never blocks reading.** The labeling
  controller enqueues nothing; entering it stops the current read (one
  controller per manager, internals §3.2); a crashed pane costs the pane,
  never the read path (label mode is offline from speakd's point of view).
- **If the pane cannot be injected** — the `win.document` DOM write fails at
  runtime — the injection runs inside the takeover's `guard()` wrapper, so
  it warns in the error console and debug log, label mode reports
  "unavailable" in the control bar, and **reading is exactly as it was**:
  labels already on disk still apply through `planned()` (the override seam
  of §8.2 needs no pane), and label mode can be retried after a Zotero
  restart. Label application and label *making* fail independently.
- **No label ever touches characters**, so the mis-highlight chain
  (`offsetmap.verify` → `apply` fallback → the `mapping.ts` evidence defence)
  has nothing new to defend against. The invariant is preserved by scope,
  not by new machinery.

## 11. Alternatives considered (and why the pane wins)

**A standalone HTML review report, generated from `capture.jsonl`.** A
static page the harness renders from a `dumpSegments` dump: the owner clicks
categories in a browser and exports `labels.json`. *Pros:* touches no reader
internals at all (a `dumpSegments` run is already a shipped, headless action),
survives any Zotero update that breaks the takeover, and is buildable in an
afternoon. *Cons:* it loses the native highlight — without the reader's own
rect drawing, the owner judges from text and geometry numbers alone, exactly
the judgment OCR defeats (deciding `i6  THE PLACE OF VALUE…` is a head
*without seeing it on the page* is the failure mode being labeled); it adds a
second UI surface to keep aligned with the locator format; and it cannot
drive the live-read preview at all.

**Decision: the embedded pane is the primary surface; the HTML report is a
documented fallback, not built in v1.** The takeover already owns this risk
class — it is the one fragile module, probed and loudly refused
(internals §7) — and embedding the labeler there adds no *new* internals
beyond what the read and export paths already reach (§14). The label format
is reader-independent, so if a Zotero update makes pane injection
unsustainable, the fallback can be built then with no data-model change and
no lost labels; deferring it costs only the option, which the fixture format
keeps open.

(An item-pane list was rejected outright: a list of segments in the items
pane shows the same blindness as the HTML report with none of its
robustness.)

## 12. Testing

- **Unit (vitest), pure modules:** `labelstore.test.ts` — exact/prefix
  matching, unmatched-label inertness, schema fail-open (and the one
  old-schema reader of §7.4), atomic save/load round-trip, locator drift
  across a synthetic re-segmentation, per-capture map caching; `gaps.test.ts`
  — synthetic raw-page/captured pairs with a planted dropped block, an empty
  page, and a page whose capture matches exactly; override application — a
  `body` label restores a recurring-prose false positive; a `footnote` label
  makes the OCR'd `il found recently…` a candidate under each policy; a
  `running-head` label drops `i6  THE PLACE OF VALUE…` regardless of
  recurrence; `heading` changes nothing spoken; unlabeled segments behave
  exactly as today (the existing filter tests must pass unchanged — they are
  the no-labels case).
- **Scoring harness:** the unaided-rules-vs-`labels.json` metric of §8.3
  (miss / over-drop, precision / recall) is itself a pure function over a
  fixture, tested on a synthetic fixture with planted misses and over-drops.
- **Corpus:** the first real fixtures — the Köhler pages 34–38, derived
  artifacts only, inside the §8.5 text-content bound — consumed by `corpus.test.ts` with
  and without overrides; the no-override run documents the failure (footnote
  inline, head kept) as the characterisation, the with-override run documents
  the fix.
- **The gap detector:** driven live through `exportFixture` on the harness
  library (the planted-"excluded" synthetic PDF), assert the README names
  the block; a reader whose pdf.js reach fails writes "gap check
  unavailable" and still exports.
- **Harness (test-live, the `zeval.sh` bridge):** enter label mode on the
  Köhler item headless, drive `labelSegment` over the three failing
  segments, export the fixture, replay it into `corpus.test.ts`; then a live
  read of the labeled pages — the footnote deferred or dropped per policy,
  the heads gone, **no highlight ever on a labeled-away segment**, and the
  labeled `body` restore audible. Time-to-first-audio re-measured to confirm
  the override map costs nothing (it is one lookup at `planned()` time).
  Also: a cold label-mode capture on a slow document (the 180 s story) —
  pane shows "capturing…", `Esc` abandons cleanly, the reader is as Zotero
  had it.
- **The labeler adds no new internals beyond `reader-takeover.ts`**, which
  already owns the fragile list: the pane rides the takeover's existing
  injection and controller machinery; a Zotero update that breaks either
  fails through the existing probes and loud refusal, not through the pane
  (§14 says exactly what is and is not added to the probe list).

## 13. Implementation sketch (for the plan this spec precedes)

| Module | Kind | What |
|---|---|---|
| `labelstore.ts` | new, pure | load/match/save, schema, stale handling; no Zotero APIs beyond paths |
| `labeler.ts` | new | the pane, the category model, label-mode controller (session's machinery, never enqueues); pane-scoped keys |
| `reader-takeover.ts` | extend (the one fragile module, unchanged in kind) | `ReaderHandle` gains `enterLabelMode` / `exitLabelMode` / `labelSegment` / `exportFixture` for the UI and the harness/dev bridge, the `dumpSegments` precedent; the pane injected through the existing `STYLE_ID` site under `guard()` |
| `footnotes.ts` `tagSegments` | extend | overrides parameter; existing callers pass none and are unchanged |
| `channel.ts` `planned()`, `export/parts.ts` | wire | consult the label store's per-capture override map; audit log per override |
| `export/fixture.ts` | new | capture.jsonl + labels.json + expected.json + README + gap report |
| `export/gaps.ts` | new, pure + glue | the pdf.js-`getTextContent` gap check, best-effort, never blocks an export |

No changes to `sections.ts`, `offsetmap.ts`, `cleanup.ts`, `mapping.ts`, or
anything daemon-side.

## 14. Internals and the probe list — exactly what is added

The labeler's only new mechanism inside the takeover is the pane and the
label-mode controller; the exact additions, and nothing else:

- **`ReaderHandle` entry points:** `enterLabelMode()`, `exitLabelMode()`,
  `labelSegment(index, category)`, `exportFixture(path, range)` — on
  `Adopted` for real, and on `Unadopted` as fail-open no-ops returning a
  refused result, the `dumpSegments` precedent. The UI (control-bar Label
  toggle) and the `zeval.sh` harness bridge both call these; there is no
  second path.
- **The probe list (internals §7) is unchanged in v1.** Everything the
  labeler needs is already probed or already guarded: the controller
  machinery (`manager._createController`, `voice.getController`,
  `'ActiveSegmentChange'`, `setSegments`) — the probes
  `installIframeHooks` already checks — is what the labeling controller is
  built from; `win.document` is the same DOM the style injection writes to.
  The pdf.js reach for the gap check is the `readStructure` reach
  (`export/structure.ts`), used best-effort in `try`/`catch` with no probe —
  a failure costs the gap report, never the reader (§8.4).
- **What the pane is injected into:** the reader iframe's `win.document`,
  the same document `reader-takeover.ts:269–273` appends its `<style>` to —
  but an interactive pane is a DOM subtree with event listeners created in
  the plugin's compartment, more than a style node. The injection runs
  inside the takeover's `guard()` wrapper, so it can never throw into
  Zotero; its failure mode is stated in §10 (warn, label mode
  "unavailable", reading and stored labels unaffected).
- **When the highlight/sink machinery is absent:** the probes above already
  refuse the whole reader before label mode is offered — a reader that
  failed adoption has no working Label button (its handle is `Unadopted`;
  the toggle shows the refusal reason). A reader that passes the probes but
  whose controller construction fails at runtime gets the same fail-open:
  the pane opens, clicking a segment reports "the highlight could not be
  driven", labels can still be made from the pane's text list, and nothing
  else degrades.

## 15. Open questions for the owner

1. **Storage location** — plugin profile dir, `speakd-reader/labels`
   (recommended default: the `stagingPath` pattern, nothing in the library,
   fixtures are the travelling artifact) vs a sidecar in the attachment's
   storage folder (follows the item through file sync, but writes into
   Zotero's tree and can produce sync junk).
2. **Heading labels** — purely informational in v1 (recommended default:
   headings are spoken; a category exists so ground truth can name them, and
   any future "don't read headings" behaviour is a policy setting, not a
   label meaning) vs having a heading label suppress speech.
3. **Skill-path consumption** — defer a `--labels` flag on `prepare_text.py`
   (recommended default: until a real corpus exists and the skill path
   actually re-reads a labeled document) vs building it with the fixture
   format.
4. **Fixture scope of the first real corpus** — the Köhler's failing pages
   only (34–38, recommended default: small, regenerable, inside the §8.5
   bound, covers all four failure modes) vs a broader range or a second real
   document (a born-digital footnote-heavy paper) for balance.
5. **The 8-page / ~20 KB text-content fixture bound** (§8.5: the prose a
   fixture carries, however encoded — geometry bytes do not count) — the
   number is a recommendation
   from the Köhler's shape (one failing spread + context); the owner may
   want it tighter (page-range only) or looser (a whole chapter) for the
   born-digital fixtures, where copyright may not bind at all.

## 16. References

- `docs/design/2026-10-08-pdf-text-cleanup-request.md` — the brief this tool
  serves; `docs/design/2026-10-08-pdf-text-cleanup-proposal-glm.md` (revised
  2026-10-08, the binding cleanup spec) — the segment-filter architecture it
  extends; `docs/superpowers/plans/2026-10-08-pdf-text-cleanup.md` — the
  implementation plan it shipped under (its global constraints, cited here);
  `docs/design/2026-10-08-pdf-text-cleanup-stage0.md` — what was
  characterised, and which thresholds stayed provisional "conditional on the
  owner's sample".
- `docs/design/2026-09-24-zotero-reader-design.md` (decisions 3–5, the LLM
  staging, the fragility containment) and
  `docs/design/2026-09-24-zotero-10-read-aloud-internals.md` — its §3.2
  (segment shape, array identity on every recomputation), §4.1 (the native
  highlight the labeler drives), §1.2 (the takeover's injection points),
  §7 (the probe list the labeler does not extend).
- The code it integrates with: `clients/zotero/src/{footnotes, segmentfilter,
  sections, channel, session, reader-takeover, controls}.ts`,
  `clients/zotero/src/export/{capture, parts, structure, debugdump, host}.ts`,
  `clients/claude-code/skills/speakd-audiobook/scripts/prepare_text.py`; the
  corpora at `clients/zotero/test/fixtures/` and `tests/fixtures/corpus/`
  (the Köhler stext regression at
  `tests/fixtures/corpus/stext/kohler-p36.xml` — five pages, PDF 35–39).
- The motivating sample:
  `~/zotfile/Uniemner/KU Leuven/ReSem Phenomenology -- Husserl, the life
  world and technology/Köhler_1939_The place of value in a world of facts.pdf`
  (pages 34–38; the failures quoted in §2 were extracted from it with
  `pdftotext`).
