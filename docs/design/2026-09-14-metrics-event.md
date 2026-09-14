# The metrics event

The pinned monitor shows four numbers: RTF, drift, memory, queue. Three of
them are only knowable inside the daemon, and none of them is emitted today.

## The contract

One event kind, `metrics`, on the existing bus:

```json
{"event": "metrics", "source_id": "", "data": {
  "rtf": 0.75,
  "drift": 0.18,
  "mem_bytes": 1632087232,
  "queue": 2
}}
```

- **`rtf`** — synthesis seconds per second of audio produced, over a trailing
  window rather than since boot, so it tracks what the machine is doing now.
  Measured on this machine at 0.68-0.88 depending on segment length, with a
  fixed cost of about 1s per call that dominates very short segments.
- **`drift`** — how far the measured clock has fallen behind the nominal one:
  `played_at` elapsed minus `audio_offset` elapsed, across the current
  utterance. This is the lagging indicator the design doc names: RTF says
  whether we are keeping up, drift says whether we have already failed to.
- **`mem_bytes`** — the daemon's resident set. The user's machine has 15 GiB
  and runs a desktop; a speech daemon holding 1.5 GiB of torch is worth a
  glance. Read from `/proc/self/statm` rather than a dependency.
- **`queue`** — utterances accepted and not yet finished, the daemon's own
  pending count.

## When it fires

Once a second while anything is speaking, and once more on the transition to
idle so the display settles rather than freezing at the last busy value.

Not while idle: a monitor that is quiet because nothing is happening should
not be producing traffic, and a GUI that has been told nothing since the last
`finished` knows the numbers are stale in the only way that matters.

## Why an event rather than a poll

The bus exists, subscribers are already bounded and dropped when they stop
reading, and polling would add a second control path with its own timeout and
failure mode. A monitor that misses a tick shows a slightly old number for a
second; a monitor that polls a wedged daemon blocks.

## What it must not do

Computing these must not touch the speech worker's critical path. Gather on
the emitting thread from state the worker already maintains; never make the
worker wait on a measurement.
