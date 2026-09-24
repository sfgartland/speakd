# Zotero 10 Read Aloud internals — reference for `reader-takeover`

Verified against the **Zotero 10.0.3** app source, extracted to
`…/scratchpad/zotero-app-10.0.3/`. Two files carry everything:

- `chrome/content/zotero/xpcom/reader.js` — cited as **`X:<line>`**: the chrome
  side (`ReaderInstance`, `ReaderTab`, `ReaderWindow`, `Zotero.Reader`).
- `resource/reader/reader.js` — cited as **`B:<line>`**: the reader iframe
  bundle. It is a webpack build but **not minified**: class and method names
  survive, and the `;// ./src/...` banners give the original module paths.

Others are cited by path. Zotero-TTS's notes (AGPL; read, not copied) were used
only to know where to look. Where they disagree with 10.0.3 it says so. Line
numbers are for 10.0.3 only and shift with every release. The probe list (§7)
checks names, not lines.

Two findings change the design brief:

1. **Zotero's controller is the right seam, not `getAudio`.** Every user
   intent reaches one object: play, pause, the skip keys, media keys, "Read
   Aloud from here", the jump button, voice changes and close. That object is
   the controller that `voice.getController(segments, backwardStopIndex,
   forwardStopIndex)` returns (`B:82664`). Give our own voice a controller
   that plays nothing and talks to speakd, and this happens by itself:
   Zotero segments, highlights and scrolls whenever that controller fires
   `ActiveSegmentChange`. We need no `setSegments` hook, no `repositionTo`
   hook and no `activeTimestamp` shadowing. The segments arrive as the
   controller's constructor arguments.
2. **Three native gates would otherwise make Zotero speak or show nothing:**
   remote voices load only when the user is logged in (`B:84271`); a
   persisted browser voice wins voice resolution (`B:82438-82450`,
   `B:82462-82499`); and the word highlight pref shows no highlight without
   word timestamps (`B:76507-76510`). §2 and §4 cover each one.

---

## 1. Lifecycle

### 1.1 Creation and `_readers.push`

- `Zotero.Reader.open(itemID, location, opts)` is at `X:2907`. It
  constructs **`new ReaderWindow({...})`** (`X:2953`), then
  `this._readers.push(reader)` (`X:2965`). It constructs
  **`new ReaderTab({...})`** (`X:2969`), then `this._readers.push(reader)`
  (`X:2995`). `_readers` is a plain array created in `Reader`'s constructor
  (`X:2670`).
- `ReaderInstance`'s constructor (`X:49-99`) **returns a `Proxy`**
  (`X:76-98`). Its `get` falls through to `_internalReader[prop]` when the
  instance has no such property; its `set` writes to `_internalReader` only
  when `_internalReader` already exists and the instance has no own property
  of that name. So what gets pushed is the Proxy, and at push time
  `_internalReader` is still undefined. An instance-property write therefore
  lands on the `ReaderInstance` itself. There is no `deleteProperty` trap, so
  `delete reader.x` also works on the target. This matches Zotero-TTS; their
  line numbers 2961/2991 are for another build.
- The subclass constructors call `this._open(...)` **synchronously but without
  awaiting it**: `ReaderTab` at `X:2035`, `ReaderWindow` at `X:2243`. There is
  also `ReaderPreview` (`X:2392`), created by `openPreview` (`X:3016`); it is
  never pushed to `_readers`, and its `_window` is unset, so its interface is
  `null` (`X:1749`).
- `ReaderInstance` is not exported. Reach its prototype from a live reader:
  `Object.getPrototypeOf(reader.constructor.prototype)`. Through the Proxy,
  `constructor` resolves on the target (`ReaderTab` or `ReaderWindow`).

### 1.2 When `_getReadAloudRemoteInterface` is read

- It is read **once, synchronously, inside `async _open()`**, as the
  `readAloudRemoteInterface:` property of the options literal
  (`X:267`). That literal goes to
  `this._iframeWindow.wrappedJSObject.createReader(Cu.cloneInto({...},
  this._iframeWindow, { cloneFunctions: true }))` (`X:234`, `X:648`).
- Before that line `_open` has already awaited `SyncedSettings.loadAll`,
  `_getData()`, annotations, `_getState()`, `_waitForReader()` (polling for
  the iframe window, `X:1572-1585`) and, in the same literal,
  `await this._getReadAloudEnabledVoices()` (`X:266`). So a synchronous
  `_readers.push` interceptor always runs first.
- It is also called from the first-run and voices dialogs
  (`X:1904`, `X:1934`) with the dialog's window as `targetWindow`.
- `onSetReadAloudStatus: this._setReadAloudStatus.bind(this)` is bound in the
  same literal (`X:632`). Wrapping `_setReadAloudStatus` therefore works only
  if it is done before `_open` reaches that line, the same as the interface.
- `this._resolveInitPromise()` runs right after `createReader` returns
  (`X:650`). **`await reader._initPromise` is the point where
  `_internalReader` (and so `_readAloudManager`) exist.**

**Safest replacement point.** Wrap `Zotero.Reader._readers.push` (restore it
on shutdown). For each pushed reader whose `_window` is set, assign an
**instance** property `reader._getReadAloudRemoteInterface = ourFactory`.
Then `await reader._initPromise` and install the manager-level hooks (§4).
Readers that are already open when the plugin loads have captured the
original interface, and it cannot be swapped afterwards: it was cloned into
the iframe at `X:648` and stored at `B:82921`. Treat those readers as
unsupported until they are reopened, or ask the user to reopen them. The
alternative is patching `ReaderInstance.prototype`, which is global and
affects every reader including the dialogs. Prefer the per-instance
assignment.

A throw inside the factory rejects `_open()`, and `ReaderTab` chains only
`.then` on it (`X:2035`). The tab dies. The factory must never throw. On any
doubt, fall back to calling the original, which is `null` when `_window` is
unset (`X:1749`).

### 1.3 `targetWindow` and compartment rules

- `targetWindow` is `this._iframeWindow`: the reader iframe's content window
  (`resource://zotero/reader/reader.html`, `X:1997`). For the dialogs it is
  their window.
- The native code does two things (comment at `X:1750`, "avoid permissions
  errors"). Every method returns `new targetWindow.Promise(...)`, and every
  resolved value is `Cu.cloneInto(value, targetWindow)` (`X:1753-1832`). The
  interface object's functions reach the iframe because the whole options
  object is cloned with `cloneFunctions: true` (`X:648`).
- The plugin sandbox is a separate system-principal compartment
  (`chrome/content/zotero/xpcom/plugins.js:137-157`). Rules for anything we
  hand to the reader iframe:
  - Promises must be the iframe's (`new win.Promise`). Values must be
    `Cu.cloneInto(v, win)`, and Blobs must be built with `new win.Blob(...)`
    or cloned. Zotero-TTS reports trouble handing sandbox Blobs and the Cache
    API across compartments (NOTES_2026-08-21). UNVERIFIED locally.
  - Functions we install **on content objects** (manager methods, controller
    methods, accessors) must be `Cu.exportFunction(fn, win)` or set through
    `Cu.cloneInto(obj, win, {cloneFunctions:true})`. Otherwise content gets
    a wrapper that throws "Permission denied" when called. UNVERIFIED by
    running, but it follows from how Zotero itself passes callbacks.
  - To read or write content objects' ordinary JS properties (for example
    `_readAloudManager._voice`, or the expando `segment` on an event), go
    through `Cu.waiveXrays(obj)`. Waiving is idempotent. Zotero-TTS says
    `_internalReader` and `_readAloudManager` already arrive unwrapped while
    `_sdt.mapper` is an Xray. Do not rely on that: waive explicitly.
  - Guard every late callback with `Cu.isDeadWrapper(win)`, as Zotero does at
    `X:1175`, `X:1184`.

---

## 2. The remote interface contract

### 2.1 Methods (`X:1748-1835`), called from the bundle

| Method | Called from | Args | Must resolve to |
|---|---|---|---|
| `getVoices()` | `RemoteReadAloudProvider.getVoices` `B:40503-40522` (from `manager.loadVoices`) | none | `{ voices, standardCreditsRemaining, premiumCreditsRemaining, devMode }` or `{ error }` |
| `getAudio(segment, voiceImpl)` | `RemoteReadAloudController._fetchAudio` `B:40341-40349`; the sample controller `B:40419` with `segment === 'sample'` | segment object (§3) or the string `'sample'`; `voiceImpl` = the parsed voice config (below) | `{ audio: Blob, timestamps? }` or `{ audio: null, error }` |
| `getCreditsRemaining()` | `RemoteReadAloudController.refreshCreditsRemaining` `B:40220-40231`, every 60 s while a controller exists (`B:82701-82706`) | none | `{ standardCreditsRemaining, premiumCreditsRemaining }` (numbers or `null`) |
| `resetCredits()` | `RemoteReadAloudController.resetCredits` `B:40232-40243` (dev-mode UI) | none | same as above |

There are no other members. The first-run popup receives the same object as
`remoteInterface` (`B:42606`).

### 2.2 Voice catalogue (`parseVoicesResponse`, `B:40528-40553`)

`voices` is keyed by tier. Only `standard`, `premium` and `local` are
accepted (`TIERS`, `B:39265`):

```
{ standard: [ {
    voices:  { "<id>": { label: "<text>" } },
    locales: { "<locale or *>": { default: ["<id>"], other: [] } | ["<id>"] },
    segmentGranularity: "sentence" | "paragraph",
    sentenceDelay?: <ms>, creditsPerMinute?: <n>, cacheVersion?: <any>
} ] }
```

Each (voice, locale) pair becomes one voice config,
`{id, label, tier, locale, creditsPerMinute, segmentGranularity,
sentenceDelay, cacheVersion}` (`B:40538-40547`). That config is wrapped in
`RemoteReadAloudVoice` (`B:40445-40481`), and it is what `getAudio` receives
as its second argument.

- `locale: "*"` is a wildcard that matches any document language
  (`isLanguageSupported`, `B:38012-38017`). It is left out of the language
  list (`B:39271-39278`) and has no region (`B:39288-39291`). **Use `"*"` for speakd.**
- `segmentGranularity` must be `"sentence"`. Otherwise the PDF highlight is
  paragraph-level (`_effectiveReadAloudPrimaryGranularity`, `B:76536-76544`),
  and paragraph granularity changes segmentation too (`segmentChain`,
  `B:71600-71606`).
- Leave out `creditsPerMinute`. Then `ReadAloudVoice.minutesRemaining`
  returns `null` (`B:39256-39263`), which the popup treats as unlimited.
- Return `standardCreditsRemaining: null` and `premiumCreditsRemaining: null`
  **explicitly**. The provider copies any value that is `!== null`
  (`B:40513-40518`), so `undefined` would be copied too and later produce
  `NaN`.
- `sentenceDelay` (ms, default 0, `B:40473-40475`) plus 200 ms at paragraph
  starts (`DELAY_PARAGRAPH`, `B:39297`, `B:39503-39507`) is the gap between
  segments in Zotero's own controller. It does not matter if we supply our
  own controller.
- Voice `id` is used unprefixed (`B:40447`). Browser voices are
  `local-<voiceURI>` with tier `local` (`B:39628`, `B:39676`). Choose a
  distinctive id, for example `speakd`.

Errors (shape `{ error: string }`):
- From `getVoices`, `'network'` or `'unknown'` (`X` syncAPIClient:
  `chrome/content/zotero/xpcom/sync/syncAPIClient.js:655-665`). Any error, or
  a missing `voices`, throws `RemoteVoicesError`
  (`B:40511-40512`). `loadVoices` catches it and falls back to `[]`
  (`B:82313-82318`). **The remote voices silently vanish.**
- From `getAudio`, `'quota-exceeded'`, `'daily-limit-exceeded'`, `'network'`
  or `'unknown'` (syncAPIClient `:720-736`). The popup shows `ErrorMessage`
  for every error except `'quota-exceeded'` (`B:38662`). `'quota-exceeded'`
  and `'daily-limit-exceeded'` expand the options panel (`B:38548`).
- Timestamps, when present, are `[{ start, end, charStart, charEnd }]`. Start
  and end are seconds into the audio (`B:40061-40067`). charStart and charEnd
  are offsets into `segment.text`, which is the normalised text
  (`B:83945`, `B:71180-71190`).

### 2.3 What `getAudio` should do, and why it matters less than it seems

Zotero's own `RemoteReadAloudController` is where segments advance:

1. `_speakInternal()` (`B:40162-40218`) sets `buffering = true`, awaits
   `_getAudioData(index)`, which calls `getAudio` and `decodeAudioData`
   (`B:40318-40339`), and then calls `_handleSegmentStart`. That dispatches
   `ActiveSegmentChange` (`B:39486-39489`), which is **what highlights**. It
   then plays the buffer through an `AudioContext` created in the
   constructor (`B:39931-39960`). Finally it calls
   `_prefetchFrom(index + 1)`, which runs up to 3 segments ahead with 2
   concurrent fetches (`B:40245-40316`).
2. The source node's `onended` calls `_handleSegmentEnd`
   (`B:40022-40030`, `B:39490-39512`). That dispatches
   `ActiveSegmentChange(null)` and advances `_position`. At the last segment
   it dispatches `Complete`. Otherwise it calls
   `_scheduleSpeak(sentenceDelay [+200])`.
3. Skips (`_skipTo`, `B:39468-39477`) dispatch `ActiveSegmentChanging` at
   once. They then re-speak through a 600 ms debounce
   (`SKIP_DEBOUNCE_DELAY`, `B:39904`, `B:40219`).

So the native controller could be made to follow speakd. Its `getAudio` for
segment *i* would resolve only when speakd starts *i*, with a tiny silent
WAV. The highlight would then move when speakd moves. This is **not
recommended.** Prefetch requests *i+1* to *i+3* early and caches the answers
(`B:40148-40150`), so a one-sentence skip never reaches `getAudio` and speakd
could not be told about it. Pause and resume would replay the cached
50 ms buffer. An `AudioContext` would still exist, although silent. What
`getAudio` should return under the recommended design (§8):

- `getAudio('sample', impl)` is used when a voice is picked in the popup
  (`handleUserVoiceSelect`, `B:38585-38593` → `getSampleController` `B:40479` → `RemoteSampleReadAloudController`). Resolve it at once
  with a short **silent** WAV built as `new win.Blob([...], {type:
  'audio/wav'})`. Do not use `{audio:null}`: it dispatches `Error`
  (`B:40431-40434`).
- `getAudio(segment, impl)` for our voice should never be called, because
  our controller replaces the remote one. If it is called, the takeover is
  broken. Resolve `{ audio: null, error: 'unknown' }` and log loudly.
- Calls for any other voice go to the original interface, if we merge
  catalogues (§8.4).

---

## 3. Segments

### 3.1 Where they are built

`Reader._requestReadAloudSegments()` (`B:84048-84075`) runs when the manager
calls `onRequestSegments` from `activate()` (`B:82550-82557`) or
`_applyVoice()` (`B:82525-82533`). It then does four things:

1. `await this._loadSDT()` (`B:84023-84046`) opens the Structured Document
   Text pack that `options.getSDTPack` supplies (`X:269`, `X:1164-1190`).
   `Zotero.SDT.getPack` (`chrome/content/zotero/xpcom/sdt.js:81`) generates
   it **locally** through `Zotero.PDFWorker.getStructuredDocumentText`
   (`sdt.js:278`) and caches it. No SDT means no segments, and Read Aloud
   stays in the spinner or progress state (`B:30110-30116`).
2. `sdt_segments_buildSDTReadAloudSegments(sdt.structure, granularity, lang)`
   (`B:71252-71263`). Reading order comes from the SDT leaf-block walk
   (`collectChainTexts`, `B:71329-71358`). Blocks whose top-level
   `flowClass === 'excluded'` are skipped (`B:71332`); this is where headers,
   footers and similar are dropped. Part chains join paragraphs split across
   pages and columns. Bracket and parenthesis groups that are mostly linked
   text, and superscript link markers, are elided (`getElidedRanges`,
   `B:71406-`). Sentence splitting is locale-aware (`segmentChain`,
   `B:71594-71613`). Pieces over 5000 UTF-8 bytes are split further
   (`B:30741`, `B:71026-`). The first segment of each chain gets
   `anchor = 'paragraphStart'` (`B:71258-71260`).
3. `_materializeSourcePositions(segments)` (`B:84096-84116`).
4. It picks a start index (§3.3) and calls
   `manager.setSegments(segments, backwardStopIndex, null)` (`B:84074`).

### 3.2 Segment shape

Created in `SDTReadAloudSegments.addSegment` (`B:71229-71250`) and completed
by `_materializeSourcePositions`:

```
{
  text:        string,         // normalizeText(): \s+ → ' ', trimmed (B:71719-71721); what TTS receives
  position:    { start: Ref, end: Ref },  // SDT content points (arrays of indices) (B:71614-71624)
  granularity: 'sentence' | 'paragraph',
  anchor:      'paragraphStart' | null,
  sourcePosition:          PDFPosition | null,   // this segment
  paragraphSourcePosition: PDFPosition | null    // whole chain, shared by its segments
}
PDFPosition = { pageIndex: number, rects: [[x1,y1,x2,y2], …], nextPageRects?: [...] }
```

The PDF shape comes from `PDFPositionMapper.textNodeSpansToSourcePosition`
(`B:61878-61920`). It holds rects for the first page only, plus
`nextPageRects` when a segment runs onto the next page; the rects are merged
per line. It is the same shape as annotation positions. EPUB and snapshot
use other mappers (`B:62488`, `B:63057`); this document covers PDF only. The
raw-text mapping kept in `SDTReadAloudSegments._sources` (a `Map` keyed by
segment identity) is private and not needed.

**Capture.** The manager's `setSegments` (`B:82543-82549`) stores the array
and **immediately** calls `_createController()`. That calls
`this._voice.getController(this._segments, backwardStopIndex,
this._forwardStopIndex)` (`B:82664`). So our controller factory receives the
live segment array together with the start index, with no separate hook.
The array identity changes on every recomputation (voice or granularity
change). `repositionTo` keeps the same array and builds a new controller
(`B:82621-82628`). Read the array through a waiver and copy what speakd needs
(`text`, index, `anchor`) into sandbox-owned data. Keep the content array
only to hand segment objects back in events.

### 3.3 Start segment: selection, "Read Aloud from here", jump button

- `_captureReadAloudStart()` (`B:84076-84093`) checks, in priority order: the
  view's current text selection (`getSelectionPosition()`, PDF
  `B:76018-76028`; it also clears the selection), then
  `manager.consumeTargetPosition()`, then the saved `lastReadAloudPosition`
  if it is within 5 pages of the view (`isPositionNearView`,
  `B:76545-76550`). Otherwise it uses `_findFirstVisibleSegmentIndex`
  (`B:84130-84145`).
- A position becomes an index in `_findReadAloudStartIndex` (`B:84117-84128`).
  `mapper.sourceToSDTPosition` (`B:61921-`) turns it into an SDT position,
  and `findSegmentIndexForSDTPosition` (`B:71299-71309`) returns the first
  segment whose end is at or after the position's start.
- The context-menu item "Read Aloud from Here"
  (`reader-read-aloud-from-here`, `B:80875-80878`) calls
  `reader.startReadAloudAtPosition(params.position)`. So does
  Ctrl/Cmd+Shift+R or L (`B:81976-81983`, without a position).
  `startReadAloudAtPosition` (`B:84239-84269`) behaves in one of two ways:
  - If already active with segments, it calls `manager.jumpTo(position)`,
    then `repositionTo(index)` (`B:82607-82628`), which builds **a new
    controller** starting at that index.
  - Otherwise it calls `setTargetPosition(position)`, sets
    `popupOpen: true` and runs `_prepareReadAloud()`. Voices load, the
    manager auto-activates (`B:83875-83878`), segments are requested, and
    `setSegments(…, startIndex)` builds a new controller.
- The PDF view's paragraph jump button (`B:76913-76925`) sends
  `onSetReadAloudState({targetPosition})`, which leads to `manager.jumpTo`
  (`B:84760-84762`).
- **Consequence:** our controller's constructor argument `backwardStopIndex`
  *is* the "start here" index, for every entry point.
- To start from our own UI, call
  `reader._internalReader.startReadAloudAtPosition(pos)`, where `pos` is a
  `PDFPosition`, for example the selection annotation's `position` (§6).
  Pass `null` for the selection, saved position or visible page.

---

## 4. Driving the highlight

### 4.1 What the view reads

`Reader._pushReadAloudToViews()` (`B:83917-83922`) sends
`_composeReadAloudStateSnapshot()` (`B:83923-83938`) to every view through
`view.setReadAloudState(snapshot)`:

```
{ popupOpen, active, paused, segmentGranularity, highlightGranularity,
  segments, activeSegment, activeWordSourcePosition, lastSkipGranularity,
  annotationPopup, lang }
```

It runs from `_updateState` whenever `readAloudState` changes identity
(`B:83388-83390`). The manager triggers that on every `_stateChanged()`
(`B:82741-82749`, microtask-batched) through `_onReadAloudEngineStateChanged`
(`B:83870-83913`), which ends with a spread of `readAloudState`.

`PDFView.setReadAloudState` (`B:76421-76490`):

- **`!state.popupOpen` clears everything** (both highlight positions and the
  jump button) and returns (`B:76434-76441`). This is the only clear path.
- If `state.activeSegment?.sourcePosition?.pageIndex` is defined, it sets
  `_readAloudHighlightedPosition` from the effective granularity
  (`B:76442-76446`, `_resolveReadAloudPrimaryPosition` `B:76502-76518`),
  re-renders, and, if the position is locked and no annotation popup is open,
  calls `navigateToPosition(activePosition, {ifNeeded, block:'center',
  behavior:'smooth', visibilityMargin: -innerHeight/4})` (`B:76468-76488`).
- **If `activeSegment` is null, nothing changes**: the old highlight stays.
  So `Complete` (which nulls `activeSegment`) and the null
  `ActiveSegmentChange` between segments (`B:39495-39496`) leave the last
  sentence highlighted.
- The highlight is drawn from `layer._readAloudHighlightedPosition` with
  `READ_ALOUD_ACTIVE_SEGMENT_COLOR` (`B:44092`, `B:30695`). After a skip whose
  granularity differs from the primary one, a 2 s flash is drawn from
  `_readAloudSentenceHighlightedPosition` (`B:76449-76465`).

Effective granularity (`B:76536-76544`):
`highlight='word' && seg='sentence'` gives `word`;
`highlight='sentence' && seg='sentence'` gives `sentence`; anything else gives
`paragraph`. **In `word` mode the highlight is
`state.activeWordSourcePosition` or nothing** (`B:76507-76510`).
`activeWordSourcePosition` comes from `manager.activeTimestamp`
(`B:83939-83947`). That getter returns `null` unless
`this._controller instanceof RemoteReadAloudController` (`B:82229-82235`),
which our controller is not. So a user with the pref on `word` would get
**no highlight**.

- The pref is `extensions.zotero.reader.readAloud.highlightGranularity`, with
  default `"sentence"` (`defaults/preferences/zotero.js:1797`). It is read at
  `X:268` and observed at `X:671` / `X:1241-1245`. The internal setter is
  `setReadAloudHighlightGranularity(g)` (`B:83948-83961`, values
  `paragraph|sentence|word`).
- Recommended: in takeover readers call
  `_internalReader.setReadAloudHighlightGranularity('sentence')` after init,
  and again from our own observer on that pref. This does not write the
  user's pref. Paragraph is also acceptable if the user chose it.

### 4.2 Follow-scroll

- `_readAloudPositionLocked` is set on activation (`B:76427-76429`), on
  unpause when the active position is in view (`B:76430-76433`), and by
  `lockPositionToReadAloud()` (`B:76494-76496`, through
  `Reader._lockPositionToReadAloud` `B:84184-84186`). Zotero calls that last
  one on skips, on play and from the keyboard (`B:82094-82109`, `B:84231`,
  `B:84246`).
- It is cleared by manual scrolling while active (`B:75418-75420` →
  `_onManualNavigation` `B:76776-76780`). Scrolls that Read Aloud started
  itself are excluded through `_readAloudScrolling`.
- We get the same "follow, until the user scrolls away" behaviour for free.
  Call `_internalReader._lockPositionToReadAloud()` when our own UI skips or
  resumes.

### 4.3 The minimal way to say "segment N is being spoken"

The manager listens to its controller (`B:82679-82714`):

| Controller event | Manager effect |
|---|---|
| `ActiveSegmentChanging` / `ActiveSegmentChange` with `event.segment` | `_activeSegment = event.segment`, `_activeTimestampIndex = null`, `_lastSkipGranularity = controller.lastSkipGranularity`, `_stateChanged()` |
| `BufferingChange` | `_buffering = controller.buffering` (popup spinner) |
| `Complete` | `_paused = true; _activeSegment = null` |
| `Error` / `ErrorCleared` | `_paused = true; _error = controller.error` / `_error = null` |
| `ActiveWordChange` | word index (unused by us) |

So "segment N now" means the controller dispatches `ActiveSegmentChange`
with `segment = segments[N]`: the *same* object from the array it was given.
Views compare `activeSegment` by identity (`B:76449`) and read its
`sourcePosition`. The event must be a content `Event` carrying an expando
`segment` (`ReadAloudEvent`, `B:39513-39519`). `ReadAloudEvent` is not
reachable from outside, so build it as `new win.Event(type)` and set
`segment` through a waiver. UNVERIFIED by running.

"Stopped, clear it" means `_internalReader.toggleReadAloudPopup(false)`
(`B:84193-84219`). That calls `manager.deactivate()` (destroys the
controller, `B:82728-82740`) and sets `popupOpen: false`, and the view
clears (`B:76434`). There is no other public way to clear the PDF highlight.
"Paused, keep highlight": do nothing.

Side effects that come for free:
- `lastReadAloudPosition` persistence (`B:83890-83892` → saved view state,
  `X:1037-1041`).
- `onSetReadAloudStatus({active, paused})` → `ReaderTab._setReadAloudStatus`
  (`X:2164-2190`). It keeps the tab's docShell active while playing (a
  background tab would otherwise throttle timers, `X:2155-2162`), updates
  the tab's audio indicator, and **pauses every other reader**
  (`toggleReadAloudPaused(true)`, `X:2173-2186`).

---

## 5. The native popup and player

- The popup is mounted when
  `props.enableReadAloud && state.readAloudState.popupOpen &&
  state.readAloudState.lang && !state.readAloudFirstRunPopup`
  (`B:42595-42604`). Otherwise the inline first-run popup is shown
  (`B:42604-42612`). In Zotero, `_onOpenReadAloudFirstRunPopup` is set, so it
  instead opens the external dialog (`X:640-643`) and **closes the popup**
  (`B:83394-83399`).
- The first-run state is set when the popup opens:
  `readAloudFirstRunPopup: !this._state.readAloudVoices.size`
  (`B:84201-84203`). `readAloudVoices` comes from the pref
  `extensions.zotero.reader.readAloudVoices` (JSON `{lang: {region, voice,
  speed, tier, tierVoices}}`, `X:1721-1742`), passed at `X:265`. It has no
  default (not in `defaults/preferences/zotero.js`). **A user who has never
  used Read Aloud gets the first-run dialog instead of our voice.** Avoid it
  by making sure the map is non-empty. Selecting our voice once through
  `manager.selectVoice(id)` persists it for that language through
  `onSetVoice` → `_setReadAloudVoice` (`B:82286-82300`, `B:84284-84331`,
  `X:1730-1742`). Or seed the pref, and restore it on uninstall.
- Root element: the `UtilityPopup` with class **`read-aloud-popup`**
  (`B:38606`, CSS `resource/reader/reader.css:808`, `.utility-popup` `:755`)
  in the iframe document. The toolbar toggle is `button#read-aloud`
  (`B:30103-30109`); its `active` class follows `popupOpen`.
- **Hide, do not unmount.** Inject into `reader._iframeWindow.document` a
  `<style>` with `.read-aloud-popup { display: none !important; }`. Unmounting
  (closing the popup) sets `popupOpen:false`, which deactivates the manager,
  destroys the controller and clears the highlight (`B:84208-84218`,
  `B:76434`).
- **Media keys live in the popup.** `ReadAloudPopup` calls `useMediaControls`
  with `useSilentAudio: true` (`B:38555-38578`). While active it:
  - appends a hidden, looping `<audio>` to the iframe `document.body` playing
    `createQuietToneWAV()` (`B:38306-38318`). That is a **100 Hz sine at
    amplitude 20/32767 (about −64 dBFS), 60 s, 8 kHz** (`createQuietToneWAV`, `B:38233-38268`),
    there "so the OS reports Now Playing". Its play and pause follow
    `paused` (`B:38386-38392`).
  - maps the element's `pause` and `playing` events to
    `manager.pause()`/`play()`, and MediaSession `previoustrack`/`nexttrack`
    to `manager.skipBack()`/`skipAhead()` (paragraph) (`B:38320-38348`,
    `B:38559-38577`).
  - sets `navigator.mediaSession.metadata` (the document title) and
    `playbackState` (`B:38370-38410`).

  So OS media keys arrive at the manager, and from there at our controller.
  **This tone is Zotero's only sound under the takeover.** It is inaudible by
  design but not digital silence. Muting it would probably stop Gecko from
  treating the tab as playing media and lose the media keys (UNVERIFIED; the
  Linux MPRIS behaviour is untested). Recommendation: leave it.
- Stopping Zotero from playing: with our own controller, nothing native is
  ever created. No `AudioContext` exists (it is created only in
  `RemoteReadAloudControllerBase`'s constructor, `B:39931-39960`), no
  `speechSynthesis` is used (only `BrowserReadAloudController`), and
  segments never advance on their own. The only other audio paths are:
  - A **browser voice** being selected (`local-*`, `speechSynthesis`). Guard
    in the `_createController` wrapper (§8.2).
  - The sample on voice pick (§2.3). Return silence.
- Keyboard shortcuts while active and focus is not on a button or select
  (`B:82085-82112`): Space toggles pause; ←/→ skip one sentence
  (Shift ×5); Alt+←/→ skip one paragraph. All of them reach
  `manager.*` and then our controller.

---

## 6. Official, stable surfaces

- `Zotero.Reader.registerEventListener(type, handler, pluginID)`
  (`X:2761-2768`). It is removed on plugin shutdown (`X:2684-2688`). Types
  (`X:2699-2703`): `renderTextSelectionPopup`, `renderSidebarAnnotationHeader`,
  `renderToolbar`, `createColorContextMenu`, `createViewContextMenu`,
  `createAnnotationContextMenu`, `createThumbnailContextMenu`,
  `createSelectorContextMenu`.
- The event is `{ reader, doc, params, append, type }`. `reader` is added
  chrome-side (`X:190-199`). `append(...nodes)` must be called
  **synchronously** within the handler, or it throws
  (`B:29385-29389`). Nodes are created with `doc.createElement` and cloned
  across by Zotero (`X:195`).
- `renderTextSelectionPopup`: `params = { annotation }` (`B:31002-31005`).
  `annotation = { type:'highlight', color, sortIndex, pageLabel, position,
  text }`. `position` is the `PDFPosition` of the first selected page (with
  `nextPageRects` for two-page selections), and `text` is the selected text
  (`B:76693-76706`, `_getAnnotationFromSelectionRanges`). **`position` can go
  straight into `startReadAloudAtPosition`** (§3.3). Selection maps to a
  start segment by its start point.
- `renderToolbar`: `params = {}` (`B:30250-30252`). The section sits in the
  toolbar's end group, before `#appearance`. Use it for our control bar.
- `create*ContextMenu`: `append({label, onCommand, …})`. There is no official
  hook on the view context menu's "Read Aloud from Here" item; per the design
  it is left alone.
- **Sandbox globals** (`plugins.js:137-183`): `atob btoa Blob crypto CSS
  ChromeUtils DOMParser fetch File FileReader TextDecoder TextEncoder URL
  URLSearchParams XMLHttpRequest`, plus `Zotero ChromeWorker IOUtils
  Localization PathUtils Services Worker XMLSerializer setTimeout
  clearTimeout setInterval clearInterval requestIdleCallback
  cancelIdleCallback`. **Not present: `AbortController`, `WebSocket`,
  `EventSource`, `Event`, `EventTarget`, `AudioContext`, `caches`.** Take the
  ones needed (for example `AbortController`, used to cancel the SSE `fetch`)
  from `Zotero.getMainWindow()`. `Components`/`Cu` should be present in a
  system-principal sandbox (UNVERIFIED in source; universally used by
  plugins).
- `Zotero.PreferencePanes.register({ pluginID, src, id?, parent?, label?,
  image?, scripts?, stylesheets?, helpURL? })`
  (`chrome/content/zotero/xpcom/preferencePanes.js:97-167`). It resolves to
  the pane id and is unregistered automatically on shutdown.
- `Zotero.Prefs.registerObserver(name, fn)` is used by Zotero itself at
  `X:670-671`.

---

## 7. Probe list

Check all of these at reader open. On any miss, restore the originals, leave
the reader native, and report the missing name. **Chrome side** (`X`):

| # | Member | Defined |
|---|---|---|
| 1 | `Zotero.Reader._readers` (Array; its `push` is wrapped) | `X:2670`, pushes `X:2965`, `X:2995` |
| 2 | `ReaderInstance.prototype._getReadAloudRemoteInterface` (function, arity 1) | `X:1748` |
| 3 | `reader._window`, `reader._iframeWindow` | `X:55-56`; set `X:1960`, `X:2216-2230` |
| 4 | `reader._initPromise` (resolves after `createReader`) | `X:60-63`, `X:650` |
| 5 | `reader._internalReader` | `X:234` |
| 6 | `reader._type === 'pdf'` (the takeover handles PDF only) | `X:71` |
| 7 | `ReaderTab.prototype._setReadAloudStatus` (read only; we do not wrap it) | `X:2164` |

**Iframe side** (`B`, through a waiver on `_internalReader`):

| # | Member | Defined |
|---|---|---|
| 8 | `_internalReader._enableReadAloud === true` | `B:82920` |
| 9 | `_internalReader._readAloudManager` (instance of the class with the methods below) | `B:82922` |
| 10 | `_internalReader.startReadAloudAtPosition(pos)` | `B:84239` |
| 11 | `_internalReader.toggleReadAloudPopup(open)` | `B:84193` |
| 12 | `_internalReader.toggleReadAloudPaused(paused)` | `B:84220` |
| 13 | `_internalReader.setReadAloudHighlightGranularity(g)` | `B:83948` |
| 14 | `_internalReader._lockPositionToReadAloud()` | `B:84184` |
| 15 | `_internalReader._state.readAloudState` with `popupOpen`, `highlightGranularity` | `B:82985-82991` |
| 16 | `_internalReader._state.loggedIn` | `B:82950` |
| 17 | `manager._createController` (wrapped) | `B:82653` |
| 18 | `manager.loadVoices(loadRemote)` (wrapped: force `true`) | `B:82310` |
| 19 | `manager._voice` (own field; `.impl.id` identifies ours) | `B:82171`, set `B:82514` |
| 20 | `manager.selectVoice(id)`, `manager.allVoices` | `B:82347`, `B:82276` |
| 21 | `manager.segments`, `activeSegment`, `active`, `paused`, `speed` getters | `B:82211-82222`, `B:82245` |
| 22 | `manager.pause()`, `play()`, `deactivate()` | `B:82569`, `B:82558`, `B:82728` |
| 23 | Controller contract the manager uses (our controller must supply all of it): `addEventListener`, events `BufferingChange ActiveSegmentChanging ActiveSegmentChange ActiveWordChange Complete Error ErrorCleared`, fields `buffering speed paused error lastSkipGranularity minutesRemaining hasStandardMinutesRemaining activeTimestampIndex`, methods `skipBack(g,acc) skipAhead(g,acc) retry() syncActiveWordToPlayback() getSegmentToAnnotate() refreshCreditsRemaining() resetCredits() destroy()` | used at `B:82653-82727`, `B:82569-82604`, `B:82241-82259`, `B:82596-82598`; base class `B:39298-39512` |
| 24 | Event field `event.segment` read by the manager | `B:82680`, `B:82686` |
| 25 | PDF view `setReadAloudState(state)`: reads `popupOpen`, `activeSegment.sourcePosition.pageIndex` | `B:76421` |
| 26 | DOM `.read-aloud-popup` class; `#read-aloud` button | `B:38606`, `B:30103` |
| 27 | Prefs `extensions.zotero.reader.readAloud.highlightGranularity`, `extensions.zotero.reader.readAloudVoices` | `zotero.js:1797`, `X:1723` |

Behavioural probes that name checks cannot cover, for the per-release manual
checklist:
- `_createController` still calls `this._voice.getController(segments,
  backwardStopIndex, forwardStopIndex)` (`B:82664`).
- `setSegments` still calls `_createController` synchronously
  (`B:82547`).
- A null `activeSegment` still leaves the PDF highlight in place, and
  `popupOpen:false` still clears it (`B:76434-76442`).

A cheap automatic check: `String(manager._createController)` contains
`getController(` and `'ActiveSegmentChange'`. Functions from content
compartments stringify normally (UNVERIFIED through Xray; waive first).

---

## 8. Recommended minimal hook design

Pseudocode, our own. Four hooks, in this order.

### 8.1 Reader creation (chrome side)

```
onStartup:
  origPush = Zotero.Reader._readers.push
  Zotero.Reader._readers.push = function (...readers) {
    for r of readers: try { adopt(r) } catch (e) { log(e) }   // never throw
    return origPush.apply(this, readers)
  }
onShutdown: restore push; for each adopted reader: undo hooks, remove style, and
            if our controller is live → toggleReadAloudPopup(false)

adopt(r):
  if (!r._window || r._type !== 'pdf' || !probesChromeSide(r)) return
  const native = ReaderInstanceProto._getReadAloudRemoteInterface
  r._getReadAloudRemoteInterface = (win) => {
    try { return makeInterface(win, native.call(r, win)) }  // merged catalogue
    catch (e) { log(e); return native.call(r, win) }
  }
  r._initPromise.then(() => installIframeHooks(r))
```

`makeInterface(win, nativeIface)` returns an object of four arrow functions.
Each returns `new win.Promise(...)` and resolves `Cu.cloneInto(..., win)`:
- `getVoices` returns the speakd tier entry (`standard`, locale `"*"`,
  `segmentGranularity: "sentence"`, no `creditsPerMinute`, credits `null`).
  Optionally it merges the native `getVoices` result (§8.4).
- `getAudio(seg, impl)`: when `seg === 'sample'`, return a silent WAV. When
  `impl.id === SPEAKD_ID`, return `{audio:null, error:'unknown'}` and log
  "takeover bypassed". Otherwise delegate to the native interface.
- `getCreditsRemaining` and `resetCredits` return
  `{standardCreditsRemaining:null, premiumCreditsRemaining:null}`, or
  delegate.

### 8.2 Manager hooks (iframe side, after `_initPromise`)

```
installIframeHooks(r):
  win = r._iframeWindow; ir = waive(r._internalReader); m = waive(ir._readAloudManager)
  if (!probesIframeSide(ir, m)) { refuse(r); return }
  injectStyle(win.document, '.read-aloud-popup{display:none!important}')
  ir.setReadAloudHighlightGranularity('sentence')          // + our pref observer re-applies

  origLoad = m.loadVoices
  m.loadVoices = exportFunction((_ignored) => origLoad.call(m, true), win)   // bypass loggedIn gate

  origCreate = m._createController
  m._createController = exportFunction(function () {
    const v = waive(m._voice)
    if (takeoverWanted(r) && v && v.impl.id !== SPEAKD_ID) {
      const ours = findVoice(m.allVoices, SPEAKD_ID)
      if (ours) { m.selectVoice(ours.id); return }          // re-enters _createController via _applyVoice
    }
    if (v && v.impl.id === SPEAKD_ID)
      v.getController = exportFunction((segs, back, fwd) => makeController(r, win, segs, back, fwd), win)
    return origCreate.call(m)
  }, win)
```

`selectVoice` → `_applyVoice` → (active, segments present, same granularity)
→ `_createController()` (`B:82347-82352`, `B:82522-82537`). If granularity
differs, it recomputes the segments instead. Either way the second pass
lands on our voice.

### 8.3 The controller (content compartment)

Create it as `new win.EventTarget()`. Attach exported functions and exported
accessors (`paused`, `speed`, `buffering`) through a waiver. State: `segs`
(the content array), `pos = back ?? 0`, `paused = false`,
`destroyed = false`, `error = null`, `lastSkipGranularity = null`,
`activeTimestampIndex = null`, `minutesRemaining = null`,
`hasStandardMinutesRemaining = false`.

```
constructor-time:  channelStart(r, snapshot(segs), pos)     // plan the enqueue; don't speak yet
set paused(p):     if (applyingRemote) { state only }
                   else if (!p) resumeOrStart()  else speakd.pause()   // global verb: only if our channel speaks
set speed(s):      speakd.speed(s) (or ignore; design says speed is the listener's global)
skipAhead(g, acc): lastSkipGranularity = g; target = g==='sentence' ? pos+(acc?5:1)
                                                                     : next paragraphStart after pos
                   emit('ActiveSegmentChanging', segs[target]); speakd.seekTo(target)
skipBack(g, acc):  symmetric (paragraph: back to the current paragraph start, or the previous one if already there)
getSegmentToAnnotate(): segs[pos]
retry():           clear error; emit('ErrorCleared', segs[pos]); resumeOrStart()
refreshCreditsRemaining/resetCredits/syncActiveWordToPlayback: async no-ops
destroy():         destroyed = true; speakd.hush(channel) unless we are the ones tearing down

speakd events → controller:
  position(index) → pos = map(index); emit('ActiveSegmentChange', segs[pos])
  paused/resumed  → with applyingRemote: m.pause()/m.play()   (keeps Zotero UI/status in step)
  finished        → emit('Complete')  (highlight stays) or ir.toggleReadAloudPopup(false) to clear
  error           → error = 'network'|'unknown'; emit('Error', segs[pos])

emit(type, seg): if destroyed return; e = new win.Event(type); waive(e).segment = seg ?? null; this.dispatchEvent(e)
```

A new controller is built on every start, jump, reposition or voice change.
Treat construction as "(re)start the channel at `back`" and `destroy()` as
"this plan is dead". Tell superseded from stopped by whether a new
controller for the same reader arrives in the same tick. `_createController`
destroys first, then creates (`B:82654`); debounce the hush by one microtask.

Starting from our UI: `ir.startReadAloudAtPosition(annotation.position)`
(selection) or `(null)` (from the saved or visible position). Stop:
`ir.toggleReadAloudPopup(false)`. Pause and resume:
`ir.toggleReadAloudPaused(bool)`. All three go through the manager, so
Zotero's state, the media session, the tab audio indicator and the
other-tab pausing stay consistent.

### 8.4 Coexisting with Zotero's own voices (policy decision)

Design §"Tab lifecycle" says Zotero's native Read Aloud is left alone. With
the interface merged (native catalogue plus speakd), a reader can do both.
`takeoverWanted(r)` is true only after our UI started the read, and is
cleared on `deactivate`/close. The `_createController` guard then pins our
voice only for our reads. When the user starts Zotero's Read Aloud, the
persisted voice resolves normally. Caveat: after our read,
`selectVoice(SPEAKD_ID)` has persisted speakd for that language
(`B:82286-82300`). Restore the user's previous voice with
`m.selectVoice(prev)` on stop, or accept that. Simpler alternative: no
merge, so a takeover reader offers only speakd, plus browser voices, which
we steer away from. That removes Zotero cloud voices from every adopted
reader.

---

## Biggest risks

1. **Private-member drift.** Everything in §7 rows 8-25 is internal. The
   controller contract (row 23) is the largest surface; it is stable in
   shape (a TS base class) but not an API.
2. **Compartment mechanics are unrun.** The reasoning in §1.3 is sound, but
   a content-side `EventTarget` with exported accessors, expando `segment`
   on content `Event`s, and exported replacements for manager methods have
   not been run here. Spike this first.
3. **Voice resolution.** A persisted browser voice, or a user who is not
   logged in, silently routes around us. §8.2 guards both; test with a fresh
   profile, a logged-out profile, and a profile whose last voice was a
   `local-*` one.
4. **First-run dialog** on profiles with an empty `reader.readAloudVoices`
   (§5).
5. **The −64 dBFS tone** is Zotero's, is required for media keys, and is not
   digital silence.
6. **Readers open before the plugin loads** captured the native interface
   and cannot be adopted.

---

## UNVERIFIED

- Cross-compartment behaviour: exported functions or accessors installed on
  content objects are callable from the bundle; `new win.EventTarget()` with
  manager listeners works; a waived expando `segment` is visible to content
  listeners; content functions stringify through a waiver (§1.3, §4.3, §7).
- Zotero-TTS's claim that `_internalReader` and `_readAloudManager` arrive
  in the sandbox unwrapped while `_sdt.mapper` is an Xray. Not checked;
  waive regardless.
- Sandbox Blobs failing across compartments, and the Cache API crash
  (Zotero-TTS NOTES_2026-08-21). Not reproduced.
- That `Components`/`Cu` exist in the plugin sandbox (true for
  system-principal sandboxes in general; not stated in `plugins.js`).
- Whether muting the quiet-tone `<audio>` loses MPRIS media keys on Linux.
- Whether `flowClass === 'excluded'` covers every header, footer and page
  number. The decision is made in the PDF worker's SDT processor, which was
  not read.
- EPUB and snapshot views (`DOMView.setReadAloudState`, `B:55394`), which
  route through a separate `_readAloud` helper with its own
  `onSetReadAloudState` (`B:55398-55401`). Not analysed; the takeover
  targets PDF.
- The full `ReaderWindow` path (separate window) was checked only for push
  and `_open` timing (`X:2953-2966`, `X:2243`), not for the rest of the
  takeover.

---

## Verified live, 2026-09-24 (Zotero 10.0.3, flatpak, headless)

A throwaway profile with its own data directory, driven through a test-only
bridge plugin that evaluates chrome-scope JavaScript posted to Zotero's local
server (Marionette cannot be used: its `newSession` waits for a
`navigator:browser` window Zotero never opens). The bridge runs in a plugin
bootstrap sandbox, so what holds there holds for the reader plugin. One
spike, prototyping §8 inline, established:

- `Components.utils` is present in the plugin sandbox (`exportFunction`,
  `cloneInto`, `waiveXrays`).
- Replacing `reader._getReadAloudRemoteInterface` on the instance inside a
  wrapped `Zotero.Reader._readers.push` takes effect: `getVoices` from our
  interface supplied the only voice (`allVoices` = `["speakd"]`).
- `manager.loadVoices` and `manager._createController`, replaced with
  `Cu.exportFunction(fn, win)` on the waived manager, are called by the
  bundle; the voice at controller creation was ours.
- `voice.getController` replaced the same way received the live segment
  array (19 sentence segments for a one-page test PDF, each with
  `sourcePosition {pageIndex, rects}`) and start index 0 from
  `startReadAloudAtPosition(null)`.
- A controller built as `new win.EventTarget()` with exported accessors and
  methods was accepted by the manager.
- `new win.Event("ActiveSegmentChange")` with `segment` set through a waiver,
  dispatched on that controller, made `manager._activeSegment` identical to
  `segments[1]`, and the PDF view's `_readAloudHighlightedPosition` became
  segment 1's position (page 0, one rect).
- `toggleReadAloudPopup(false)` cleared the highlight.
- Seeding `extensions.zotero.reader.readAloudVoices` with an entry for the
  document's language avoided the first-run dialog. Segmentation (SDT) runs
  headless.

Still unverified live: skip and pause relay through the manager to our
controller, tab-close teardown, the media-key tone, and two-column PDFs.
