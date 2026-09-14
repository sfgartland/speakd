# speakd

Streaming speech synthesis daemon for local AI agents.

Most TTS integrations for coding agents wait for the complete response, synthesise
it in one go, and only then start playing. `speakd` streams: it splits into
sentences, starts speaking the first one while later ones are still synthesising,
and publishes a map of which source text is being spoken when — so other software
can follow along.

## Design

One daemon owns the model, the queue, the plugin host and the API. Everything else
is a client: editor plugins, agent hooks, document readers.

```
hooks/CLI ──enqueue──▶ speakd ──▶ transforms ──▶ segmenter ──▶ engine ──▶ player
                         │                                       │
                         └────── event bus ◀── span-to-time map ◀─┘
                                    │
                         out-of-proc subscribers (PDF highlighter, …)
```

**The span-to-time map** is the core data structure. Every synthesised segment
records which source offsets it covers and when it plays. Playback position,
seeking, resume-where-you-left-off and follow-along highlighting are all reads of
it.

**Channels, not one queue.** Each source — an agent session, a document reader —
has a role, a priority and a profile. Foreground channels are read fully;
background channels emit only what their profile marks as worth interrupting for.
Arbitration is deterministic: an agent changes its own standing by sending a
control verb, never by being reasoned about. The intelligence lives in the agents;
the daemon stays dumb and fast.

**Two plugin tiers.** Transforms run in-process because they sit on the path to
first audio. Effects and integrations run out-of-process over the API, so they can
be written in any language and cannot take audio down when they crash.

Anything opinionated is a plugin. Markdown handling, citation reading and LLM
summarisation are transforms, not core features — which is what keeps
domain-specific behaviour out of everyone else's install.

## Status

The daemon works. It speaks over a Unix socket, streams position events as it
goes, and can be interrupted mid-word.

Built and merged: the speaking pipeline, the plugin host and built-in
transforms, the streaming player, the daemon, and the transcript reader for the
Claude Code client. The client's hooks and the desktop shell are in progress.

## Using it

```bash
uv run speakd &                                        # start the daemon
uv run speakctl enqueue "Hello." --source mine         # speak; returns at once
uv run speakctl hush   --source mine                   # stop talking, drop the queue
uv run speakctl cancel --source mine                   # skip this one, queue continues
uv run speakctl pause  --source mine
uv run speakctl resume --source mine
uv run speakctl subscribe                              # live events, JSON lines
uv run speakctl status                                 # channels, as JSON
uv run speakctl say "No daemon needed."                # speak locally, in-process
```

`enqueue` returns as soon as the daemon accepts the text, not when it stops
speaking — which is what lets an agent hook call it and get out of the way.

The event stream is the same one the GUI reads:

```json
{"event": "started",  "source_id": "mine", "data": {"text": "One. Two."}}
{"event": "position", "source_id": "mine", "data": {"text": "One.", "span_start": 0,
                                                    "audio_offset": 0.0, "played_at": 89731.96}}
{"event": "finished", "source_id": "mine", "data": {"cancelled": false, "aborted": false}}
```

Two clocks, deliberately: `audio_offset` is nominal position in the synthesised
audio, `played_at` is `time.monotonic()` when it actually reached the device.
The gap between them is the drift a monitor should show.

## Measured

On an i7-10510U with Kokoro on CPU (RTF 0.75). **Read the provenance** — some of
these come from a substitute rig rather than a real device, and the difference
matters:

| | | |
|---|---|---|
| Time to first audio, 417-char passage | 20.2s batched -> **10.9s** streamed | real device |
| Audio after a hush | 1.816s -> **0.256s** | substitute rig |
| `speakctl enqueue` return | **0.27s**, five sentences still queued | real device |
| Hook latency, against a 3s budget | **0.22s** | real device, no daemon |

The hush figures are a sound *contrast* — both arms ran the identical script —
but the absolute numbers were taken with a sleeping fake sink and want
re-measuring on hardware. That rig reported a clean shutdown for code that
segfaulted inside ALSA the first time it met a sound card, so its silences are
not evidence.

## Development

```bash
uv sync --group dev     # core + dev tooling
uv sync --extra kokoro  # add the speech engine (needs Python < 3.14)
uv run pytest
uv run ruff check . && uv run ruff format --check . && uv run mypy src tests
```

On a machine whose GPU predates the CUDA wheel's minimum (an MX250 is sm_61),
install CPU torch explicitly and then use `uv run --no-sync`, because a plain
`uv sync` resolves `torch` back to the CUDA build:

```bash
uv pip uninstall torch
uv pip install torch --index-url https://download.pytorch.org/whl/cpu
```

## License

MIT
