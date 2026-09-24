# Piper and Supertonic beside Kokoro, switched by hand

Date: 2026-09-24. Status: draft for review. Depends on
`2026-09-24-settings-and-languages-design.md` phases 1–2 (settings, language
resolution per utterance, voice maps), and is built after them.

## Intent

Cheaper engines to switch to by hand, mainly on battery. Kokoro stays the
default and the best voice. Piper is the cheap one; Supertonic sits between
them, and speaks 31 languages. Switching is manual for now. Switching on power
source, holding renders on battery and capping Kokoro's threads are recorded in
`docs/HANDOFF.md` as later work.

Measured on this laptop (CPU only, one ~200-character sentence), in
CPU-seconds per second of audio. Samples of the same passage are in
`~/Music/speakd-tts-compare/`.

| engine | CPU cost | notes |
|---|---|---|
| Kokoro | ~2.1 | best voice; torch, 1.4 GB resident |
| Supertonic 3, 8 steps (default) | ~1.1–1.5 | ~0.62 with one thread; 590 MB resident |
| Piper `high` | ~1.4 | |
| Supertonic 3, 2 steps | ~0.41–0.48 | quality to be judged by ear |
| Piper `medium` | ~0.23 | ~9× cheaper than Kokoro; 380 MB resident |
| Piper `low` | ~0.17 | 16 kHz, audibly duller |

Multi-threaded ONNX burns more total CPU than one thread (Supertonic went from
1.55 to 0.62 CPU-s per audio-s at one thread, at a wall RTF of 0.62, still
faster than real time). So the cheap engines default to one thread.

Success: `speakctl set speech.engine piper` (or `supertonic`, or the window's
Settings view) makes the next utterance speak through that engine at a
fraction of the CPU; German speaks with Piper's or Supertonic's German voice;
a language the chosen engine has no voice for is still spoken, by Kokoro.

Out of scope: switching automatically; downloading Piper voices from inside
speakd (Piper's `python -m piper.download_voices` does it, and the README says
how); a picker that lists installed voices (text fields, as for Kokoro voices
in phase 1).

### Risks, stated up front

- **Supertonic is archived.** `supertone-inc/supertonic` was archived on
  2026-09-09 ("development and support have ended"); the last PyPI release is
  1.3.1 (2026-05-18). The model still downloads from `Supertone/supertonic-3`.
  So Supertonic is an optional extra with the package version pinned, and the
  model revision pinned (the package pins it already). If the model ever stops
  resolving, `status` reports it unavailable and choosing it is refused. Nothing
  else breaks.
- **Licences.** `piper-tts` is GPL-3.0, since it embeds espeak-ng. Supertonic's
  weights are OpenRAIL-M (use restrictions: among them, no synthetic speech
  passed off as human, and no impersonation), and its code is MIT. Both stay
  optional extras, installed by the user, never bundled, and the README links
  both licences and restates Supertonic's use restrictions.

## 1. Settings

- `speech.engine`: choice `kokoro` | `piper` | `supertonic`, default `kokoro`.
  Takes effect at the next utterance; the one speaking finishes on the engine
  it started with. Refused, with the reason, when the chosen engine is not
  available (§4).
- **Piper:**
  - `speech.piper_voices` (`voice_map`): language → Piper voice name. Defaults:
    `en` → `en_US-ryan-medium`, `en-gb` → `en_GB-alan-medium`,
    `de` → `de_DE-thorsten-medium`, `fr` → `fr_FR-siwis-medium`,
    `es` → `es_ES-davefx-medium`, `it` → `it_IT-paola-medium`.
  - `speech.piper_voice_dir` (string), default `$XDG_DATA_HOME/piper-voices`.
- **Supertonic:**
  - `speech.supertonic_voice` (choice of its ten styles, `F1`–`F5`, `M1`–`M5`),
    default `F1`. One style speaks every language.
  - `speech.supertonic_steps` (int 2–16, default 5). Fewer steps are cheaper
    and rougher.
  - `speech.supertonic_threads` (int 1–8, default 1, `restart`).

`de` joins the language codes phase 2 accepts. Supertonic's 31 codes that
phase 2 does not know (`nl`, `pl`, `sv`, …) are accepted too, and are
unsupported under Kokoro.

## 2. Choosing the engine per utterance

After phase 2 has resolved the utterance's language:
1. The chosen engine (`speech.engine`) can speak that language: **that
   engine**.
   - Piper can when the language has an entry in `speech.piper_voices` and
     both its `.onnx` and `.onnx.json` files exist in the voice directory.
   - Supertonic can when the language is one of its 31.
2. Otherwise → **Kokoro**, exactly as today, including phase 2's
   `speech.unsupported_language` handling for a language Kokoro lacks.

The Kokoro fallback respects the load switch. Kokoro is never loaded
implicitly (the `LazyEngine` contract): if it is unloaded when a fallback needs
it, the utterance is declined with reason `no voice for <lang>`. So on battery
you can switch engines and press **Unload model** to get Kokoro's 1.4 GB back,
and anything the cheap engine can't speak says why instead of silently loading
Kokoro again.

A profile's own `voice` is a Kokoro voice. It is used only when Kokoro speaks.

This is one pure function, `choose_engine(lang, settings, availability) ->
(engine, voice) | Declined`, tested row by row.

## 3. The engines

Both are `Synthesizer`s in `src/speakd/synth/`. Both are loaded on first use,
and "unloaded" by dropping them when the engine setting moves away:
- `piper_engine.py`: one `PiperVoice` per voice name, each loaded in about a
  second. `speed` maps to `SynthesisConfig(length_scale=1/speed)`.
- `supertonic_engine.py`: one `TTS(intra_op_num_threads=…,
  inter_op_num_threads=1)`, loaded in about a second.
  `synthesize(text, voice_style, total_steps, speed, lang)`; its own chunking
  never triggers, since speakd hands it one sentence at a time. Supertonic's
  speed range is 0.7–2.0, so speakd's speed is clamped to it.

Common to both:
- Output is resampled to the player's 24 kHz with
  `scipy.signal.resample_poly` (Piper is 22.05 kHz or 16 kHz; Supertonic is
  44.1 kHz), so the sink never reopens and a switch mid-queue is seamless.
- The same `trim_silence` Kokoro's output gets, so sentence gaps are the same
  on every engine.
- `synth_lock` (from the render work) covers them like Kokoro: one engine call
  at a time across live speech and renders.

Extras: `speakd[piper]` (`piper-tts`, `scipy`, `sounddevice`) and
`speakd[supertonic]` (`supertonic==1.3.1`, `scipy`, `sounddevice`). Neither
pulls torch.

## 4. Numbers per engine

Kokoro reads years itself, which is why `transforms/pronunciation.py` has no
year rule (a measured decision; see that module). Piper phonemises with
espeak-ng, which reads "1994" as "one thousand nine hundred ninety four", and
Supertonic has no text normaliser at all. So:
- Each engine declares `reads_years: bool`. Kokoro says true; Piper and
  Supertonic say false.
- For an engine that says false, the pronunciation transform gains a year rule
  (`1100`–`2099` standing alone → "nineteen ninety-four" and similar), applied
  after the existing range rule.
- The transform chain runs before synthesis, but engine choice happens per
  utterance after language resolution. So the engine's flag is passed into
  `prepare`, the same way the language is.
- Verified by ear on the comparison passage, which has both a year and a
  range, before the rule is kept. The Supertonic samples may show that it
  learned years end to end; if so, it says true.

## 5. What the rest of the daemon sees

- `status.engine` gains `name` (the engine chosen for the next utterance) and
  `available: {piper: {ok, voices: [installed]}, supertonic: {ok, reason?}}`.
- The `engine` event's existing states are unchanged. Load and unload still
  mean Kokoro, the one engine big enough to be worth unloading.
- The `language` event phase 2 publishes on a fallback gains `engine`.
- The window's metrics row shows which engine made the sentence being spoken,
  beside rtf.
- **Renders** record the engine and voice per part at submission, in the
  manifest, so a resumed render keeps one voice throughout even if the setting
  changed in between.
- **The Zotero reader** steps aside to Zotero's own Read Aloud only when speakd
  has no voice for the document's language. It asks `status` (phase 2's
  language list, extended with what the chosen engine speaks) instead of
  assuming German is unsupported.
- Audio already cached for the current utterance (replay, seek) is replayed as
  it was made, even after a switch.

## 6. Testing

- `choose_engine`: every row of §2, including the declined-while-unloaded
  case, for both cheap engines.
- Each engine against its real model when installed (skipped otherwise):
  output at 24 kHz, duration scaling with `speed`, silence trimmed.
- The year rule: on for an engine that says false, off for Kokoro. Pure tests
  of the rule itself.
- A daemon test with FakeEngines standing in for all three: setting
  `speech.engine` mid-queue changes the next utterance's engine and not the
  current one, and an unavailable engine is refused.
- `status.engine.available` with and without each extra importable.
- CI installs neither extra, so the real-model tests are skipped there, as the
  Kokoro ones are.
