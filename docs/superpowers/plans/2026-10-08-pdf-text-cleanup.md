# PDF Text Cleanup for Narration — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development. TDD throughout.

**Goal:** Clean up PDF text for narration on both surfaces: running headers/footers, graceful footnote handling, correct reading order, and safe small-OCR-error repair — in the Zotero plugin (`clients/zotero/`) and the `speakd-audiobook` skill (`clients/claude-code/skills/speakd-audiobook/scripts/prepare_text.py`). Optional LLM stage (Stage B) in the plugin, off by default.

**Spec (binding):** `docs/design/2026-10-08-pdf-text-cleanup-proposal-glm.md` (revised 2026-10-08 — the revision note at its top is part of the spec). Cited below as "proposal §N". The Zotero internals reference is `docs/design/2026-09-24-zotero-10-read-aloud-internals.md` ("internals §N").

**Workspace:** worktree `../speakd-worktrees/pdf-text-cleanup`, branch `feat/pdf-text-cleanup`.
- Python: `uv sync` in the worktree, then `uv run pytest`, `uv run mypy src tests`, `uv run ruff check .`, `uv run ruff format --check .`.
- Zotero plugin: `cd clients/zotero && npm ci`, then `npm test` and `npm run build`.
- Skill script: stdlib-only Python 3.11; its tests live in the root suite (`tests/test_audiobook_prepare.py`), so the root gates cover it.

## Global Constraints

- **Never mis-highlight (the offset-map contract).** Character-level stages must be `Stage`s (text + verified map) built with the existing `offsetmap.ts` machinery (`rewrite`, `diff`, `apply` fallback). Never reorder characters. Dropping, keeping or reordering **whole segments** is map-honest (highlights resolve by segment index, `sections.ts`); do segment-level filtering *before* `buildSections` builds text and offsets. The 12-letter evidence defence in `mapping.ts` is the final backstop — nothing may weaken it.
- **Time-to-first-audio stays 2–4 s.** All deterministic document-wide analysis runs at controller-construction/capture time (pure arithmetic over the segment list). The LLM, if enabled, runs per-section and **section 1 (ordinal 0) skips it entirely**; no stage of a live read may await the network. Footnote classification is fired in the background and consulted from a cache; a miss falls back immediately to the policy default (keep).
- **No daemon-side rewriting.** `PDF_PROFILE` (`src/speakd/profiles.py`) keeps no rewriting transforms; all cleanup is client-side. Do not touch the daemon's transform chain.
- **No LLM pipeline on the skill path.** The agent following `SKILL.md` is that surface's semantic stage; the script stays deterministic and stdlib-only.
- **Internals stay in one module.** The only change to `reader-takeover.ts` is copying more off `sourcePosition` through the existing waiver; missing `rects` → null geometry, never a throw, never a drop. The probe list (internals §7) is unchanged. Stage B's LLM is **off until an endpoint is configured**, and the preference pane carries the one-line disclosure that enabled, it sends the paper's text to that endpoint (reader-design revision #7).
- **Footnote policy is a setting, default `classify` (brief option (a)).** Plugin: `extensions.speakd-reader.footnote_policy` (`classify` | `drop_all` | `defer_all`). Skill: `--footnotes classify|drop|defer` (default `classify`). The owner's later answer is a setting change, not a rewrite. Under `drop_all`/`defer_all` no footnote classifier (LLM or otherwise) runs.
- **Repo style gates hold:** ruff line length 100, mypy strict (root), vitest + `tsc --noEmit` (`clients/zotero`, `npm run build` runs both). Commit messages say why, not what.
- **Never touch the user's real setup.** No writes to `~/Zotero`, the real Zotero profile, the running daemon, its socket or port 8642. Live Zotero work uses only the harness in `clients/zotero/test-live/` (its own profile under `~/.cache/speakd-zotero-test`, fake-engine daemon on HTTP 8743). Kill harness processes with a bracketed pattern: `pkill -f "[s]peakd-zotero-test/profile"`.

## Task ordering

Stage 0 (characterisation) **must** land before any filter threshold is tuned or trusted. Stage A (deterministic, rules-only, fully useful on its own) lands before the optional Stage B (LLM). Tasks 9 and 10 are **conditional** on Task 2's recorded outcome; Tasks 7b and the threshold tuning inside Task 5 are **conditional** on the owner's failing sample (proposal open question 2) — do not invent numbers there.

---

## Stage 0 — characterisation

### Task 1: Segment dump (dev-only) — `clients/zotero/src/export/debugdump.ts`

New pure module `debugdump.ts`: `dumpSegments(segments): string` writes one JSON line per captured segment — index, text, `pageIndex`, `anchor`, and **geometry coverage**: `rectCount` (how many entries `sourcePosition.rects` had, 0/null when absent) and `firstRectY` (the first rect's y, null when absent) — plus a trailing summary line per page (segments, how many had rects, min/max firstRectY). Add `firstRect`/`rectCount` fields to `SegmentInfo`/`CapturedSegment` (session.ts `readSegments` copies them tolerantly: `waive(segment.sourcePosition)?.rects` may be missing, undefined, or an empty array → null, never a throw). Expose it as a dev-only action driven through the harness test bridge (a method on the takeover handle; no shipped UI, no menu item) writing to a file path. **Tests first** (`clients/zotero/test/export/debugdump.test.ts`): synthetic `SegmentInfo` lists with rects, without rects, with `null` sourcePosition; assert the JSONL shape and the per-page summary; assert a segment without rects is emitted with null geometry, not dropped, and nothing throws. **Verification:** `cd clients/zotero && npm test && npm run build`.

Then **run it live in the harness** on: the owner's failing sample (if provided), a two-column paper, and a footnote-heavy philosophy paper (the harness sample library; add PDFs there if needed). Write the findings to `docs/design/2026-10-08-pdf-text-cleanup-stage0.md`: (a) does Zotero's `flowClass === 'excluded'` drop headers/footers/page numbers on these samples, or do they appear as segments? (b) do footnote texts appear as segments, and with what anchors/markers/page tails? (c) **rect coverage**: do all segments carry rects, or only some — and what share of the bottom-of-page candidates have them? (d) is two-column reading order correct in the segment list? These findings bind Tasks 4–6; if the sample is not provided, mark (a)–(d) open and make the band-threshold tuning in Task 5 conditional.

### Task 2: Skill-path two-column characterisation (decides Task 9's branch)

Add to `tests/test_audiobook_prepare.py` (or a sibling file `tests/test_audiobook_stage0.py`) a characterisation test: generate a **real two-column academic-layout PDF** (PyMuPDF, `pytest.importorskip("fitz")`; two columns via two `insert_textbox` columns per page, ~8 pages with running headers) and run the *current* `prepare_pdf` on it; assert the output preserves a known cross-column sentence intact (i.e. the test encodes whichever order poppler's default mode actually produces, and is marked as a characterisation, not an endorsement). If the owner's failing sample is two-column and available, run the current script on it too and record the result. Record the outcome in `docs/design/2026-10-08-pdf-text-cleanup-stage0.md` under "skill path": **`stext`** (poppler default mode fails the reading order → build the Task 9 stext extractor) or **`pdftotext`** (order is already correct → Task 9 is marker-based footnote deferral only, no column code). Poppler's default mode *attempts* reading-order detection (proposal §1.3), so the outcome is genuinely open; the fixture and the recorded command make the decision reproducible. **Verification:** `uv run pytest tests/test_audiobook_prepare.py` (or the new file).

---

## Stage A — deterministic, rules-only (fully useful alone)

### Task 3: `clients/zotero/src/segmentfilter.ts` — running-head frequency filter

Pure module, the skill's `running_lines` transposed to segments (proposal §4.2): group segments by `pageIndex` (inherit the predecessor's page when null, as `capturedSegments` does); normalise (whitespace-collapsed, digits → `#`) the opening and closing *short* (≤ ~80 chars, non-`paragraphStart`-anchored-or-any — keep it simple: any segment) paragraph of each page group; a normalised string recurring as first or last on ≥ `HEADER_SHARE` (0.4) of ≥ `HEADER_MIN_PAGES` (5) pages (mirror the skill's constants, cite them) is dropped. A body paragraph that opens a page once is never dropped. **Tests first** (`clients/zotero/test/segmentfilter.test.ts`): recurring head across 6 pages dropped; under 5 pages nothing dropped; share below 0.4 not dropped; digit-normalisation ("Chapter 3" vs "Chapter 4" both → "Chapter #") matches them as one running head; a page's unique closing sentence is never dropped; empty/short-list input returns input unchanged. **Verification:** `cd clients/zotero && npm test && npm run build`.

### Task 4: `clients/zotero/src/footnotes.ts` — detection, deterministic classification, policy

Pure module per proposal §4.2/§5.2. Exports: `FootnotePolicy = "classify" | "drop_all" | "defer_all"`, `FootnotePlan` (per segment index: `body` | `drop` | `defer` | `classify`), and `planFootnotes(segments, policy, opts?): FootnotePlan`. Detection: candidate = `anchor === 'paragraphStart'` with firstRectY in the page's bottom band **and** text opening with a footnote marker (`¹²³⁰-⁹`, `^\d{1,2}[.\s]`, `^n\d`, `^[*†‡¶]`); **no-rects fallback**: with no rect coverage for a page group, a `paragraphStart` segment opening with a marker, followed (before the next `paragraphStart` body) by further sequential-marker paragraphs, is a candidate — marker-only, never content-based. Continuation: non-anchor paragraphs after a candidate still in the band. Classification (policy `classify` only): citation-shape (author-year, `Ibid.`, `Cf.`, `op. cit.`, `See`, digits+punctuation-dominated, short) → `drop`; long multi-sentence prose without a citation opening → `defer`; the in-between → `classify` (the LLM's only input, Task 13). Policies `drop_all`/`defer_all` mark every candidate/continuation `drop`/`defer` with no classification. **The bottom-band percentile is a provisional constant** (start conservative, e.g. the bottom ~25% of the page's observed y-range); **tuning it is conditional on Task 1's dump** — if the dump shows real footnote y-distributions, set the value from them; if the sample is missing, leave the provisional value and mark it in the Stage 0 notes. Every dropped segment logs (via a returned `drops` list, not console side effects in the pure module). **Tests first** (`clients/zotero/test/footnotes.test.ts`): synthetic geometry (body y≈200, footnotes y≈750 of 842-tall pages; markers, wrapped continuations, two-page footnotes); the **no-rects variant** (marker-only detection still works, nothing thrown); citation-shape → drop, clear prose → defer, borderline → classify; each of the three policies; a body paragraph opening with a digit ("1994 was…") is not a candidate when its y is in the body band and it is not followed by sequential markers. **Verification:** `cd clients/zotero && npm test && npm run build`.

### Task 5: Deferral in `sections.ts` + highlight proof in `mapping.ts`

Extend `sections.ts`: a wrapper (or overload of `buildSections`) taking the tagged segment list — `defer`red footnote segments are held back and appended at the end of the section containing their page, with **true original indices** in `segmentIndices` and true offsets. Sizes and the first-section rule unchanged. Dropped segments never appear. **Tests first:** in `sections.test.ts` — deferred footnotes land at the end of the section that owns their page; a section of only dropped segments yields nothing; indices are original document indices; in `mapping.test.ts` — a daemon sentence over a deferred footnote's offset highlights **the footnote's own segment** (never a body segment); in `channel.test.ts` (or a new case) — a read whose first section consists only of a dropped segment's page still starts with speech. **Verification:** `cd clients/zotero && npm test && npm run build`.

### Task 6: New rules in `cleanup.ts` (+ `ocrSuspicion`)

Add to `cleanup.ts` (proposal §4.2): `fixLigatures` (ﬁ ﬂ ﬀ ﬃ ﬄ → expansions; zero-width/control/BOM/stray `·` `¬` → removed) via the existing `rewrite` helper; export `ocrSuspicion(text): number` (ligature remnants, control chars, split-digit hits, ratio of 1–2-letter tokens) — deterministic, used later to gate Stage B's OCR prompt and the skill's `--ocr-report`. `joinSplitDigits` is **conditional**: implement it only if Task 1's dump (or the fixture corpus) shows real split-digit artefacts with a safe year-context guard (`in|im Jahr|since|bis` before the split run, joined result a 4-digit year); otherwise cut it and note the cut. **Tests first** in `cleanup.test.ts`: ligature expansion and its map (verify round-trip offsets via `apply`/`lookup`), control-char deletion, `ocrSuspicion` high on a synthetic OCR-mangled string, low on clean prose (the exact cut-off is provisional — assert ordering, not a fixed number); split-digit guards if implemented ("in 1 994" joins, "pp. 12– 20" does not). **Verification:** `cd clients/zotero && npm test && npm run build`.

### Task 7: Wire the live and export paths + the footnote policy setting

- **Live path:** before `buildSections` in the read start, run `segmentfilter` then `planFootnotes` (policy from a new pref `extensions.speakd-reader.footnote_policy`, default `classify` — declare it beside the plugin's existing settings, e.g. `prefs.ts`), then build sections from the tagged list. All at read start, synchronously (milliseconds; the TTFA budget is untouched). Under `classify`, `classify`-tagged segments are treated as **deferred-kept** (the policy default for unresolved) until Stage B exists; after Stage B they are resolved by the cache-first background batch.
- **Export path:** `export/parts.ts` runs the captured `CapturedSegment[]` through `segmentfilter` + `planFootnotes` (same policy pref) before page-range/chapter selection, and through `cleanup()` per part before `joinText` (proposal §4.2). Nothing in `exporter.ts`/`jobs.ts` changes.
- **Tests first:** `channel.test.ts` cases — a fixture read where the running head and citation footnotes are absent from every enqueued section and deferred footnotes appear at their sections' ends; the policy pref flipping `drop_all`/`defer_all`/`classify` changes exactly the footnote segments' treatment; `export/parts.test.ts` — parts now come out cleaned (the old raw-join behaviour is the "no-op rules" case with an empty filter result); `prefs.test.ts` — the new pref and its default.
- **Live check (harness, rules-only):** read the harness sample papers end-to-end; confirms highlights still correct with filtering on. **Verification:** `cd clients/zotero && npm test && npm run build`.

### Task 8: Skill path — footnote handling (branch per Task 2) + `--footnotes` + `--ocr-report`

In `prepare_text.py`, per the recorded Stage 0 branch (proposal §4.3):

- **Branch `stext` (Task 2 recorded a poppler reading-order failure):** add an stext extraction path — `mutool draw -F stext -o <file.xml> <pdf>` (tolerant: missing mutool or a failed call → fall back to the existing `pdftotext` path, degraded, one stderr line), parse the XML (stdlib `xml.etree.ElementTree`), and build the page structure: `order_columns` (blocks grouped by x-overlap into columns; each column read fully top-to-bottom before the next), band drops (recurring top-band head lines — reuse `running_lines` semantics over the *first block per column per page*; bottom band below the body's main text flow), footnote detection (spans with font size below the body's dominant size, and/or blocks in the bottom band, opening with a marker) → drop or defer to the end of the page group per policy. The downstream pipeline (`join_paragraphs`, `cut_references`, outline chapters) is unchanged.
- **Branch `pdftotext` (Task 2 recorded poppler correct):** no column code. Marker-based footnote deferral only: a line-led group at a page's tail opening with a footnote marker (superscripts may degrade to plain digits — `^1 …`, `^n1 …`), taken as a footnote paragraph, deferred/dropped per policy.

Either branch: `--footnotes {classify,drop,defer}` (default `classify`; here `classify` = the deterministic citation-shape filter from Task 4's taxonomy, mirrored in Python, dropping citation-shaped footnotes and deferring the rest — the agent remains the semantic classifier), and `--ocr-report`: when an `ocr_suspicion()` heuristic (mirror of Task 6's) scores above its provisional threshold, print one stderr line ("OCR artefacts likely at …; worth a spot-check pass") — the exact cut-off is **conditional on a real scan fixture**; ship conservative and mark it. **Tests first** in `tests/test_audiobook_prepare.py` (or the Stage 0 sibling): checked-in stext XML fixtures (no PDF needed at test time) → column order, band drops, footnote defer/drop per policy, fallback when mutool is missing (monkeypatch `shutil.which`/subprocess), `--footnotes` flag behaviour, `--ocr-report` firing and not firing; the pdftotext branch gets marker-deferral tests over page-text fixtures. Full gates at the root. **Verification:** `uv run pytest tests/test_audiobook_prepare.py && uv run mypy src tests && uv run ruff check . && uv run ruff format --check .` (stdlib-only is itself a constraint — no new imports outside the stdlib).

### Task 9: `SKILL.md` — footnote and OCR notes

Two short edits to `clients/claude-code/skills/speakd-audiobook/SKILL.md`: in step 3, note that `--footnotes` policy was applied (default: citation-shaped footnotes dropped, the rest deferred to page end) and where to look if the spot-check shows footnote trouble; in step 4, note the `--ocr-report` stderr line means the file likely has OCR artefacts worth a targeted-edit pass (confusion pairs like tbe/the, modem/modern). No new steps. **Verification:** `uv run pytest tests/test_audiobook_prepare.py` (the skill-install test already copies the directory; nothing structural changes).

---

## Stage B — optional LLM (`llm.ts`, off until configured)

### Task 10: `clients/zotero/src/llm.ts` — the chat client and the per-section text-repair stage

New module (proposal §4.2, §5.3): an OpenAI-compatible chat-completions client reading endpoint/key/model/on-off from prefs (off until configured); a `repairSection(text, mode: "ordinary" | "ocr", ordinal): Promise<Stage>` that sends the section text with the prompt contract from proposal §5.3 (listed edit classes only; "return the text unchanged if unsure; do not rephrase, reorder, or add/remove sentences"), parses the answer, and passes it through `stageFromDiff` with `maxEdits = max(8, ceil(len(text) * 0.02))`. The OCR mode runs **only when `ocrSuspicion` is above the (provisional, dump-tuned) gate**; the ordinary mode runs only when the endpoint is configured. Timeout raced with a `later` hook; HTTP error / empty answer / unparsable / over-budget → the deterministic stage's result, logged, one bar mark (extend `Problem` or the bar's failure surface minimally — one boolean state, not a new UI). **Tests first** (`clients/zotero/test/llm.test.ts`, stubbed fetch + injected clock): success path produces a verified `Stage`; timeout → deterministic stage; HTTP error; empty answer; an over-budget rewrite is rejected by `stageFromDiff` and the input stands; ordinal 0 short-circuits before any fetch call (assert zero calls); unconfigured endpoint short-circuits.

### Task 11: Batched footnote classification + cache

In `llm.ts` (or a small `llm.footnotes.ts`), per proposal §5.2: `classifyFootnotes(footnotes: {index, text}[]): Promise<Map<index, "citation"|"substantive">>` — **one call per section's residue**, the numbered-list prompt, a JSON array answer parsed in order; residues > 20 chunked into calls of ≤ 20 (export path may pass a part's whole residue). Cache keyed by footnote-text hash for the session. Unparsable/short/wrong-length answer → the **whole call's residue falls back to the policy default (kept/deferred)**, logged. Wire into the read start: at read start, the per-section residues are classified in the background (issued lazily, one section ahead, matching how sections are cleaned — never all at once); a section due to enqueue reads the cache; a miss defers to the default **immediately** (never awaits the network). On the export path, the per-part residue is classified batch-style before `renderPayload`. **Tests first** (`llm.test.ts`): a two-footnote residue produces exactly one fetch call with both in the prompt; a 45-footnote residue chunks to 3 calls; the cache returns the second identical footnote without a call; an unparsable answer maps nothing and falls back; a timeout mid-classification never blocks `enqueueNext` (channel-level test); `drop_all`/`defer_all` policies make zero calls. **Verification:** `cd clients/zotero && npm test && npm run build`.

### Task 12: Channel wiring + preference pane

- `channel.ts`/read start: pass a `cleanWithLLM` as the `Clean` option — deterministic `cleanup()` always; the LLM repair stage additionally for `ordinal > 0` when configured and (ocr mode) gated (proposal §6). Ordinal 0 must be byte-identical to today's first section. Footnote classification from Task 11 is consulted (cache-first) when building sections under policy `classify`.
- Preference pane (the plugin's existing pane surface): endpoint, key (masked), model, LLM on/off (default off), the disclosure line ("enabled, this sends the paper's text to this endpoint"), and the footnote policy choice (`classify` default). **Tests first:** `channel.test.ts` — ordinal 0 skips the LLM (zero fetch calls, identical text); a configured endpoint changes sections 2+ text; an LLM failure leaves speech identical to rules-only; `prefs.test.ts`/pane tests — defaults and the disclosure string; a rules-only run with the LLM unconfigured is indistinguishable from Stage A. **Verification:** `cd clients/zotero && npm test && npm run build`.

---

## Closing tasks

### Task 13: Shared fixture corpus + Stage 0 notes closed out

`test/fixtures/pdf/` (root, git-ignored from nothing — checked in): the three document classes — a two-column paper, a footnote-heavy paper, an OCR-scan sample (generated: PyMuPDF for the first two; a scan-like one from a text-mangled render) — plus, per class, captured fixtures for **both** harnesses: `clients/zotero/test/fixtures/` JSONL segment dumps (text/page/anchor/rect geometry, produced via Task 1's dump in the harness) and `tests/fixtures/` stext XML / page-text fixtures, each with expected cleaned text per policy. `docs/design/2026-10-08-pdf-text-cleanup-stage0.md` gets its final "outcomes" section: which branch Task 8 took, the tuned (or still-provisional) thresholds — anything still conditional on the owner's failing sample says so explicitly. **Verification:** both suites consume the corpus green (`cd clients/zotero && npm test`; `uv run pytest tests/test_audiobook_prepare.py`).

### Task 14: Live/harness verification (proposal §8)

Using `clients/zotero/test-live/` (README "Development"): `setup-profile.sh`, `start-fake-daemon.sh &`, `run-zotero.sh`, `zeval.sh`. Checks, recorded in the Stage 0 notes file:
- Read the two-column paper and the footnote-heavy paper end-to-end with highlights on: every highlight on the right sentence; deferred footnotes highlight their own rects; dropped segments never highlight.
- Export the same papers and diff the exported text against the corpus expectations.
- A rules-only run (LLM unconfigured) is indistinguishable-in-latency from today; **time-to-first-audio measured** against the 2–4 s target (first section unchanged by construction — confirm by timing).
- With the LLM configured (fake endpoint in the harness daemon or a stub): kill the endpoint mid-read → the read continues deterministically with one bar mark; footnote classification misses fall back to kept without stalling.
- The footnote policy pref flipped to `drop_all` and `defer_all` live: no classifier calls, all footnotes go/defer.

### Task 15: Docs and the full gate

- `clients/zotero/README.md`: the new cleanup stages, the `footnote_policy` and LLM settings (with the disclosure line), the limits (marker-only degradation when a PDF's segments lack rects), and the two-column/footnote-heavy entries already on the per-release checklist now explained.
- `SKILL.md` already updated (Task 9); top-level `README.md` only if it mentions skill behaviour that changed.
- `docs/HANDOFF.md`: move the "PDF text cleanup" entry from "Deferred and known gaps" to built, noting Stage B is off by default and what remains conditional on the owner's sample.
- **The gate, all green:** `cd clients/zotero && npm test && npm run build`; at the root `uv run pytest`, `uv run mypy src tests`, `uv run ruff check .`, `uv run ruff format --check .`.

---

## Conditional items (do not guess; wait for the named input)

| Item | Blocked on | Task |
|---|---|---|
| Bottom-band percentile's final value | Task 1's rect/geometry dump on the owner's sample (or corpus fixtures) | 4 |
| OCR suspicion cut-off's final value | a real OCR-scan fixture in the corpus | 6, 10 |
| `joinSplitDigits` (build at all) | Stage 0 evidence of real split-digit artefacts | 6 |
| Skill-path stext extractor (vs marker-only) | Task 2's recorded two-column outcome | 8 |
| Stage 0 findings (a)–(d) on real failing documents | the owner's failing sample (proposal open question 2) | 1 |
