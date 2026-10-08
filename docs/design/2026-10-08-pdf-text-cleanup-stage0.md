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

## Live check — the rules-only read (Task 7)

The harness read of `footnotes.pdf` (item 20/21), end-to-end with the
filters on (fake-engine daemon on 8743, throwaway profile on 23129):

- The running header and the page numbers appear in no enqueued section.
- The citation-shaped footnotes are gone; the prose footnotes are read at
  the end of the section that owns their pages, never inline mid-flow.
- Highlights resolve correctly with the filters on: sampled every 5 s over
  ~2 minutes, each highlighted segment's text matches what was being
  spoken, and while the deferred footnotes were spoken (the section's last
  sentences) the highlight sat on the footnotes' **own** original segment
  indices (42, 43) — never a body segment's. The read rolled on into the
  next section and no problem fired.

What the harness could not cover: a full playback to the last word (the
fake engine speaks in real time; the check sampled ~2 minutes of it) — the
unit suite covers the rest of the section chain. Highlights on the
two-column paper were covered by the Task 1 dump reading order finding
and the same channel machinery; a full two-column playback remains a
Task 14 item.

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
(PyMuPDF in the project venv); until then the fixture-based tests
`importorskip` `fitz` (only those -- the stext/fallback/flag tests run
always), and the recorded outcome above is the decision record.

**Task 8's outcome, per this branch:** the stext extractor landed
(`mutool draw -F stext`, tolerant `pdftotext` fallback with marker-only
footnote deferral), `--footnotes classify|drop|defer` and `--ocr-report`
with it. The characterisation test above is re-pinned to the fallback
path, where poppler's interleaved order is still the whole truth; the
stext-path test reads the same fixture in correct column order with the
cross-column sentence intact. The `--ocr-report` gate (6.0) is
provisional and conservative, conditional on a real scan fixture; the
bottom band (25% of the page height, from stext's own geometry) mirrors
the plugin's provisional constant.

## Thresholds after Stage 0

| Threshold | Value | Status |
|---|---|---|
| Running-head recurrence (both surfaces) | 40% of ≥ 5 pages | established (`prepare_text.py:20-21`), mirrored |
| Bottom-band percentile (plugin) | bottom ~25% of the page's observed y-range | **provisional** — holds on the fixtures; final value conditional on the owner's sample |
| OCR suspicion cut-off | conservative/high | **provisional** — conditional on a real OCR-scan fixture |
| `joinSplitDigits` | — | **cut** (Task 6): Stage 0's dumps show no split-digit artefacts — the fixtures are born-digital, and the owner's sample has not arrived. Re-open only with real scan evidence. `ocrSuspicion` still counts split-digit hits as a signal. |
| Skill-path extractor | `mutool stext` + tolerant fallback | decided here: poppler default fails the fixture |

## Shared fixture corpus (Task 13)

`test/fixtures/pdf/` now holds the three document classes as checked-in
PDFs (regenerated by `test/fixtures/pdf/make_fixtures.py`):
`twocolumn.pdf`, `footnotes.pdf` (both byte-identical to the Stage 0
generation above) and `ocr-sample.pdf`, a scan-like text layer with OCR
confusions, split digits and stray marks. The plugin consumes the same
corpus through the captured dumps in `clients/zotero/test/fixtures/`
(`twocolumn.jsonl`, `footnotes.jsonl`; tested by `test/corpus.test.ts`),
and the skill through `tests/fixtures/corpus/` stext XML / pdftotext page
text (tested by `tests/test_audiobook_corpus.py`).

The corpus test surfaced one honest property of the running-head filter:
on the synthetic footnote dump, one prose footnote text repeats verbatim
as the last segment of three pages, so the recurrence filter drops it as a
running head before the footnote plan sees it. That is the filter behaving
as designed (recurrence in the same slot, never content); every such drop
is in the audit list. A real paper's footnotes differ page to page.

## Outcomes and what stays conditional

- **Task 8 branch:** `stext` — the `mutool stext` extractor with the
tolerant `pdftotext` fallback (recorded above).
- **Bottom-band percentile:** still the provisional bottom ~25% of the
  page's observed y-range; holds on the fixtures, final value conditional
  on the owner's failing sample.
- **OCR suspicion cut-off:** still the provisional conservative 6.0 (plugin
  default gate and skill `--ocr-report` gate); the scan-like corpus fixture
  is generated, not a real scan, so the final value stays conditional on a
  real OCR-scan fixture.
- **`joinSplitDigits`:** cut, as recorded above.
- **Live/harness verification (Task 14):** the rules-only read was checked
  live above (footnotes.pdf, highlights correct, deferred footnotes
  highlight their own indices). The full two-column playback, the export
diff against corpus expectations, time-to-first-audio against the 2–4 s
target, and the LLM-on failure paths are **pending** — they need a harness
run with a display and the fake endpoint, which this environment did not
provide.
