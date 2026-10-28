# A second speech engine: kokoro-onnx measured against the torch engine

Branch `feat/onnx-engine`. Nothing is wired into the daemon and the default is
unchanged; `KokoroEngine` is still what `__main__.py` and `cli.py` construct.

Measured 2026-09-14 on the user's machine: 8 cores, load average 0.13 at the
start of the run, `speakd.service` active but idle. Python 3.12.12.

---

## 1. What was added

| Path | What it is |
|---|---|
| `src/speakd/synth/kokoro_onnx_engine.py` | `KokoroOnnxEngine`, same `Synthesizer` protocol |
| `tests/test_kokoro_onnx_engine.py` | 16 tests, none needing the extra or an audio device |
| `tools/bench_engines.py` | the benchmark; not shipped, not imported by the package |
| `pyproject.toml` | `kokoro-onnx` optional extra + mypy override |

`kokoro_onnx` is imported inside `__init__`, never at module import time, so
`import speakd.synth` and `from speakd.synth.kokoro_onnx_engine import ...` both
work in the default environment. Verified against the main checkout's venv,
which does not have the extra.

Both failure paths print one actionable line:

```
speakd: the kokoro-onnx extra is not installed (uv pip install 'speakd[kokoro-onnx]')
speakd: kokoro-onnx needs kokoro-v1.0.onnx and voices-v1.0.bin in
  /home/severingartland/.cache/speakd/kokoro-onnx -- download from
  https://github.com/thewh1teagle/kokoro-onnx/releases/tag/model-files-v1.0
```

(wrapped here for the page; it is a single line in the code, and a test asserts
`message.count("\n") == 0`.)

Model files live in `$XDG_CACHE_HOME/speakd/kokoro-onnx`, defaulting to
`~/.cache/speakd/kokoro-onnx`. They are 353 MB and are not in the repository.

---

## 2. The environment constraint, and why there are now two venvs

**The production venv was never touched, and `uv sync` was never run in any form.**

The check before any install:

```
$ uv run --no-sync python -c "import torch, kokoro, sounddevice; print(torch.__version__, torch.version.cuda)"
2.14.0+cpu None          # numpy 2.5.3
```

`uv pip install --dry-run kokoro-onnx==0.6.1` reported five additions and no
removals — flatbuffers, kokoro-onnx, onnxruntime, phonemizer, protobuf — which
looks safe. **It is not.** `misaki[en]` requires `phonemizer-fork` 3.3.2, and
`kokoro-onnx` requires `phonemizer` 3.4.0. These are two distributions that both
own the top-level `phonemizer/` package directory:

```
$ cat .../phonemizer_fork-3.3.2.dist-info/RECORD | cut -d/ -f1 | sort -u
phonemizer
phonemizer_fork-3.3.2.dist-info
$ grep -l phonemizer .../misaki/*.py
.../misaki/espeak.py      # EspeakFallback, misaki's last resort for English
```

uv does not report this as a removal because the distribution *names* differ; it
would simply overwrite the fork's files in place, leaving the dist-info behind
and silently changing the phonemiser the torch engine falls back to for
out-of-dictionary words. Proper nouns like "Husserl" and "Stiegler" are exactly
what that fallback handles.

So, per the instruction to build separately if an install would disturb the
torch stack, the ONNX side went into an isolated venv at
`/home/severingartland/programing_linux/speakd-bench-venv`, made by copying the
main venv (a real copy — link counts verified at 1, no hardlink sharing) and
installing kokoro-onnx into the copy only.

The check after the install:

```
$ uv run --no-sync python -c "import torch, kokoro, sounddevice, numpy; ..."
2.14.0+cpu None numpy 2.5.3        # unchanged
$ uv pip list | grep -iE "phonem|onnx"
phonemizer-fork            3.3.2   # still the fork, no onnx packages
```

The bench venv now lists **both** `phonemizer 3.4.0` and `phonemizer-fork 3.3.2`,
which is the collision made visible.

### Is the torch engine still itself in the bench venv?

The benchmark had to run both engines in one process, so this needed proving,
not assuming. Result: the torch engine produced **identical sample counts for
all nine test texts** across the pristine main venv (twice) and the bench venv.

A side finding worth knowing: the torch engine is **not bit-deterministic**. Two
runs in the *same untouched venv* give different SHA-256 over the audio and RMS
differing in the 4th decimal. So hashes cannot be used to compare engines here;
duration can, and duration is what a phonemisation change would move.

Identical durations are strong evidence, not proof, that phonemizer 3.4.0 left
misaki's output alone on these texts. It is one more reason not to install both
extras into one environment in production.

---

## 3. Real-time factor

RTF = synthesis wall time / duration of the audio produced. Lower is better;
below 1.0 means synthesis outruns playback.

One warmup call per engine was made and discarded, as in the original
measurement. The figures below are medians of 3 rounds with both engines loaded
into one process, alternating over the same texts.

| Case | chars | torch synth | torch RTF | onnx synth | onnx RTF |
|---|---:|---:|---:|---:|---:|
| `"Yes."` | 4 | 0.558 s | 0.45 | 0.518 s | 0.78 |
| `"The answer is four."` | 19 | 0.732 s | 0.44 | 0.710 s | 0.64 |
| sentence | 77 | 1.931 s | 0.41 | 2.084 s | 0.51 |
| pair | 146 | 3.784 s | 0.43 | 3.841 s | 0.48 |
| trio | 213 | 5.787 s | 0.45 | 5.831 s | 0.47 |

Run-to-run spread was small (e.g. len213 torch 5.522–5.899 s, onnx 5.441–6.066 s).

### The RTF column is misleading, and the synth column is the honest one

**Synthesis time is essentially identical between the two engines** — within 7%
either way, with ONNX slightly faster on short input and slightly slower on long.
ONNX's RTF looks worse only because it *produces less audio for the same words*:

| Case | torch audio | onnx audio |
|---|---:|---:|
| len4 | 1.25 s | 0.66 s |
| len19 | 1.68 s | 1.11 s |
| len77 | 4.70 s | 4.12 s |
| len146 | 8.80 s | 8.09 s |
| len213 | 12.90 s | 12.35 s |

The gap is almost entirely silence — see §6. Dividing by a longer clip flatters
the torch engine on a metric that is supposed to describe throughput.

### These numbers do not reproduce the earlier torch baseline

The figures given for today's torch run were RTF 0.85 / 0.77 / 0.68 / 0.88 /
0.68, fixed cost near 1 s per call, model load 8.9 s. I measured 0.45 / 0.44 /
0.41 / 0.43 / 0.45, fixed cost ~0.56 s, model load 6.16 s — roughly twice as
fast throughout. I cannot explain the difference from here and did not try to
force agreement. Two things are worth noting:

- Only the first two texts were specified exactly. For 77, 146 and 213
  characters I had to write my own sentences to those exact lengths (they nest:
  77, then 77+1+68, then 146+1+66). Different prose at the same length produces
  different speech density and therefore different RTF, which may be most of it.
  The exact strings are in `tools/bench_engines.py`.
- The earlier figures are non-monotonic (0.68 at 77 chars, 0.88 at 146, 0.68 at
  213); mine are flat at 0.41–0.45. A flat RTF is what a fixed per-call cost
  amortising over longer input should look like.

The **head-to-head is still valid** — both engines measured in one process, same
inputs, same machine state, same minute. The comparison to the earlier absolute
numbers is not.

---

## 4. Resident memory

From `/proc/self/statm` field 2 × page size, in a fresh process per engine
(a process holding both tells you nothing about either).

| | at start | after load | after synthesis |
|---|---:|---:|---:|
| torch | 34 MB | 1092 MB | 1406 MB |
| onnx | 34 MB | 457 MB | 692 MB |

ONNX is **714 MB lighter after synthesis**, roughly half the footprint.

## 5. Cold start — what a daemon restart costs

Fresh process, time until the first audio exists.

| | model load | first synthesis | total to first audio |
|---|---:|---:|---:|
| torch | 6.16 s | 1.40 s | **7.56 s** |
| onnx | 0.96 s | 1.31 s | **2.27 s** |

The load step is where it all is: 6.16 s against 0.96 s, a 6.4× difference. The
first synthesis costs both engines about the same.

## 6. Installed size

Attributed by walking each engine's transitive requirement closure and summing
file sizes from each distribution's `RECORD`.

| | packages | size |
|---|---:|---:|
| torch-only deps (incl. `misaki[en]`: spacy, phonemizer-fork, …) | 84 | 1175.3 MB |
| onnx-only deps (onnxruntime, kokoro-onnx, phonemizer, flatbuffers, protobuf) | 5 | 62.6 MB |
| shared (numpy, packaging, typing-extensions) | 3 | 54.4 MB |
| torch model weights (HF cache, Kokoro-82M) | | 625.1 MB |
| onnx model files (`~/.cache/speakd/kokoro-onnx`) | | 337.4 MB |

Totals: torch ≈ **1.80 GB**, onnx ≈ **454 MB**. Adding ONNX alongside an
existing torch install costs ~64 MB of packages (confirmed by `du` on the
bench venv) plus 337 MB of model files.

The 84-package torch figure includes the `misaki[en]` extras (spacy and its
dependency tree, 241.8 MB across 45 packages) which a naive closure walk misses,
because they sit behind an `extra ==` marker.

---

## 7. Does it sound the same?

**WAV files:** `/home/severingartland/programing_linux/speakd-worktrees/onnx/.superpowers/wav-comparison/`

Eight files, `af_heart` at speed 1.1, named `<case>__<engine>.wav`:

```
01-ordinary__torch.wav                 01-ordinary__onnx.wav
02-abbreviations-symbols__torch.wav    02-abbreviations-symbols__onnx.wav
03-proper-nouns__torch.wav             03-proper-nouns__onnx.wav
04-citation-key__torch.wav             04-citation-key__onnx.wav
```

16-bit PCM so they play anywhere. `summary.json` beside them holds the
measurements. The `.wav` files are gitignored as regenerable artifacts;
`tools/bench_engines.py wavs --out <dir>` recreates them.

**I have not listened to these.** Everything below is measured from the
waveforms and from the two engines' phoneme strings.

### 7.1 Silence padding — the clearest difference

| Case | torch lead / trail | onnx lead / trail |
|---|---|---|
| ordinary | 0.280 s / 0.486 s | 0.026 s / 0.099 s |
| abbreviations | 0.288 s / 0.463 s | 0.031 s / 0.081 s |
| proper nouns | 0.284 s / 0.446 s | 0.028 s / 0.071 s |
| citation key | 0.265 s / 0.498 s | 0.046 s / 0.102 s |

Checked at four thresholds (absolute 0.001 and 0.01, and 1% and 5% of peak) and
the figures barely move, so this is real and not an artefact of where the
threshold sits.

**The torch engine puts roughly 0.75 s of silence around every segment** —
~0.28 s before and ~0.47 s after — and the ONNX engine trims to about 0.13 s
total. That is `Kokoro.create`'s `trim=True` default, which I deliberately left
on because it is what an adopter would get.

This matters more than any RTF number. speakd synthesises segment by segment,
so on the torch path that padding is paid *per segment*: a five-segment
utterance carries about 3.7 s of dead air inside it. It is also most of the
"ONNX produces less audio" effect in §3.

The flip side: ONNX segments will butt up against each other with almost no gap,
which may sound rushed at segment boundaries. Which is preferable is a listening
judgement, not a measurement.

### 7.2 Loudness

ONNX is louder by a consistent **~3.9 dB** (RMS -22.3…-22.6 dBFS against torch's
-26.2…-26.5), with peaks 0.51–0.67 against 0.34–0.47. Switching engines without
adjusting volume would be immediately noticeable. No clipping in either.

### 7.3 Pronunciation

The two phonemise differently by construction — misaki with a dictionary and an
espeak-ng fallback, kokoro-onnx with espeak-ng for everything. But comparing raw
phoneme strings overstates it badly, because `misaki.espeak.EspeakFallback`
*rewrites* espeak's IPA into its own convention before the model sees it
(`aɪ`→`I`, `eɪ`→`A`, `ɚ`→`əɹ`, `ɜː`→`ɜɹ`, `ɾ`→`T`, length marks dropped).

`tools/bench_engines.py phonemes` applies that same rewrite before diffing, so
what follows is what the two would genuinely say differently. Full output in
`.superpowers/phoneme-comparison.txt`.

**Identical after normalisation:** `"Yes."` — and, importantly, **every proper
noun tested**: Kant's, Husserl's, Stiegler, Technics, deduction. The worry that
philosophical proper nouns would degrade is not borne out on this sample.

**Genuine differences that matter:**

1. **Years are read wrong by ONNX.** This is the serious one.

   | text | misaki (torch) | espeak (onnx) |
   |---|---|---|
   | `1994` | nineteen ninety four | nineteen **hundred** ninety four |
   | `1787` | seventeen eighty seven | **one thousand seven hundred** eighty seven |

   For a vault full of citation keys — "Stiegler 1994", "ca. 1787" — this is a
   regression on exactly the content this user reads aloud most.

2. **Hyphens are spoken as "dash" by ONNX.** `pp. 34-38` becomes "pp thirty four
   **dash** thirty eight"; misaki renders it "thirty four thirty eight". Page
   ranges are common in the same citations.

3. **Unstressed initial vowels are not reduced by espeak**, where misaki reduces
   them: example `ɪɡzæmpəl`/`ɛɡzæmpəl`, experience `ɪkspɪɹiəns`/`ɛkspiəɹiəns`,
   empirical `ɪmpɪɹəkᵊl`/`ɛmpɪɹɪkəl`, edition `ədɪʃən`/`ᵻdɪʃən`. A systematic
   accent difference across "ex-/em-/en-" words rather than an error.

4. **`cannot`**: misaki `kənɑt` ("kuh-not"), espeak `kænɑt` ("can-not").

5. **espeak merges function words across boundaries**: `as it is` → `æzɪtɪz`,
   `in the` → `ɪnðə`. Same segments, different rhythm and stress.

6. Minor: espeak inserts a linking r in "answer is" (`ænsɚɹ ɪz`); syllabic
   consonants notated `ᵊl`/`ᵊn` by misaki appear as `əl`/`ən`.

**The pronunciation table still works on both.** `§12` becomes "section twelve"
on both engines, and the table's `e.g.` → "for example" rewrite fires before the
engine and fixes that case for both. The table is not invalidated by the switch —
but it has no rule for bare four-digit years or for hyphenated ranges, because
misaki never needed one. Those rules would have to be written to adopt ONNX.

---

## 8. Test discipline

16 tests, all passing in both the extra-less main venv and the bench venv.

Every one was mutation-checked: the line it pins was removed or altered, the
test was confirmed to fail, and the source restored — with
`PYTHONDONTWRITEBYTECODE=1` and `__pycache__` cleared before each run, since
CPython validates a cached `.pyc` against source mtime in whole seconds plus
size. 16 mutations, all caught, every test accounted for as the catcher of at
least one.

One test was rewritten as a result. `test_audio_of_length_zero_gives_empty_float32`
originally fed an empty float32 array, which `np.asarray` handles identically
whether or not the cast is present — it pinned nothing. It now feeds an empty
*list* (the realistic "model yielded nothing" shape) and asserts an `ndarray`
comes back, which does pin the cast. One line of production code was deleted for
the same reason: `len(audio) == 0` in the empty guard was unreachable behaviour,
since `np.asarray` already returns empty float32 for empty input.

---

## 9. Gate results

This worktree has no `.venv`. Gates were run with `PYTHONPATH` set to this
worktree's `src` against the main checkout's interpreter, which puts the
worktree's source ahead of the editable install (verified: `speakd.__file__`
resolves into the worktree). Each gate its own command, nothing piped:

```
cd /home/severingartland/programing_linux/speakd-worktrees/onnx
export PYTHONPATH=$PWD/src
MAIN=/home/severingartland/programing_linux/speakd/.venv/bin/python

$MAIN -m pytest -q                     # 6 failed, 569 passed
$MAIN -m ruff check .                  # All checks passed!
$MAIN -m ruff format --check .         # 81 files already formatted
$MAIN -m mypy src tests                # Success: no issues found in 69 source files
```

The 6 failures are the known pre-existing ones — 2 in `test_cc_hook.py`, 4 in
`test_cc_plugin_manifest.py` — which shell out to `<repo>/.venv/bin/speakd-claude-hook`
and fail in any worktree without its own `.venv`. Confirmed against the untouched
base at `13ee040` before starting: **6 failed, 553 passed**, the same six names.
569 − 553 = 16, the new tests, and no previously-passing test broke.

`tools/` is outside `mypy src tests` by the project's existing convention; it is
covered by `ruff check .` and `ruff format --check .`.

---

## 10. Recommendation

**Do not switch the default. Keep `KokoroEngine` as it is.**

The case for switching is real and it is about resources, not speed: cold start
7.56 s → 2.27 s, resident memory 1406 MB → 692 MB, install 1.8 GB → 454 MB. For
a daemon that restarts with a user session, a 5.3-second-faster start and 700 MB
back is worth wanting.

What decides it against, for now, is §7.3 item 1. This vault is full of citation
keys, and the ONNX engine says "Stiegler nineteen **hundred** ninety four" and
"ca. **one thousand seven hundred** eighty seven". Page ranges gain a spoken
"dash". The pronunciation table was ported from `claude-code-narrator` precisely
to get this class of thing right, and it has no rule for bare years or hyphenated
ranges because misaki never needed one. Switching today trades a correctness
property this project already paid for against a resource win.

Speed is not part of the decision either way: synthesis times are within 7%.
Anyone reading the RTF column as a reason to stay on torch is reading an artefact
of silence padding.

**What would change the recommendation**, roughly in order of value:

1. Two rules in `speakd.transforms.pronunciation` — four-digit years to
   "nineteen ninety four" form, and `\d+-\d+` ranges to "34 to 38". Both are
   small, both are testable without either engine, and both would improve the
   torch path too (the table runs before whichever engine is in use). This is
   the cheapest path and it removes the blocking objection.
2. A listen to the eight WAV files, which is the part I cannot do. Specifically:
   whether losing the torch engine's ~0.75 s per-segment padding sounds better
   (less dead air) or worse (segments running together), and whether the
   unreduced "ex-/em-" vowels sound wrong or merely different.
3. A decision on the ~3.9 dB level difference, which needs a gain adjustment
   somewhere if the engine changes.

Until at least (1) and (2) are done, the ONNX engine is worth having as a
measured, tested alternative that is not the default — which is what this branch
leaves in place.

### One thing to carry forward regardless

The two extras cannot safely share an environment. `kokoro` and `kokoro-onnx`
disagree about which distribution owns `phonemizer/`, and installing both lets
whichever landed last decide the torch engine's espeak fallback — silently, with
no resolver error. The extras are documented as mutually exclusive in
`pyproject.toml`. If the daemon ever offers an engine choice at runtime, it
should be one extra per environment, not both.

**`uv sync` must still never be run in this project's main checkout** — it prunes
kokoro, sounddevice, scipy and torch. Everything here used `uv run --no-sync`,
`uv pip install --python <bench venv>`, or the main venv's interpreter directly.
