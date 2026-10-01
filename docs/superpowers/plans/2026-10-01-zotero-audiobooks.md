# Zotero Audiobooks — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development. TDD throughout.

**Goal:** Finish the Zotero export flow (right-click a PDF → **Create audio version…** → an mp3, or an m4b with chapters, attached to the item), and give agents (Claude Code, Codex, OpenCode) a skill that turns a PDF or text into an audiobook through `speakctl render`.

**Spec:** `docs/superpowers/specs/2026-09-24-audio-export-design.md` (binding). This plan finishes Part B of `docs/superpowers/plans/2026-09-24-audio-export.md`, whose **Global Constraints (B)** and **Review Focus (B)** sections bind Tasks 3–5 below verbatim; read them there. Part A (the daemon's `render` / `render_cancel` verbs, `render` events, `status.render`, `speakctl render`) and Task B1 (`clients/zotero/src/export/{references,outline,range,parts,estimate}.ts`) are already on main.

**Workspace:** worktree `../speakd-worktrees/zotero-export`, branch `feat/zotero-export`.
- Python: `uv sync` in the worktree (no extras needed; tests use FakeEngine), then `uv run pytest`, `uv run mypy src tests`, `uv run ruff check .`, `uv run ruff format --check .`.
- Zotero plugin: `cd clients/zotero && npm ci`, then `npm test` and `npm run build`.

## Global Constraints

- **Never touch the user's real setup.** No writes to `~/Zotero`, the real Zotero profile, the running speakd daemon, its socket (`$XDG_RUNTIME_DIR/speakd/speakd.sock`) or its HTTP port 8642. Live Zotero work uses only the harness in `clients/zotero/test-live/` (its own profile under `~/.cache/speakd-zotero-test`, its own library, a fake-engine daemon on HTTP 8743). Kill harness processes with a bracketed pattern: `pkill -f "[s]peakd-zotero-test/profile"`.
- Live Zotero verification for renders needs a daemon that can render: the harness's fake-engine daemon (`fakedaemon.py`) must support `render` (FakeEngine audio is fine) — extend it if it does not.
- Commit messages say why, not what; end each with the attribution lines the controller gives you.
- The repo's existing style rules hold: ruff line length 100, mypy strict, comment density like the surrounding code.

## Tasks

### Task 1: Chapter titles in `speakctl render`

`speakctl render textfile` splits on form feeds (`\f`) and names parts "Part 1", "Part 2", … (`_render_parts` in `src/speakd/cli.py`). Change it so a part whose first non-blank line is a Markdown-style heading `# <title>` takes `<title>` as its part title, and that heading line is removed from the part's spoken text. Parts without one keep "Part N" (N = the part's 1-based position among all parts). A single-part file keeps today's behaviour (`--title`, else the file stem), except that a leading `# <title>` line, when present and no `--title` is given, becomes its title. Blank parts (only whitespace after removing the heading) are dropped. Update the `textfile` help text to mention the heading. Tests in the existing CLI render test file: titled parts, mixed titled/untitled, single part with heading, single part with heading and `--title` (flag wins; heading line still removed), blank part dropped.

### Task 2: The `speakd-audiobook` agent skill

A skill that lets an agent turn a PDF (including a Zotero item's PDF), an EPUB-free plain text, or Markdown into an audiobook file with `speakctl render`.

- **Location:** `clients/claude-code/skills/speakd-audiobook/` (the Claude Code plugin root is `clients/claude-code/`, so the plugin ships it). It contains `SKILL.md` and `scripts/prepare_text.py`.
- **`scripts/prepare_text.py`** — stdlib-only Python 3.11, shells out to poppler/mupdf tools that are present on the user's machine (`pdftotext`, `mutool`). Usage: `prepare_text.py <input.pdf|.txt|.md> --out <file.txt> [--pages 12-48] [--chapters auto|none] [--keep-references]`. It:
  - extracts text per page (`pdftotext -layout -f N -l N` is not required; plain `pdftotext -f N -l N` per page or one call split on `\f`, which pdftotext emits between pages);
  - finds chapters from the PDF outline (`mutool show <pdf> outline`; top-level entries only) when `--chapters auto` and an outline exists, writing each chapter as a part beginning with `# <outline title>` and separated by `\f`; otherwise one part;
  - removes running headers/footers: a line (whitespace-normalised, digits replaced by `#`) occurring as the first or last non-blank line on at least 40 % of pages, when there are at least 5 pages;
  - drops lines that are only a page number (arabic or roman);
  - joins hyphenated line breaks (`exam-\nple` → `example`) when the hyphen ends a line and the next line starts lowercase; joins lines within paragraphs into one line; keeps blank-line paragraph breaks;
  - unless `--keep-references`, cuts the document (or the last chapter) at the last line matching `^(References|Bibliography|Works Cited|Literatur(verzeichnis)?)$` (case insensitive) that lies in the second half — the same pattern as the spec's references cut;
  - prints a summary to stderr: parts and their titles, characters, and a rough duration at 15 characters per second of speech.
  - `.txt`/`.md` input skips extraction and header logic; it only normalises paragraphs and passes through existing `\f` and `# ` headings.
- **Tests:** `tests/test_audiobook_prepare.py`, importing the script by path (`importlib.util.spec_from_file_location`). Pure functions get unit tests (header/footer detection, page-number lines, de-hyphenation, paragraph joining, references cut incl. first-half match ignored, outline parsing of `mutool show … outline` output captured as a fixture string). One end-to-end test generates a small PDF with an outline using PyMuPDF if importable (`pytest.importorskip("fitz")`) and the tools if on PATH (`shutil.which`, skip otherwise).
- **`SKILL.md`** (frontmatter `name: speakd-audiobook`, a `description` that triggers on "make an audiobook / audio version / read this PDF into an mp3/m4b / listen to this paper later"). Body, concise:
  1. Check the daemon can render: `speakctl status` → `render.available` true (ffmpeg present). If no daemon: tell the user to open speakd.
  2. Find the source. A Zotero item: PDFs live in `~/Zotero/storage/<KEY>/`; prefer a `find-source` skill if one is available; read-only — never modify Zotero's files or database.
  3. `prepare_text.py` to a working file under the scratch/tmp dir; read only its stderr summary and spot-check a few hundred characters at 2–3 places — never the whole text into context.
  4. Optionally fix what the spot-check shows with targeted edits (e.g. a stray header the heuristic missed).
  5. Render: `speakctl render <file> --out <path> --title … --artist … --date … [--album …] [--lang …] --no-wait` — `.m4b` when there is more than one part, else `.mp3`. Default output folder `~/Audiobooks/` named `<Author> - <Title>.<ext>`; ask only if the user named somewhere else.
  6. Report the job id and estimate; progress via `speakctl status` (the `render.jobs` entry). Renders yield to live speech and survive a daemon restart.
  7. For attaching the result to a Zotero item, point the user to the plugin's right-click **Create audio version…**.
- **Install for the other agents:** `clients/codex/install.sh` copies the skill directory to `${CODEX_HOME:-~/.codex}/skills/speakd-audiobook/`; `clients/opencode/install.sh` copies it to `${XDG_CONFIG_HOME:-~/.config}/opencode/skills/speakd-audiobook/`. Copy (replace on re-run), do not symlink. Extend `tests/test_codex_install.py` for the Codex copy; add a similar small test for the OpenCode installer if none exists (run it with `XDG_CONFIG_HOME` pointed at a tmp dir).

### Task 3: Segment capture without playing (Part B Task 2)

As specified in the Part B plan's Global Constraints (B), "Segments": open the PDF in a background reader (or reuse an open one), run segmentation through the manager with a `captureOnly` controller that never starts a channel, resolve with a copy of the segment array (`text`, `pageIndex`, `anchor`), close the tab if the plugin opened it. Unit tests for the controller logic with vitest where it can be isolated. **Verified live in the harness:** the daemon's enqueue count stays at zero and the captured segments are complete for the sample article. Record the live evidence in the report.

### Task 4: The dialog and the declared settings (Part B Task 3)

The XHTML dialog (`window.openDialog`) with range (chapter list from the outline, page range, or whole document), the references cut toggle, format, destination (attach or folder) and the estimate line, with defaults from `zotero.export_*` settings. Declare `zotero.export_format_article` (choice mp3/opus/m4b, default mp3), `zotero.export_format_book` (choice, default m4b), `zotero.export_destination` (choice attach/folder, default attach), `zotero.export_folder` (string, default empty = ask), `zotero.export_skip_references` (bool, default true), following how the plugin already declares its settings. Put the defaults-by-item-type and dialog-state → parts/selection logic in a pure module with vitest tests (article vs `book`/`bookSection`; book without outline offers page range only; skip front/back matter). The menu entry (`Zotero.MenuManager.registerMenu`, enabled for one PDF attachment or a regular item with exactly one PDF child) opens it. A live smoke check in the harness that the menu item appears and the dialog opens with the right defaults.

### Task 5: Render round-trip, progress, attach, resume (Part B Task 4)

Send `render` (out under `PathUtils.tempDir`, or the chosen folder), show progress in an item pane section (`Zotero.ItemPaneManager.registerSection`: state, percentage, estimate, Cancel → `render_cancel`), attach on completion (`Zotero.Attachments.importFromFile({file, parentItemID, title: "Audio — <title>"})`, then delete the temp file) or leave it in the folder, notify with `Zotero.ProgressWindow` on done/failed, and pick up running renders for an item from `status.render.jobs` on startup. Handle the "no text" case (PDF without text: a clear message, no render). **Verified live:** the sample article becomes an attached mp3 whose duration ffprobe confirms; a generated PDF with an outline (fpdf2's `start_section` or PyMuPDF's `set_toc`) becomes an m4b whose chapters ffprobe lists; cancel mid-render removes the job.

### Task 6: Docs and the gate (Part B Task 5)

- `clients/zotero/README.md`: the export flow — menu, dialog, settings, progress, where files go.
- Top-level `README.md` "Audio files" section: chapter headings in `speakctl render` (Task 1) and the agent skill (Task 2), how to install it for each agent.
- `docs/HANDOFF.md`: move audio export Part B from "Next" to built, note the skill.
- The gate, all green: `npm test` and `npm run build` in `clients/zotero`; `uv run pytest`, `uv run mypy src tests`, `uv run ruff check .`, `uv run ruff format --check .` at the root; `npm test` in `clients/opencode`.
