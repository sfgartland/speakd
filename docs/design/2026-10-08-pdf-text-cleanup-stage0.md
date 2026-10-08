# PDF text cleanup — Stage 0 characterisation

**Date:** 2026-10-08
**Status:** in progress (Tasks 1–2 recorded; the thresholds they leave
provisional stay provisional)
**Inputs:** the harness sample library
(`~/.cache/speakd-zotero-test`, generated fixtures — see below). The owner's
failing sample (proposal open question 2) was **not provided**; every finding
below is from generated fixtures, and the same questions on the owner's real
failing documents stay open.

## Zotero path — segment dump (Task 1)

The dev action is `ReaderHandle.dumpSegments(path)`
(`export/debugdump.ts`), driven through the live-test bridge. The fixtures
were generated with PyMuPDF (`~/.cache/speakd-zotero-test/mkstage0.py`,
`uv run --with pymupdf python mkstage0.py`):

- `twocolumn.pdf` — 8 pages, two equal columns (54–284 and 311–541 pt),
  a running header ("A Tale of Two Columns") and a page number footer on
  every page, one sentence planted to start at the bottom of page 1's left
  column and end at the top of its right column.
- `footnotes.pdf` — 6 pages, single column, running header + page number,
  6 body paragraphs per page, 4–5 footnotes per page in the bottom band
  (y ≈ 700–756 top-down), citation-shaped and prose by page, plus one body
  line opening with a year ("1994 was…").

Imported as items 18/19 and 20/21 in the harness library; dumps written to
`~/.cache/speakd-zotero-test/dump-twocolumn.jsonl` (736 segments) and
`dump-footnotes.jsonl` (136 segments). Reproduce with:

```bash
cd clients/zotero && npm run build && test-live/setup-profile.sh && test-live/run-zotero.sh
ZEVAL_TIMEOUT=300 test-live/zeval.sh '<open the reader, then:
  Zotero.SpeakdReader.handles().find(...).dumpSegments("/path/dump.jsonl")>'
```

### (a) Running headers, footers, page numbers

**Dropped before the capture boundary.** Neither dump contains a single
segment with the running header's text or a bare page number (0 of 736 and
0 of 136). Zotero's SDT worker excludes them on these fixtures —
`flowClass === 'excluded'`, presumably. `segmentfilter.ts` (Task 3) stays as
the backstop for documents where that fails, not as the primary defence.

### (b) Footnotes appear as segments

Every footnote is a segment, `anchor === 'paragraphStart'`, opening with
its marker (`1`–`5`), sitting in the page's bottom band (firstRectY ≈
84–140 of an 842-pt page; body paragraphs sit at ≈ 640–770). A footnote
with internal punctuation is split into *several* segments
("4 Cf. KrV, A51/B75." → "4 Cf." + "KrV, A51/B75."), of which only the
first carries `paragraphStart`; the rest are non-anchor continuations,
still in the band. The marker-led candidate + band continuation + the
no-rects marker-sequence fallback of `footnotes.ts` (Task 4) all have
something real to bite on. The body line opening with "1994 was…"
(paragraphStart, firstRectY ≈ 650, body band) is the marker-guard case.

### (c) Rect coverage: complete, and y runs bottom-up

**100% of segments carry rects** on both fixtures (`rectCount: 1` each; no
null geometry anywhere). The y coordinate is standard PDF user space —
**larger y is higher on the page, the bottom band is low y** — so the
bottom-band percentile is computed over the page's observed y-range with
low y as the bottom. On the footnote fixture the bottom ~25% band (page 0
observed range 84.3–769.7 → y ≤ 255.7) contains every footnote segment
(≤ 140) and no body segment (≥ 641): the provisional 25% constant from the
plan holds with margin on these fixtures. Its final value stays
conditional on the owner's sample.

### (d) Two-column reading order: correct in the segment list

Page 1's segments run the left column in full (paragraphs 0–14) and then
the right (15–29), no interleaving. The planted cross-column sentence
comes out as two adjacent segments — index 45 ("The cross column sentence
begins", firstRectY 60, bottom of the left column) followed immediately by
index 46 ("and ends in the rightmost column.", firstRectY 780, top of the
right). The plugin needs **no reading-order work** (proposal non-goal),
only the filters.

### Still open (blocked on the owner's failing sample)

The same four questions on a real failing document — a scan, a PDF whose
headers survive Zotero's exclusion, a paper whose footnotes sit outside
the bottom quartile, a document whose segments lack rects. Nothing in
Tasks 3–6 assumes a definitive answer: every threshold is provisional
where the plan says so.

## Skill path — two-column characterisation (Task 2)

Recorded outcome: **`stext`** — poppler's default mode fails the reading
order on the fixture, so Task 8 builds the `mutool draw -F stext`
extractor (with the tolerant `pdftotext` fallback).

The characterisation (`tests/test_audiobook_stage0.py`, run once-off with
`uv run --with pymupdf pytest tests/test_audiobook_stage0.py`) generates
the same two-column layout as `twocolumn.pdf` above and runs the current
`pdftotext`/`prepare_pdf` on it. What poppler's default mode actually
produces on that layout:

- **line-band interleaving**: the page's output alternates left-column and
  right-column lines (L0, R0, L1, R1, …), because poppler's default column
  detection groups the two columns' baselines into one band each;
- the planted cross-column sentence is therefore split far apart: its left
  half (bottom of the left column) and its right half (top of the right
  column) land ~180 lines apart in the page's text;
- today's `prepare_pdf` has no column logic of its own, so its parts
  inherit the interleaved order verbatim.

The test encodes exactly that — interleaving, cross-column sentence split
— and is marked as a **characterisation, not an endorsement**: it records
which order poppler's default mode actually produces so the Task 8 branch
decision is reproducible, not because that order is wanted. When Task 8's
stext path lands, the characterisation is re-pinned to the `pdftotext`
*fallback* path (mutool absent), where poppler's behaviour is still the
whole truth.

Making this test always-run needs a dependency decision from the owner
(PyMuPDF in the project venv); until then it `importorskip`s `fitz` and
the recorded outcome above is the decision record.

## Thresholds after Stage 0

| Threshold | Value | Status |
|---|---|---|
| Running-head recurrence (both surfaces) | 40% of ≥ 5 pages | established (`prepare_text.py:20-21`), mirrored |
| Bottom-band percentile (plugin) | bottom ~25% of the page's observed y-range | **provisional** — holds on the fixtures; final value conditional on the owner's sample |
| OCR suspicion cut-off | conservative/high | **provisional** — conditional on a real OCR-scan fixture |
| `joinSplitDigits` | — | **cut** unless dump evidence shows real split-digit artefacts (none seen; the fixtures are born-digital) |
| Skill-path extractor | `mutool stext` + tolerant fallback | decided here: poppler default fails the fixture |
