# Audio Export — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:executing-plans. TDD throughout.

**Goal:** Right-click a Zotero PDF, choose **Create audio version…**, and get an mp3 or opus file (for an article) or an m4b with chapters (for a book), rendered by speakd in the background and attached back to the item.

**Spec:** `docs/superpowers/specs/2026-09-24-audio-export-design.md` (binding). It builds on settings (phase 1), languages (phase 2) and the language clients (phase 3), all merged: voice choice per language, `render.*` settings, and `zotero.*` export settings.

**Facts:** `ffmpeg` and `ffprobe` are at `/usr/bin` with libmp3lame, libopus and aac. The Zotero test harness is in `clients/zotero/test-live/` (see the plugin README).

## Part A: the daemon's render jobs (Python)

### Global Constraints (A)
- **`src/speakd/render.py`** holds `RenderQueue` (one worker thread, jobs in order) and `RenderJob` (parts, out, format, lang, profile, metadata, state).
- **Work directory:** `$XDG_STATE_HOME/speakd/renders/<job-id>/`, holding `manifest.json` (parts, the next part and sentence index, seconds done) and `part-<n>.wav` (mono, the engine's sample rate, 16-bit PCM, appended as sentences finish).
- **Yielding to live speech:**
  - Before each sentence, the worker waits while `daemon.speaking` (any live utterance in flight or queued) is true, checking every 100 ms. So live speech is never delayed by more than the one sentence already synthesising.
  - Synthesis uses the same engine object as live speech, under a lock shared with the live pipeline's synthesize calls, so they never run at the same time.
- **Voice and language** use phase 2's resolution function: the job's `lang`, else detection over the part, else the default. The voice comes from the same choice function.
- **Encoding** runs `ffmpeg` via `subprocess.run` with an argument list, never a shell:
  - **mp3:** `-c:a libmp3lame -b:a <render.mp3_bitrate> -ac 1`
  - **opus:** `-c:a libopus -b:a <bitrate>`
  - **m4b:** `-c:a aac -b:a <bitrate> -f mp4`, with an ffmetadata chapters file (`[CHAPTER] TIMEBASE=1/1000 START END title=`) and `-map_metadata 1`
  - Tags: title, artist, album, date.
  - Output goes to `<out>.partial`, then `os.replace` to `out`.
- **Gaps:** `speech.sentence_gap_ms` (declare it if phase 2 did not; default 250) of silence between sentences, and `render.chapter_gap_ms` (default 1500) between parts.
- **Verbs:**
  - `render` answers `{job}`.
  - `render_cancel {job}`.
  - `status` gains `render: {available: bool (ffmpeg found), jobs: [...]}`.
  - The event is `render {job, state, part, parts, done_seconds, estimate_seconds, out?, error?}`, throttled to every 2 s while running.
  - Over HTTP, `render` and `render_cancel` are added to `VERBS` for any owner, with `out` required to be absolute and inside `Path.home()` after `resolve()`.
- **Resume:** at daemon start, unfinished manifests are re-queued and continue at the recorded sentence.
- **Settings declared:** `render.mp3_bitrate` (choice of `32k`, `48k`, `64k`, `96k`; default `64k`) and `render.chapter_gap_ms` (int 0–10000).

### Review Focus (A)
- A render running while a live enqueue arrives: the live utterance starts within one sentence's synthesis time. Tested.
- ffmpeg failing (a bad codec, disk full) should mean state `failed` with its stderr tail, the work directory kept for a retry, and no partial file left at `out`. Tested with a fake ffmpeg script on `PATH`.
- `out` resolving through a symlink to outside home should be refused. Tested.
- Cancelling a queued (not running) job should remove it without touching the running one. Tested.

### Tasks (A)
1. `render.py` with the queue, the manifest and PCM appending, using FakeEngine, and tests for manifest round-trips and resuming at a recorded sentence.
2. Yielding and the shared synthesis lock, with tests using a slow live player, as in `tests/test_seek.py`.
3. Encoding and chapters. With a real ffmpeg: an mp3 and an m4b of known lengths; ffprobe shows the duration within 5 % and the chapter titles and starts; tags present; the fake-ffmpeg failure path.
4. The verbs, events, status, HTTP rules and settings, each with tests.
5. `speakctl render <textfile> --out file.mp3 [--lang fr] [--title …]` for rendering from the CLI (one part per file, or per `\f` form feed), with a test.
6. A README section, "Audio files", and the gate. CI installs ffmpeg with apt: add `sudo apt-get install -y ffmpeg` to the Python job.

## Part B: the Zotero export flow (TypeScript)

### Global Constraints (B)
- **The menu** is `Zotero.MenuManager.registerMenu` on the item context menu. It is enabled for one PDF attachment, or one regular item with exactly one PDF child.
- **Segments** are captured through the takeover without playing:
  - Open the PDF in a background reader (`Zotero.Reader.open(id, null, {openInBackground: true})`), or reuse an open one.
  - Start segmentation through the manager with a controller that never starts a channel (`captureOnly`), resolving with a copy of the segment array: `text`, `pageIndex`, `anchor`.
  - Then close the tab if the plugin opened it.
  - Verify live that this plays nothing and sends no enqueue.
- **Pure modules, with vitest:**
  - `export/references.ts`: finds the references cut, the last heading-like segment (at most 60 characters, starting a paragraph with `anchor === 'paragraphStart'`) matching the spec's pattern in the second half.
  - `export/outline.ts`: turns the pdf.js outline plus a page index resolver into chapter ranges.
  - `export/range.ts`: parses page ranges like `12-48, 60`.
  - `export/parts.ts`: builds `[{title, text}]` from the segments and a selection, with the article and book defaults by item type.
  - `export/estimate.ts`: seconds from character count and a measured RTF (from `status` or a default).
- **The dialog** is an XHTML dialog (`window.openDialog`) with a range (the chapter list, pages, or whole), the references cut, the format, the destination (attach or folder), and the estimate. The defaults come from `zotero.export_*` settings, declared in the Part B tasks.
- **Rendering** sends `render` with `out` = a file under `PathUtils.tempDir`, or the chosen folder. Progress shows in an item pane section (`Zotero.ItemPaneManager.registerSection`) with state, percentage, estimate and Cancel.
- **When it's done:**
  - "Attach" means `Zotero.Attachments.importFromFile({file, parentItemID, title: "Audio — <title>"})`, then deleting the temporary file. "Folder" means leaving it there.
  - A notification (`Zotero.ProgressWindow`) says done or failed.
  - Renders still running when Zotero starts are picked up from `status.render.jobs` for their item.

### Review Focus (B)
- A book PDF without an outline should offer a page range, not an empty chapter list. Tested.
- A references heading in the first half (an early "References" in a table of contents) should be ignored. Tested.
- Closing Zotero mid-render should let the render continue, and the next start show its progress again. Verified live.
- Export on an item whose PDF has no text should show the "no text" problem, with no render. Tested.

### Tasks (B)
1. The pure modules, with vitest.
2. Segment capture without playing, verified live in the harness: the enqueue count stays at zero and the segments are complete.
3. The dialog and the declared settings.
4. The render round-trip, progress, attaching, and resuming across a restart. Verified live: the sample article becomes an attached mp3 whose duration ffprobe confirms, and a generated PDF with an outline (fpdf2's `start_section`) becomes an m4b with chapters.
5. The README and the gate: `npm test`, `npm run build`, and the Python gate.

Parts A and B can run in parallel on separate worktrees. B's live tasks (2–4) need A merged first.
