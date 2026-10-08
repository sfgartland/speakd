# PDF text cleanup for narration — architectural proposal

**Date:** 2026-10-08
**Status:** revised after review — awaiting owner sign-off as the binding spec
**Author:** assistant (GLM), from the brief in
`docs/design/2026-10-08-pdf-text-cleanup-request.md`
**For:** the spec + plan flow the brief prescribes

This is a proposal, not a spec. It restates the problem from the brief,
weighs architectures, recommends one, and designs it to the module level.
Nothing here is implemented. Two decisions are flagged as the owner's, not
assumed: footnote behaviour (brief, open question 1) and the failing sample
(brief, open question 2) — but a default is proposed for each so the design
is complete without them.

## Revision, 2026-10-08 (after review)

Three corrections, all folded into the body below rather than kept as an
errata list:

1. **Footnote classification is batched, not per-footnote.** The earlier
   "one tiny call per footnote" contract could mean hundreds of calls on a
   footnote-heavy philosophy paper even before caching. §5.2 now sends only
   the deterministic classifier's *borderline residue*, batched per section
   (one call, numbered footnotes, a JSON array back), and the cost analysis
   is corrected to match.
2. **The `pdftotext` column claim is withdrawn.** Poppler's default mode is
   *not* a naive top-to-bottom sweep: it attempts reading-order/column
   detection, and it is `-layout` that preserves physical layout. §1.3 now
   states the skill's two-column failure as a **hypothesis** that Stage 0
   must test with a real two-column PDF before the `mutool stext` rewrite is
   committed to (§8).
3. **`sourcePosition.rects` coverage is unverified.** The plugin's footnote
   geometry assumes every captured segment carries usable rects. That was
   observed only for 19 segments of a one-page test PDF (internals, "Verified
   live"). §4.2 now makes rect coverage an explicit Stage 0 item and keeps the
   tolerant-degradation design (missing rects → marker-only footnote
   detection, never a crash).

Also folded in from the brief: the small-OCR-error requirement (brief item
4) is §1.4, §5.3 and §7; and the footnote policy is a configurable setting
whose default is option (a), so the owner's later answer changes a setting,
not the architecture (§3).

---

## 1. Problem restatement, per failure mode, per surface

The two surfaces extract text in unrelated ways (`clients/zotero/` hijacks
Zotero 10's SDT-based Read Aloud segmentation; the skill shells out to
`pdftotext`/`mutool`), so every failure mode must be assessed twice.

### 1.1 Running headers and footers

- **Zotero path — supposedly handled, unverified.** Zotero's SDT builder skips
  blocks whose top-level `flowClass === 'excluded'`
  (`docs/design/2026-09-24-zotero-10-read-aloud-internals.md` §3.1, B:71332),
  "where headers, footers and similar are dropped." But the same doc's
  "UNVERIFIED" section says the decision is made in the PDF worker's SDT
  processor, *which was not read*, and asks whether `excluded` covers every
  header, footer and page number. The plugin's own safety net is thin:
  `clients/zotero/src/cleanup.ts` has only `joinHyphenation`,
  `dropPageNumbers`, `collapseWhitespace` (`RULES`, cleanup.ts:85) — no
  recurring-header detection.
- **Skill path — handled, heuristically.** `running_lines` +
  `strip_pages` (`prepare_text.py:49-66`) drop a whitespace-normalised line
  (digits → `#`) that opens or closes ≥ 40 % of pages (≥ 5 pages), plus bare
  page numbers. This is the right *shape* of solution (frequency across
  pages, deterministic), and the plugin path has no equivalent.
- **What is actually broken:** possibly nothing on the Zotero path (if
  `excluded` works), certainly something on the skill path when a header
  varies (chapter titles in the head, say) or a document is under 5 pages.
  **Unverified is the operative word for the Zotero path:** the capture
  boundary is ours (`reader-takeover.ts` `readSegments`,
  reader-takeover.ts:433), what happens below it is Zotero's.

### 1.2 Footnotes read inline

- **Zotero path — contradicted assumptions.** Reader design decision #4 says
  "citations and footnotes are skipped silently" and the architecture
  diagram repeats "headers, footnotes and citations already handled"
  (`docs/design/2026-09-24-zotero-reader-design.md`). But the brief states
  the observed reality: footnotes are read inline, mid-flow. What Zotero's
  SDT elides is *inline* citation-shaped bracket/parenthesis groups and
  superscript link markers (internals §3.1, `getElidedRanges`), not footnote
  *flows*. Which of the two is true for a given PDF is **unverified** —
  two-column and footnote-heavy PDFs are still on the manual checklist
  (internals, "Verified live" sections; `clients/zotero/README.md`,
  "Per-release checklist"). Either the design decision is wrong for
  footnote-heavy PDFs, or the owner's observation came from the skill path,
  where footnote handling demonstrably does not exist.
- **Skill path — not handled at all.** `prepare_text.py` has no footnote
  step; `pdftotext` emits footnote lines where they sit on the page, so a
  footnote-heavy paper interleaves body and footnotes in the output.

### 1.3 Multi-column reading order

- **Zotero path — owned by Zotero, out of our hands below the capture
  boundary.** SDT part chains join paragraphs across pages and columns
  (internals §3.1); the plugin receives flat segments in whatever order the
  SDT walk produced. If that order is right, the plugin's job is filtering,
  not reordering (brief, open question 3). **Unverified** — two-column PDFs
  are on the untested list.
- **Skill path — a hypothesis, not an established failure.** The earlier
  draft of this proposal claimed `pdftotext`'s default output interleaves
  both columns for most two-column academic PDFs. That claim is
  **overstated and withdrawn**: poppler's *default* mode attempts
  reading-order and column detection (it is `-layout`, which this repo does
  not use, that preserves physical layout and would interleave). Whether the
  default mode gets a given two-column paper right is an empirical question
  about poppler's heuristics on real layouts, and `prepare_text.py`
  (prepare_text.py:159) runs no column logic of its own either way. Stage 0
  therefore includes a **real two-column PDF test on the skill path** (§8)
  before any extractor rewrite is committed to. The `mutool draw -F stext`
  rewrite stays in the design (§4.3) because stext's per-span font size is
  also the skill path's only footnote signal — but it is *conditional* on
  that test, and is cut to "footnote handling only" (or entirely, with
  marker-based footnote deferral over `pdftotext` output) if poppler's
  default mode already orders the fixture correctly.

### 1.4 Small OCR errors

- **Zotero path — not handled.** Nothing in `cleanup.ts` or the capture
  path corrects OCR artefacts; a scanned PDF's text layer is spoken
  verbatim (`tbe` for `the`, `1 994` for `1994`, `modem` for `modern`,
  broken ligatures, stray characters — brief, item 4).
- **Skill path — not handled**, with one nuance: the skill has a *semantic*
  stage the plugin lacks — the agent itself (SKILL.md step 4, "fix what the
  spot-check shows with targeted edits"). The skill's OCR repair can stay
  mostly deterministic plus an explicit report that tells the agent OCR
  artefacts are likely and worth a pass; the plugin's needs the Stage B LLM
  (§5.3).

### 1.5 What Zotero already handles that we must not redo

Reading order via part chains, sentence splitting, citation-shaped inline
elision, source positions, page attribution (`sourcePosition.pageIndex`),
paragraph anchors. Any plugin-side design that *reorders characters* also
fights the offset-map contract (constraint 1); any design that *reorders
segments* does not, which §4 exploits.

**The honest uncertainty summary:** below the capture boundary we know the
segment shape (`text`, `anchor`, `sourcePosition {pageIndex, rects}`) but
not Zotero's actual behaviour on the three failing document classes, nor
whether every segment carries usable `rects` (§4.2). Before building
filters, that behaviour must be characterised — cheaply, because the export
capture path (`session.ts` `capture()`, `export/capture.ts`) already dumps
every segment with text, page and anchor without playing anything. §8's
Stage 0 is that characterisation, and it needs the owner's failing sample.

---

## 2. Candidate architectures

### Candidate A — "Zotero path does everything; skill gets parity patches"

Work happens: **Zotero path (heavy), skill path (light).**

Build, in the plugin: a deterministic segment-level filter (headers,
footnote band, page numbers) driven by geometry and cross-segment frequency;
extend `cleanup.ts` with safe new rules (ligatures, OCR suspicion scoring);
build the designed-but-unbuilt `llm.ts` (per-section edit-only pass for OCR
repair + footnote classification); finally wire the same filter + cleanup
into `export/parts.ts` so exports stop shipping raw text. Skill path: keep
`pdftotext`, add a footnote heuristic (marker-led lines in the tail of a
page) and leave columns alone.

- **Pros:** one cleanup brain; export/live asymmetry exploited naturally
  (same filter, LLM optional per path); the offset-map machinery already
  exists and is tested.
- **Cons:** if the Stage 0 two-column test shows poppler's default mode
  *fails*, this candidate has no answer for the worst thing an audiobook
  can do short of silence; a marker-based footnote heuristic without
  geometry is weak (superscripts degrade to plain digits in `pdftotext`);
  the two surfaces diverge further. If the test shows poppler is fine, A
  becomes cheap — but that is exactly what Stage 0 exists to find out.

### Candidate B — "Shared deterministic core, exposed as a service"

Work happens: **shared.** Put header/footer/column/footnote detection in one
place (a small daemon endpoint, or a TS library the skill calls via Node),
so both surfaces run identical logic.

- **Pros:** one implementation, one test corpus, guaranteed parity.
- **Cons:** rejected by the constraints. The daemon must not rewrite PDF
  text (`PDF_PROFILE` binds no rewriting transforms precisely to keep
  highlight spans sentence-exact — `src/speakd/profiles.py:57`; brief,
  constraint 4); a text-cleanup endpoint in the daemon is that same surgery
  by another name. The skill is deliberately stdlib-only and runs wherever
  the agent runs; adding a Node/TS dependency for cleanup breaks that.
  Genuine code-sharing across TS and Python is a fiction; what *can* be
  shared is the rule taxonomy and the test corpus — which A and C share
  anyway. Over-engineering; cut it.

### Candidate C — "Deterministic geometry on both surfaces; LLM only where structure cannot decide"

Work happens: **Zotero path + skill path, sharing design and corpus, not
code.**

- **Zotero path:** a *segment filter* stage before section building —
  geometry (footnote band) and frequency (residual running heads) over the
  whole captured segment list, all deterministic and cheap; new safe rules
  in `cleanup.ts`; the designed `llm.ts` as a strictly optional Stage B for
  the residual semantic calls (OCR repair on gated sections, classification
  of the borderline footnote residue, abbreviation expansion); export gets
  the filter and cleanup it lacks today.
- **Skill path:** a **conditional** swap of the PDF extraction source to
  `mutool draw -F stext` XML (mupdf is already a required tool, used for the
  outline) — conditional on Stage 0's two-column test showing poppler's
  default mode fails the fixture — because stext carries **per-span font
  size and block bounding boxes**: the same geometric signals (columns by
  x-overlap, footnotes by size + bottom band, headers by top band) that
  Zotero's SDT worker uses, applied deterministically in Python. If the
  test shows poppler's default is already correct, the skill path instead
  gets marker-based footnote deferral over the existing `pdftotext` output
  and no column code at all. The agent-in-the-loop (SKILL.md step 4)
  remains the skill's semantic stage either way; no LLM pipeline here.
- **Pros:** each surface uses the strongest signals it can actually get
  (and only pays for the stext rewrite if Stage 0 proves it necessary); no
  network dependency on the skill path; the plugin's LLM stage stays
  additive and optional, exactly as the reader design staged it (Stage A
  rules-only, Stage B LLM); both surfaces validated against one corpus.
- **Cons:** two implementations of the same concepts (unavoidable given the
  languages); if justified, the skill's stext extractor is a real rewrite
  of ~100 lines of `prepare_pdf`; mupdf XML parsing is more code than a
  text dump.

---

## 3. Recommendation

**Candidate C**, with Candidate A's export wiring folded in (it is in C
already). Reasoning:

1. It is the only candidate that can fix the skill path's two-column
   failure *if it is real* (Stage 0 decides), and footnote handling there
   needs geometry (`mutool stext` gives font size; nothing else available to
   the skill does).
2. It honours every binding constraint by construction: deterministic
   geometry for document-wide analysis (cheap, explainable, testable — the
   brief's own conclusion about structural vs semantic judgment); LLM
   confined to per-section, edit-only or label-only work with the
   diff-verified offset map and the existing fallback chain; section 1
   skips the LLM so time-to-first-audio is untouched.
3. It matches the repo's existing staging: reader design decision #5 and
   `docs/HANDOFF.md` already plan `llm.ts` as an additive Stage B over a
   fully-useful rules-only Stage A. C builds exactly that, no more.
4. The skill path's "LLM" already exists — it is the agent following
   SKILL.md. Duplicating an LLM pipeline there would be YAGNI.

### Decisions this recommendation needs from the owner

- **Footnote behaviour (brief, open question 1) — a setting, not an
  architecture decision.** The design ships a `footnote_policy` setting with
  three values: **`classify` (default, option (a))** — drop citation-shaped
  footnotes silently, read kept ones at the end of their section, with the
  LLM (Stage B, if configured) resolving only the borderline residue;
  **`drop_all` (b)** — every footnote goes, no classifier anywhere; and
  **`defer_all` (c)** — every footnote is kept and read at the end of its
  section, no classifier either. The owner's answer is a setting change
  (`extensions.speakd-reader.footnote_policy` in the plugin,
  `--footnotes` on the skill script), never a rewrite; under (b)/(c) the
  LLM's footnote-classification role disappears entirely (§5.2) and the
  design is deterministic-only on that axis.
- **The failing sample (open question 2).** Everything in §1 marked
  *unverified* is settled by Stage 0 (§8) once a real two-column,
  footnote-heavy PDF is captured — the owner's failing sample is the
  highest-leverage input. Where a threshold genuinely cannot be fixed
  without it (the footnote band percentile, the OCR suspicion cut-off),
  the design marks it provisional and the plan makes the tuning task
  conditional on the sample rather than inventing final numbers.

---

## 4. Module / data-flow design (recommended architecture)

### 4.1 The pipeline shape

The key structural idea, valid on both surfaces: **split cleanup into a
segment-level stage and a character-level stage.**

- **Segment filter** — decides which captured segments are spoken at all
  (headers, footnotes per policy, page numbers). Operates on whole-document
  segment lists with geometry and frequency. **No offset map needed**:
  dropping or *reordering whole segments* never breaks the highlight
  contract, because highlights are resolved by segment index
  (`Section.segmentIndices`, `sections.ts`), not by position arithmetic
  (`mapping.ts` `planHighlights` looks the segment up from `offsets`, and
  `offsets` are built *after* filtering). Character-level reordering remains
  forbidden, as ever.
- **Character cleanup** — the existing `cleanup.ts` rule chain over a
  section's (or a part's) joined text, each rule a `Stage` carrying its map
  (`offsetmap.ts`), plus the optional LLM stage whose answer goes through
  `stageFromDiff` (offsetmap.ts:69) with `apply`'s fallback.

### 4.2 Zotero plugin — new and changed modules

```
reader-takeover.ts (readSegments)     ← extend: copy first-rect y (tolerant; rects may be absent)
        │  SegmentInfo {text, anchor, pageIndex, firstRectY?}
        ▼
segmentfilter.ts   [NEW, pure]        running-head frequency filter over whole segment list
footnotes.ts       [NEW, pure]        geometry + marker detection → deterministic classification
        │                               → FootnotePlan (policy: classify | drop_all | defer_all)
        ▼
sections.ts (buildSections)          ← extend: accept tagged list; append "deferred" footnote
        │                               segments at the end of the section they fall in
        ▼
channel.ts (clean option)            ← extend: wire cleanWithLLM (deterministic → llm.ts)
        │                               ordinal 0 skips the LLM (signature already passes ordinal)
        ▼
cleanup.ts (+ rules) / llm.ts [NEW]  Stage chain, diff-verified (offsetmap unchanged)
```

- **`reader-takeover.ts` — the only internals-touching change (keep it that
  way).** `readSegments` (reader-takeover.ts:433-447) currently copies
  `text`, `anchor`, `sourcePosition.pageIndex`. Extend it to also copy the
  first rect of `sourcePosition.rects` (its y coordinate) when present, and
  per page the max rect y seen — enough to say "this paragraph starts in the
  bottom band of its page." **Rect coverage is unverified and must not be
  assumed:** internals §3.2 defines `sourcePosition: PDFPosition | null`
  — a segment may have *no* source position at all — and the live spike
  verified rects only for 19 segments of a one-page test PDF. The plugin's
  footnote geometry therefore has a built-in degradation: a segment with no
  rects (or a document where few segments carry them) falls back to
  marker-only footnote detection, and the page-band percentile is computed
  only over pages that yielded y values. Missing rects are *null geometry*,
  never a throw, and never a reason to drop a segment. Stage 0 (§8) dumps
  rect coverage on the fixture corpus and the owner's sample before the
  band threshold is tuned; the probe list is unchanged (rects are read
  through the same waiver as `pageIndex`, and their absence is tolerated).
  Font size is *not* available on segments, so the plugin's footnote
  geometry is weaker than the skill's; the deterministic citation-shape
  filter (§5.2) covers most of the difference, the LLM classifier the rest.
- **`segmentfilter.ts` [new, pure]** — the cross-page running-head filter
  the plugin lacks (the skill's `running_lines` transposed to segments):
  normalise (digits → `#`) the opening/closing short paragraph of each
  *page group* of segments; one recurring across ≥ 40 % of pages (≥ 5) is
  dropped. (0.4 and 5 are the repo's established thresholds —
  `prepare_text.py:20-21` — mirrored, not invented.) This is the backstop
  for whatever `flowClass === 'excluded'` misses, and it only fires on
  *recurrence* — a body paragraph never recurs, page after page, in the
  same slot. Conservative by construction.
- **`footnotes.ts` [new, pure]** — input: the whole `SegmentInfo[]` for a
  read/capture, plus the policy; output: a `FootnotePlan` = for each
  segment index, `body | footnote: {drop | keep | defer | classify}`.
  Deterministic rules:
  - *candidate* = paragraph (`anchor === 'paragraphStart'`) whose first
    rect sits in the bottom band of its page **and** whose text opens with
    a footnote marker (¹²³, `[0-9]{1,2}[.\s]`, `n[0-9]`, `*`, `†`); with no
    rect coverage, a paragraph opening with a marker whose *page group's*
    subsequent paragraphs open with sequential markers is a candidate
    (footnotes run in order down the page tail);
  - *continuation* = a non-anchor paragraph that follows a candidate and
    also sits in the band (footnotes wrap);
  - *classification, deterministic-first* (policy `classify` only):
    segments whose text is dominated by citation shape (author-year /
    "Ibid." / "Cf." / "op. cit." / "See" / mostly digits and punctuation,
    few content words) → `drop`; long multi-sentence prose with no
    citation opening → `keep`; everything in between → `classify`, and
    that residue — typically zero to a handful per section — is the LLM's
    *only* footnote input (§5.2);
  - policies `drop_all` and `defer_all` need no classifier at all
    (candidate/continuation detection still runs).
  - The **bottom-band percentile is provisional**: its right value depends
    on where footnotes actually sit in the owner's corpus, which Stage 0's
    dump shows; the plan tunes it there instead of shipping a guessed
    number as final.
- **`sections.ts`** — `buildSections` gains an overload taking the tagged
  list: `defer`red footnote segments are held back and appended at the end
  of the section that contains their page. Segment indices are preserved in
  `segmentIndices`, so a deferred footnote still highlights its own PDF
  rect — reordering *segments* within a section is map-honest (§4.1).
  First-section size and `DEFAULT_SIZE` (sections.ts:26-40) unchanged:
  footnote tails ride along.
- **`cleanup.ts`** — add to `RULES` (cleanup.ts:85):
  - `fixLigatures`: `ﬁ ﬂ ﬀ ﬃ ﬄ` → expansions, and stray zero-width /
    control characters → removed (pure deletions/substitutions, map
    trivial, no false-positive risk);
  - `joinSplitDigits`: a digit-run broken by a single space where the join
    forms a 4-digit year-like token *adjacent to year-context words* —
    deliberately narrower than "any `1 994`" to avoid collapsing genuine
    ranges. Gated by Stage 0 evidence; cut if the corpus shows it is
    fiddly and leave digit repair to the LLM gate;
  - `ocrSuspicion(text): Score` — deterministic OCR detector (ligature
    remnants, `·`/control chars, split-digit hits, ratio of 1–2-letter
    tokens) used to gate the LLM's OCR pass (§5.3). The suspicion cut-off
    is provisional until Stage 0 runs it over the corpus; it ships
    conservative (high) and is lowered only against real artefacts.
- **`llm.ts` [new]** — the designed Stage B, in two call types, both
  strictly bounded (§5):
  - *text-repair* (one call per section): OCR mode when gated by
    `ocrSuspicion`, ordinary mode (residual citation noise, abbreviations)
    otherwise; output through `stageFromDiff` with a tightened per-section
    edit budget;
  - *footnote classification* (§5.2): one call per **section** containing
    borderline footnotes, carrying that section's residue as a numbered
    list, answered as a JSON array of labels.
  Against a configured OpenAI-compatible endpoint (prefs: endpoint, key,
  model, on/off; off until configured, per reader-design revision #7,
  including the one-line "it sends the paper's text to this endpoint"
  disclosure). Timeout raced with `hooks.later` (the sandbox has no
  `AbortController` — internals §6 says take it from the main window, or
  simply ignore the late resolution), HTTP error, empty or unparsable
  answer, or budget overrun → the deterministic result stands, the failure
  is logged and marked once in the bar.
- **`channel.ts`** — no structural change: the `Clean` seam
  (channel.ts:114, `options.clean ?? ((text) => cleanup(text))`) already
  receives `ordinal` and may be async, so `cleanWithLLM` slots in as an
  option and `cleaned()`'s try/apply fallback already contains every
  failure path. Ordinal 0 skips the LLM call.
- **Export path** — fix the asymmetry with the *same* pure modules, not a
  parallel pipeline: `export/parts.ts` `buildParts` currently raw-joins
  segment text (parts.ts:42). Change the export entry to run the captured
  `CapturedSegment[]` (session.ts:78) through `segmentfilter` + `footnotes`
  first, then through `cleanup()` per part before `joinText` — exports have
  no offset map to protect, so the rules run freely, and the LLM (if
  configured) may run per *part*, batch-style, before `renderPayload`
  (export/render.ts). Nothing in `exporter.ts`/`jobs.ts` changes; the
  capture machinery (`export/capture.ts`) is reused as-is. The export path
  runs the footnote batch (all of a part's borderline footnotes in one
  call, chunked at ~20) rather than per-section, since there is no latency
  budget there.

### 4.3 Skill path — `prepare_text.py` restructure

```
pdftotext (kept: plain reflow for .txt/.md and as the no-mutool fallback)
mutool draw -F stext  [CONDITIONAL primary PDF source — Stage 0's two-column
                       test decides: XML, blocks with bboxes, spans with font sizes]
   │
   ├─ order_columns()   [with stext] group blocks by x-overlap; read each column fully
   ├─ drop_bands()       [with stext] top-of-page recurring head band; bottom-of-page footnote band
   ├─ footnotes()        font-size-dropped + bottom-band + marker blocks → drop / defer to
   │                        the end of the page group (policy mirrors the plugin's)
   └─ (existing) running_lines / page numbers stay as the fallback path's logic
   ▼
join_paragraphs / cut_references / outline chapters  (unchanged)
```

- The stext extractor is **conditional on Stage 0** (§8): it is built only
  if the real two-column fixture shows poppler's default mode fails to
  order it; if poppler is correct, the skill path keeps `pdftotext` and
  gets marker-based footnote deferral only. `--footnotes classify|drop|defer`
  (default `classify`) mirrors the plugin's policy — on this surface
  "classify" means the deterministic shape filter, with the agent (SKILL.md
  step 4) as the semantic classifier of last resort.
- `mutool` is already a declared dependency (prepare_text.py docstring;
  `parse_outline` tolerates it missing — the same tolerance for stext: fall
  back to today's `pdftotext` path, degraded, never fatal).
- Footnote deferral appends footnote text after the page's body paragraphs
  — the spoken equivalent of (a)/(c) without an LLM. SKILL.md gains a line
  telling the agent footnote policy was applied and where to look.
- No OCR repair pipeline on this surface: an `--ocr-report` stderr note
  when the suspicion heuristic fires (the same scoring concept as the
  plugin's), telling the agent OCR artefacts are likely and worth a
  spot-check pass. The agent *is* the LLM stage here.

### 4.4 What the two surfaces share

No shared code (languages differ; constraint 4 forbids daemon-side sharing).
Shared instead:

1. **The rule taxonomy and thresholds** (bottom-band percentile,
   marker set, citation-shape patterns, recurrence share 0.4 / min 5
   pages) — written once in this spec and mirrored.
2. **A test corpus**: `test/fixtures/pdf/` with the three document classes
   (two-column, footnote-heavy, OCR scan) plus expected cleaned text, used
   by both `clients/zotero/test` (over captured-segment fixtures) and the
   skill's tests (over stext/pdftotext fixtures). One corpus, two
   harnesses.

---

## 5. Where the LLM fits — task by task

The brief's own conclusion is the design rule: **semantic → LLM,
structural/geometric → deterministic.** Applied:

### 5.1 Header/footer detection — deterministic only

Recurrence across pages and y-position are the signals; a rule explains
itself and can be unit-tested (`running_lines` is the existing proof). An
LLM asked to "remove headers" will also rewrite prose — precisely
constraint 1's collision. **No LLM.**

### 5.2 Footnote classification — deterministic shape filter first; the LLM sees only the borderline residue, batched per section

Structural part (is this paragraph a footnote?): geometry + marker,
deterministic (§4.2). Semantic part (is this footnote a bare citation or
substantive prose?): this is a judgment a rule cannot make reliably
("Heidegger 1962, p. 65" vs "Heidegger's reading of 1962 reverses the
inaugural lecture's order"), and the default policy keeps substantive ones
(brief, open question 1, option (a), made a setting in §3).

- **Contract — batched, never per-footnote.** After the deterministic
  classifier, each section holds a *residue* of borderline footnotes
  (typically none to a few; the classifier is tuned so clear citations and
  clear prose never reach the model). For each section with a non-empty
  residue, the LLM stage makes **one call** carrying that residue as a
  numbered list, and asks for a JSON array of labels in the same order:

  > "Below are footnotes from a philosophy paper, numbered. For each,
  > decide: is it a bare citation (references to works, page numbers,
  > 'Ibid.', 'Cf.'), or does it contain substantive argument or content?
  > Answer a JSON array of the strings "citation" or "substantive", one per
  > footnote, in order, and nothing else. 1. … 2. …"

  A residue larger than ~20 footnotes is chunked into further calls of
  ≤ 20 (only realistic on the export path, where a whole part's footnotes
  are classified at once). Answers are cached by footnote-text hash for
  the session, so a jump or re-read never re-asks.
- **Cost, stated correctly this time.** A footnote-heavy philosophy paper
  may carry hundreds of footnotes; a call per footnote would mean hundreds
  of prompts even before latency, and an unbounded burst at read start —
  which is why that design is withdrawn. With the batched contract: every
  footnote is first classified *deterministically* for free; only the
  borderline residue is paid for; and the residue is paid for **once per
  section, not once per footnote**. A 40-page paper reading as ~30
  sections has at most 30 classification calls *however many footnotes it
  carries*, and in practice far fewer (most sections have an empty
  residue and make no call at all). Combined with the text-repair call
  (§5.3, gated), the bound is **at most two LLM calls per section** — one
  classification (only when the residue is non-empty), one repair (only
  when the suspicion gate fires).
- **Offset-map contact: none.** The output is labels over whole segments;
  dropping/keeping/deferring whole segments is map-honest (§4.1). No
  `stageFromDiff` on this path.
- **Failure/timeout/unparsable answer:** the policy default applies —
  under `classify` an unclassified footnote is *kept* (deferred to its
  section end), never silently lost — the footnote is logged, the residue
  for that call falls back wholesale; on the live path nothing ever
  *waits* on it (§6). Under `drop_all`/`defer_all` there is no LLM role at
  all.

### 5.3 OCR repair — LLM, gated, budgeted

- **Deterministic and safe:** ligatures, zero-width/control characters
  (§4.2 `fixLigatures`). Everything else (`tbe`/`the`, `modem`/`modern`,
  `rn`/`m`, `cl`/`d`) is context-dependent: a rule that always maps
  `tbe→the` corrupts a genuine (if rare) word; the plugin has no
  dictionary to lean on. **Model, with surrounding sentence** — exactly
  the brief's reasoning.
- **Gate:** the deterministic `ocrSuspicion` score. Below threshold, the
  OCR prompt never runs — a clean PDF never risks being "fixed". This is
  the main defence against corrupting correct text: *most documents never
  see the OCR pass at all.* The cut-off ships conservative (high) and is
  tuned only against Stage 0's scan fixture.
- **Prompt:** the section text plus an explicit instruction list
  (typo-class OCR confusions only; broken ligatures; split numbers; stray
  characters; **return the text unchanged if unsure; do not rephrase, do
  not reorder, do not add or remove sentences**). Output →
  `stageFromDiff`.
- **Budget:** the current `DEFAULT_MAX_EDITS = 2000` (offsetmap.ts:80) is a
  memory guard, not a semantic one. The LLM stage passes its own
  `maxEdits = max(8, ceil(len(text) * 0.02))` — ~36 edits for a default
  1800-char section. Overrun = "the model rewrote, not repaired" → the
  deterministic text stands. Combined with the gate, a wrong-word risk
  needs *both* a suspicious score *and* an over-budget-free rewrite — and
  the worst case is bounded: one misheard word where an OCR error would
  have been misheard anyway.
- **Timeout/failure:** as the reader design already specifies —
  deterministic text spoken, logged, marked once in the control bar.

### 5.4 Abbreviation expansion — LLM (ordinary mode), not a rule

A `Fig.→Figure` table is temptingly deterministic, but abbreviation sense
is language- and corpus-dependent, and the daemon's per-sentence
`pronunciation` transform already covers how abbreviations are *said*
(`PDF_PROFILE` binds `sentence_transforms=("pronunciation",)` —
`src/speakd/profiles.py:57`). A cleanup rule would duplicate it and drift.
Leave to the LLM's ordinary mode (and the agent on the skill path); if
Stage 0/field use shows `pronunciation` already suffices, cut it from the
prompt too. YAGNI until observed otherwise.

### 5.5 The deterministic-only design, honestly

Without the LLM: headers, columns (if Zotero's order is right — Stage 0),
and most of footnote handling (the `drop_all` and `defer_all` policies
outright; under `classify`, the deterministic filter resolves clear cases
and the residue defaults to *kept*) all work; OCR repair covers ligatures
only. What is lost is exactly the residue the brief calls semantic. The
cost of the LLM: an endpoint dependency, a preference pane, failure paths,
and the privacy line in the pane — plus, with the batched contract, a
bounded number of calls (≤ 2 per section, §5.2). **Verdict: build Stage A
fully rules-only first (it is independently useful — the existing staging
philosophy), add `llm.ts` as Stage B behind the existing `Clean` seam, off
by default.** If the owner sets footnote policy to (b) or (c) and the
corpus turns out low-OCR, Stage B may shrink to abbreviation/noise only or
be deferred entirely — the architecture does not force it.

---

## 6. Offset-map / highlight contract and the latency budget

The contract, restated from the code that enforces it today: every
character-level stage is a `Stage` verified by `offsetmap.verify`, with
`apply`'s fallback on any inconsistency; `planHighlights` (mapping.ts:43)
maps daemon sentences back through the composed map and runs the
12-letter evidence defence (mapping.ts:25) that drops a highlight rather
than guess.

**How each new stage respects it:**

- **Segment filter / footnote deferral:** operate strictly on whole
  segments, *before* `buildSections` builds text and offsets. A dropped
  segment simply never appears in any `Section`; a deferred one appears
  with its true index in `segmentIndices` and a true offset — the map
  stays exact with no new machinery. Reordering is legal at segment
  granularity and illegal at character granularity; this design never
  does the latter. (Export path: no map at all, only more freedom.)
- **New `cleanup.ts` rules:** each built through the existing `rewrite`
  helper (cleanup.ts), which produces its map per match and falls back
  whole-stage on any local diff failure.
- **LLM text-repair stage:** `stageFromDiff` + `apply` — the designed path;
  the tightened edit budget (§5.3) makes `diff` fail loudly on rewrites;
  the evidence defence in `mapping.ts` remains the final backstop: even a
  stage that somehow verified with a wrong map drops highlights, never
  mis-highlights.
- **Footnote-classification LLM:** never touches characters; it labels
  segments (§5.2).

**Time-to-first-audio (target 2–4 s, binding):**

- All deterministic stages run at controller-construction / capture time
  over a whole-document segment list — a few hundred segments, pure
  arithmetic, single-digit milliseconds. No change to section-1 latency.
- The section-1 rule is already wired: `Clean` receives `ordinal`
  (channel.ts:114 region); `cleanWithLLM` returns the deterministic stage
  for `ordinal === 0`. The LLM cleans sections 2+ while section 1 speaks —
  the reader-design revision #5 behaviour, now actually implemented.
- Footnote classification runs *in the background* at read start: the
  residues of all sections are batched and asked for asynchronously, and a
  section due to enqueue consults the cache — a hit is free; an
  unresolved miss falls back to the policy default (keep) **immediately**.
  **The read never waits on the network, on any section, for any reason.**
- A background burst at read start is also bounded by the batched contract
  (§5.2): at most one outstanding classification call per section, issued
  lazily as its section approaches (one ahead, matching how sections are
  cleaned), not all at once.

**Export asymmetry, exploited:** exports run the same pure stages plus
optional per-*part* LLM (batch budget, no latency constraint), and the
whole-document deterministic pass is free there — which is where
`segmentfilter`'s frequency analysis is naturally at home anyway (a live
read also has the whole segment list at controller construction, since
Zotero hands the controller the full array — internals §3.2).

---

## 7. OCR repair — what is safe, what is not

| Class | Deterministic? | Where |
|---|---|---|
| Broken ligatures `ﬁ`→`fi`, `ﬀ`→`ff`, … | Yes — unambiguous codepoint substitutions | `fixLigatures` rule, both surfaces |
| Zero-width / control / BOM / stray `·` and `¬` marks | Yes — deletions | same rule |
| Split digits ("in 1 994") | Narrow yes — only with a year-context guard; otherwise no | `joinSplitDigits`, gated by Stage 0 evidence; cut if fiddly |
| Confusion pairs (`tbe`/`the`, `modem`/`modern`, `rn`/`m`, `cl`/`d`, `vv`/`w`) | **No** — context decides; a static map corrupts correct text | LLM, `ocrSuspicion`-gated, edit-budgeted (§5.3); skill path: agent spot-check, prompted by `--ocr-report` |
| Word joins/splits ("ofthe"→"of the") | **No** — semantic | LLM only, same gate |
| Abbreviation repair | No | LLM ordinary mode; daemon `pronunciation` may already cover |

The corruption-avoidance stack, in order: (1) the gate keeps clean text out
of the OCR pass entirely; (2) the prompt demands minimal, listed-class
edits and "unchanged if unsure"; (3) the 2 % edit budget rejects rewrites;
(4) `apply`'s fallback drops a failed stage; (5) the evidence defence drops
highlights before any mis-highlight; and the ground truth: the source
document, its highlights, and its annotations are never written — a wrong
fix is a misheard word, not corrupted data.

---

## 8. Testability and verification

**Stage 0 — characterisation before construction (the cheap answer to every
"unverified" in §1).** The export capture path already yields a full
segment dump (`capture()` via `export/capture.ts`, no playback). Add a
dev-only debug verb (log/panel, not shipped UI) that dumps captured segments
with text, page, anchor, **first-rect y, rect count, and per-page rect
coverage** to a file. Run it on the owner's failing sample (open question 2)
plus one two-column paper and one footnote-heavy paper. Read the dump and
write down, per failure mode, whether Zotero already handles it, and whether
rects are present on every segment or only some — the footnote band design
(§4.2) degrades to marker-only wherever coverage is thin. **Any filter built
before this dump is guessing** — the same mistake reader-design decision #4
made.

**Stage 0 also includes the skill-path two-column test.** Run
`prepare_text.py` (the current, unchanged script) on a real two-column PDF —
the owner's sample if it is two-column, else a checked-in fixture — and
compare the output against the paper's true reading order. Record the
result: it decides whether §4.3's `mutool stext` rewrite is built at all.
Poppler's default mode attempts reading-order detection (§1.3), so the
outcome is genuinely open; either way the finding is written into the
Stage 0 notes with the fixture so the decision is reproducible.

**Unit tests (vitest, plugin):**

- `segmentfilter.test.ts`: recurring head across ≥ 40 % / ≥ 5 pages; a
  body paragraph that opens a page once is never dropped.
- `footnotes.test.ts`: fixture segment lists (synthetic geometry: body
  paragraphs at y≈200, footnotes at y≈750 of 842-tall pages, markers,
  wraps, two-page footnotes; and the no-rects variant) → expected
  `FootnotePlan`, each policy; citation-shape vs borderline classification.
- `cleanup.test.ts`: new rules — ligature expansion maps, split-digit
  guards (ranges survive), `ocrSuspicion` behaviour.
- `llm.test.ts` (stubbed fetch): timeout race → deterministic stage; HTTP
  error; empty answer; over-budget rewrite rejected by `stageFromDiff`;
  ordinal 0 skips the call; the batched footnote call — residue chunking,
  cache hits, an unparsable JSON array → the whole residue falls back to
  the policy default.
- `sections.test.ts`: deferred footnotes appended at section end with true
  indices and offsets; `mapping.test.ts`: a deferred footnote's daemon
  sentence highlights the footnote's own segment.
- `export/parts.test.ts`: parts now run through filter + cleanup (the
  current raw-join behaviour becomes the "no-op rules" case).

**Unit tests (skill, Python):** stext fixtures (checked-in XML dumps, no
PDF needed at test time) → column order, band drops, footnote defer,
fallback path without mutool, existing heuristics unchanged — all on
whichever branch Stage 0 picked, plus the `--footnotes` flag and
`--ocr-report`.

**Shared corpus:** the three fixture PDFs + expected cleaned text under
`test/fixtures/pdf/`, consumed by both harnesses (§4.4).

**Live/harness verification (manual, per the README/internals checklist):**
read a two-column paper and a footnote-heavy paper end-to-end with
highlights on; export the same papers and diff the exported text against
the corpus expectations; a rules-only run (LLM unconfigured) must be
indistinguishable-in-latency from today; with the LLM on, kill the endpoint
mid-read and confirm the read continues deterministically with one bar
mark. Time-to-first-audio re-measured on the reference laptop against the
2–4 s target.

---

## 9. Risks, non-goals, open questions

**Risks**

- **Silent drops are the worst failure of a filter.** A header rule that
  over-fires removes speech with no visible trace (the mirror of
  mis-highlighting). Mitigations: recurrence-only and band-only rules
  (never content-based body drops), Stage 0 grounding, corpus tests, and a
  log line per dropped segment for the debugging session. Accept that a
  wrong *kept* footnote is preferable to a wrong *dropped* paragraph.
- **Zotero drift and rect coverage.** The new geometry fields come from
  `sourcePosition.rects` via the one fragile module, and rect coverage is
  unverified (§4.2); a Zotero change — or simply a document class where
  segments lack rects — degrades the footnote band to marker-only, never
  crashes (missing-rects tolerance), and the probe list still fails loudly
  on the members that matter. Stage 0 measures real coverage before the
  band threshold is trusted.
- **LLM cost/privacy:** bounded at ≤ 2 calls per section by the batched
  contract (§5.2), but still a call on every read of a long paper once the
  LLM is on; the pane's disclosure line (reader-design revision #7) is
  mandatory; off by default.
- **The skill's stext rewrite (if Stage 0 justifies it)** is the largest
  single chunk of new code on either surface; the `pdftotext` fallback
  bounds the blast radius.
- **Wrong OCR fixes are heard, not seen.** Bounded by the §7 stack, but
  the owner should decide whether "possibly better-sounding, occasionally
  wrong" beats "faithfully wrong" — the gate defaults to the cautious side.

**Non-goals (cut on purpose)**

- No character-level reordering, ever (constraint 1).
- No daemon-side text rewriting, no shared TS/Python "library" (§2 B).
- No LLM pipeline on the skill path (the agent is it).
- No reading-order work in the plugin beyond what Zotero's SDT already
  produces (open question 3 — pending Stage 0, assumed handled).
- No footnote *inline reading* option (d) — deferral at section end covers
  the use case with one mechanism; (d) is a v2 knob if the owner insists.
- No word-level karaoke, cross-section seek, or any reader v1 non-goal —
  untouched.

**Open questions for the owner**

1. Footnote policy `classify`/`drop_all`/`defer_all` — default `classify`
   (option (a)); the others shrink the design (§3, §5.2). A setting
   change, not a rewrite.
2. The failing sample PDF, needed before Stage 0 can run (open question 2
   of the brief, still the highest-leverage unanswered item) — and the
   same sample, or a nominated two-column paper, decides the skill-path
   extractor (§8).
3. Whether OCR repair's residual risk is acceptable at all, or whether
   Stage B should ship footnote-classification/noise/abbreviations only
   and leave OCR to the ligature rule (a one-line change to the gate).
4. Endpoint/model choice and cost expectations for the plugin's LLM stage
   (the preference pane needs defaults; OpenRouter is the incumbent
   pattern via the TTS engine, but no chat client exists in the repo yet).
5. On the skill path: is deferring kept footnotes to page-end acceptable,
   or should the agent be told (SKILL.md line) and left to move them
   chapter-end when the outline allows it?

**Proposed sequence** (for the plan the brief asks for after this is
approved): Stage 0 characterisation (segment dump + rect coverage + the
skill-path two-column test) → plugin deterministic stages + export wiring +
skill-path footnote/column work per Stage 0's branch (Stage A, fully
useful, rules-only) → `llm.ts` Stage B behind the existing seams → corpus +
live checklist.
