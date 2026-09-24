# Languages — Implementation Plan (phase 2 of settings-and-languages)

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:executing-plans. TDD throughout.

**Goal:** Each utterance is spoken in its own language, with that language's voice. The language comes from the client, the channel, detection or the default, in that order. Languages Kokoro cannot speak follow `speech.unsupported_language`.

**Spec:** `docs/superpowers/specs/2026-09-24-settings-and-languages-design.md` §2 (binding). The settings core (phase 1, `src/speakd/settings/`) is merged and provides `Settings`, declarations, `settings.get(key)` and `on_change`.

**Facts verified locally:**
- kokoro 0.9.4's `KPipeline(lang_code, repo_id=None, model: KModel|bool=True, trf=False, en_callable=None, device=None)` takes `model=` to share one loaded model across pipelines.
- `misaki.espeak` imports, so es, fr, it, pt-br and hi work.
- `misaki.ja` fails on the missing `pyopenjtalk`, and `misaki.zh` fails on the missing `ordered_set`, so those two languages count as unsupported unless their imports succeed.

## Global Constraints

- **Codes:** `en en-gb es fr hi it pt-br ja zh`.
  - Kokoro lang codes: `en→a`, `en-gb→b`, `es→e`, `fr→f`, `hi→h`, `it→i`, `pt-br→p`, `ja→j`, `zh→z`.
  - Normalise on input: lower-case, `_`→`-`; `pt`→`pt-br`; `en-us`→`en`; `zh-cn`/`zh-hans`→`zh`; any other `xx-yy`→`xx` when `xx` is supported, else unsupported.
- **Resolution per utterance:** payload `lang` > channel language > detection (if `speech.detect_language` and the detector is available and the text has at least 20 letters) > `speech.default_language`.
- **Detection:**
  - `lingua-language-detector` in a new extra, `lang`, imported lazily on first use. `tests/test_import_cost.py` must stay green.
  - The detector is built once for the supported languages plus de, nl, sv, nb, da and pl, and its result is mapped to our codes: lingua's ENGLISH→`en`, PORTUGUESE→`pt-br`, CHINESE→`zh`, and the rest by ISO 639-1.
  - Without the extra, detection is off, with one stderr line at startup.
- **Settings declared by core in this phase:**
  - `speech.voices` (voice_map), with defaults `en:af_heart, en-gb:bf_emma, es:ef_dora, fr:ff_siwis, hi:hf_alpha, it:if_sara, pt-br:pf_dora, ja:jf_alpha, zh:zf_xiaobei`.
  - `speech.unsupported_language`, a choice of `default` or `decline`, defaulting to `default`.
  - `speech.default_language` and `speech.detect_language` already exist from phase 1; use them.
- **Voice choice:** the profile's voice is used when its first letter maps to the resolved language (`a`→en, `b`→en-gb, `e`→es, `f`→fr, `h`→hi, `i`→it, `p`→pt-br, `j`→ja, `z`→zh). Otherwise `speech.voices[lang]`, falling back to `speech.voices[default]`.

## Review Focus

- A payload `lang` the daemon cannot parse (`"klingon"`) should be treated as unsupported, not crash. Tested in Task 2.
- A detector that throws should mean falling back to the channel or default language, not losing the utterance. Tested in Task 3.
- Changing `speech.voices` while speaking should apply from the next utterance without a restart. Tested in Task 5.
- The first pipeline for a new language is created on the synthesis thread; creation failing (a missing G2P) should fall back per `unsupported_language`, not kill the worker. Tested in Task 4.
- `set_language auto` should clear the channel's language. Tested in Task 2.

### Task 1: languages module (`src/speakd/languages.py`)
- Provide `SUPPORTED`, `KOKORO_CODES`, `normalise(code) -> str | None`, `voice_language(voice) -> str | None`, and `supported_languages(importable: Callable[[str], bool]) -> list[str]`. The last drops ja and zh unless their G2P imports succeed, and is injectable for tests.
- Tests: every normalisation rule; `voice_language` for each prefix; the ja/zh availability switch.

### Task 2: channel language and the payload
- `Channel.lang: str | None = None`.
- The verb `set_language {lang}` stores the normalised code, or None for `auto`/`""`. It publishes `mode`-style `language {lang}` on the channel and appears in `status` channels. The HTTP `VERBS` gains `set_language`.
- `speakctl lang <code|auto> --source …`.
- `enqueue` accepts `lang` (a string), normalises it, and stores it on the `_Job`.
- Tests: the verb, status, the CLI (fake call), an unparseable code being kept as unsupported (a sentinel such as `"und"`), and `auto` clearing it.

### Task 3: detection (`src/speakd/detect.py`)
- `Detector` has `available` and `detect(text) -> str | None`. It is lazy, returns None under 20 letters, returns None on any exception (logged once), and maps unsupported results to their own code (`de`) so the caller can treat them as unsupported.
- `pyproject.toml` gets the extra `lang = ["lingua-language-detector"]`.
- Tests: fake the lingua module through `sys.modules`, covering the mapping, the short-text rule, an exception giving None, and unavailability.

### Task 4: engine
- `Synthesizer.synthesize(text, voice, speed, lang="en")`. FakeEngine accepts `lang` and records it for tests; LazyEngine passes it through.
- `KokoroEngine`:
  - Loads one `KModel`, then creates `KPipeline(lang_code=KOKORO_CODES[lang], model=shared_model, device=...)` lazily per language, cached in a dict under a lock.
  - Keeps the device selection logic.
  - `supported_languages()` uses `languages.supported_languages`.
  - A pipeline that fails to create raises `UnsupportedLanguage(lang)`.
- The pipeline (`src/speakd/pipeline.py`) passes `lang` through to `engine.synthesize`: `speak(..., lang="en")`.
- Tests: a fake `kokoro` module (the pattern in `tests/test_engine_device.py`) proves one model is shared, pipelines are created once per language and never per call, and creation failure raises `UnsupportedLanguage`. The existing tests pass `lang` defaults.

### Task 5: daemon resolution, voices and unsupported
- In `_speak`: resolve the language (payload > channel > detection over the prepared text > default); then choose the voice; then, if the language is not in `engine.supported_languages()` (or FakeEngine's configured set):
  - `default`: use the default language and its voice, and publish `language {requested, used, reason:"unsupported"}`.
  - `decline`: publish `declined {reason: "no voice for <code>"}` and a `finished` with cancelled False and aborted True, and speak nothing.
- `started` gains `lang`. `status` gains `languages: {supported, default, detect}`.
- Settings are read at each utterance, so changes apply to the next one.
- Tests with FakeEngine (supported languages configurable):
  - each step of the resolution order
  - voice choice (the profile's voice kept when its language matches)
  - both unsupported modes
  - `started.lang`
  - `status.languages`
  - `speech.voices` changed between two utterances takes effect
  - UnsupportedLanguage raised mid-synthesis falls back

### Task 6: real-engine test and docs
- `tests/test_kokoro_languages.py` (skipped without the kokoro extra): French text with lang `fr` and voice `ff_siwis` produces audio, and `supported_languages()` includes `fr` and excludes `ja` here.
- A README section, "Languages", explaining the resolution, the voices setting, `speakctl lang`, `speakd[lang]` for detection, and the unsupported-language setting.
- Gate: ruff, format, mypy, pytest, all green. Also run with the lang extra installed: `uv sync --extra kokoro --extra lang` in the worktree, then the tests.
