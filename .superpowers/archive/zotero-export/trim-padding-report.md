# Trimming Kokoro's per-segment padding

Branch `feat/trim-padding`, worktree `/home/severingartland/programing_linux/speakd-worktrees/trim`.

## How everything here was run

This worktree has no `.venv`. Nothing was installed and `uv sync` was never run in any
form. Every command ran the main checkout's interpreter against this worktree's source:

```
cd /home/severingartland/programing_linux/speakd-worktrees/trim
PYTHONPATH=$PWD/src /home/severingartland/programing_linux/speakd/.venv/bin/python -m <pytest|ruff|mypy> ...
```

`torch 2.14.0+cpu` / `cuda None` verified importing before the work started and again at
the end — unchanged, because nothing was installed.

## Baseline

Confirmed against the untouched worktree at `13ee040` before any edit:

```
6 failed, 553 passed, 4 warnings in 48.64s
```

The six are `test_cc_hook.py` (2) and `test_cc_plugin_manifest.py` (4). All six assert on
`<worktree>/.venv/bin/speakd-claude-hook`, which only exists in a worktree that has its own
venv. Pre-existing, not chased.

## What was measured, and why the brief's numbers move

A corpus of **135 clips** was synthesised and cached: 27 texts (one-word replies, soft
fricative onsets, breathy onsets, plosive onsets, a long sentence, a heading+body unit)
across five voice/speed combinations (`af_heart` at 1.0/1.1/1.5, `af_bella` 1.1,
`am_michael` 1.1).

**The pad is not digital silence.** Not one clip had a single exactly-zero sample. Kokoro
emits dither around 1e-6 at both ends, so a "trim the zeros" implementation would have
found nothing to trim.

**Most of the reported 0.28 s / 0.47 s is not inserted silence.** Silence length depends
heavily on the threshold it is measured at, and the two ends behave differently:

| threshold | lead min | lead median | lead max | trail min | trail median | trail max |
|---|---|---|---|---|---|---|
| 1e-6 | 0.0000 | 0.2060 | 0.2125 | 0.1623 | 0.1780 | 0.1935 |
| 5e-6 | 0.1541 | 0.2071 | 0.2371 | 0.1673 | 0.1799 | 0.2430 |
| 1e-5 | 0.1546 | 0.2075 | 0.2521 | 0.1678 | 0.1810 | 0.3618 |
| 1e-4 | 0.1835 | 0.2321 | 0.3879 | 0.1713 | 0.1964 | 0.4183 |
| 1e-3 | 0.2022 | 0.3150 | 0.4095 | 0.2246 | 0.4287 | 0.5432 |

Down at the noise floor the numbers are tight and content-independent — lead clusters at
0.207 s and trail at 0.180 s regardless of what is being said. Up at 1e-3 they spread
wildly with the text (trail ranges 0.22–0.54 s). That spread is the giveaway: it is not
padding, it is **the decay of the final syllable**. The brief's 0.47 s figure is a
loud-threshold measurement, and roughly 0.25 s of it is real speech fading out.

**So the genuinely inserted pad is ~0.21 s leading and ~0.18 s trailing**, and a trim
aimed at 0.47 s of trailing "silence" would audibly clip the last word of every sentence.

## The threshold: 5e-6

Chosen from two non-overlapping measured populations, not from intuition.

- **Loudest sample found anywhere inside a pad**: `1.9893e-06`. Measured over a *fixed*
  window (the first 0.10 s of every clip), which is unconditionally inside the pad since
  the earliest onset seen anywhere was 0.154 s. This matters: an earlier attempt measured
  the floor *below the detected onset*, which is circular — that can never exceed the
  threshold it is derived from, and it reported a meaningless 1.0x margin.
- **Quietest genuine speech onset**: `1.0156e-05`, the soft fricative opening
  *"Should he have known?"* at `af_heart`/1.0 — exactly the soft-onset case at risk.

Usable band is therefore `(1.99e-6, 1.02e-5)`, geometric centre `4.50e-6`.

**`SILENCE_THRESHOLD = 5e-6`** sits essentially at that centre:

- **2.51x above** the loudest padding sample
- **2.03x below** the softest speech onset
- `-106.0 dBFS`, which is **9.7 dB below the 16-bit half-LSB** (`1.526e-5`). Any sample at
  or under this threshold rounds to zero in 16-bit PCM, so nothing discarded here could
  have survived quantisation, let alone been heard.

The failure modes are asymmetric and the bias is deliberately toward under-trimming: a
threshold set too *low* just means trimming less than possible (harmless), where one set
too *high* eats a syllable.

### The residual doubles as the clipping margin

The 30 ms leading residual is not decoration — it is sized so a wrong threshold still
costs nothing. Distance from the 5e-6 crossing to a higher crossing, across all 135 clips:

| if the true onset were at | median | p90 | **max** |
|---|---|---|---|
| 1e-5 (2x too high) | 0.4 ms | 3.9 ms | 22.5 ms |
| 3e-5 (6x too high) | 2.1 ms | 17.6 ms | **24.9 ms** |
| 1e-4 (20x too high) | 24.8 ms | 121.8 ms | 180.8 ms |

A 30 ms residual fully absorbs a **six-fold** threshold error on every clip in the corpus.
Even if 5e-6 were badly wrong, no speech would be lost.

Tightest case overall: the earliest onset seen was 154.1 ms, against a 30 ms residual —
124 ms of pad still stands between the cut and the speech. Nothing clamps.

## The residuals: 0.03 s leading, 0.10 s trailing

They differ because the two ends are not worth the same thing.

**Leading (0.03 s)** is pure cost. The pause a listener hears between two sentences is
delivered by the *previous* segment's trailing residual; a leading residual adds nothing
perceptual and is charged straight to time-to-first-audio. So it is sized purely as the
safety margin above.

**Trailing (0.10 s)** is the audible one, and is deliberately not zero — sentences run
together otherwise. 0.10 s is the short end of a natural sentence-final pause, and it sits
*on top of* the final syllable's decay, which the trim keeps. The perceived gap between
sentences is therefore longer than 0.13 s: measured at an audible threshold it falls from
about 0.75 s to about 0.49 s, a ~35% reduction, with the remainder being real decay and
real onset ramp — which is what natural speech sounds like.

## Before / after, real engine, `af_heart` @ 1.1

| clip | before (n) | after (n) | before (s) | after (s) | cut (s) | cut % | lead cut | trail cut |
|---|---|---|---|---|---|---|---|---|
| `Yes.` | 30000 | 23434 | 1.250 | 0.976 | 0.274 | 21.9% | 0.182 | 0.091 |
| `Okay.` | 31800 | 25111 | 1.325 | 1.046 | 0.279 | 21.0% | 0.182 | 0.097 |
| `Should he have known?` | 37800 | 31456 | 1.575 | 1.311 | 0.264 | 16.8% | 0.178 | 0.086 |
| `Shhh, listen carefully now.` | 65400 | 58944 | 2.725 | 2.456 | 0.269 | 9.9% | 0.179 | 0.090 |
| `However, the situation is more subtle…` | 75000 | 68626 | 3.125 | 2.859 | 0.266 | 8.5% | 0.178 | 0.087 |
| `Hesitantly, she whispered his name.` | 58200 | 51838 | 2.425 | 2.160 | 0.265 | 10.9% | 0.179 | 0.086 |
| `The streaming pipeline plays one segment…` | 116400 | 109924 | 4.850 | 4.580 | 0.270 | 5.6% | 0.178 | 0.091 |

**17.275 s → 15.389 s over 7 segments — 1.886 s removed, 10.9%, a flat 0.269 s per
segment** regardless of length. `"Yes."` drops from 1.250 s to 0.976 s.

**Five-sentence utterance: 10.550 s → 9.214 s, 1.336 s of inserted silence removed.**

### Time to first audio

Realistic first segment (83 chars, the streaming-pipeline sentence):

```
synthesis (best of 3)     1.911 s
pad before first sound    0.208 s  ->  0.030 s after trim
TTFA                      2.120 s  ->  1.941 s   (-0.178 s, -8.4%)
```

The 0.178 s is a constant saving on every segment, so it is worth far more on short ones:
on `"Yes."` the pad was 15% of the entire clip.

## Proof no speech was lost

Asserted programmatically for all seven clips above (the script raises otherwise):

1. **Every removed sample is at or below the threshold** — the discarded leading and
   trailing regions were re-checked against `SILENCE_THRESHOLD` after the cut.
2. **The speech span is bit-identical**: `np.array_equal(before[first_loud:last_loud+1],
   after[first_loud':last_loud'+1])` for every clip. Not "similar" — the same samples.
3. **The first and last above-threshold samples survive with the full residual as
   margin**: measured lead after trimming is exactly 0.030 s and measured trail exactly
   0.100 s (`abs=1e-6`) on every clip.

This is also pinned as a test against the real model
(`test_the_real_engine_returns_no_more_padding_than_the_residuals`), which runs whenever
the `kokoro` extra is present and skips otherwise.

## WAV files

`/home/severingartland/programing_linux/speakd-trim-wavs/` — 14 files, 16-bit mono
24 kHz, all `af_heart` at speed 1.1, named `NN-slug--BEFORE-untrimmed.wav` and
`NN-slug--AFTER-trimmed.wav`. **I have not listened to them.** They are written for the
user to check by ear, and the clips were chosen to stress the risk: `01-yes` and
`02-okay` where padding dominates, `03-should-he-have-known` (the softest onset in the
whole corpus), `04-shhh-listen` and `06-hesitantly-whispered` for breathy/fricative
starts.

## Timeline and pipeline

`audio_offset` is accumulated in `pipeline.speak` from the same trimmed lengths that
`duration` comes from, so the span-to-time map shrinks in lockstep and now describes the
audio that actually plays. Everything checked:

- **`Timeline.append`** rejects `duration < 0` only; zero is legal. Trimming cannot reach
  zero from speech-bearing audio — the result is always at least
  `lead + 1 + trail` samples — and all-silent audio is returned unchanged.
- **`Timeline.append`'s ordering guard** (`audio_offset < self.duration - 1e-9`) still
  holds exactly: offsets and durations shrink together, so no overlap can appear.
- **`Timeline.span_at` / `time_of`** have no minimum-duration assumption. `span_at`'s
  window narrows correctly with the shorter duration.
- **`Timeline.drift`** is unaffected in kind — both its terms shrink consistently.
- **`player.play`** loops `range(0, len(audio), self._chunk)`; shorter audio is simply
  fewer chunks, and the zero-length case is already handled deliberately.
- **Nothing anywhere ties text length to audio length.** `FakeEngine` does
  (`chars_per_second`), but it is a separate class the trim is not applied to — and its
  output is pure zeros, so `trim_silence` would return it unchanged even if it were. That
  is why no existing pipeline, timeline or player test moved.

### Two real consequences worth knowing about

**1. Reported real-time factor will rise, without anything getting slower.**
`metrics.SynthesisWindow.rtf()` divides synthesis cost by seconds of audio produced.
Synthesis cost is untouched; audio is now ~0.27 s shorter per segment. So RTF rises by the
trim fraction — about 5.6% on a long segment, about 28% on `"Yes."`. `metrics.py`'s own
comment already notes that short segments read high; they now read higher. No code change
is warranted, but a monitor's numbers shift and that is not a regression.

**2. The pipeline has ~0.27 s less slack per segment.** Streaming works by playing segment
N while N+1 is synthesised, so playback duration is the budget synthesis has to hide in.
That budget just shrank by 0.269 s per segment. On this machine there is ample headroom
(1.9 s of synthesis against 4.58 s of trimmed audio), but for a run of very short segments
the margin is thinner than before — a five-sentence utterance now offers 9.2 s of cover
instead of 10.6 s. Worth watching on slower hardware; it did not show up here.

## TDD evidence

Tests were written first and run red before any implementation existed:

```
tests/test_trim_silence.py:12: in <module>
    from speakd.synth.kokoro_engine import (
E   ImportError: cannot import name 'LEADING_SILENCE_SECONDS' from 'speakd.synth.kokoro_engine'
=========================== short test summary info ============================
ERROR tests/test_trim_silence.py
!!!!!!!!!!!!!!!!!!!! Interrupted: 1 error during collection !!!!!!!!!!!!!!!!!!!!
1 error in 0.22s
```

Then green after implementing `trim_silence` and calling it from `synthesize`:
`25 passed`. The engine-level tests added afterwards were red-checked by mutation instead
(first row of the table below).

The trimming logic is a module-level free function, so all 14 unit tests run against
hand-built arrays with no model, no `kokoro` extra and no audio device.

## Teeth verification

Every new test was checked by deleting the line it pins and confirming failure, then
restoring. Run with `PYTHONDONTWRITEBYTECODE=1` and `__pycache__` cleared before each run.

```
baseline: (0, '29 passed, 3 warnings in 8.38s')

CAUGHT   engine stops calling trim_silence                    -> 3 failed, 13 passed
CAUGHT   the nothing-above-threshold guard is deleted         -> 3 failed, 26 passed
CAUGHT   start is no longer clamped at 0                      -> 2 failed, 11 passed
CAUGHT   the threshold comparison becomes >=                  -> 1 failed, 12 passed
CAUGHT   the leading residual is dropped                      -> 10 failed, 19 passed
CAUGHT   the trailing residual is dropped                     -> 11 failed, 18 passed
CAUGHT   the residuals stop being scaled by the sample rate   -> 1 failed, 12 passed
CAUGHT   the +1 that keeps the last speech sample is dropped  -> 7 failed, 6 passed
CAUGHT   the threshold is raised out of the measured band     -> 1 failed, 12 passed
CAUGHT   the leading residual is made the larger of the two   -> 3 failed, 26 passed

restored: rc=0 29 passed
ALL MUTANTS CAUGHT
```

**One mutant initially survived**, and it found dead code rather than a weak test. The
implementation had `end = min(len(audio), ...)`; removing the `min` changed nothing,
because numpy already clamps a slice end past the buffer. A *negative start*, by contrast,
wraps and silently returns a later chunk with the speech gone — so `max(0, ...)` is
load-bearing and `min(len(audio), ...)` was a line no test could ever fail on. The `min`
was deleted and the asymmetry documented in a comment, rather than inventing a test that
could not fail honestly.

## Gate output

Each run as its own command. Nothing piped.

**`pytest`**
```
6 failed, 570 passed in 48.56s
FAILED tests/test_cc_hook.py::test_no_payload_ever_reaches_claude_code_as_a_traceback
FAILED tests/test_cc_hook.py::test_a_prompt_against_an_unreachable_daemon_stays_inside_its_budget
FAILED tests/test_cc_plugin_manifest.py::test_the_wrapper_falls_back_to_the_checkout_venv
FAILED tests/test_cc_plugin_manifest.py::test_a_well_formed_payload_leaves_the_log_alone
FAILED tests/test_cc_plugin_manifest.py::test_an_installed_copy_finds_the_interpreter_through_speakd_home
FAILED tests/test_cc_plugin_manifest.py::test_with_home_unset_both_halves_pick_the_same_log
```
553 → 570 passed (+17 new). Failures are the same six as the untouched baseline.

**`ruff check .`**
```
All checks passed!
```

**`ruff format --check .`**
```
79 files already formatted
```

**`mypy src tests`**
```
Success: no issues found in 68 source files
```

## Constraints

Python 3.11–3.13 compatible; numpy only, no new dependency; `kokoro` still imported inside
`__init__` and never at module import time; no test requires an audio device or the kokoro
extra (the three that need the real model were already `importorskip`-gated, and the one
added beside them follows the same fixture); ruff line length 100.
