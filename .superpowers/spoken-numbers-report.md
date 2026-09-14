# Spoken numbers: years and numeric ranges

Branch `feat/spoken-numbers`, worktree
`/home/severingartland/programing_linux/speakd-worktrees/numbers`.

## How everything here was run

This worktree has no `.venv`, and `uv sync` must never be run (it prunes the
hand-built torch/kokoro stack and substitutes a CUDA build this machine cannot
run). Every command below borrows the main checkout's interpreter:

```
cd /home/severingartland/programing_linux/speakd-worktrees/numbers
PYTHONPATH=$PWD/src /home/severingartland/programing_linux/speakd/.venv/bin/python -m pytest -q
PYTHONPATH=$PWD/src /home/severingartland/programing_linux/speakd/.venv/bin/python -m ruff check .
PYTHONPATH=$PWD/src /home/severingartland/programing_linux/speakd/.venv/bin/python -m ruff format --check .
PYTHONPATH=$PWD/src /home/severingartland/programing_linux/speakd/.venv/bin/python -m mypy src tests
```

## What the engine already does correctly — do not fix this

The brief supplied espeak's behaviour as the motivating evidence. espeak is
the phonemiser of the **ONNX** engine, which is a candidate under evaluation
on another branch, not something this one ships: `src/speakd/synth/` here
contains only `kokoro_engine.py` and `fake.py`. So the reading that governs
this change is misaki's, and it was measured directly
(`misaki.en.G2P(trf=False, british=False, fallback=None)`):

| input | phonemes | reads as | verdict |
|---|---|---|---|
| `1994` | `nˌIntˈin nˈIndi fˈɔɹ` | nineteen ninety-four | correct |
| `1787` | `sˌɛvəntˈin ˈATi sˈɛvən` | seventeen eighty-seven | correct |
| `1900` | `nˌIntˈin hˈʌndɹəd` | nineteen hundred | correct |
| `2000` | `tˈu θˈWzᵊnd` | two thousand | correct |
| `1809` | `ˌAtˈin ˈO nˈIn` | eighteen oh nine | correct |
| `2020` | `twˈɛnti twˈɛnti` | twenty twenty | correct |
| `2005` | `tˈu θˈWzᵊnd fˈIv` | two thousand five | correct |
| `1500 people` | `fˌɪftˈin hˈʌndɹəd pˈipᵊl` | fifteen hundred people | correct |
| `version 2.1.4` | `vˈɜɹʒən tˈu wˈʌn fˈɔɹ` | two one four | correct |

**Every awkward case the brief named is already right, including all four it
flagged as hard.**

### The year rule was tested, not assumed — and then not shipped

Spelling a year out was measured three ways:

| text | phonemes | vs. digits |
|---|---|---|
| `1994` (digits) | `nˌIntˈin nˈIndi fˈɔɹ` | — |
| `nineteen ninety-four` (hyphenated) | `nˌIntˈin nˈIndifˌɔɹ` | **worse** — "ninety four" fused into one word, stress on "four" demoted |
| `nineteen ninety four` (unhyphenated) | `nˌIntˈin nˈIndi fˈɔɹ` | **identical** |

The hyphenated form — the exact string the brief asked for — is a regression.
The unhyphenated form is neutral, and that was verified exhaustively rather
than sampled: misaki's algorithm was derived from measurement (century pair;
"hundred" when the last two digits are zero, "oh" when they are under ten;
"two thousand N" for 2000-2009), a speller was written to it, and **every year
from 1100 to 2099 was phonemised both as digits and as words:**

```
checked 1000 years in 1100-2099
phoneme mismatches: 0
```

So on the engine this project ships, a year rule **cannot change what anyone
hears** — for any year in the window, including a false fire on `1500 people`.
It is a provable no-op.

### Why it is still not shipped

`dd37f5c` ("Measure the two engines against each other", on another branch —
`tools/` does not exist here) recommends keeping the torch engine *"until the
pronunciation table grows a year rule and a range rule"*, because the ONNX /
espeak engine reads `1994` as "nineteen **hundred** ninety four" and `1787` as
"**one thousand seven hundred** eighty seven", and speaks `34-38` with an
audible "dash". That engine is worth real money: 2.27 s cold start against
7.56 s, 692 MB resident against 1406 MB, 454 MB installed against 1.8 GB.

That is the year rule's only beneficiary, and **it is not on this branch.**
Shipping the rule today would therefore buy nothing on the engine in use while
accepting exactly the false fire the brief names as unacceptable (`1500
people` is not a year, and no context-free rule can tell it from `1994` —
both are four-digit numbers in the plausible window). The discriminating cues
are citation cues, which the design doc assigns to the vault pack.

The honest split: **the range rule is shipped, because it fixes a real defect
on the engine in use. The year rule is not, because on that engine it is a
verified no-op.** Half of the ONNX report's blocking objection is now cleared;
the other half should land in the change that adopts ONNX, where its cost buys
something. The derived speller and its neutrality proof are recorded here so
that change is cheap:

```python
ONES = (
    "zero one two three four five six seven eight nine ten eleven twelve "
    "thirteen fourteen fifteen sixteen seventeen eighteen nineteen"
).split()
TENS = ",,twenty,thirty,forty,fifty,sixty,seventy,eighty,ninety".split(",")


def two(n: int) -> str:
    if n < 20:
        return ONES[n]
    t, o = divmod(n, 10)
    return TENS[t] + (" " + ONES[o] if o else "")


def spell_year(y: int) -> str:
    hi, lo = divmod(y, 100)
    if hi % 10 == 0 and lo < 10:  # 2000-2009 -> "two thousand five"
        return f"{ONES[hi // 10]} thousand" + (f" {ONES[lo]}" if lo else "")
    if lo == 0:
        return f"{two(hi)} hundred"  # 1900 -> "nineteen hundred"
    if lo < 10:
        return f"{two(hi)} oh {ONES[lo]}"  # 1809 -> "eighteen oh nine"
    return f"{two(hi)} {two(lo)}"  # 1994 -> "nineteen ninety four"
```

Note the output is deliberately **unhyphenated**; hyphenating it is the
regression documented above. Verified byte-identical to the digits for all
1000 years in 1100-2099, which is the window it should be limited to. It
would need the same lookaround guards the range rule uses, so that it never
reaches a version string, an ISBN, or the inside of a dash chain.

Per the brief, no citation-key handling was added; the design doc (line 229)
assigns `@Stiegler-1994` to the separately installable vault pack.

## What is actually broken: numeric ranges

| input | phonemes | problem |
|---|---|---|
| `34-38` | `θˌɜɹTi fˌɔɹθˈɜɹTi ˈAt` | `fˌɔɹθˈɜɹTi` — "four" and "thirty" **welded into one token**, no separator at all |
| `34–38` (en dash) | `θˈɜɹTi fˈɔɹ❓ θˈɜɹTi ˈAt` | en dash becomes an **unknown token** `❓` |
| `34 - 38` | `θˈɜɹTi fˈɔɹ ❓ θˈɜɹTi ˈAt` | same |
| `34 − 38` (minus) | `θˈɜɹTi fˈɔɹ ❓ θˈɜɹTi ˈAt` | same |

The fix, and why the digits are kept rather than spelled out:

| candidate | phonemes |
|---|---|
| `34 to 38` (digits kept) | `θˈɜɹTi fˈɔɹ tə θˈɜɹTi ˈAt` |
| `thirty four to thirty eight` | `θˈɜɹTi fˈɔɹ tə θˈɜɹTi ˈAt` (identical) |
| `thirty-four to thirty-eight` | `θˈɜɹTifˌɔɹ tə θˈɜɹTiAt` (fused, worse) |

Replacing only the dash gives phonemes **identical** to fully spelling the
range out, so the engine's own number reading — already correct — does the
work and this module never owns a number speller that could drift from it.
The listener still hears "thirty-four to thirty-eight".

## The rule and its firing conditions

In `src/speakd/transforms/pronunciation.py`: `_NUMERIC_RANGE` + `_spoken_range`,
applied at the top of `_apply`. Conditions are stated in full in the source
comment; in brief, all of these must hold:

1. **1–4 digits each side, no leading zeros** — a leading zero means an
   identifier or date part (`01-15`), never a quantity.
2. **Same digit count, or both at most 3 digits** — a four-digit half paired
   with a shorter one is a phone number (`555-1234`), not a locator. This is
   the guard that protects phone numbers, and it costs wide ranges like
   `950-1020`.
3. **Strictly ascending** — a score reads high-first (`Kant 5-3`) and prose
   subtraction descends, so neither is a range.
4. **Symmetric spacing** around the dash — both sides or neither, so a
   parenthetical `34 -38` is not swept up.
5. **Nothing touching either operand** — no letter, digit, underscore, dot or
   further dash, via a lookbehind/lookahead pair.

Dashes handled: hyphen `-`, en dash `–`, minus `−`. **Em dash is excluded** —
between two numbers in prose it is more often a sentence break than a range.

## Negatives protected, and by which guard

Each is a row in `RANGES_THAT_MUST_NOT_FIRE`, and each was verified to be able
to fail (see teeth below).

| input | why it must not fire | guard |
|---|---|---|
| `555-1234` | phone number | digit-count |
| `978-0-262-03384-8` | ISBN | lookarounds (+ ascending) |
| `2024-01-15` | ISO date | leading-zero + digit-count + lookahead |
| `01-15` | identifier | leading-zero |
| `5-3` | score, reads high-first | ascending |
| `38-34` | subtraction / reversed | ascending |
| `12-12` | not ascending | ascending |
| `v1-2` | version string | left lookbehind |
| `2.1-2.2` | version numbers | both lookarounds |
| `1.5-2.5` | decimal range, out of scope | both lookarounds |
| `34-38a` | suffixed token | right lookahead |
| `12345-12346` | too big for a locator | `\d{1,4}` |
| `1-800-555-1212` | full phone number | right lookahead |
| `8601-1` | standard's part number | digit-count + ascending |

`COVID-19`, `GPT-4` and `Kokoro-82M` are excluded one step earlier, by the
requirement of digits on *both* sides of the dash. They are deliberately **not**
tested: no slip in this rule could reach them, so a test would assert nothing.

## TDD evidence

Tests were written first and watched fail:

```
FAILED tests/test_pronunciation_transform.py::test_numeric_ranges_are_read_as_to
  AssertionError: hyphen, the common page range: '34-38'
  assert '34-38' == '34 to 38'
FAILED tests/test_pronunciation_transform.py::test_a_citation_range_survives_the_rest_of_the_table
2 failed, 19 passed
```

After implementing: `20 passed`.

## Teeth verification

Run with `PYTHONDONTWRITEBYTECODE=1` and `__pycache__` cleared throughout. A
mutation harness applied 14 mutations to the implementation (drop the rule;
drop each dash character; drop the spaced form; drop each of the three numeric
guards; drop each lookaround; widen the operand width; and four combined
mutations), re-imported the module fresh for each, and recorded which table
rows noticed:

```
rows without teeth: NONE - every row can fail
```

All 9 positive rows die when the rule is dropped. All 14 negative rows fire
under at least one mutation — e.g. `555-1234` under "drop the digit-count
guard", `2.1-2.2` under "drop both lookarounds", `12345-12346` under "widen
operands to five digits". Four negatives (`978-…`, `2024-01-15`, `2.1-2.2`,
`1.5-2.5`) are guarded *twice over*, so only a combined mutation exposes them;
combined mutations were added for exactly that reason.

The three non-table tests were checked individually through real pytest:

- `test_a_four_digit_year_is_left_for_the_engine_to_read` — adding a naive year
  rule makes it FAIL; restored, PASS.
- `test_a_citation_range_survives_the_rest_of_the_table` — dropping the range
  rule makes it FAIL; restored, PASS.
- `test_hyphenated_words_are_not_touched` (`well-known`, `COVID-19`, `GPT-4`) —
  **passed under every mutation, i.e. it asserted nothing.** The pattern
  structurally requires digits on both sides, so those strings are unreachable.
  **This test was deleted** and replaced with two negatives that can genuinely
  fail (`8601-1`, `1.5-2.5`).

## WAVs

`/home/severingartland/programing_linux/speakd-worktrees/numbers/.superpowers/spoken-numbers-wavs/`

They are **gitignored**, following the convention set by `dd37f5c` ("WAV files
are gitignored as regenerable artefacts"); no `.wav` has ever been tracked in
this repo. Regenerate them by re-running the synthesis described below.

Ten files, `<case>--raw.wav` and `<case>--transformed.wav`, synthesised with
the real `KokoroEngine` (voice `af_heart`, speed 1.0). **Not listened to.** The
exactly verifiable artefact is the transform's text output:

| case | raw | transformed |
|---|---|---|
| `01-citation-year` | `Stiegler 1994, Technics and Time, volume 1.` | *unchanged* |
| `02-page-range` | `See §12, e.g. pp. 34-38, ca. 1787.` | `See §12, for example pp. 34 to 38, ca. 1787.` |
| `03-negative-quantity` | `The survey covered 1500 people.` | *unchanged* |
| `04-negative-version` | `Upgrade to version 2.1-2.2 of the plugin.` | *unchanged* |
| `05-negative-phone` | `Call 555-1234 for the archive desk.` | *unchanged* |

Only the page range changed; the year and all three negatives passed through
untouched, and the year in case 02 (`ca. 1787`) was left for the engine.

A caution on the WAVs: **kokoro synthesis is not deterministic.** The same text
synthesised twice gives the same sample count but different samples (max abs
diff 0.13), so every raw/transformed pair differs bytewise even where the input
text is identical. Byte comparison of the audio proves nothing. The unchanged
pairs do have identical file sizes, which corroborates identical text.

## Gates

Each run as its own command, never piped into `tail` or anything else.

```
$ … -m pytest -q
6 failed, 557 passed, 4 warnings in 53.92s

$ … -m ruff check .
All checks passed!

$ … -m ruff format --check .
79 files already formatted

$ … -m mypy src tests
Success: no issues found in 67 source files
```

The 6 failures were confirmed against the untouched base **before** any change:
`6 failed, 553 passed`, the same six tests
(`test_cc_hook.py` ×2, `test_cc_plugin_manifest.py` ×4) — pre-existing, caused
by this worktree having no `.venv`. Pass count 553 → 557 is the four new tests.
