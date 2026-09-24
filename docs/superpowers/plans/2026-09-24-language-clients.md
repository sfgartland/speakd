# Language in the Clients — Implementation Plan (phase 3 of settings-and-languages)

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:executing-plans. TDD throughout.

**Goal:** The Zotero plugin sends each document's language, steps aside to Zotero's own Read Aloud when speakd cannot speak it, and declares its settings. Claude Code's `brief` takes an optional language, and its briefing guide becomes a setting.

**Spec:** `docs/superpowers/specs/2026-09-24-settings-and-languages-design.md` §3 (binding). Phases 1 and 2 are merged, providing `settings`, `set_setting`, `declare_settings`, the `setting` event, `status.languages`, and `lang` on enqueue.

## Global Constraints

- **Zotero:**
  - The document's language is read from Zotero's Read Aloud manager (`manager._lang`, the language Zotero detected or the item's language field) and normalised as in phase 2, in TypeScript.
  - The supported list comes from `status.languages.supported`, cached per link connection and refreshed on reconnect.
- **Stepping aside:** when the language is unsupported, the plugin does not take over.
  - Zotero's own voice selection proceeds unchanged. The takeover's `takeoverWanted` stays false, and the popup is not hidden.
  - The bar shows "speakd has no <Language> voice — Zotero is reading", with the language named via `Intl.DisplayNames`.
  - This applies only to reads the plugin's buttons start. Zotero's own button already never involves speakd.
- **Zotero settings:** `zotero.first_section_segments` (int 1–5, default 2) and `zotero.section_chars` (int 600–4000, default 1800).
  - Declared on connect through `declare_settings`.
  - Read through `settings {owner: "zotero"}` and applied from `setting` events.
  - They replace the constants in `sections.ts`, whose defaults stay as the fallback when the daemon is unreachable.
- **Claude Code:**
  - `speakd-mcp`'s `brief` input schema gains an optional `lang` (a string), passed as `lang` on the enqueue.
  - The server declares `claude-code.briefing_guide` (a multiline string whose default is `guide.DEFAULT`) when it first reaches the daemon, and reads the guide from settings for `initialize`'s instructions and for `briefing_status`, falling back to the file or the default when the daemon is unreachable.
  - Migration, once: if `~/.config/speakd/briefing.md` exists and the setting is still at its default, set the setting from the file, then rename the file to `briefing.md.migrated`.

## Review Focus

- A daemon that is unreachable at read start should leave the Zotero plugin's behaviour as before: no stepping aside on unknown support. Tested in Task 1.
- A document whose language Zotero cannot determine (`_lang` empty) should send no `lang`, so detection applies. Tested in Task 1.
- The briefing guide migration should run once only, and a failed rename must not repeat it. Tested in Task 3.

### Task 1: Zotero, sending the language and stepping aside
- A pure module `src/language.ts` provides `normaliseLang(code)` and `decide(docLang, supported | null) -> {take: boolean, lang?: string, reason?: string}`, with vitest tests.
- Wire it into `session.ts` and `reader-takeover.ts` at read start, so every enqueue carries `lang`.
- Verify live in the harness (`clients/zotero/test-live/`): with a French text PDF generated with fpdf2 (`uv run --with fpdf2`), the enqueue carries `lang:"fr"`; with a German one, Zotero keeps its own voice and the bar says so.

### Task 2: Zotero, declared settings
- Declare on connect, read, and apply live.
- vitest tests for the settings-to-sections parameters, with a live check that changing `zotero.first_section_segments` through `speakctl set` affects the next read's first section.

### Task 3: Claude Code
- `brief` gains `lang`; the guide setting is declared, read and migrated.
- Tests in `tests/test_mcp.py` using the existing subprocess harness: `brief` with lang passes it through, and it shows up in the fake daemon's enqueue; the guide comes from the setting once it is set; migration runs once, including its rename.

### Task 4: docs and gate
- Update the READMEs (the main one and `clients/zotero`), then run the full Python gate and the plugin's `npm test` and `npm run build`.
