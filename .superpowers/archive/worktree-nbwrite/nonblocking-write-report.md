# The speech worker never blocks inside PortAudio

Branch `feat/nonblocking-write`, worktree
`/home/severingartland/programing_linux/speakd-worktrees/nbwrite`.

`SoundDeviceSink.write()` no longer hands PortAudio a blocking write. It asks
the stream how much room it has and hands over at most that much, looping with
a 5 ms sleep until the device has taken everything. The write watchdog merged
this morning is removed; the argument is at the end.

---

## How everything below was run

This worktree has no `.venv` of its own, and `uv sync` was never run in any
form. Every gate ran against the main checkout's environment with this
worktree's `src` in front of it:

```
cd /home/severingartland/programing_linux/speakd-worktrees/nbwrite
PYTHONDONTWRITEBYTECODE=1 \
PYTHONPATH=/home/severingartland/programing_linux/speakd-worktrees/nbwrite/src \
uv run --no-sync --project /home/severingartland/programing_linux/speakd <gate>
```

Confirmed to resolve the worktree, not the checkout:

```
$ ... uv run --no-sync --project .../speakd python -c "import speakd, sys; print(speakd.__file__); print(sys.executable)"
/home/severingartland/programing_linux/speakd-worktrees/nbwrite/src/speakd/__init__.py
/home/severingartland/programing_linux/speakd/.venv/bin/python3
```

The teeth and repetition runs below call that same interpreter directly
(`/home/severingartland/programing_linux/speakd/.venv/bin/python -m pytest`),
because they drive pytest from a Python harness.

---

## The reproduction, before

Daemon at `501214f` (the merged watchdog), running from the main checkout.

```
[17:31:30.426] enqueue
[17:31:30.659] enqueued
[17:31:36.661] hush
[17:31:36.908] hush returned rc=0
[17:31:39.913] second enqueue (does the daemon still speak?)
[17:31:40.148] second enqueue returned rc=0
```

The bus, with the metrics ticker filtered out, shows one `started` and nothing
else. No `finished`. No speech after the hush:

```
{"event": "started", "source_id": "wedge", "data": {"text": "One. Two. ..."}}
```

The ticker went on firing with frozen values for the remaining fifty seconds —
`rtf` and `drift` and `mem_bytes` identical from 17:31:3x on, `queue` climbing
from 1 to 2 as the second utterance was accepted and never spoken.

The journal shows the watchdog doing exactly what it was designed to do:

```
sep. 14 17:31:41.797494 fedora speakd[1514005]: speakd: audio device wedged:
  a 2048-frame write did not return within 5.0s; the stream was aborted to
  release the speech thread
```

and a `py-spy dump` taken a quarter of an hour later shows the abort achieved
nothing:

```
Thread 1514296 (idle): "speakd-speech"
    _raw_write (sounddevice.py:1317)
    write (sounddevice.py:1583)
    write (speakd/player.py:397)
    play (speakd/player.py:697)
    speak (speakd/pipeline.py:128)
    ...
Thread 1514424 (idle): "speakd-write-watchdog"
    wait (threading.py:355)
    _watch_loop (speakd/player.py:546)
```

The premise is confirmed dead: `Pa_AbortStream` does not release a thread
blocked in `Pa_WriteStream` on this stack.

## The reproduction, after

Same script, same machine, the user service pointed at this worktree through a
temporary systemd drop-in (`Environment=PYTHONPATH=.../nbwrite/src`, since
removed — see *State left behind*).

```
[17:50:42.278] enqueue
[17:50:42.517] enqueued
[17:50:48.536] py-spy dump mid-playback
[17:50:48.597] hush
[17:50:48.846] hush returned rc=0
[17:50:51.851] py-spy dump after the hush
[17:50:51.913] second enqueue (does the daemon still speak?)
[17:50:52.138] second enqueue returned rc=0
[17:51:02.141] third enqueue
```

Mid-playback the speech thread is not in PortAudio at all. It is in its own
sleep, in this worktree's code:

```
Thread 1540636 (idle): "speakd-speech"
    _hand_over (.../nbwrite/src/speakd/player.py:455)   <- time.sleep(_WRITE_POLL_INTERVAL)
    write (.../nbwrite/src/speakd/player.py:378)
    play (.../nbwrite/src/speakd/player.py:629)
    speak (.../nbwrite/src/speakd/pipeline.py:128)
```

Three seconds after the hush it is back on the queue, released:

```
Thread 1540636 (idle): "speakd-speech"
    wait (threading.py:355)
    get (queue.py:171)
    _consume (.../nbwrite/src/speakd/daemon.py:528)
```

There is no `speakd-write-watchdog` thread in either dump, because the sink no
longer starts one.

The bus shows the hush landing cleanly and the daemon speaking twice more:

```
{"event": "finished", "source_id": "wedge",  "data": {"cancelled": true,  "aborted": false}}
{"event": "started",  "source_id": "after",  "data": {"text": "Alpha. Bravo. Charlie."}}
{"event": "finished", "source_id": "after",  "data": {"cancelled": false, "aborted": false}}
{"event": "started",  "source_id": "after",  "data": {"text": "Delta. Echo."}}
{"event": "finished", "source_id": "after",  "data": {"cancelled": false, "aborted": false}}
```

Both later utterances were audible. Nothing reached the journal.

### Hush timings, real device, three trials

`speakctl hush` to the `finished` event on the bus, wall clock:

| trial | hush sent | hush returned | `finished` | send -> finished | return -> finished |
|---|---|---|---|---|---|
| 1 | …167.430 | …167.737 | …168.295 | 0.865 s | 0.558 s |
| 2 | …176.965 | …177.232 | …177.351 | 0.386 s | 0.119 s |
| 3 | …186.510 | …186.773 | …187.159 | 0.649 s | 0.385 s |

Read these carefully: 0.26-0.31 s of each is `speakctl` itself starting a
Python interpreter before the request is even sent, and the remainder is
dominated by `speak()`'s drain-to-sentinel, which waits out one in-flight
`synthesize()` call by design (`pipeline.py`, the `finally` block). Neither is
the sink. Audio stops at `player.stop()` -> `sink.stop()` -> `stream.abort()`,
which discards the device buffer immediately.

## Hush responsiveness, measured

The budget to protect was 0.256 s of audio after the hush. Measured against
the modelled device, ten seconds of audio with a hush two seconds in, counting
frames the sink took after `stop()` was called:

```
hand-over loop (this branch): 0.043s of audio after the hush; play() returned: True
```

0.043 s is one 1024-frame block — the loop was mid-poll, saw the abort on its
next attempt, and stopped. Well inside the budget. `StreamingPlayer` and
`FakeSink` are untouched by this change, so the chunk-granularity half of that
budget (85 ms at 2048 frames) is unchanged by construction.

## Underruns, measured on the real device

`OutputStream.write()` returns `True` when the device ran dry since the last
call. Neither the old sink nor the new one looks at that return value, so it
was asked here explicitly: five seconds of a 440 Hz tone, 2048-frame chunks,
three trials each way.

```
blocking (old)   trial 1: 0 underruns in  59 writes, 4.918s for 5.000s of audio
blocking (old)   trial 2: 0 underruns in  59 writes, 4.944s for 5.000s of audio
blocking (old)   trial 3: 0 underruns in  59 writes, 4.930s for 5.000s of audio
hand-over (new)  trial 1: 0 underruns in 174 writes, 4.934s for 5.000s of audio
hand-over (new)  trial 2: 0 underruns in 174 writes, 4.940s for 5.000s of audio
hand-over (new)  trial 3: 0 underruns in 174 writes, 4.930s for 5.000s of audio
```

No underruns either way, and identical pacing. The new loop makes about three
times as many `write()` calls because it hands over 1024-frame pieces once the
buffer is primed. The margin it gives up is small: measured on this device the
ring buffer holds 2048 frames and the callback takes a 1024-frame block every
42.7 ms, so a 5 ms poll finds the buffer still holding some 38 ms of audio.

## What the real device actually does, probed first

The design rests on three facts about this stack, so they were measured before
anything was written rather than assumed:

```
opened; latency: 0.0427  blocksize: 1024
write_available right after start: 2048
handed over 48000 frames in 1.884s with 415 polls
first 6 (t, room, wrote, write_ms):
  [(0.0001, 2048, 2048, 0.074), (0.0052, 1056, 1056, 0.047), (0.0461, 1024, 1024, 0.053), ...]
max single write() ms: 0.104
aborted at 2.085
write_available never raised; last value: 2048
write() on aborted stream raised: PortAudioError('Stream is stopped', -9983)
write_available on aborted stream: 2048
```

1. A write of no more than `write_available` frames returns in 0.04-0.10 ms.
   It genuinely does not wait.
2. The hand-over paces itself in real time (1.884 s for 2 s of audio), so the
   pipeline keeps using playback as its clock exactly as before.
3. **`write_available` keeps reporting the whole buffer free after an
   `abort()`.** It is not an escape hatch. The loop therefore checks the
   sink's own `_aborted` under `_lock` before every attempt, and the fake
   models this rather than the kinder behaviour, so a sink that trusted the
   stream would be caught.

## TDD

Red first. The fake `sounddevice` stream grew a ring buffer (`write_available`,
`stop_draining()`, `drain_at()`), the watchdog tests were replaced with
hand-over tests, and the suite was run against the unmodified sink:

```
15 failed, 39 passed in 4.31s
```

Then the implementation, then:

```
54 passed in 3.44s
```

One of those fifteen was my own test failing its realism check rather than the
code: `test_a_device_that_keeps_draining_is_never_called_wedged` completed in
5 ms, so it could not have proved that a write outlasting the stall timeout is
not a wedge. The fake's drain model gained an `every` parameter so a slow
device really does make the loop wait, and the test now asserts
`elapsed > 4 * timeout` so it fails if it ever stops proving its point.

## Teeth

Thirteen mutants, each deleting or inverting the exact production line a test
rests on, each run with `PYTHONDONTWRITEBYTECODE=1` and `__pycache__` cleared:

```
RED    the cap on how much is handed over: min(room, ...) -> everything
RED    the stall verdict: never conclude the device has stopped
RED    the raise that abandons the utterance
RED    the wedge's entry in the error log
RED    the wedge's line to the journal
RED    counting the frames the device actually took
RED    taking the dead stream out of service
RED    the loop's own look at whether it has been stopped
RED    the guard that keeps the wedge path off a healthy write
RED    progress restarting the stall clock
RED    re-raising a device failure that was not ours
RED    recording the frames a hush cost
RED    the absence of a background thread (inverted: start one)

all 13 mutants were caught
```

`test_the_sink_runs_no_threads_of_its_own` pins an absence, so its teeth were
checked the other way round: a thread was added to `start()` and the test went
red.

Repetition, fresh subprocess each time, `__pycache__` cleared first:

- `tests/test_player.py` — 20 runs, `54 passed` every time, 0 failures.
- `tests/test_streaming_player.py tests/test_pipeline.py` — 20 runs,
  `33 passed` every time, 0 failures.

## The gates

Each run as its own command, nothing piped.

```
$ ruff check .
All checks passed!

$ ruff format --check .
78 files already formatted

$ mypy src tests
Success: no issues found in 67 source files

$ pytest
...
6 failed, 544 passed
```

**The six failures are the worktree having no `.venv`, not a regression.** The
first one says so itself:

```
venv_bin = Path(__file__).resolve().parent.parent / ".venv" / "bin"
entry = venv_bin / "speakd-claude-hook"
assert entry.exists(), "run `uv sync --group dev` first"
```

All six are in `test_cc_hook.py` and `test_cc_plugin_manifest.py`, which shell
out to the console script in the checkout's own venv. They touch nothing this
change touches. Proven twice:

- stashing this branch's changes and running them on the pristine base in this
  worktree gives the identical `6 failed, 45 passed`;
- running them in the main checkout, which has a venv, gives `51 passed`.

Creating the venv would have meant `uv sync`, which is forbidden here and
would have pruned the hand-installed kokoro, sounddevice, scipy and CPU-pinned
torch. Excluding those two files, the suite is `499 passed`.

The remaining warnings are `test_transport.py`'s deliberate
`RuntimeError("can't start new thread")` fault injection, which is
pre-existing; promoting it to an error confirms it is the only source.

`mypy` needed one accommodation. `if ... or self._stream is not stream` narrows
the caller's non-optional handle to `Any | None` for the rest of the function,
because mypy's `is` narrowing runs in both directions. Rather than write dead
`None` checks apologising for a state the caller cannot be in, the comparison
moved into a one-line `_is_current(stream: object) -> bool`.

## The watchdog: removed

Removed, with the evidence for each half of the decision.

**Its mechanism is disproven.** The watchdog's entire premise was that
"the abort is the only thing that releases a `sounddevice` write which has
stopped returning". The dump above shows the speech thread still in
`_raw_write` minutes after the watchdog aborted its stream and struck it off
`_inflight`. On this stack `Pa_AbortStream` does not release a blocked
`Pa_WriteStream`. As a release mechanism it is dead, and the journal line it
printed — "the stream was aborted to release the speech thread" — was untrue
at the moment it was written.

**The failure it was built for is now seen by the thread that suffers it.**
The production wedge is a device that stops draining: the callback stops
running, the ring buffer stays full, and a blocking write waits for room that
never comes. In that state `write_available` is 0 and stays 0 — which is
precisely what the loop watches. The writer detects it, reports it to
`sink.errors` and to the journal, takes the dead stream out of service, and
raises `AudioDeviceWedged` on its way out, all from the thread that would
otherwise have been the casualty. Nothing is left for a second thread to
notice.

**What it could still have caught, it could not have acted on.** The one
residual hypothetical is a `stream.write()` of no more than `write_available`
frames blocking anyway, against PortAudio's documented contract. A watchdog
could print a line about that; it could not end it, for exactly the reason
above. That is a diagnostic, and a weak one, bought with a thread per sink, a
condition variable sharing the sink's lock, an in-flight register, a
timed-out set, a join with its own timeout, and the ability to abort a live
stream out from under a healthy writer. The same diagnostic is available from
outside, which is how both of today's bugs were actually found: the metrics
ticker firing with frozen values, and `py-spy`.

**Keeping it would also have been actively wrong now.** Every remaining
power it has is a hazard in the new design. Its abort sets `_aborted`, which
stops a sink whose writer may be perfectly healthy; its `_timed_out` set
decides a write's outcome from another thread, duplicating a verdict the loop
now reaches itself; and its own failure mode — the parked
`_watch_loop` in the dump — is one more thread to reason about in a class
whose whole difficulty is threads.

`AudioDeviceWedged`, its `sink.errors` entry, and its journal line are all
kept. Only the thread and the abort-from-outside are gone. The wording changed
because the old one described a mechanism that no longer exists and never
worked:

```
audio device wedged: 2048 frames went unwritten because the device took
nothing for 5.0s; the stream was given up rather than waited on
```

The timeout changed shape with it. `_MIN_WRITE_TIMEOUT` plus
`_WRITE_TIMEOUT_FACTOR` existed because a per-write bound has to be scaled by
the size of the write or it cuts off a caller handing over ten seconds of
audio. `_STALL_TIMEOUT` bounds a stall instead: any frame the device accepts
restarts the clock, so a long hand-over is never cut off and the factor is not
needed. `test_a_device_that_keeps_draining_is_never_called_wedged` is the old
scaling test rewritten to pin the stronger guarantee.

## Concerns

- **The wedge-detection path is not verified against genuinely dead hardware.**
  Its escape path is (the real-device runs above), its semantics are (the
  probe above), and its logic is (tests, teeth verified). But a device that
  stops draining could not be induced on demand: `pactl suspend-sink` does not
  do it, because PipeWire keeps draining the client node and merely discards
  the output — `no stall: all 192000 frames handed over in 7.93s`. If you know
  a way to stall the real sink, that is the one gap worth closing.
- **`_abandon()` calls `abort()` from the speech thread.** The evidence says
  this is safe — the watchdog's aborts returned promptly, it is the *release*
  they were supposed to cause that this stack does not honour — but it is the
  one device call on the wedge path that is not bounded by anything we own.
- **`close()` still leaks the stream handle when a writer is in flight**, and
  with the loop polling every 5 ms that writer is usually only microseconds
  from returning. Waiting a poll interval or two in `close()` would let it
  actually close the device in the common case. Pre-existing, out of scope,
  and worth a look.
- **The six failing tests are still failing in this worktree** and will fail
  for anyone who runs the suite from a worktree rather than the checkout.
  Deriving the venv from `SPEAKD_HOME` with a fallback, or skipping when the
  console script is absent, would make the suite worktree-portable.

## State left behind

The temporary systemd drop-in that pointed the user service at this worktree
was removed and the daemon restarted; `/proc/<pid>/environ` confirms no
`PYTHONPATH`. **The running daemon is back on the merged code and still
wedges on a mid-playback hush.** Merging this branch is what fixes the
machine.
