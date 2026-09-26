# Piper beside Kokoro, switched by hand

Date: 2026-09-24. Status: draft for review. Depends on
`2026-09-24-settings-and-languages-design.md` phases 1–2 (settings, language
resolution per utterance, voice maps), and is built after them.

## Intent

A cheaper engine to switch to by hand, mainly on battery. Kokoro stays the
default and the better voice; Piper is the one you pick when speech should
cost little. Switching is manual for now. Switching on power source, holding
renders on battery and capping Kokoro's threads are recorded in
`docs/HANDOFF.md` as later work. Supertonic 3 was evaluated and dropped (its
repository was archived on 2026-09-09); the measurements are below for the
record.

Measured on this laptop (CPU only, one ~200-character passage), in
CPU-seconds per second of audio. Samples of the same passage are in
`~/Music/speakd-tts-compare/`.

| engine | CPU cost | wall RTF | notes |
|---|---|---|---|
| Kokoro | ~2.1 | ~0.5 | best voice; torch, 1.4 GB resident |
| Piper `high` | ~1.4 | ~0.35 | |
| Piper `medium`, default threads | ~0.25 | ~0.07 | |
| **Piper `medium`, one thread** | **~0.13** | ~0.13 | ~16× cheaper than Kokoro; 380 MB resident |
| Piper `low` | ~0.17 | ~0.04 | 16 kHz, audibly duller |
| Supertonic 3 (dropped) | 0.6–1.5 | 0.27–0.62 | archived; no text normaliser |

Multi-threaded ONNX burns more total CPU than one thread, since the threads
spin while they wait on each other. One thread still runs Piper `medium` at
about 8× real time, so Piper defaults to one.

Success: `speakctl set speech.engine piper` (or the window's Settings view)
makes the next utterance speak through Piper at a fraction of the CPU; German
speaks with a downloaded German Piper voice; a language with no Piper voice is
still spoken, by Kokoro.

Out of scope: switching automatically; downloading voices from inside speakd
(Piper's own `python -m piper.download_voices` does it, and the README says
how); a picker that lists installed voices (a text field, as for Kokoro voices
in phase 1).

**Licence.** `piper-tts` is GPL-3.0, since it embeds espeak-ng, and speakd is
MIT. So Piper is an optional extra the user installs, never bundled into a
speakd distribution, and the README says so.

## 1. Settings

- `speech.engine`: choice `kokoro` | `piper`, default `kokoro`. Takes effect at
  the next utterance; the one speaking finishes on the engine it started with.
  Refused, with the reason, when Piper is not available (§5).
- `speech.piper_voices` (`voice_map`): language → Piper voice name. Defaults:
  `en` → `en_US-ryan-medium`, `en-gb` → `en_GB-alan-medium`,
  `de` → `de_DE-thorsten-medium`, `fr` → `fr_FR-siwis-medium`,
  `es` → `es_ES-davefx-medium`, `it` → `it_IT-paola-medium`.
- `speech.piper_voice_dir` (string), default `$XDG_DATA_HOME/piper-voices`
  (where Piper's downloader already put `en_US-ryan-high` on this machine).
- `speech.piper_threads` (int 1–8, default 1, `restart`).
- `render.piper_threads` (int 0–16, default 0 meaning every core, `restart`).
  A render is long and nobody is waiting on each sentence, so it takes the
  whole machine by default rather than live speech's frugal single thread.
  It still yields to live speech between sentences, as every render does. On
  battery, set it to 1.

`de` joins the language codes phase 2 accepts: with Piper there is finally a
German voice to resolve to. Under Kokoro it is unsupported.

## 2. Choosing the engine per utterance

After phase 2 has resolved the utterance's language:
1. `speech.engine` is `piper`, the language has an entry in
   `speech.piper_voices`, and its `<name>.onnx` and `<name>.onnx.json` both
   exist in the voice directory → **Piper**, with that voice.
2. Otherwise → **Kokoro**, exactly as today, including phase 2's
   `speech.unsupported_language` handling for a language Kokoro lacks.

The Kokoro fallback respects the load switch. Kokoro is never loaded
implicitly (the `LazyEngine` contract): if it is unloaded when a fallback needs
it, the utterance is declined with reason `no voice for <lang>`, as phase 2
declines one. So on battery you can switch to Piper and press **Unload model**
to get Kokoro's 1.4 GB back, and anything Piper can't speak says why instead of
silently loading Kokoro again.

A profile's own `voice` is a Kokoro voice, used only when Kokoro speaks.

This is one pure function, `choose_engine(lang, settings, piper_voices_on_disk,
kokoro_loaded) -> (engine, voice) | Declined`, tested row by row.

## 3. The Piper engine

`src/speakd/synth/piper_engine.py`, a `Synthesizer`:
- One `PiperVoice` per voice name, loaded on first use (about a second) and
  kept until the engine setting moves away from Piper.
- Built with speakd's own `onnxruntime.InferenceSession`, whose
  `SessionOptions` set `intra_op_num_threads` to `speech.piper_threads` and
  `inter_op_num_threads` to 1. `PiperVoice.load` offers no thread control, but
  `PiperVoice(config=…, session=…)` accepts a session (verified with
  piper-tts 1.4.1).
- `speed` maps to Piper's `SynthesisConfig(length_scale=1/speed)`.
- Output is resampled to the player's 24 kHz with `scipy.signal.resample_poly`
  (Piper is 22.05 kHz for `medium`/`high` voices and 16 kHz for `low`), so the
  sink never reopens and a switch mid-queue is seamless.
- The same `trim_silence` Kokoro's output gets, so sentence gaps are the same
  on both engines.
- `synth_lock` (from the render work) covers it like Kokoro: one engine call at
  a time across live speech and renders.

A new extra, `speakd[piper]`: `piper-tts`, `scipy` and `sounddevice`, so
Piper-only installs can play audio without pulling torch.

## 4. Years

Kokoro reads years itself, which is why `transforms/pronunciation.py` has no
year rule (a measured decision; see that module). Piper phonemises with
espeak-ng, which reads "1994" as "one thousand nine hundred ninety four".
- Each engine declares `reads_years: bool`. Kokoro says true, Piper false.
- For an engine that says false, the pronunciation transform gains a year rule
  (a standalone `1100`–`2099` → "nineteen ninety-four", "two thousand five",
  "twenty twenty-six"), applied after the existing range rule.
- Engine choice happens per utterance after language resolution, so the flag
  is passed into `prepare` the same way the language is.
- Checked by ear on the comparison passage, which has both a year and a range,
  before the rule is kept.

## 5. What the rest of the daemon sees

- `status.engine` gains `name`, the engine the settings choose for the next
  utterance (`piper` while `speech.engine` is piper and Piper is usable, else
  `kokoro`; the per-utterance fallback is language-dependent and reported
  per-event on `started`/`position`), and
  `piper: {available: bool, voices: [installed names]}`. `available` is false
  when `piper-tts` isn't importable, and then `set_setting speech.engine piper`
  is refused with that reason.
- The `engine` event's existing states are unchanged. Load and unload still
  mean Kokoro, the one engine big enough to be worth unloading.
- The `language` event phase 2 publishes on a fallback gains `engine`.
- The window's metrics row shows which engine made the sentence being spoken,
  beside rtf.
- **Renders** have no length limit: parts are stored as headerless PCM and
  streamed into ffmpeg, so neither WAV's 4 GB (~25 h) ceiling nor memory bounds
  a book, and the HTTP body cap for `render` is 256 MB (1 MB for every other
  verb). Built in the audio-export work, before this.
- **Renders** record the engine and voice per part at submission, in the
  manifest, so a resumed render keeps one voice throughout even if the setting
  changed in between.
- **The Zotero reader** steps aside to Zotero's own Read Aloud only when speakd
  has no voice for the document's language. It asks `status` (phase 2's
  language list, extended with Piper's installed languages when
  `speech.engine` is `piper`) instead of assuming German is unsupported.
- Audio already cached for the current utterance (replay, seek) is replayed as
  it was made, even after a switch.

## 6. Testing

- `choose_engine`: every row of §2, including the declined-while-unloaded
  case, as pure-function tests.
- `PiperEngine` against a real voice when one is installed (skipped
  otherwise): output at 24 kHz, duration scaling with `speed`, silence trimmed,
  and the session built with the configured thread count.
- The year rule: on for an engine that says false, off for Kokoro, plus pure
  tests of the rule itself.
- A daemon test with FakeEngines standing in for both: setting `speech.engine`
  mid-queue changes the next utterance's engine and not the current one, and
  choosing Piper when unavailable is refused.
- `status.engine.piper` with and without `piper-tts` importable.
- CI installs no Piper, so the real-voice tests are skipped there, as the
  Kokoro ones are.
