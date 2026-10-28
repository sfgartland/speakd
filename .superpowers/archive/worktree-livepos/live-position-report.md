# Live position reporting

Branch `feat/live-position`, worktree
`/home/severingartland/programing_linux/speakd-worktrees/livepos`.

`pipeline.speak()` now tells its caller which segment is starting to play, at the
moment it starts, and the daemon publishes `position` from that instead of from
the finished timeline.

## How everything here was run

This worktree has no `.venv` and `uv sync` is forbidden (it prunes the
hand-installed kokoro/sounddevice/scipy and the CPU-pinned torch, and re-resolves
to a CUDA build this machine cannot run). Every gate ran against the main
checkout's environment with this worktree's `src` ahead of it on `PYTHONPATH`:

```
cd /home/severingartland/programing_linux/speakd-worktrees/livepos
PYTHONPATH=/home/severingartland/programing_linux/speakd-worktrees/livepos/src \
UV_PROJECT_ENVIRONMENT=/home/severingartland/programing_linux/speakd/.venv \
uv run --no-sync <gate>
```

Verified that this is the code actually under test, not the main checkout's:

```
$ ... uv run --no-sync python -c "import speakd.pipeline as p; print(p.__file__)"
/home/severingartland/programing_linux/speakd-worktrees/livepos/src/speakd/pipeline.py
```

The editable install in that venv is a plain `.pth` adding the main checkout's
`src`; `.pth` entries are appended after `PYTHONPATH`, so this worktree wins.

`PYTHONDONTWRITEBYTECODE=1` was set and `__pycache__` cleared before every
mutation run, so no same-length mutant could alias to stale bytecode.

## Before and after, measured against the running daemon

`speakctl subscribe` cannot show this bug: the `played_at` inside each event is
correct even when the event is handed over late. Only the subscriber's own
arrival clock shows it. The probe
(`scratchpad/arrivals.py`) subscribes, enqueues on a
second connection, and prints `time.monotonic()` at the instant each event
arrives.

The daemon was the real one — kokoro, `StreamingPlayer`, `SoundDeviceSink`, real
audio out — run from this worktree on its own socket with the user service
stopped, so nothing was faked but the socket path.

**Before** (HEAD, `One. Two. Three. Four. Five.`):

```
  0.00s  enqueue -> ok=True
  0.00s  started   {'text': 'One. Two. Three. Four. Five.'}
  6.78s  position  'One.'    played_at=155078.302708243
  6.78s  position  'Two.'    played_at=155079.489918590
  6.78s  position  'Three.'  played_at=155080.641817310
  6.78s  position  'Four.'   played_at=155081.836797157
  6.78s  position  'Five.'   played_at=155083.073856130
  6.78s  finished  {'cancelled': False, 'aborted': False}
```

Every position in one burst at the end, though their own stamps span 4.77s.

**After** (same text, same daemon build, restarted on this branch):

```
  0.00s  enqueue -> ok=True
  0.00s  started   {'text': 'One. Two. Three. Four. Five.'}
  0.75s  position  'One.'    played_at=155498.167849092
  1.97s  position  'Two.'    played_at=155499.384818506
  3.12s  position  'Three.'  played_at=155500.536626625
  4.32s  position  'Four.'   played_at=155501.731606916
  5.55s  position  'Five.'   played_at=155502.968633057
  6.79s  finished  {'cancelled': False, 'aborted': False}
```

The arrival gaps (1.22, 1.15, 1.20, 1.23) match the `played_at` gaps (1.217,
1.152, 1.195, 1.237) to within a few milliseconds: delivery now tracks speech
rather than trailing it. `finished` still lands at 6.79s against 6.78s before, so
reporting live costs the utterance nothing.

A longer one, for the shape:

```
  0.00s  started   {'text': 'The first sentence. ... Finally the sixth.'}
  0.79s  position  'The first sentence.'
  2.31s  position  'The second sentence.'
  4.01s  position  'A third one here.'
  5.68s  position  'And a fourth.'
  7.09s  position  'Then a fifth.'
  8.53s  position  'Finally the sixth.'
 10.24s  finished  {'cancelled': False, 'aborted': False}
```

Five further utterances through the same live daemon left its thread count at 29
throughout, so the per-utterance notifier thread is not accumulating.

The daemon was then killed, its socket removed, and `systemctl --user restart
speakd` brought the user service back. Hush was never exercised mid-playback —
that is the live defect another branch is fixing.

## What was built

`speak(..., on_playing: Callable[[Segment], None] | None = None)`. Optional, so
every existing caller (`cli._say`, every test) is untouched.

The `Segment` is built *before* `player.play()` — at the existing
`started = time.monotonic()` stamp, which is already there precisely so
`played_at` marks when audio really begins — handed to the callback, and then
appended to the timeline after a successful `play()`. The same object goes both
ways, so a report and the timeline entry cannot drift.

`_Announcer` owns one thread and an unbounded queue per utterance:

- **A slow callback cannot hold up playback.** The speech thread only ever does
  an unbounded `Queue.put`, which cannot block. The callback is called from the
  announcer's own thread. Calling it inline is exactly the shape of the two
  liveness bugs already shipped here (the unbounded audio write; the subscriber
  that stopped reading its socket), and it is in the daemon that this matters:
  the callback ends inside an arbitrary bus subscriber.
- **The queue is unbounded on purpose.** A bound must choose between blocking the
  speech thread and dropping a report; dropping breaks the promise that nothing
  is spoken unreported. The utterance bounds it in practice — a few dozen
  sentences.
- **A raising callback is caught and recorded, and delivery carries on.** It lands
  in `SpeechResult.errors` exactly like a failed synthesis, which the daemon
  already republishes as an `error` event. `BaseException`, not `Exception`: a
  callback raising outside `Exception` would otherwise kill the delivery thread
  and take every later report with it, silently.
- **`speak()` waits 1.0s at the end for the queue to drain**, so all of an
  utterance's positions are out before the caller publishes `finished`. If the
  callback has not returned by then the wait is abandoned and
  `"position reports are still pending after 1.0s: the callback has not returned"`
  is added to `errors`. The thread is a daemon thread holding nothing but its own
  queue. Measured cost in the real daemon: none (the bus's socket outbox is
  already non-blocking), which is why `finished` lands at the same 6.8s as before.
- **Closed in the `finally`**, so a `BaseException` leaving `play()` does not
  strand the thread on a queue nobody will close.
- **No announcer at all when `on_playing` is None** — no thread, no queue.

The daemon's `_speak` defines a local `announce()` that publishes the `position`
event with the same five fields as before, passes it as `on_playing`, and the
post-hoc `for segment in result.timeline.segments:` loop is gone.

## Judgement calls

**1. The report goes out before `play()`, and that changes one behaviour.**

The brief asked whether the daemon publishes `position` for a segment whose
playback failed. Checked, not assumed — HEAD's code and this branch's were each
driven through a daemon with a sink that raises on its second `play()`:

```
HEAD:   positions=['One.']           errors=['player: PortAudioError: device unavailable']
branch: positions=['One.', 'Two.']   errors=['player: PortAudioError: device unavailable']
```

So it did **not**, and now it does. This is the one behaviour this change moves,
and it is not silent: it is documented at the call site, in `speak()`'s
docstring, in the daemon's `announce()`, and pinned by two tests
(`test_a_segment_whose_playback_failed_is_reported_but_not_in_the_timeline`,
`test_a_sentence_whose_playback_failed_is_still_announced`).

It cannot be avoided while the feature works. `play()` blocks for the length of
its audio, so a report sent after it describes a sentence already spoken; nothing
at the pre-play moment can know that `play()` is about to raise. The only way to
preserve the old silence exactly is to report after a successful `play()`, which
makes every highlight a full sentence late — the bug in a thinner disguise. The
mutation run below shows precisely that: moving the report after `play()` still
emits one position per segment, in order, with the right fields, and still fails
every liveness test.

Taking the change rather than the quirk is also the better half on its own
merits: `StreamingPlayer` writes a segment chunk by chunk, so a sink that fails
mid-write has already put part of that sentence through the speakers, and this
daemon's promise is that nothing spoken goes unreported. The old silence broke
that promise; the timeline-derived loop just made it invisible. The failure is
still reported on its own `error` event either way.

**What did not change:** `SpeechResult.timeline` still receives a segment only
after `play()` has succeeded, so it is still exactly what was played — the
span-to-time map, seek and resume read it, and the existing
`test_a_raising_player_keeps_the_timeline_built_so_far` still passes untouched.

**2. A raising callback keeps receiving later segments** rather than being dropped
the way `EventBus` drops a dead subscriber. A callback that trips over one
segment's text must not cost every later segment its report; the bus does its own
dropping one layer down, where a *subscriber* is the thing that died.

**3. One announcer thread per utterance**, not one long-lived one on the daemon.
The guarantee belongs to `speak()`, since `speak()` owns the play loop and the
callback is its parameter — a contract that says "your callback must be fast" is
the shape that produced the previous two liveness bugs.

**4. Named `on_playing`**, not `on_segment`: the pipeline makes segments in two
places (synthesis and playback) and this fires at the second.

**5. `errors` is copied out of the announcer rather than shared**, so a callback
that returns after the 1.0s wait cannot grow a list the daemon is already
iterating.

## TDD

Nine tests written first, run against unmodified code:

```
8 failed, 80 passed
FAILED tests/test_pipeline.py::test_a_segment_is_reported_while_it_is_still_being_spoken
FAILED tests/test_pipeline.py::test_every_segment_played_is_reported_exactly_once_and_in_order
FAILED tests/test_pipeline.py::test_a_raising_callback_does_not_cost_the_utterance
FAILED tests/test_pipeline.py::test_a_callback_that_never_returns_does_not_hold_up_playback
FAILED tests/test_pipeline.py::test_a_segment_whose_playback_failed_is_reported_but_not_in_the_timeline
FAILED tests/test_pipeline.py::test_reporting_leaves_no_thread_behind
FAILED tests/test_daemon.py::test_a_position_reaches_subscribers_while_that_sentence_is_still_playing
FAILED tests/test_daemon.py::test_a_sentence_whose_playback_failed_is_still_announced
```

The ninth, `test_every_sentence_is_announced_once_and_before_finished`, passed
against the broken code — deliberately. It is the regression guard the brief
warns about (positions *are* emitted today, all at once), so it carries none of
the liveness proof; its teeth are checked against a different mutant below.

After the implementation: `88 passed` for those two files.

## Teeth

Eleven mutants, each deleting or inverting the one production line a test rests
on, `__pycache__` cleared and `PYTHONDONTWRITEBYTECODE=1` for every run.

```
RED    the report itself: announce the segment that is about to play
         killed 8 test(s) -> 8 failed, 80 deselected
RED    exactly once: announce the same segment twice
         killed 3 test(s) -> 3 failed, 85 deselected
RED    off the speech thread: call the callback inline instead
         killed 1 test(s) -> 1 failed, 87 deselected
RED    draining before speak() returns: skip close() entirely
         killed 2 test(s) -> 2 failed, 86 deselected
RED    saying so when the callback never came back
         killed 1 test(s) -> 1 failed, 87 deselected
RED    the daemon actually asking for the reports
         killed 4 test(s) -> 4 failed, 84 deselected
RED    the daemon's fields: span_start dropped from the payload
         killed 3 test(s) -> 3 failed, 85 deselected
```

Two of the first pass were mis-specified and had to be redone, which is worth
recording because both would have counted as unearned kills:

- *"moved to after a successful play()"* deleted the report instead of moving it,
  making it mutant 1 over again.
- *"a raising callback is caught"* replaced `try:` with `if True:` and left the
  `except` behind — a `SyntaxError`, so the "kill" was the module failing to
  parse.

Respecified, with a parse check on the mutant before it is allowed to count:

```
RED    WHERE the report goes: moved to after a successful play()
         must die       -> 4 failed, 84 deselected
         must stay green -> 2 passed, 86 deselected
RED    a raising callback is caught, not left to kill delivery
         must die       -> 1 failed, 87 deselected
```

That first one is the load-bearing result. The plausible wrong implementation —
reporting each segment once it has played — still emits exactly one position per
segment, in order, with every field right, and the two "emitted correctly" tests
stay green against it. Only the four liveness tests kill it. That is the property
that was actually missing, and it is the one being asserted.

All nine new tests use threads, so they were run 20× in fresh subprocesses with
`__pycache__` cleared between runs: `passed=20 failed=0`.

## Gates

Each run as its own command, nothing piped into anything.

```
$ ... uv run --no-sync pytest -q
6 failed, 550 passed, 4 warnings in 50.80s
FAILED tests/test_cc_hook.py::test_no_payload_ever_reaches_claude_code_as_a_traceback
FAILED tests/test_cc_hook.py::test_a_prompt_against_an_unreachable_daemon_stays_inside_its_budget
FAILED tests/test_cc_plugin_manifest.py::test_the_wrapper_falls_back_to_the_checkout_venv
FAILED tests/test_cc_plugin_manifest.py::test_a_well_formed_payload_leaves_the_log_alone
FAILED tests/test_cc_plugin_manifest.py::test_an_installed_copy_finds_the_interpreter_through_speakd_home
FAILED tests/test_cc_plugin_manifest.py::test_with_home_unset_both_halves_pick_the_same_log
```

Those six fail identically on the unmodified branch point and are environmental,
not a regression: each shells out to `<repo>/.venv/bin/...`, and this worktree has
no `.venv` because `uv sync` is forbidden here. One says so outright —
`assert entry.exists(), "run 'uv sync --group dev' first"` — and the same six
pass in the main checkout, which has one:

```
$ cd /home/severingartland/programing_linux/speakd && uv run --no-sync pytest -q \
      tests/test_cc_plugin_manifest.py tests/test_cc_hook.py
51 passed in 5.91s
```

```
$ ... uv run --no-sync ruff check .
All checks passed!

$ ... uv run --no-sync ruff format --check .
79 files already formatted

$ ... uv run --no-sync mypy src tests
Success: no issues found in 67 source files
```

(79, not the 67 Python files: this ruff formats Python inside Markdown too, and
this report is in the tree. Without it the count is 78.)

## Concerns

- **The failed-playback position is a real behaviour change**, argued above and
  pinned by tests, but it is a change. If the project would rather keep the old
  silence, the only implementation that does costs a full sentence of lag, and
  the mutation run shows exactly what that trade looks like.
- **A callback wedged forever leaks one thread per utterance.** Bounded waiting was
  chosen over unbounded, matching the existing synthesis-thread join, and it is
  reported in `errors` — but a caller whose callback never returns will
  accumulate threads. In the daemon that cannot happen: the bus's socket outbox
  is `put_nowait` onto a bounded queue and drops the subscriber instead.
- **Hush mid-playback was not exercised**, as instructed; that wedge is another
  branch's.
- **Six tests cannot run in a `.venv`-less worktree at all.** Nothing to do here
  without `uv sync`, but it does mean this branch's gates are 6 tests thinner
  than CI's.
