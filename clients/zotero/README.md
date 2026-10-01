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

There is no export settings pane yet; edit these prefs through Zotero's Config
Editor (**Tools → Preferences → Advanced → Config Editor**) or by hand in
`prefs.js` in your Zotero profile directory.

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

## Per-release checklist

Before raising `strict_max_version`, run against the new Zotero:

- a single-column article, a two-column paper, and a footnote-heavy
  philosophy paper
- read selection, read from here, pause/resume, ← and →, speed, stop, closing
  the tab mid-read, and killing speakd mid-read

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
