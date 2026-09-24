# Audio versions of Zotero items

Date: 2026-09-24. Status: approved in conversation. Depends on
`2026-09-24-settings-and-languages-design.md`, which is built first; the
render uses its voice per language and its settings.

## Intent

From Zotero, right-click a PDF and choose **Create audio version…** to get an
audio file of the paper or book, attached back to the item. It is read through
the same pipeline as live reading: Zotero's Read Aloud segmentation (reading
order, headers, footers and citations skipped), speakd's cleanup and
segmentation, and Kokoro.

Articles and books differ:
- **An article** becomes one file, ending before its References section.
- **A book** becomes one M4B with a chapter marker per outline chapter, and
  asks which chapters or pages first.

Success: a 20-page article becomes an MP3 attached to its item, with title,
authors and year tagged, without disturbing live speech while it renders; a
book with an outline becomes an M4B whose chapters a podcast or audiobook app
shows.

Out of scope: EPUB and snapshot items; LLM cleanup (Stage B of the reader
design, which would improve these files and can be added later); rendering
from anything but Zotero (a `speakctl render` for arbitrary text is a small
follow-up the daemon verb already enables).

## 1. Daemon: render jobs

A new verb, `render`, with payload:
- `parts: [{title, text}]`, one per chapter; an article is a single part
- `out`: an absolute path, whose extension decides the format
- `format`: `mp3`, `opus` or `m4b`
- `lang`, `profile`, and `metadata: {title, artist, album, date}`

It answers `{job}` at once.

**Scheduling.** Render jobs run on their own worker, never the live queue. They
yield to live speech: while any channel is speaking, the render worker pauses
between sentences. So a render never delays a sentence, and live speech never
waits on a render. One render runs at a time, and the rest wait in order.

**Synthesis.** Each part is prepared (transforms per profile) and segmented
exactly as a live utterance, then each sentence is synthesised with the
language's voice (settings-and-languages §2). Sentence audio is appended to a
per-part WAV in a work directory. `speech.sentence_gap_ms` (default 250)
silence goes between sentences and `render.chapter_gap_ms` (default 1500)
between parts.

**Resumable.** The work directory
(`$XDG_STATE_HOME/speakd/renders/<job>/`) holds a manifest recording the parts
done and the next sentence. A daemon restart resumes unfinished renders.
`render_cancel {job}` stops a render and deletes its work directory.

**Encoding.**
- **mp3 and opus:** ffmpeg concatenates the parts into one file, at
  `render.mp3_bitrate` (default `64k` mono, plenty for speech).
- **m4b:** AAC in MP4, with an ffmetadata chapter list built from the parts'
  titles and durations.
- **Tags** come from `metadata` in every format.
- **The finished file** is written to `out` through a temporary name and a
  rename, and the work directory is removed.
- **No ffmpeg:** the render refuses up front with a clear error (checked by
  `status.render.available`).

**Events.** `render {job, state: queued|running|paused|done|failed|cancelled,
part, parts, done_seconds, estimate_seconds, out?, error?}`, published on
state changes and at most once every 2 s while running. `status` lists render
jobs.

**Over HTTP**, `render`, `render_cancel` and `status` are allowed for the
`zotero:` owner. `out` must be under the user's home directory, and the daemon
refuses anything else.

## 2. Zotero: the export flow

**Entry.** `Zotero.MenuManager` adds **Create audio version…** to the item
context menu for a PDF attachment, or for a regular item with exactly one PDF.

**Getting the text.** The PDF is opened in a reader tab in the background, and
Zotero's Read Aloud segmentation is triggered through the takeover without
playing. The plugin captures the complete segment array, with `text`,
`sourcePosition.pageIndex` and `anchor`, then closes the tab if it opened it.
This is the same mechanism that live reading uses, verified live; the export
only captures the segments and never plays them.

**The dialog**, defaults by item type:
- **Article** (anything that is not `book` or `bookSection`): range "whole
  document", with "Stop before References / Bibliography" on. It finds the
  last heading-like segment matching
  `^(References|Bibliography|Works Cited|Literatur(verzeichnis)?)$` (case
  insensitive) in the second half of the document and cuts there. Format mp3.
- **Book or book section:** a chapter list from the PDF outline (pdf.js
  `getOutline` through the reader), each ticked, plus a page-range field (for
  example `12-48`). Without an outline, page range only. "Skip front and back
  matter" is on by default: pages before the first chapter and after the last.
  Format m4b.
- **For every type:** the format (mp3, opus, m4b), the output (attach to the
  item, or save to a folder), and a line estimating the render time from the
  text length and the measured real-time factor.

**Parts.** An article is one part, titled with the item title. For a book,
each selected chapter is one part titled from the outline; with a bare page
range, one part per range.

**Result.**
- By default the file is imported as a child attachment of the item, titled
  "Audio — <title>". The daemon writes to a temporary path, then the plugin
  imports the file and deletes the temporary copy.
- "Save to folder" writes to the chosen folder instead.

**Progress** is shown in the item pane (a section under the attachment),
with state, percentage, estimate and a Cancel button. Zotero notifies when
the render is done or has failed. A render continues if Zotero is closed, and
the plugin picks it up again from `status` on the next start.

**Settings** (`zotero.*`, declared per the settings design):
`export_format_article`, `export_format_book`, `export_destination`
(attach or folder), `export_folder`, and `export_skip_references`.
Render-wide settings are `render.*`: `mp3_bitrate`, `chapter_gap_ms`, and
`max_concurrent` (fixed at 1 for now).

## Testing

- **Daemon:** render with FakeEngine to a real mp3 and m4b (ffmpeg present in
  CI via apt, else skipped); duration within tolerance; chapters read back with
  ffprobe; tags present; yields to live speech (a live enqueue during a render
  finishes first, and the render pauses between sentences); cancel removes the
  work dir; resume after a simulated restart continues at the recorded
  sentence; `out` outside home refused; no ffmpeg refused.
- **Plugin (vitest):** the references cut (headings, no match, a match in the
  first half ignored); outline to chapter ranges; page-range parsing; part
  building; defaults by item type; the estimate.
- **Live, in the test Zotero harness:** export the sample article and get an
  attached mp3 whose length matches; export a generated PDF with an outline as
  an m4b with chapters; cancel mid-render.
