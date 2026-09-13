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

Early. The design is settled; the implementation is being written.

## Development

```bash
uv sync                 # core + dev tooling
uv sync --extra kokoro  # add the speech engine (needs Python < 3.14)
uv run pytest
uv run ruff check .
```

## License

MIT
