# PDF text cleanup for narration — request (not yet designed)

**Date:** 2026-10-08
**Status:** requested, unstarted — needs a spec and an implementation plan
**Requested by:** Severin Gartland
**Written up by:** assistant during a brainstorming session

This is a capture of a request, not a design. It records what was asked, what
the two text-extraction paths do today, and the constraints any design must
honour. The next model should treat it as a starting brief and run the normal
design flow (questions → approaches → written spec → plan) before touching code.

## What was asked

Improve how speakd turns PDFs into narrated text, on **both** surfaces:

1. **Remove running headers and footers.**
2. **Handle footnotes graciously** — today they are read inline, mid-flow, which
   is wrong for footnote-heavy papers. The exact desired behaviour is an open
   question (see below).
3. **Handle multi-column text properly** (reading order).
4. **Fix small OCR errors** (added by the owner after the first write-up): a
   scanned or badly-OCR'd text layer's small mistakes — `tbe` for `the`, `1 994`
   for `1994`, `modem` for `modern`, broken ligatures, stray characters — should
   be corrected where confidence is high, not read aloud verbatim.
5. "etc." — i.e. the general class of layout junk a PDF's text layer leaves
   behind that should not be spoken.

The owner also floated using an **LLM in the Zotero plugin to decide** how to
handle footnotes, headers and similar. See "On the LLM idea" — the conclusion
was: LLM for semantic calls only, deterministic rules for structural/geometric
ones, with the LLM stage confined to per-section, edit-only work.

## The two surfaces (both in scope)

There is no single "zotero integration"; text is extracted in two unrelated
places, and the request covers both.

### A. The Zotero plugin — reads and exports through Zotero's own machinery

`clients/zotero/` (TypeScript). The plugin does **not** extract text itself. It
hijacks Zotero 10's Read Aloud path and captures the segments Zotero's
Structured Document Text (SDT) worker builds.

- Capture: `clients/zotero/src/session.ts` (`capturedSegments`, `capture()`),
  `clients/zotero/src/reader-takeover.ts`, host wrapper in
  `clients/zotero/src/export/capture.ts`.
- Zotero's SDT builder is where reading order and header/footer dropping already
  supposedly happen. See `docs/design/2026-09-24-zotero-10-read-aloud-internals.md`
  §3.1: part chains join paragraphs across pages and columns; blocks whose
  top-level `flowClass === 'excluded'` are skipped ("this is where headers,
  footers and similar are dropped"); bracket/parenthesis groups that are mostly
  linked text, and superscript link markers, are elided.
- **But it is unverified.** The same doc's "Known gaps" list asks whether
  `flowClass === 'excluded'` covers *every* header, footer and page number —
  "the decision is made in the PDF worker's SDT processor, which was not read."
  Two-column and footnote-heavy PDFs are still on the manual test checklist
  (`clients/zotero/README.md`, `docs/HANDOFF.md`).
- The plugin's own cleanup is `clients/zotero/src/cleanup.ts`: only
  `joinHyphenation`, `dropPageNumbers`, `collapseWhitespace`. Rules are `Stage`s
  carrying offset maps (`clients/zotero/src/offsetmap.ts`).
- **Cleanup is wired into live reading but not exports.**
  `clients/zotero/src/channel.ts` sets `this.clean = options.clean ?? ((text) =>
  cleanup(text))`. The export path, `clients/zotero/src/export/parts.ts`
  (`joinText`, `buildParts`), just joins raw captured segment text — an exported
  audiobook gets **no** cleanup at all today.
- The designed LLM stage (`llm.ts`, one chat-completions call per section) was
  **never built** — see reader design decision #5 and `docs/HANDOFF.md`
  ("Stage B (LLM text cleanup) is designed but not built").

### B. The `speakd-audiobook` skill — extracts with `pdftotext`/`mutool`

`clients/claude-code/skills/speakd-audiobook/scripts/prepare_text.py`
(stdlib-only Python; this is the path agents use to make audiobooks from PDFs in
`~/Zotero/storage/...`). It already has:

- running header/footer removal — a whitespace-normalised line (digits → `#`)
  occurring as the first or last non-blank line on at least 40 % of pages, with
  at least 5 pages (`running_lines`, `HEADER_SHARE`, `HEADER_MIN_PAGES`);
- bare page-number drops, arabic and roman (`is_page_number`);
- de-hyphenation, paragraph joining, and a trailing references cut.

It has **no footnote handling and no explicit multi-column handling**; it relies
on `pdftotext`'s default reflow. This is the surface where "add some steps" is
most directly implementable.

## Constraints any design must honour

These are load-bearing; a design that ignores them will be rejected.

1. **Never mis-highlight (live reading).** Spoken segments map back to PDF
   source positions through an offset map. An LLM (or any) rewrite that does not
   diff cleanly either drops the segment's span to the whole piece or must be
   passed through unchanged. The established rule: any inconsistency → the
   stage's input is used unchanged. So new steps must be edit-only and
   diff-verified; never a reflow or reorder of the live text.
2. **Time-to-first-audio for live reading.** Live reading streams ~page-sized
   sections and targets 2–4 s to first audio. A whole-document pass before
   section 1 breaks that. Document-wide analysis must be deterministic (cheap);
   the LLM, if used in the live path, must be **per-section**.
3. **Export and live are different jobs.** Export is batch: it can afford a
   whole-document pass and currently gets no cleanup at all. Live reading
   already runs `cleanup.ts` per section. A shared design should exploit this
   asymmetry rather than force one pipeline on both.
4. **The daemon does not rewrite it.** The `pdf` profile binds no rewriting
   transforms (`src/speakd/profiles.py`, `PDF_PROFILE`) precisely so highlight
   spans stay sentence-exact. Cleanup therefore lives client-side, in the plugin
   (and/or the skill), not in a daemon transform — consistent with reader design
   decision #5.

## On the LLM idea

The owner asked whether the plugin should use LLM processing to decide how to
handle footnotes, headers, etc. The session's conclusion:

- **Use the LLM for semantic judgment**: is a footnote a bare citation or
  substantive prose; abbreviation expansion; residual citation-noise cleanup;
  and **small OCR corrections** (a rule cannot tell `modem` from `modern`, but a
  model with the surrounding sentence usually can).
- **Do not use it for structural/geometric judgment**: which lines are running
  heads/footers, where column boundaries are, reading order. Those are
  frequency/geometry problems (same y-position, recurring across pages), better
  done deterministically, explainably and testably. An LLM asked to find headers
  may rewrite prose in the process, which collides with constraint 1.
- The plugin already has a designed-but-unbuilt LLM stage to build on:
  deterministic-first, then LLM; timeout/HTTP error/empty answer/diff failure →
  speak the deterministic text, log it, mark it once in the control bar. Reading
  never waits on the network beyond the first section's budget.
- There is no LLM/chat client in the repo yet. The daemon has an OpenRouter
  *TTS* engine (`src/speakd/synth/openrouter_engine.py`), not a chat client. The
  reader design expects the plugin to talk to a configured OpenAI-compatible
  endpoint with its own endpoint/key/model settings, and to support a rules-only
  mode.

## Open questions (unresolved — ask the owner)

1. **Desired footnote behaviour** — this decides whether the LLM is even needed.
   - (a) Classify each footnote: drop citation-only ones silently, keep
     substantive prose, and read kept ones at the end of the page/section
     rather than inline. (LLM classifies; deterministic fallback.)
   - (b) Drop all footnotes silently, like citations.
   - (c) Keep all footnotes but move them to the end of the section/chapter,
     out of the body flow — no LLM classification.
   - (d) Something else (e.g. read a short footnote inline where referenced,
     defer long ones).
   The session leant towards (a), given a philosophy corpus whose footnotes
   carry real argument — but this is the owner's call, not assumed.
2. **A failing sample.** A PDF where the current output is bad (repeated running
   heads, footnote text read inline, two columns interleaved) was requested but
   not provided. It would tell us which failure mode actually occurs, rather
   than guessing — especially for the Zotero path, where the behaviour is
   Zotero's, not ours.
3. **How much multi-column work belongs in the Zotero path at all**, given
   Zotero's SDT builder owns reading order there and the plugin only sees flat
   captured text. If Zotero's order is already right, the plugin's job is
   header/footer/footnote filtering, not re-ordering.

## Classification and next steps

This is **architectural**, not a bounded change: it spans two codebases, adds a
new LLM subsystem (endpoint config, failure handling, offset-map verification),
and hinges on a cross-cutting invariant (highlight correctness) plus a latency
contract. It therefore needs the heavier path:

1. Answer the open questions above with the owner.
2. Propose 2–3 approaches with trade-offs.
3. Write a spec to `docs/superpowers/specs/YYYY-MM-DD-pdf-text-cleanup-design.md`.
4. Get it reviewed, then create a plan via the writing-plans skill.

Do not start implementing from this note alone.

## Where the code is (quick index)

| Concern | File |
|---|---|
| Zotero plugin capture (segments) | `clients/zotero/src/session.ts`, `reader-takeover.ts`, `export/capture.ts` |
| Plugin deterministic cleanup | `clients/zotero/src/cleanup.ts` |
| Plugin offset maps | `clients/zotero/src/offsetmap.ts`, `mapping.ts`, `sections.ts` |
| Plugin live reading wiring | `clients/zotero/src/channel.ts` |
| Plugin export text assembly (no cleanup) | `clients/zotero/src/export/parts.ts` |
| Zotero internals reference | `docs/design/2026-09-24-zotero-10-read-aloud-internals.md` |
| Reader design (decisions, LLM stage) | `docs/design/2026-09-24-zotero-reader-design.md` |
| Skill PDF extraction | `clients/claude-code/skills/speakd-audiobook/scripts/prepare_text.py` |
| Skill | `clients/claude-code/skills/speakd-audiobook/SKILL.md` |
| Daemon `pdf` profile (no rewriting transforms) | `src/speakd/profiles.py` |
| Daemon render verb | `src/speakd/cli.py`, audio-export design docs |
