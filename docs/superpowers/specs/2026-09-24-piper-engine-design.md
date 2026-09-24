# Piper as a second engine, switched by hand

Date: 2026-09-24. Status: draft for review. Depends on
`2026-09-24-settings-and-languages-design.md` phases 1–2 (settings, language
resolution per utterance, voice maps), and is built after them.

## Intent

A cheaper engine to switch to by hand, mainly on battery. Kokoro stays the
default and the better voice; Piper is the one you pick when speech should
cost less than it sounds good. Switching is manual for now. Switching on power
source, holding renders on battery and capping Kokoro's threads are recorded in
`docs/HANDOFF.md` as later work.

Measured on this laptop (CPU only, one 160-character sentence), in CPU-seconds
per second of audio:

| engine | CPU cost | vs Kokoro |
|---|---|---|
| Kokoro | ~2.1 | 1× |
| Piper, `high` voice | ~1.4 | ~1.5× cheaper |
| Piper, `medium` voice | ~0.23 | ~9× cheaper |
| Piper, `low` voice | ~0.17 | ~12× cheaper |

So the saving is in Piper's `medium` voices, and those are the defaults below.
A Piper process holds about 380 MB, against Kokoro's 1.4 GB.

Success: `speakctl set speech.engine piper` (or the window's Settings view)
makes the next utterance speak through Piper at a fraction of the CPU; English
and German both work with a downloaded Piper voice; a language with no Piper
voice is still spoken, by Kokoro.

Out of scope: switching automatically; downloading voices from inside speakd
(Piper's own `python -m piper.download_voices` does it, and the README says
how); a voice picker that lists installed voices (a text field, as for Kokoro
voices in phase 1).

## 1. Settings

- `speech.engine`: choice `kokoro` | `piper`, default `kokoro`. Takes effect at
  the next utterance; the one speaking finishes on the engine it started with.
- `speech.piper_voices` (`voice_map`): language → Piper voice name. Defaults:
  `en` → `en_US-ryan-medium`, `en-gb` → `en_GB-alan-medium`,
  `de` → `de_DE-thorsten-medium`, `fr` → `fr_FR-siwis-medium`,
  `es` → `es_ES-davefx-medium`, `it` → `it_IT-paola-medium`.
- `speech.piper_voice_dir` (string), default `$XDG_DATA_HOME/piper-voices`
  (where Piper's downloader already put `en_US-ryan-high` on this machine).

`de` joins the language codes that phase 2 accepts, since with Piper there is
finally a German voice to resolve to.

## 2. Choosing the engine per utterance

After phase 2 has resolved the utterance's language:
1. `speech.engine` is `piper`, the language has an entry in
   `speech.piper_voices`, and its `<name>.onnx` and `<name>.onnx.json` exist in
   the voice directory → **Piper**, with that voice.
2. Otherwise → **Kokoro**, exactly as today, including phase 2's
   `speech.unsupported_language` handling for a language Kokoro lacks.

The Kokoro fallback respects the load switch. Kokoro is never loaded
implicitly (the `LazyEngine` contract): if it is unloaded when a fallback needs
it, the utterance is declined with reason `no voice for <lang>`, as phase 2
declines one. So on battery you can switch to Piper and press **Unload model**
to get Kokoro's 1.4 GB back, and anything Piper can't speak says why instead of
silently loading it again.

A profile's own `voice` is a Kokoro voice. When Piper speaks, the profile's
voice is ignored and the language's Piper voice is used.

## 3. The Piper engine

`src/speakd/synth/piper_engine.py`, a `Synthesizer`:
- One loaded `PiperVoice` per voice name, loaded on first use and kept.
  Loading is about a second.
- `speed` maps to Piper's `SynthesisConfig(length_scale=1/speed)`.
- Output is resampled to the player's rate (24 kHz; Piper is 22.05 kHz for
  `medium`/`high` voices and 16 kHz for `low`) with
  `scipy.signal.resample_poly`, so the sink never reopens and a switch mid-queue
  is seamless.
- The same `trim_silence` Kokoro's output gets, so sentence gaps are the same
  on both engines.
- `synth_lock` (from the render work) covers it like Kokoro: one engine call at
  a time across live speech and renders.

A new extra, `speakd[piper]`: `piper-tts`, `scipy` and `sounddevice`, so
Piper-only installs can play audio without pulling torch.

## 4. What the rest of the daemon sees

- `status.engine` gains `name` (the engine chosen for the next utterance) and
  `piper: {available: bool, voices: [installed names]}`. `available` is false
  when `piper-tts` isn't importable, and then `set_setting speech.engine piper`
  is refused with that reason.
- The `engine` event's existing states are unchanged. Load and unload still
  mean Kokoro, the one engine big enough to be worth unloading.
- The `language` event that phase 2 publishes on a fallback gains
  `engine: "kokoro" | "piper"`.
- The window's metrics row shows which engine made the sentence being spoken,
  next to rtf.
- **Renders** record the engine and voice per part at submission, in the
  manifest, so a resumed render keeps one voice throughout even if the setting
  changed in between.
- **The Zotero reader** steps aside to Zotero's own Read Aloud only when
  speakd has no voice for the document's language. It asks `status` (the
  language list phase 2 adds, now including languages with an installed Piper
  voice when `speech.engine` is `piper`) instead of assuming German is
  unsupported.
- Audio already cached for the current utterance (replay, seek) is replayed as
  it was made, even after a switch.

## 5. Testing

- The choice function: every row of §2, including the declined-while-unloaded
  case, as pure-function tests.
- `PiperEngine` against a real voice when one is installed (skipped
  otherwise): output at 24 kHz, duration scaling with `speed`, silence trimmed.
- A daemon test with FakeEngines standing in for both: setting `speech.engine`
  mid-queue changes the next utterance's engine and not the current one.
- `status.engine.piper` with and without `piper-tts` importable.
- CI installs no Piper, so the real-voice tests are skipped there, as the
  Kokoro ones are.
