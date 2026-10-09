# Labeler verification notes — 2026-10-09

The live/harness half of the PDF component labeler (plan
`docs/superpowers/plans/2026-10-09-speakd-pdf-labeler.md`, Task 7; spec
`docs/superpowers/specs/2026-10-09-speakd-pdf-labeler-design.md`). The
Stage 0 notes (`2026-10-08-pdf-text-cleanup-stage0.md`) are the pattern.

Environment: flatpak Zotero 10.0.6, `clients/zotero/test-live/` (its own
profile under `~/.cache/speakd-zotero-test`, fake-engine daemon on HTTP
8743), `MOZ_HEADLESS=1`. The worktree's built `.xpi` was installed by
`setup-profile.sh`; the Köhler PDF was imported into the throwaway harness
library (item 22, attachment key `NZBZ2NQ9`). Nothing of the owner's real
Zotero, profile or daemon was touched; both harness processes were killed
with `pkill -f "[s]peakd-zotero-test/profile"` and the fake daemon with
`pkill -f "[f]akedaemon.py"`.

## What was run, and what it showed

1. **Plugin loads in the harness.** `Zotero.SpeakdReader.handles()` exists;
   `adoption: "ready"` after the reader's `_initPromise`.
2. **Cold label-mode capture on the Köhler scan.**
   `enterLabelMode()` returned `{ ok: true, segments: 6054 }` in **10.0 s**
   (one-off; the spec's "capturing…" state covers this). A second entry in
   the same session is instant (the segments are held).
3. **Fixture export, pages 34–38.**
   `exportFixture(<repo>/clients/zotero/test/fixtures/pdf/kohler, { first:
   34, last: 38 })` wrote the four files. 5 pages, **18,107 bytes** of
   whitespace-collapsed text (bound ~20 KB), `sha256`
   `f66453e8cd6df3cafda5f49609d39cae5c8ea4533fce0ac1b9c6c0605707e783`.
   The files are checked in under `clients/zotero/test/fixtures/pdf/kohler/`;
   the PDF is not.
4. **The running head and title head were dropped before the capture.**
   The capture's page 34 begins at `of spatial and of temporal experience.`
   (the sentence split across the page break is joined, as internals §3.1
   expects), and pages 35–36 carry no `THE PLACE OF VALUE…` / `THE CASE
   AGAINST SCIENCE` head at all. These are the "suspected pre-capture drop"
   class: unlabelable by construction, and exactly what the gap detector is
   for.
5. **The gap report names them.** With the pdf.js reach working, the README
   reports `i6 THE PLACE OF VALUE IN A WORLD OF FACTS`, `THE CASE AGAINST
   SCIENCE 15/17/19`, `18 THE PLACE OF VALUE IN A WORLD OF FACTS`. The
   remaining listed lines are hyphenated page-end lines the SDT text joined
   differently — best-effort false positives, kept, small (the README is
   ~2 KB and the fixture stays inside the text bound).
6. **The one labelable failure is the OCR'd footnote.** The label was written
   through `labelSegment(291, "footnote")` to
   `<profile>/speakd-reader/labels/NZBZ2NQ9.json` (atomic; page 34, prefix
   `il found recently that these words…`). The replay into `corpus.test.ts`
   matches it (1/1, unmatched 0) and the unaided rules already detect it
   (`classify`), so the fixture scores 1.0/1.0 with footnote detection 1/1
   and the footnote is deferred to the part's end, never inline. **The
   Köhler's real debt is the pre-capture gap, not a threshold.**

## Bug found and fixed live

Zotero's `PDFPageProxy` is Xray-wrapped: `page.getTextContent()` is not a
function on the wrapped object, so the first live export wrote "gap check
unavailable". `export/gaps.ts` now waives the page with
`Components.utils.waiveXrays` (identity outside Zotero, so the unit tests
still run in node). A second grouping fix — honour pdf.js's own `hasEOL`
marks, and treat a line the page's segments cover between them as covered —
turned the garbled 90-line report into the ~25-line one above.

## Pending (not faked)

| Item | Blocked on |
|---|---|
| A live read of the labelled pages: heads gone, footnote deferred per policy, no highlight on a labelled-away segment | a live read needs the fake-engine daemon and a longer harness run; not driven here |
| Time-to-first-audio re-measured against 2–4 s | same; the override map is one lookup at `planned()` time by construction, but no live timing was taken |
| Fail-open live: a hand-corrupted label file → one warning, read as unlabelled | unit-level only so far (`parseLabelFile` schema fail-open, resolver fail-open) |
| Two reader tabs on one item: last writer wins | not driven |
| The planted-"excluded" synthetic PDF gap check (README names the block) | the real Köhler gap report exercises the same path; the synthetic planted PDF was not built |
| The pane's own `Esc`/capture-abort on a cold capture | `Esc` aborts through `session.giveUp`, unit-covered; not driven live |
