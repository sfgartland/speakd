# The `metrics` event — implementation report

Branch `feat/metrics-event`, worktree
`/home/severingartland/programing_linux/speakd-worktrees/metrics`, from
`c313abc Settle the metrics event contract`.

Contract implemented: `docs/design/2026-09-14-metrics-event.md`.

Two commits, each green on its own tree:

- `5b0bc65 Measure synthesis and drift where they already happen` --
  `metrics.py`, `Timeline.drift`, the pipeline's `window=`/`timeline=` seams,
  and their tests. Verified on that tree alone: `515 passed` (505 baseline +
  10), same six environment failures.
- `522fdfc Put the monitor's four numbers on the bus` -- the daemon's ticker,
  `tests/test_metrics.py`, and the README's event stream. `538 passed`.

This report is left untracked, as the sibling worktrees leave theirs.

---

## 1. The environment, first, because it changes how the gates were run

**This worktree had no virtual environment.** The task said the worktree shares
the checkout's environment; it does not. Every sibling worktree
(`cc-hooks`, `cc-reader`, `wiring`) has its own `.venv` with the core+dev set
(numpy, pytest, ruff, mypy, and `speakd` installed editable against *that*
worktree's `src`). This one had none, and my first `uv run --no-sync pytest`
created an empty `.venv` as a side effect, which then fell through to the
linuxbrew system pytest on Python 3.14 and died importing `langsmith`.

`uv sync` in any form was forbidden. `uv pip install -e . --group dev --offline`
was **denied by the permission classifier**, so I could not populate it.

What I did instead — read-only, nothing installed, nothing mutated:

```
UV_PROJECT_ENVIRONMENT=<...>/speakd-worktrees/cc-hooks/.venv \
PYTHONPATH=<...>/speakd-worktrees/metrics/src \
uv run --no-sync <gate>
```

The borrowed environment is the CI-equivalent one (no kokoro, no torch, no
sounddevice — Python 3.12.12). `PYTHONPATH` precedes site-packages in
`sys.path`, so the `.pth` editable pointing at cc-hooks' `src` is shadowed and
**this worktree's source is what runs**. Verified:

```
$ ... uv run --no-sync python -c "import speakd, sys; print(speakd.__file__); print(sys.version)"
/home/severingartland/programing_linux/speakd-worktrees/metrics/src/speakd/__init__.py
3.12.12 (main, Jan 14 2026, 19:35:58) [Clang 21.1.4 ]
```

The main checkout's `.venv` was deliberately *not* used: it has kokoro
installed, which un-skips `test_kokoro_engine.py`'s real-model tests — exactly
the "no test may require the kokoro extra" case I was told to avoid.

**Consequence: six tests fail, before and after my change.** They are the
Claude Code hook wrapper tests, which require `<worktree>/.venv/bin/speakd-claude-hook`
and `<worktree>/.venv/bin/python`. One of them asserts it in so many words:

```
assert entry.exists(), "run `uv sync --group dev` first"
```

Baseline recorded on the **unmodified tree at c313abc, before any code was
written** (`scratchpad/baseline.txt`):

```
FAILED tests/test_cc_hook.py::test_no_payload_ever_reaches_claude_code_as_a_traceback
FAILED tests/test_cc_hook.py::test_a_prompt_against_an_unreachable_daemon_stays_inside_its_budget
FAILED tests/test_cc_plugin_manifest.py::test_the_wrapper_falls_back_to_the_checkout_venv
FAILED tests/test_cc_plugin_manifest.py::test_a_well_formed_payload_leaves_the_log_alone
FAILED tests/test_cc_plugin_manifest.py::test_an_installed_copy_finds_the_interpreter_through_speakd_home
FAILED tests/test_cc_plugin_manifest.py::test_with_home_unset_both_halves_pick_the_same_log
6 failed, 505 passed, 3 skipped, 1 warning in 34.29s
```

The same six, and only those six, fail after the change. None of them touches
`daemon.py`, `pipeline.py`, `timeline.py`, `events.py` or `metrics.py`. I did
not fabricate venv contents to turn them green — that would be dressing a gate
rather than passing it. **This is the one thing that needs a human decision**
(see §8).

---

## 2. What was built

| File | Change |
|---|---|
| `src/speakd/metrics.py` | New. `SynthesisWindow` (trailing RTF), `resident_bytes()`. |
| `src/speakd/timeline.py` | New `Timeline.drift` property. |
| `src/speakd/pipeline.py` | `speak(..., window=, timeline=)`: times each `synthesize()`, fills a caller-supplied timeline. |
| `src/speakd/daemon.py` | `_accepted` counter, `_synthesis` window, `_timeline`, and the `speakd-metrics` ticker thread with its start/stop lifecycle and `_metrics_payload`. |
| `README.md` | The event added to the documented stream (it already documented `started`/`position`/`finished` and said "the gap between them is the drift a monitor should show"). |
| `tests/test_metrics.py` | New, 23 tests. |
| `tests/test_timeline.py` | +6 drift tests. |
| `tests/test_pipeline.py` | +4 window/timeline tests. |

Event shape, exactly as the contract specifies:

```json
{"event": "metrics", "source_id": "", "data": {
  "rtf": 0.75, "drift": 0.18, "mem_bytes": 1632087232, "queue": 2}}
```

---

## 3. The hard constraint, and how it is met

> gathering these numbers must never make the speech worker wait

Nothing in the tick path touches the worker. The ticker reads three pieces of
state the worker has already written down:

- `_pending` and `_accepted`, under `_idle`, the lock the daemon already uses.
  Held for four attribute reads, no I/O.
- the `SynthesisWindow`, whose own lock is held for one `deque.append` (worker
  side) or one bounded list copy of at most five pairs (ticker side). This is
  the same "snapshot under the lock, work outside it" shape `EventBus.publish`
  already uses from the speech thread.
- the `Timeline`, read by index at its two ends while the worker appends.

`/proc` reading, RTF averaging and `bus.publish` all happen **outside** the
lock. A subscriber slow to take a `metrics` event stalls the ticker, never the
worker — and `transport.py` already drops a subscriber that stops reading.

The worker's only added work is one `time.monotonic()` pair and one guarded
append per synthesised segment, inside the producer thread of `speak()`.

This is asserted, not asserted-about:
`test_the_ticker_does_not_wait_on_a_worker_stuck_in_play` wedges the worker
inside `player.play()` — the exact shape of the shipped hour-long stall — and
requires three further ticks to arrive while it is stuck.

---

## 4. Judgement calls

### 4.1 The trailing window is **five segments**

`_WINDOW_SEGMENTS = 5` in `metrics.py`.

Reasoning. Segments are sentence-sized (`segmenter.DEFAULT_MAX_CHARS = 180`;
the 417-character benchmark passage splits into five), so a segment is roughly
three to six seconds of speech. Five of them is fifteen to twenty-five seconds
of audio — recent enough to describe the machine now, not "since boot".

The size is set by the thing the contract warns about: a fixed cost near one
second per call. A "Done." of a third of a second reads as RTF above 3 on its
own. Averaged with four ordinary segments (~20s of audio, ~15s of synthesis) it
moves the answer from 0.75 to about 0.79 — about 0.05, which is the honest size
of its contribution rather than a spike that swamps the display. One segment
would show the spike as the reading; twenty would be half a minute of history
and no longer "now".

Rejected alternative: a window measured in *audio seconds* rather than
segments. More principled (it makes the averaging period fixed regardless of
segment length), but it needs eviction logic against a running total, and the
contract says "a window of a few segments". Noted here as the obvious next
refinement if the number ever looks laggy on very long segments.

### 4.2 RTF is summed, not averaged over per-segment ratios

`sum(cost) / sum(audio)`, not `mean(cost_i / audio_i)`. The question is what
the last few seconds of speech cost; a quarter-second segment must not weigh as
much as a five-second one. `test_rtf_weighs_by_audio_rather_than_averaging_the_segments`
pins this with data where the two differ (0.30 against 0.56).

### 4.3 Synthesis is timed in the pipeline, not in an engine wrapper

`synth/__init__.py` already says it: "Segmentation and timing belong to the
pipeline." The alternative — a `_MeteredEngine` decorator in the daemon —
leaves no trace at the place synthesis actually happens and would have to
re-implement the Protocol's attributes. Nothing in the pipeline timed
`synthesize()` before this; what already existed was the audio-length
measurement (`len(audio) / engine.sample_rate`) and the `played_at` stamp, and
I reused the first as the denominator rather than re-deriving audio length
somewhere else. A call that *raised* is not recorded: it produced no audio, and
seconds per second of nothing is not a measurement.

### 4.4 Drift lives on `Timeline`, and is measured first-to-last

`Timeline` holds both clocks, so it answers the question: measured elapsed
(`played_at`) minus nominal elapsed (`audio_offset`) between the first segment
and the last. Literally the contract's wording. It deliberately does **not**
bring `time.monotonic()` into it, which would make drift grow during a stall
in a way the contract does not describe, and would make the number depend on
when it was asked rather than on what was played.

Zero before anything has played, zero for one segment, zero for an unstamped
timeline. The guard is exactly `if not self._segments` — I first wrote
`len(...) < 2`, and the mutation run showed that stronger guard was
unfalsifiable (with one segment first *is* last, so the arithmetic already
gives 0.0). Guarding only the case that cannot be computed is what gave
`test_drift_before_anything_has_played_is_zero` teeth.

### 4.5 A field that is not knowable is omitted, never guessed

`mem_bytes` where there is no `/proc` (contract's instruction), and `rtf`
before the daemon's first segment has been synthesised (the same honesty, by
extension — `0.0` means "infinitely fast", which is a reading, not a gap).
`drift` and `queue` are always knowable and always present.

### 4.6 The settle tick uses a counter, not a "was busy" flag

The ticker holds its own `settled` baseline and compares it against the
daemon's monotonic `_accepted`. A ticker-local "did I emit last tick" flag
would have been smaller, but it misses an utterance that begins and ends
between two ticks — "Done." on a one-second interval — and a monitor that
shows nothing for short speech is the common case, not an edge case.
`test_an_utterance_shorter_than_a_tick_still_settles_the_monitor` covers it.

**The baseline is read in `start()` under the lock, not on the ticker's first
pass.** This is a bug the tests caught rather than a design I got right: the
first version read `settled = self._accepted` inside the thread, and an
enqueue landing between `start()` releasing `_idle` and the ticker's first
statement made the ticker start one ahead — it ticked through the whole
utterance and then never settled. It reproduced roughly one run in five
(`scratchpad/repro.py`, 41 busy events, last `queue: 1`, `pending=0`,
`accepted=1`, ticker alive). Fixed by passing the baseline as a thread
argument, evaluated under `start()`'s own lock. 40/40 clean after.

### 4.7 Ticker lifecycle: started first, retired by Event *and* identity

- Started **before** the speech worker inside `start()`'s existing locked
  block, because it is the half that can still be withdrawn by hand (it
  publishes nothing until there is speech, and an Event retires it). A worker
  that has started retires itself by identity off `_worker`; if it were up
  when the ticker failed there would be nothing honest to roll back.
- Retired in `stop()` under the same lock that takes the worker, then joined
  outside it (bounded, `_JOIN_METRICS_SECONDS = 2.0`) — it takes `_idle` on
  every pass, so joining under the lock would deadlock.
- Both the Event and the identity check are load-bearing. The Event makes
  retirement *prompt* (`test_stopping_does_not_wait_out_a_tick`, at a 30s
  interval). Identity is what stops a ticker whose `join` timed out inside a
  slow subscriber and which then wakes to find the Event cleared by the next
  `start()` (`test_a_ticker_the_join_gave_up_on_does_not_publish_for_its_successor`).
  The mutation run found both were untested at first and I added those two
  tests to close it — see §6.
- `silence()` deliberately leaves the ticker up: it is the immediate half of a
  shutdown, and `stop()` is the teardown.
- A worker that dies leaves the ticker running; `_retire` drives `_pending` to
  zero, so the monitor gets its settling tick for the utterances discarded on
  the way down, and a supervisor's `start()` installs a new ticker that retires
  the old one by identity.

### 4.8 Numbers go out unrounded

`0.7500000000001` is ugly but lossless; rounding is a display policy and
`clients/gui/pinned.html` already does `toFixed(2)`.

### 4.9 `metrics_interval` is a constructor argument

Default `_METRICS_INTERVAL_SECONDS = 1.0`, the contract's cadence, pinned by
`test_the_shipped_interval_is_one_second`. Tests run at 0.02s so the suite is
not mostly sleep — without the seam, every timer test would cost three seconds
and the repeat runs would cost an hour.

---

## 5. TDD evidence

Tests were written first and run red before any implementation existed:

```
$ ... uv run --no-sync pytest -q tests/test_metrics.py tests/test_timeline.py tests/test_pipeline.py
tests/test_metrics.py:19: in <module>
    from speakd.daemon import _METRICS_THREAD_NAME, Daemon, ProfileView
E   ImportError: cannot import name '_METRICS_THREAD_NAME' from 'speakd.daemon'
tests/test_pipeline.py:8: in <module>
    from speakd.metrics import SynthesisWindow
E   ModuleNotFoundError: No module named 'speakd.metrics'
ERROR tests/test_metrics.py
ERROR tests/test_pipeline.py
!!!!!!!!!!!!!!!!!!! Interrupted: 2 errors during collection !!!!!!!!!!!!!!!!!!!!
2 errors in 0.52s
```

Then green, after `metrics.py`, `Timeline.drift`, the pipeline wiring and the
daemon ticker:

```
$ ... uv run --no-sync pytest -q tests/test_metrics.py tests/test_timeline.py tests/test_pipeline.py
55 passed in 6.92s
```

Two tests were added *after* the mutation run, not before, because mutation is
what proved they were missing: `test_stopping_does_not_wait_out_a_tick` and
`test_a_speech_worker_that_will_not_start_takes_the_ticker_with_it` (the latter
retargeted to a 30s interval so only the Event, not the identity check, can
retire the ticker in time).

---

## 6. Teeth: 26 mutants, 26 killed

Harness: `scratchpad/mutants.py` + `mutants.json`. For each mutant it applies a
single textual change, clears every `__pycache__` under the worktree, runs the
tests that should notice with `PYTHONDONTWRITEBYTECODE=1` and
`-p no:cacheprovider`, then restores the file and clears the caches again.
No bytecode is written at any point, so same-length mutants written inside one
second cannot alias to stale `.pyc` files. Kills are measured against the
mutant, never by re-running the clean suite.

```
KILLED    window-unbounded: 1 failed in 0.22s
KILLED    rtf-mean-of-ratios: 1 failed in 0.22s
KILLED    rtf-no-zero-audio-guard: 1 failed in 0.21s
KILLED    statm-first-field: 1 failed in 0.20s
KILLED    statm-no-oserror-guard: 1 failed in 0.25s
KILLED    statm-no-valueerror-guard: 1 failed in 0.22s
KILLED    statm-pages-not-bytes: 2 failed in 0.27s
KILLED    drift-measured-only: 2 failed in 1.61s
KILLED    drift-last-hop-only: 2 failed in 0.07s
KILLED    drift-no-empty-guard: 1 failed in 0.12s
KILLED    drift-no-stamp-guard: 1 failed in 0.06s
KILLED    pipeline-records-nothing: 1 failed in 0.21s
KILLED    pipeline-records-failed-calls: 1 failed in 0.23s
KILLED    pipeline-ignores-supplied-timeline: 2 failed in 5.21s
KILLED    daemon-always-emits: 2 failed in 1.51s
KILLED    daemon-never-settles: 2 failed in 9.18s
KILLED    daemon-emits-only-once-idle: 2 failed in 10.31s
KILLED    daemon-queue-is-the-backlog-only: 1 failed in 5.27s
KILLED    daemon-mem-guessed-as-zero: 1 failed in 0.78s
KILLED    daemon-stop-leaves-the-ticker: 1 failed in 2.29s
KILLED    daemon-refused-start-leaves-the-ticker: 1 failed in 5.30s
KILLED    daemon-no-identity-check: 1 failed in 2.64s
KILLED    daemon-baseline-off-by-one: 1 failed in 3.29s
KILLED    daemon-interval-not-a-second: 1 failed in 0.23s
KILLED    daemon-drops-the-live-timeline: 1 failed in 1.59s
KILLED    daemon-drops-the-window: 2 failed in 1.49s

26 mutants, 26 killed
```

What each mutant actually changes, and which test refuses it:

| Mutant | The change | Killed by |
|---|---|---|
| window-unbounded | `deque(maxlen=size)` → `deque()` | `test_the_window_forgets_what_the_machine_was_doing_before` |
| rtf-mean-of-ratios | sum/sum → mean of per-segment ratios | `test_rtf_weighs_by_audio_rather_than_averaging_the_segments` |
| rtf-no-zero-audio-guard | `if audio <= 0` → `if not samples` | `test_a_call_that_produced_no_audio_does_not_divide_by_zero` |
| statm-first-field | `fields[1]` → `fields[0]` (size, not resident) | `test_resident_bytes_is_the_second_field_of_statm_in_pages` |
| statm-no-oserror-guard | `return None` → `raise` | `test_resident_bytes_is_unknown_rather_than_a_guess_without_proc` |
| statm-no-valueerror-guard | `except ValueError` → `except KeyError` | `test_a_statm_that_is_not_linuxs_is_unknown_rather_than_an_exception` |
| statm-pages-not-bytes | drop `* os.sysconf("SC_PAGE_SIZE")` | `test_this_process_reports_its_own_resident_set`, the statm test |
| drift-measured-only | drop the nominal term | `test_drift_is_measured_elapsed_minus_nominal_elapsed`, `test_the_drift_reported_is_the_stall_in_the_utterance_being_spoken` |
| drift-last-hop-only | `_segments[0]` → `_segments[-2]` | `test_drift_is_measured_across_the_whole_utterance_not_the_last_hop`, `test_drift_over_a_single_segment_is_zero` |
| drift-no-empty-guard | delete the empty guard | `test_drift_before_anything_has_played_is_zero` (IndexError) |
| drift-no-stamp-guard | delete the `played_at is None` guard | `test_drift_of_segments_that_were_never_stamped_is_zero` (TypeError) |
| pipeline-records-nothing | `window.record(...)` → `pass` | `test_speak_records_what_each_synthesis_cost_against_the_audio_it_made` |
| pipeline-records-failed-calls | record in the `except` branch too | `test_a_segment_that_failed_to_synthesise_is_not_counted_as_audio` |
| pipeline-ignores-supplied-timeline | always build a fresh `Timeline()` | `test_speak_fills_a_timeline_the_caller_supplied`, `test_a_supplied_timeline_can_be_read_while_it_is_still_being_built` |
| daemon-always-emits | drop the idle guard | `test_nothing_goes_out_while_the_daemon_has_nothing_to_say`, `test_the_ticker_settles_the_monitor_once_and_then_stops` |
| daemon-never-settles | never emit once idle | `test_an_utterance_shorter_than_a_tick_still_settles_the_monitor`, `test_the_event_carries_the_four_numbers_the_monitor_shows` |
| daemon-emits-only-once-idle | the forbidden design: gather only at the end | `test_the_ticker_does_not_wait_on_a_worker_stuck_in_play`, `test_the_queue_reported_is_the_work_the_daemon_has_not_finished` |
| daemon-queue-is-the-backlog-only | `pending` → `self._jobs.qsize()` | `test_the_queue_reported_is_the_work_the_daemon_has_not_finished` |
| daemon-mem-guessed-as-zero | omitted field → `0` | `test_mem_bytes_is_left_out_rather_than_guessed` |
| daemon-stop-leaves-the-ticker | drop `_metrics_stop.set()` in `stop()` | `test_stopping_does_not_wait_out_a_tick` |
| daemon-refused-start-leaves-the-ticker | drop `_metrics_stop.set()` in the rollback | `test_a_speech_worker_that_will_not_start_takes_the_ticker_with_it` |
| daemon-no-identity-check | `if self._metrics is not me` → `if False` | `test_a_ticker_the_join_gave_up_on_does_not_publish_for_its_successor` |
| daemon-baseline-off-by-one | `args=(self._accepted,)` → `+ 1` | `test_an_utterance_shorter_than_a_tick_still_settles_the_monitor` |
| daemon-interval-not-a-second | `1.0` → `0.25` | `test_the_shipped_interval_is_one_second` |
| daemon-drops-the-live-timeline | stop passing `timeline=` to `speak()` | `test_the_drift_reported_is_the_stall_in_the_utterance_being_spoken` |
| daemon-drops-the-window | stop passing `window=` to `speak()` | `test_the_rtf_reported_is_what_synthesis_cost_against_the_audio_it_made`, `test_the_event_carries_the_four_numbers_the_monitor_shows` |

**Three mutants survived the first run, and all three were real gaps:**

1. `drift-no-empty-guard` (`len < 2` → `len < 1`) survived because with one
   segment the arithmetic already gives 0.0 — the `< 2` guard was
   unfalsifiable. Fixed by narrowing the guard to `if not self._segments`, at
   which point deleting it raises IndexError and the test fails.
2. `daemon-stop-leaves-the-ticker` survived because the identity check retires
   the ticker within one 0.02s tick, so no test could see that the Event was
   what made it *prompt*. Fixed by adding `test_stopping_does_not_wait_out_a_tick`
   at a 30-second interval.
3. `daemon-refused-start-leaves-the-ticker` survived for the same reason; fixed
   by moving `test_a_speech_worker_that_will_not_start_takes_the_ticker_with_it`
   to a 30-second interval.

---

## 7. Repeat runs and the gates

### Threaded tests, 20× in fresh subprocesses

Every `uv run` is a fresh interpreter; `__pycache__` cleared beforehand,
`PYTHONDONTWRITEBYTECODE=1` and `-p no:cacheprovider` throughout. All four
files that now contain threads or touch the daemon:

```
run 1: 119 passed in 11.64s      run 11: 119 passed in 11.43s
run 2: 119 passed in 11.61s      run 12: 119 passed in 11.81s
run 3: 119 passed in 11.63s      run 13: 119 passed in 11.70s
run 4: 119 passed in 11.67s      run 14: 119 passed in 11.61s
run 5: 119 passed in 11.73s      run 15: 119 passed in 11.60s
run 6: 119 passed in 11.65s      run 16: 119 passed in 11.50s
run 7: 119 passed in 11.76s      run 17: 119 passed in 11.82s
run 8: 119 passed in 11.69s      run 18: 119 passed in 11.66s
run 9: 119 passed in 11.72s      run 19: 119 passed in 11.68s
run 10: 119 passed in 11.71s     run 20: 119 passed in 11.65s
```

20/20, 0 failures, 0 errors
(`tests/test_metrics.py tests/test_daemon.py tests/test_pipeline.py tests/test_timeline.py`).

Before the baseline-race fix (§4.6), the same suite failed roughly 1 run in 5.

The thread-leak requirement is also asserted inside the suite rather than only
counted here: `test_the_ticker_does_not_outlive_the_daemon` requires the
`speakd-metrics` thread count to return to its pre-start value after `stop()`
*and* requires no further events; `test_a_restarted_daemon_ticks_once_not_twice`
requires exactly one ticker after a stop/start cycle.

### Gate 1 — `uv run --no-sync pytest`

```
============================= test session starts ==============================
platform linux -- Python 3.12.12, pytest-9.1.1, pluggy-1.6.0
rootdir: /home/severingartland/programing_linux/speakd-worktrees/metrics
configfile: pyproject.toml
testpaths: tests
collected 547 items
...
FAILED tests/test_cc_hook.py::test_no_payload_ever_reaches_claude_code_as_a_traceback
FAILED tests/test_cc_hook.py::test_a_prompt_against_an_unreachable_daemon_stays_inside_its_budget
FAILED tests/test_cc_plugin_manifest.py::test_the_wrapper_falls_back_to_the_checkout_venv
FAILED tests/test_cc_plugin_manifest.py::test_a_well_formed_payload_leaves_the_log_alone
FAILED tests/test_cc_plugin_manifest.py::test_an_installed_copy_finds_the_interpreter_through_speakd_home
FAILED tests/test_cc_plugin_manifest.py::test_with_home_unset_both_halves_pick_the_same_log
============= 6 failed, 538 passed, 3 skipped, 1 warning in 42.88s =============
```

505 → 538 passing (+33 new tests). The six failures are the pre-existing
environment failures of §1, byte-for-byte the same set as at HEAD with no
changes applied. The one warning is also pre-existing: `test_transport.py`'s
deliberate "can't start new thread" test.

### Gate 2 — `uv run --no-sync ruff check .`

```
All checks passed!
```

### Gate 3 — `uv run --no-sync ruff format --check .`

```
78 files already formatted
```

### Gate 4 — `uv run --no-sync mypy src tests`

```
Success: no issues found in 67 source files
```

Each was run as its own command. Nothing was piped anywhere; every exit status
above is the tool's own.

---

## 8. Concerns

1. **The six pre-existing failures need a human decision.** They want
   `<worktree>/.venv` populated — the test's own message says
   "run `uv sync --group dev` first" — which is exactly what I was told never
   to do, and `uv pip install -e . --group dev --offline` was denied by the
   permission classifier. Either the worktree needs a venv built the way the
   siblings' were, or those tests need to skip when the checkout has no venv
   (they are environment assertions, not behaviour assertions). I did not
   hand-assemble venv contents to make them pass.
2. **I left an empty `.venv` in this worktree**, created as a side effect of my
   first `uv run`. It is gitignored and harmless, but a later
   `uv run --no-sync` there without the env prefixes above will use it and fall
   through to the system pytest. Populating or deleting it are both fine; I
   removed nothing in case the intent was to install into it.
3. **The gates ran against a borrowed interpreter.** The environment is the
   CI-equivalent one and the source under test is verifiably this worktree's,
   but nobody has run this branch against the real kokoro environment. Nothing
   in the change touches an engine or a sink, and no new test needs either.
4. **Two integration tests assert on wall-clock timing** — the reported drift
   (0.30s ± 0.12) and RTF (0.35 ± 0.12) against a sleeping fake sink. The bands
   are wide enough to exclude every mutant I tried and survived 20 consecutive
   runs, but they are the tests most likely to flake on a loaded machine. The
   arithmetic itself is pinned separately and deterministically in
   `test_timeline.py` and the `SynthesisWindow` unit tests.
5. **The window size is a judgement, not a measurement** (§4.1). Five segments
   is reasoned from the contract's own numbers rather than from a profile of
   this machine. If the display looks laggy on long passages, the fix is an
   audio-seconds window rather than a smaller segment count.
6. **The settling tick can be up to one interval late.** The ticker polls; it
   does not wake on the transition to idle. At the shipped one-second cadence a
   monitor settles within a second of `finished`, which matches "once more on
   the transition to idle" without adding a second notify path through
   `_idle` — the concurrency here has been reviewed hard and I did not want to
   add a wake-up edge to it for a display.
