# Follow-along text, working seek, live speed

Date: 2026-09-23. Status: approved in conversation.

## Why

The pinned window shows three clamped lines (previous / now / next) of plain
text. Its ← and → buttons send `seek`, which the daemon refuses outright, and
the window cannot know the *next* sentence anyway because the daemon reveals
segments one `position` at a time. Speed is fixed per profile at synthesis.

The user wants to read along while listening: the whole utterance, rendered
as the markdown it arrived as, with the highlight walking through it; working
back/forward; and a speed control.

## 1. Seeking (daemon)

**Segmentation moves ahead of speaking.** `Daemon._speak` calls
`segment(pieces)` itself and passes the units to `speak(units=...)`, which
skips its own segmentation when given them. `started` gains

```json
"segments": [{"index": 0, "text": "…", "span_start": 0, "span_end": 42}, …]
```

(`text` is the unit's spoken text.) Every `position` gains `"index"`, the
unit's absolute index in that list. `ShellSource` stops counting positions and
reads `index` from the wire.

**`seek` is implemented.** Payload is exactly one of `{"index": k}` or
`{"by": n}` (n an int, in practice ±1). It targets whatever is speaking, like
pause, and ignores `source_id`.

- Nothing speaking → `ok: false`, "nothing is speaking".
- Target computed from the index last *announced* (`position`), so relative
  seeks are relative to what the user saw.
- Target < 0 → 0 (← on the first sentence restarts it).
- Target ≥ len(units) → behaves as `cancel` of that utterance; the queue runs
  on. Response `{"index": null, "ended": true}`.
- Otherwise response `{"index": k}`.

Mechanism: the daemon keeps a `_Current` record for the utterance in flight
(source, units, cache, last announced index, pending `seek_to`). `seek` sets
`seek_to` and the utterance's cancel Event under `_idle`'s lock, then stops the
player outside it. `_speak` loops: when `speak()` returns cancelled with a
`seek_to` pending, it installs a fresh cancel Event and a fresh Timeline and
calls `speak()` again with `start_index=seek_to`. No `finished`/`started` is
published between passes; the next `position` says where playback is.
`_stop_speaking` (hush/cancel/mute/disable) clears `seek_to` when it cancels,
so a hush that races a seek wins.

Pause is sticky in the player, so a seek while paused lands paused: the new
position is announced and playback waits for resume.

**Audio cache.** `speak()` takes an optional `AudioCache`: index → (audio,
made_at multiplier). The producer uses a cached entry instead of synthesising.
Cap 64 MiB of float32 samples per utterance; over the cap, entries furthest
from the current index are evicted. The cache dies with the utterance.

## 2. Speed (hybrid)

**Verb `set_speed {"speed": x}`**: a multiplier on the profile's speed,
clamped to [0.7, 1.6], rounded to 0.05. Response `{"speed": applied}`.
Published as event `speed {"speed": applied}`. `status` reports `"speed"`.
Persisted in `DaemonState.speed` (default 1.0); every `state.save` call site
switches to `dataclasses.replace(state.load(), …)` so no writer drops another's
field. `speakctl speed X` sets it; `speakctl speed` with no argument prints it.

**A shared `Tempo`** (thread-safe float, the multiplier) is owned by the
daemon and handed to the pipeline and the player.

- Synthesis: new units are synthesised at `profile.speed * tempo.value`, and
  recorded with `made_at = tempo.value`.
- Playback: `StreamingPlayer` grows `play_at(audio, sr, made_at) -> float`
  (seconds actually played). Before each chunk it compares `tempo.value` with
  the multiplier the remaining buffer currently represents; when they differ,
  it time-stretches the remainder by their ratio. So the playing sentence and
  the buffered one change speed within ~85 ms; later ones are native.
  The pipeline uses `play_at` when the player has it and plain `play`
  otherwise, and uses the returned seconds for the timeline's duration.
- Stretching: `speakd/stretch.py`, WSOLA in numpy, no new dependency.
  ~30 ms frames, 50% overlap, cross-correlation search ±~8 ms. `ratio > 1`
  shortens. Ratios within 1e-3 of 1 return the input unchanged.

## 3. The window

**Text region** replaces the three lines with the whole utterance from
`started.text`, rendered by `clients/gui/shared/markdown.js`, a small
in-repo renderer mirroring the grammar of `transforms/markdown.py`: fences,
ATX headings, bullets, ordered items, quotes, rules, pipe tables, paragraphs;
inline links, code, bold, italic. DOM is built with `createElement` and
`textContent` only; no `innerHTML` for message text, ever. Each block records
its source range `[start, end)`.

**Mapping segments to text**, done once per `started`, since all segments are
then known: a segment's span selects the block(s) it overlaps; within the
block, the segment's spoken text is located in the block's rendered text by a
normalised search (letters and digits only, case-folded, with an index map
back to the rendered string), starting from the previous match in that block so
repeated sentences resolve in order. Not found → the segment highlights its
whole block.

**Highlight**: the current segment's rendered range is wrapped in
`span.lit` elements (text nodes split as needed) and unwrapped when it moves.
Blocks wholly before the current segment are dimmed. Paused shows the
existing dashed-underline treatment instead of the fill.

**Clicking** text seeks to the segment whose range contains the click
(caret position), falling back to the block's first segment.

**Follow**: the lit segment is scrolled into view (centered, smooth unless
reduced motion), except for 4 s after the user scrolls the region themselves.

**Controls**: Play · ← · → · (− 1.0× +) · Hush. ← / → send `seek {by: ∓1}`.
− / + send `set_speed` in 0.1 steps; the label reads the daemon's answer and
the `speed` event, never its own request. Keyboard, when focus is not in the
textarea: ←/→ seek, −/+ speed, space play/pause.

The scrubber shows all segments from `started`, equal flex (durations are not
known ahead); clicking a tick seeks by index.

**Shell**: `set_speed` joins `FORWARDED` in `bridge.rs`.

**SimulatedSource** mirrors all of it: its fixture becomes a short markdown
passage whose segment spans are block-level as the real transform's are;
`started` carries `segments`; `position` carries `index`; it honours
`seek {index|by}` and `set_speed` (scaling its clock) and emits `speed`.

## 4. Testing

- `tests/test_seek.py`: seek by index / by ±1 with FakeEngine and a pausable
  recording player; clamping at 0; past-end ends the utterance and the queue
  continues; nothing speaking is refused; hush during seek wins; `started`
  carries segments and `position` carries index; cache hit avoids
  re-synthesis.
- `tests/test_speed.py`: clamp and rounding, persistence across a new Daemon,
  `status`/event, other state fields survive, synthesis uses the multiplied
  speed.
- `tests/test_stretch.py`: output length ≈ n/ratio; a 220 Hz sine keeps its
  dominant frequency; identity at ratio 1; short input safe.
- `tests/test_streaming_player.py` additions: `play_at` stretches when tempo
  differs, returns played seconds.
- Frontend: browser tab on the simulation, then the real daemon and shell.
- Full suite stays green (874 at start).
