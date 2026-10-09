# speakd reader for Zotero

Reads a paper aloud from inside Zotero 10's PDF reader, through speakd. While
it speaks, Zotero's own Read Aloud highlight marks the current sentence and
follows it down the page, so you can read along.

speakd does the speaking. The plugin borrows Zotero's Read Aloud machinery
for everything else: the reading order (two columns included), skipping
headers, footers and citations, and drawing the highlight. Zotero's player
never makes a sound. The paper is a speakd channel like any other,
`zotero:<item>`, named after the item's title. It shows up in the speakd window
and `speakctl status`, and hush, mute and priority treat it the same as every
other channel.

## Install

1. Build it, or take a released `.xpi`:
   ```bash
   cd clients/zotero && npm ci && npm run build   # → .scaffold/build/speakd-reader.xpi
   ```
2. In Zotero: **Tools → Plugins**, the gear icon, **Install Plugin From File…**,
   then pick the `.xpi`.
3. Get the token speakd's HTTP transport requires, and paste it into
   **Settings → speakd reader**:
   ```bash
   speakctl http-token
   ```
   The port defaults to 8642 (`SPEAKD_HTTP_PORT` on the daemon's side).
4. **Reopen any PDFs that were already open.** A reader opened before the
   plugin loaded can't be taken over.

## Use

- Select text, then **Read selection** or **Read from here** in the selection
  popup.
- The bar in the reader's toolbar has play/pause, ← and → (one sentence),
  speed −/+, and stop.
  - ← and → and pause act only while this paper is what speakd is speaking.
    With another channel talking, the bar says so and leaves it alone.
  - Speed is speakd's one listener speed, the same one the window's −/+ and
    `speakctl speed` change.
- Zotero's own Read Aloud keys work during a read: Space, ←/→, and Alt+←/→ by
  paragraph.
- Zotero's own Read Aloud, started from its toolbar button with one of its own
  voices, is left completely alone.

The bar shows what speakd reports, never a guess: **reading**, **another
channel is speaking**, **speakd is not running**, or **wrong token**.

## Text cleanup and footnotes

Before a section is spoken (or a part exported), the PDF's captured text is
cleaned deterministically: recurring running heads are dropped, hyphenation
and line breaks are joined, ligatures and stray scan marks are fixed, and
page numbers are removed. Footnotes are detected by geometry — a marker-led
paragraph that starts in the bottom band of its page, with its wrapped
continuations — and treated by the **Footnote policy** setting:

- `classify` (default): bare citations (author–year, "Ibid.", "Cf.", "See",
  mostly digits and punctuation) are dropped; everything a shape rule cannot
  judge is kept and read at the end of the section that owns its page, never
  inline mid-flow.
- `drop_all`: every footnote is dropped.
- `defer_all`: every footnote is kept, read at its section's end.

The policy is `extensions.speakd-reader.footnote_policy`, set in the plugin's
preferences pane (**Edit → Settings → speakd reader**). Under `drop_all` and
`defer_all` no classifier runs at all.

### Optional language model (off by default)

A language model can repair small OCR errors and classify the borderline
footnotes the shape rules cannot. It is **off until an endpoint and a model
are configured** in the preferences pane. Section 1 never calls it, so the
first sound is unaffected, and no live read ever waits on it — footnote
classification is asked for one section ahead in the background, and a miss
keeps the footnote immediately. Every repair is diff-verified against a 2 %
edit budget, so a model that rewrote rather than repaired loses its answer;
any failure leaves the deterministic text standing. **When enabled, this
sends the paper's text to that endpoint.**

Settings in the pane: `llm_enabled` (default false), `llm_endpoint`,
`llm_key` (masked), `llm_model`.

## Label mode and ground-truth fixtures

A whole-document, human-in-the-loop component labeler for tuning the
footnote and running-head rules against real documents. It is a mode of the
reader, not a separate window.

**Enter it** with the **L** button in the speakd bar. The first entry of a
session captures the document once (Zotero's SDT walk is whole-document);
the pane shows a spinner, elapsed seconds, and "Zotero is preparing the
document; this happens once per label-mode session" — elapsed time, never a
faked percentage. `Esc` abandons the wait (the SDT pass finishes in the
background and its result is discarded), and the 180 s capture timeout says
why and exits. **During a read** the segments are already held, so the pane
is instant and entering label mode stops the read (one controller per
reader). A reader the plugin could not adopt shows the refusal reason in the
bar instead of opening the pane.

**The pane** lists the current page's captured segments in order, each with a
colored chip seeded from the pipeline's own prediction (body, footnote,
running head), so labeling is mostly confirm-or-correct. ↑/↓ walk segments
and continue into the next page; a **next unlabeled** jump goes to the first
segment with no label yet; labels that no longer match the document are
listed ("N labels no longer match this document — re-label or discard").
Clicking a segment drives Zotero's own Read Aloud highlight on it and scrolls
to it. Keys are **pane-scoped**, never global: `b` body, `f` footnote, `h`
running head, `p` page number, `k` junk, `g` heading; `Esc` exits. The
reader's own keys are never intercepted.

**What a label does.** A label attaches to a whole captured segment and
applies only where whole-segment decisions are already made, so a highlight
can never land on a labeled-away segment. `body` forces a keep (restoring a
running-head false positive), `footnote` makes a segment a footnote
*candidate* and the **Footnote policy** still decides drop/defer/classify,
`running-head` / `footer` / `page-number` / `junk` drop it, and `heading` is
informational in v1 (spoken as body). Every applied override writes the same
one-line audit log the filters write. Labels apply to the live read and to
the export through the same seam; the override map is resolved once per
capture, so time-to-first-audio is unchanged.

**Where labels live.** One JSON file per attachment at
`<profile>/speakd-reader/labels/<attachmentKey>.json`, written atomically
(temp + rename). Labels key on the segment's page, anchor, a 48-character
text prefix and the full-text sha256 — never its array position — so a
recapture or a Zotero update can re-segment without mis-applying a label. An
unparsable file or an unknown `schema` is ignored with one warning and the
read proceeds unlabeled (fail-open); unmatched locators are inert and listed
in the pane.

**Harness entry points** (the `dumpSegments` precedent; the same actions the
UI calls, no second path): `enterLabelMode()`, `exitLabelMode()`,
`labelSegment(index, category)`, `exportFixture(path, range)`, driven as
`Zotero.SpeakdReader.handles().find(h => h.sourceId.includes("<key>"))…`.

**Export a fixture** with the pane's **Export fixture…** button (written under
the profile, since v1 has no folder dialog) or `exportFixture(path, range)`
from the harness. Four files: `capture.jsonl` (the captured segments),
`labels.json` (**the ground truth**), `expected.json` (a regression snapshot
of what today's pipeline with the labels produces — not ground truth), and
`README.md` (source `sha256` and path, the reproduction command, the
capture-gap report, and the bound record). The gap report diffs each page's
raw pdf.js text against its captured segments and names what never became a
segment — the "SDT dropped a body block" class. It is best-effort inside a
try/catch: a reader where the pdf.js reach fails writes "gap check
unavailable" and still exports. The pane's button and the harness call the
same action.

**Limits.** A label can only re-categorize what the capture produced; text
Zotero dropped before the plugin saw it cannot be labeled or spoken — the
gap report makes it visible, it does not fix it. Labels never reorder, merge
or edit characters, so they cannot mis-highlight. The fixture bound is
concrete: **derived artifacts only, the PDF never checked in; at most 8
pages and ~20 KB of text content per in-copyright work** without the owner's
sign-off (geometry bytes do not count).

## Audio export

**On one PDF attachment**, or a regular item (article, book) with exactly one
PDF child, right-click **Export audiobook…** to render an mp3, opus, or m4b audio
file. Zotero first prepares the PDF's text, which for a long book can take a
while; a "Reading the PDF…" notice stays up until the dialog opens. The dialog
offers:

- **Range:** for a book (item type Book or Book Section) with chapters in its
  outline, choose **chapters** (with checkboxes), **page range**, or **whole
  document**; for a book with no outline, choose **page range** only; for any
  other item type, choose **whole document** or **page range**. Page ranges are
  the PDF's own page numbers (the first page is 1), not the numbers printed on
  the pages. Chapters such as Contents, Index, References or Notes start unticked.
- **Format:** mp3, opus, or m4b. Books use `export_format_book` (default m4b);
  articles use `export_format_article` (default mp3). This is determined by item
  type, not by whether the document has chapters.
- **Destination:** `attach` (default) imports the audio into Zotero as a child
  attachment titled "Audio — <title>", staged in your Zotero profile's
  `speakd-exports/` folder before importing; `folder` leaves the file in a folder
  on your machine (the path is `export_folder`).

**Progress:** while rendering, an item-pane section shows state ("Waiting for speakd",
"Waiting to render", "Rendering", "Paused while speakd speaks", "Done", "Failed",
or "Cancelled"), a percentage bar, the estimated time left, and a **Cancel** button.
Renders keep going if Zotero closes and are picked up again on the next start.

**When there is no text:** a PDF with no text shows a "no text" message in place of
rendering.

**Settings** are in the Zotero Config Editor, under `extensions.speakd-reader.*`:
- `export_format_article` — `mp3`, `opus`, or `m4b` (default: mp3)
- `export_format_book` — `mp3`, `opus`, or `m4b` (default: m4b)
- `export_destination` — `attach` or `folder` (default: attach)
- `export_folder` — path when destination is `folder`
- `export_skip_references` — skip the item's references section when exporting
  the whole document (default: true); chapter and page exports read what you chose

There is no pane for the *export format/destination* settings yet; edit
`export_format_*`, `export_destination`, `export_folder` and
`export_skip_references` through Zotero's Config Editor
(**Tools → Preferences → Advanced → Config Editor**) or by hand in
`prefs.js` in your Zotero profile directory. The footnote policy and the
optional language model are in the plugin's preferences pane.

## Limits

- **Zotero 10.0.x only** (`strict_max_version: "10.0.*"`). The plugin stands on
  Read Aloud internals that aren't a public API. Each is probed when a reader
  opens, and if any is missing the plugin refuses that reader and says so in
  the bar, rather than highlighting the wrong thing.
- **A read is sent in sections of about a page.** Going back stops at the
  current section's start.
- **Sentences are speakd's.** speakd splits text into sentences its own way.
  When one of its sentences spans two of Zotero's, the first is highlighted.
  A highlight whose words aren't in the segment it would light is not drawn.
- **Zotero's media-key tone.** To keep OS media keys working, Zotero's hidden
  player loops a 100 Hz tone at about −64 dBFS. It's inaudible, but it isn't
  digital silence.
- **PDFs only.** EPUB and snapshot reading are not handled yet.
- **Footnote geometry needs rects.** Footnote detection sits a marker-led
  paragraph in the bottom band of its page using the segment's first rect. A
  PDF whose segments carry no rects (or a page with none) falls back to
  marker-only detection: a run of sequentially numbered marker-led
  paragraphs at a page tail. That can miss footnotes whose markers are not
  sequential and can keep a marker-led body line; keeping a wrong footnote
  is preferred to dropping a right paragraph.
- **Cleanup never reorders characters.** Dropped and deferred footnotes are
  whole segments, so highlights stay exact; a deferred footnote still
  highlights its own place in the PDF.

## Per-release checklist

Before raising `strict_max_version`, run against the new Zotero:

- a single-column article, a two-column paper, and a footnote-heavy
  philosophy paper — the two-column paper exercises Zotero's own reading
  order (the plugin does not reorder) and the footnote-heavy one the
  footnote policy and the LLM failure paths
- read selection, read from here, pause/resume, ← and →, speed, stop, closing
  the tab mid-read, and killing speakd mid-read
- with the LLM off, a run is the deterministic cleanup only; with it on and
  the endpoint killed mid-read, the read continues deterministically

The internals the plugin depends on, with their locations in 10.0.3's source,
are in `docs/design/2026-09-24-zotero-10-read-aloud-internals.md`. That doc
ends with what has been verified live.

## Development

```bash
npm test          # vitest, the pure logic
npm run build     # tsc --noEmit, then the .xpi
```

Only `src/reader-takeover.ts` touches Zotero internals. Everything else is
plain TypeScript with tests.

**Live tests** run a headless Zotero with a throwaway profile and its own
library (never `~/Zotero`), next to a fake-engine speakd:

```bash
test-live/setup-profile.sh        # profile + test bridge + its token + the built plugin
test-live/start-fake-daemon.sh &  # this checkout's daemon, HTTP on 8743
test-live/run-zotero.sh           # headless Zotero, local server on 23129
test-live/zeval.sh 'return Zotero.version'
```

Each script takes the test directory (default `~/.cache/speakd-zotero-test`;
`zeval.sh` reads it from `ZOTERO_TEST_DIR`). A second profile can run beside
the first on its own port:

```bash
ZOTERO_TEST_PORT=23139 test-live/setup-profile.sh ~/.cache/speakd-zotero-test-fresh
test-live/run-zotero.sh ~/.cache/speakd-zotero-test-fresh
ZOTERO_TEST_DIR=~/.cache/speakd-zotero-test-fresh test-live/zeval.sh 'return Zotero.version'
```

`run-zotero.sh` stops only the instance running on its own profile, never
another test profile's and never your own Zotero.

`test-live/bridge/` is a **test-only** plugin that evaluates JavaScript posted
to that Zotero's local server. Marionette can't drive Zotero, because its
session waits for a browser window Zotero never opens, so this bridge is how
the reader is driven from outside. What it evaluates runs with chrome
privileges, so it answers only a request that carries the random token
`setup-profile.sh` writes to `<dir>/profile/speakd-test-token` (mode 0600)
in an `X-Speakd-Test-Token` header, with a `Host` of `127.0.0.1:<port>` or
`localhost:<port>`; anything else gets a 403, and without the token file it
evaluates nothing. `zeval.sh` sends the token. Still, never install the
bridge in a real profile.
