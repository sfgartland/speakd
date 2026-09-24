# Piper Engine — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development (recommended) or superpowers:executing-plans. TDD throughout: every test is written first and watched failing before the code. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Piper as a second engine beside Kokoro, chosen by hand with `speech.engine`. Anything Piper has no voice for is spoken by Kokoro.

**Architecture:**
- A `PiperEngine` `Synthesizer` in `src/speakd/synth/piper_engine.py`. It owns its own ONNX sessions, resamples to 24 kHz, and speaks years itself.
- A pure `choose_engine` in `src/speakd/engines.py` picks the engine and voice per utterance, after phase 2's language resolution.
- The daemon holds both engines. Renders record their engine per part.

**Tech stack:** Python 3.11+, `piper-tts` 1.4.x (onnxruntime), `scipy.signal.resample_poly`, and the existing settings, languages and render modules.

**Spec:** `docs/superpowers/specs/2026-09-24-piper-engine-design.md` (binding).

**Prerequisites:** all merged to `main` before Task 1 starts:
- settings core (phase 1)
- languages (phase 2): `speakd.languages.normalise`, `resolve`, `Resolution`, `supported_languages`, and the `lang` argument on `Synthesizer.synthesize(text, voice, speed, lang="en")`
- audio export Part A: `speakd.render.RenderQueue` and `RenderJob`, headerless-PCM parts, the `render` verbs

Read the merged code for their exact shapes before writing a test that touches them.

**Deviation from spec §4, deliberate:** the year rule runs inside `PiperEngine.synthesize` on the text handed to Piper, not in `prepare`. `prepare` runs before the engine is chosen. Rewriting only the text Piper receives leaves the displayed text and its spans untouched, and needs no plumbing through the transform chain. The range rule has already run in `prepare` by then, so the spec's ordering holds. Each engine still declares `reads_years`, and the daemon doesn't need it; it documents the choice and is what the tests pin.

## Global Constraints

- **Settings**, declared by core beside the other `speech.*` settings:
  - `speech.engine`: choice `kokoro` | `piper`, default `kokoro`.
  - `speech.piper_voices`: voice_map with defaults `en: en_US-ryan-medium, en-gb: en_GB-alan-medium, de: de_DE-thorsten-medium, fr: fr_FR-siwis-medium, es: es_ES-davefx-medium, it: it_IT-paola-medium`.
  - `speech.piper_voice_dir`: string; default `$XDG_DATA_HOME/piper-voices`, with `XDG_DATA_HOME` falling back to `~/.local/share`.
  - `speech.piper_threads`: int 1–8, default 1, `restart: true`.
  - `render.piper_threads`: int 0–16, default 0 (0 = let onnxruntime use every core), `restart: true`.
- **The `de` code:** phase 2 accepts it as known but unsupported by Kokoro. If phase 2 did not, add it to `normalise`'s known set in Task 3, and say so in the commit.
- **Engine choice**, first match wins:
  - `speech.engine == "piper"`, `lang` in `speech.piper_voices`, and both `<dir>/<name>.onnx` and `<dir>/<name>.onnx.json` exist → Piper with that voice.
  - Otherwise, Kokoro, via phase 2's existing voice and unsupported-language handling.
  - Kokoro needed but unloaded → declined, reason `no voice for <lang>`. Kokoro is never loaded implicitly.
- **Audio:**
  - `PiperEngine.sample_rate == 24000`. Output is float32 mono, resampled with `scipy.signal.resample_poly` from the voice's rate (read from its `.onnx.json`), then `trim_silence(audio, 24000)`.
  - Speed maps to `SynthesisConfig(length_scale=1.0 / speed)`.
- **Threads:** sessions are built by speakd: `onnxruntime.SessionOptions()` with `intra_op_num_threads = threads` (skipped when `threads == 0`), `inter_op_num_threads = 1`, and `providers=["CPUExecutionProvider"]`. The voice is `PiperVoice(config=PiperConfig.from_dict(json), session=session)`. Verified against piper-tts 1.4.1.
- **Imports:** `piper` and `onnxruntime` are imported lazily, inside the engine, so `tests/test_import_cost.py` stays green.
- **Extra:** `piper = ["piper-tts>=1.4,<2", "scipy", "sounddevice"]` in `pyproject.toml`. Run `uv lock`. Never add Piper to the default dependencies. Its licence is GPL-3.0.
- **Locking:** every Piper synthesize call runs under the daemon's shared `synth_lock`, as Kokoro's does.
- **Fakes:** no test may need a real Piper voice except those marked to skip without one. Fakes stand in for the `piper` module the way `tests/test_engine_device.py` fakes `kokoro`.

## Review Focus

1. **Deleting a voice file between utterances:** the next utterance falls back to Kokoro instead of raising, because file existence is checked per utterance. Tested in Task 3.
2. **`speech.engine = piper` with `piper-tts` not installed:** `set_setting` is refused with "piper-tts is not installed (pip install 'speakd[piper]')", and the stored value stays `kokoro`. Tested in Task 4.
3. **A corrupt `.onnx` file, where loading raises:** that utterance falls back to Kokoro with one stderr line, and later utterances retry the load. A bad file is not cached as loaded. Tested in Task 2 (engine) and Task 4 (daemon).
4. **Switching `speech.engine` mid-utterance:** the current utterance finishes on its engine, and replay or seek within it reuses its cached audio. Tested in Task 4.
5. **A resumed render after the engine setting changed:** it keeps the engine and voice recorded in its manifest. Tested in Task 5.

---

### Task 1: the year rule

**Files:**
- Create: `src/speakd/synth/years.py`
- Test: `tests/test_years.py`

**Interfaces:**
- Produces: `speak_years(text: str) -> str`.

A standalone 4-digit year from 1100 to 2099 becomes words, in the way people say years:
- `1994` → "nineteen ninety-four"
- `1900` → "nineteen hundred"
- `1905` → "nineteen oh five"
- `2000` → "two thousand"
- `2005` → "two thousand five"
- `2010` → "twenty ten"
- `2026` → "twenty twenty-six"
- `1100` → "eleven hundred"

"Standalone" means not adjacent to a digit, a `.` or `,` followed by a digit, a letter, `@`, `-`, `/`, or `:`. So `3.1994`, `1,994`, `@Stiegler-1994`, `1994-95`, `12:1994`, `ISBN 1994…` inside a longer number, and `v1994` are untouched. A trailing `s` makes a decade: `1990s` → "nineteen nineties".

- [ ] Write `tests/test_years.py` with a parametrised table: every example above, plus the untouched ones. Also: `"In 1994, Habermas"` → `"In nineteen ninety-four, Habermas"`; `"(1994)"` → `"(nineteen ninety-four)"`; `"1099"` and `"2100"` untouched; the empty string.
- [ ] Run `uv run pytest tests/test_years.py -q` and watch it fail (module missing).
- [ ] Implement with one compiled regex, `(?<![\w@/.,:-])(1[1-9]\d\d|20\d\d)(s?)(?![\w/-]|[.,:]\d)`, and a small number-words helper for 0–99. No external dependency.
- [ ] Run until green. Then run `uv run ruff check . && uv run mypy src tests`.
- [ ] Commit: "Say years the way people do, for engines that read them digit by digit".

### Task 2: `PiperEngine`

**Files:**
- Create: `src/speakd/synth/piper_engine.py`
- Modify: `pyproject.toml` (the `piper` extra), `uv.lock`
- Test: `tests/test_piper_engine.py`

**Interfaces:**
- Consumes: `speak_years` (Task 1); `trim_silence` (from `speakd.synth.kokoro_engine`, or wherever it lives; if it is inside kokoro_engine and importing it would pull torch, move it to `src/speakd/synth/audio.py` first and re-export it); `speakd.languages`.
- Produces:
  - `piper_available() -> bool`: `importlib.util.find_spec("piper") is not None`.
  - `class PiperEngine` with `name = "piper"`, `sample_rate = 24000`, and `reads_years = False`.
  - `PiperEngine.__init__(self, voice_dir: Path, threads: int)`.
  - `PiperEngine.has_voice(voice: str) -> bool`: both files exist.
  - `PiperEngine.installed_voices() -> list[str]`: sorted stems of `*.onnx` files that have a matching `.onnx.json`.
  - `PiperEngine.synthesize(text: str, voice: str, speed: float, lang: str = "en") -> np.ndarray` (float32, 24 kHz). Raises `PiperVoiceError(voice, reason)` when the voice can't load.
  - `PiperEngine.unload() -> None`: drops every loaded voice.
  - `PiperEngine.set_voice_dir(path: Path) -> None`: unloads, then points at the new directory.
- Also add `reads_years = True` as a class attribute on `KokoroEngine`, `LazyEngine` and `FakeEngine`, and `reads_years: bool` to the `Synthesizer` Protocol.

- [ ] Write tests with a fake `piper` package (install it into `sys.modules` in a fixture: `piper.PiperVoice`, `piper.config.PiperConfig.from_dict`, `piper.config.SynthesisConfig`) and a fake `onnxruntime` (`SessionOptions`, and an `InferenceSession` that records its `sess_options` and `providers`):
  - one session per voice, created once across two synthesize calls;
  - `intra_op_num_threads == 3` when `threads=3`, left unset when `threads=0`, and `inter_op_num_threads == 1` always;
  - `length_scale == 0.5` at speed 2.0;
  - a 22050 Hz fake chunk of N samples comes out at about `N * 24000 / 22050` samples (±2), float32;
  - the text handed to the fake voice has its years spoken ("In 1994" → "In nineteen ninety-four");
  - `has_voice` false when the `.json` is missing;
  - a fake `InferenceSession` that raises → `PiperVoiceError`, and the next call tries again (not cached);
  - `unload()` then synthesize loads again;
  - importing `speakd.synth.piper_engine` doesn't import `piper` or `onnxruntime` (check `sys.modules`).
- [ ] Add one real-voice test, skipped unless `piper` is importable and `$XDG_DATA_HOME/piper-voices/en_US-ryan-medium.onnx` exists: sample rate 24000; the duration at speed 2.0 is less than 0.65× the duration at 1.0; leading and trailing silence under 50 ms.
- [ ] Watch the tests fail, then implement. Voices live in a dict under a `threading.Lock`. Loading happens outside the lock, and the result is stored only on success. The voice's rate is `config.sample_rate`. Resample with `resample_poly(audio, 24000 // g, rate // g)`, where `g = gcd(24000, rate)`. Chunks come from `voice.synthesize(text, syn_config)` as `AudioChunk.audio_float_array` (or `audio_int16_array / 32768` if the float field is absent; check the real class in site-packages).
- [ ] Add the extra and run `uv lock`. Make sure `uv sync --extra kokoro` still keeps spaCy's model (see the comments in `pyproject.toml`). Install `--extra piper` locally so the real test runs.
- [ ] Gate: ruff, format, mypy, and the full pytest suite.
- [ ] Commit: "Add Piper as an engine: its own single-threaded sessions, resampled to Kokoro's rate".

### Task 3: `choose_engine`

**Files:**
- Create: `src/speakd/engines.py`
- Test: `tests/test_engines.py`

**Interfaces:**
- Produces:
  - `@dataclass(frozen=True) class EngineChoice: engine: str; voice: str | None`. `voice` is None when Kokoro speaks, meaning "phase 2 chooses as before".
  - `@dataclass(frozen=True) class Declined: reason: str`.
  - `choose_engine(lang: str, engine_setting: str, piper_voices: Mapping[str, str], piper_has_voice: Callable[[str], bool], piper_ok: bool, kokoro_loaded: bool) -> EngineChoice | Declined`.

- [ ] Write a parametrised table test covering these rows:
  - kokoro setting → kokoro;
  - piper setting, voice mapped and present → piper with that voice;
  - piper setting, voice mapped but missing → kokoro;
  - piper setting, language unmapped → kokoro;
  - piper setting but `piper_ok` false → kokoro;
  - any Kokoro outcome with `kokoro_loaded` false → `Declined("no voice for <lang>")`;
  - piper chosen with `kokoro_loaded` false → piper (Kokoro not needed).

  Also check that `de` normalises to `de` in `speakd.languages.normalise` (add it to the known set if phase 2 didn't).
- [ ] Watch it fail, implement (pure, no I/O: `piper_has_voice` is injected), and go green.
- [ ] Commit: "Choose Piper or Kokoro per utterance, falling back to Kokoro".

### Task 4: the daemon

**Files:**
- Modify: `src/speakd/daemon.py`, `src/speakd/__main__.py`, `src/speakd/transport_http.py` (only if a new verb appears; none should)
- Test: `tests/test_piper_daemon.py`

**Interfaces:**
- Consumes: `PiperEngine`, `piper_available` (Task 2); `choose_engine`, `EngineChoice`, `Declined` (Task 3); phase 2's `_speak` resolution.
- Produces:
  - `Daemon.__init__(..., piper: Synthesizer | None = None)`, with `self.piper`.
  - `started`, `position` and `language` events carry `engine: "kokoro" | "piper"`.
  - `status.engine` gains `name` and `piper: {available: bool, voices: list[str]}`.
  - `status.languages.supported` includes the languages with an installed Piper voice when `speech.engine == "piper"`.

Steps:
- Declare the five settings from Global Constraints next to the existing `speech.*` declarations.
- In `_speak`, after phase 2 resolves the language, call `choose_engine`, using `self.settings.get(...)` for the settings, `self.piper.has_voice` for voice presence, `self.piper is not None and piper_available()` for `piper_ok`, and `self.engine.loaded` for `kokoro_loaded`.
- On `Declined`, publish `declined {reason}` and `finished {cancelled: False, aborted: True}`, exactly as phase 2's decline does (reuse its helper).
- On Piper, pass `engine=self.piper` and the chosen voice to `speak(...)`. On Kokoro, keep phase 2's path unchanged.
- If `speak` reports `PiperVoiceError` for a unit, fall back: re-run that utterance on Kokoro, and write one stderr line. Keep it simple: catch it at the utterance level, before any audio has played if possible. Otherwise decline the rest, and document which you did.
- Refuse `set_setting speech.engine piper` when `self.piper is None or not piper_available()`, with the message from Review Focus 2, before calling `settings.set`.
- On a `speech.engine` change away from `piper`, call `self.piper.unload()`. On a `speech.piper_voice_dir` change, call `self.piper.set_voice_dir(...)`.
- In `__main__`, when `piper_available()`, build `PiperEngine(voice_dir, threads=settings speech.piper_threads)`, otherwise None. Pass it to `Daemon`.
- Tests use two FakeEngines standing in for Kokoro and Piper (give the Piper fake `has_voice`, `unload` and `set_voice_dir`), covering:
  - `speech.engine` switched mid-queue: the current utterance's `started.engine` is kokoro, and the next one's is piper (Review Focus 4);
  - replay after a switch reuses the cache (no new synthesize calls on either fake);
  - an unmapped language under piper → `engine: kokoro`;
  - Kokoro unloaded plus an unmapped language → declined with the reason;
  - Kokoro unloaded, piper chosen → spoken;
  - refusal when piper is unavailable, with the value unchanged (Review Focus 2);
  - the voice file removed between utterances → kokoro (Review Focus 1);
  - `PiperVoiceError` → kokoro plus a stderr line, and the next utterance tries piper again (Review Focus 3);
  - `status.engine.piper` and `status.languages.supported`;
  - `unload()` called on switching away.
- [ ] Watch the tests fail, implement, then run the full gate.
- [ ] Commit: "Speak through Piper when chosen, and through Kokoro for what it lacks".

### Task 5: renders

**Files:**
- Modify: `src/speakd/render.py`, and the daemon's render wiring
- Test: `tests/test_render.py` (add to it)

**Interfaces:**
- Consumes: `choose_engine` (Task 3); a second `PiperEngine` built with `render.piper_threads`, owned by the daemon as `self.render_piper`.
- Produces: `Part.engine: str` and `Part.voice: str`, fixed at submission and persisted in the manifest. `RenderQueue(engines: Mapping[str, Synthesizer], ...)` replaces the single `engine` (keep a compatible default for existing tests: `engines={"kokoro": engine}`).

- At `render` submission, the daemon resolves each part's language and runs `choose_engine`, recording the result on the part. A `Declined` refuses the whole render up front with its reason.
- Before each sentence, the worker looks up `engines[part.engine]`. It still yields to live speech and still takes `synth_lock`.
- A manifest written before this change (without `engine` or `voice`) loads as kokoro with the job's old voice.
- Tests:
  - a two-part render where part 1 is en (piper) and part 2 is an unmapped language (kokoro): each fake engine gets only its own part's sentences;
  - the manifest round-trips `engine` and `voice`;
  - resume after `speech.engine` changed keeps the recorded engines (Review Focus 5);
  - an old manifest without the fields loads;
  - the render PiperEngine was built with `render.piper_threads`.
- [ ] Watch the tests fail, implement, then run the gate.
- [ ] Commit: "Record each render part's engine at submission, and render Piper on every core".

### Task 6: the window and Zotero

**Files:**
- Modify: `clients/gui/pinned.html`, `clients/gui/shared/speakd-source.js`
- Modify: the Zotero plugin's language cache (added by phase 3; find where `status.languages.supported` is cached)
- Test: `clients/gui/test-browser/settings_check.py` (add to it), and a vitest test in `clients/zotero/test/`

Steps:
- **Window:** the rtf metric's sub-line (`#rtf-sub`) shows the engine of the sentence being spoken ("kokoro" or "piper"), from `started.engine` or `position.engine` when present. Otherwise it keeps what it shows today.
- **SimulatedSource:** gains `speech.engine` in its schema, and puts `engine` on its `started` events.
- **Browser check:** after setting `speech.engine` to piper in the sim and starting playback, `#rtf-sub` contains "piper".
- **Zotero:** on a `setting` event whose key is `speech.engine`, drop the cached supported-language list so the next read start asks `status` again. vitest: the cache is invalidated by that event and not by other keys.
- [ ] Run the browser check, `npm test` and `npm run build` in `clients/zotero`, and `cargo build`.
- [ ] Commit: "Show which engine is speaking, and let Zotero notice an engine switch".

### Task 7: docs, the ear check, and the gate

- A README section "Piper", in the file's voice, covering:
  - install with `uv sync --extra piper` or `pip install 'speakd[piper]'`;
  - download voices with `python -m piper.download_voices en_US-ryan-medium de_DE-thorsten-medium --data-dir ~/.local/share/piper-voices`;
  - switch with `speakctl set speech.engine piper`, or with the window's Settings;
  - the Kokoro fallback and the unload interaction;
  - the two thread settings;
  - the GPL-3.0 note: an optional extra, never bundled;
  - the measured CPU table from the spec.
- **Ear check:** with a real voice, render the comparison passage ("…In 1994, Habermas replied in pp. 34–38.") through `PiperEngine` to `~/Music/speakd-tts-compare/piper-medium-speakd.wav`. Report the path so the person can confirm the year and range sound right. If they don't, fix Task 1's rule before merging.
- Full gate: `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy src tests`, `uv run pytest -q`, `cargo build` in `clients/gui/app/src-tauri`, and `npm test && npm run build` in `clients/zotero`.
- Update `docs/HANDOFF.md`: Piper is built; automatic switching on battery is still deferred.
- [ ] Commit: "Document Piper, and how to switch to it".
