# speakd

Streaming speech synthesis daemon for local AI agents.

Most TTS integrations for coding agents wait for the complete response, synthesise
it in one go, and only then start playing. `speakd` streams: it splits into
sentences, starts speaking the first one while later ones are still synthesising,
and publishes a map of which source text is being spoken when — so other software
can follow along.

## Design

One daemon owns the model, the queue, the plugin host and the API. Everything else
is a client: editor plugins, agent followers, document readers. The daemon spawns
and restarts the clients that have to outlive a single command — the Claude Code
follower is one — so there is still only one service to start and stop.

```
follower/CLI ──enqueue──▶ speakd ──▶ transforms ──▶ segmenter ──▶ engine ──▶ player
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
transforms, the streaming player, the daemon, and the Claude Code client — its
two hooks, its transcript reader, and the follower that speaks a session as it
is written. The desktop shell is in progress.

## Running it at login

```bash
./packaging/systemd/install-service.sh     # writes the unit with this checkout's paths
systemctl --user daemon-reload
systemctl --user enable --now speakd
```

Then `systemctl --user status speakd` and `journalctl --user -u speakd -f`.

It takes about half a minute to load the model. Nothing waits on that: the
socket appearing at `$XDG_RUNTIME_DIR/speakd/speakd.sock` is the readiness
signal, and `speakctl` prints its own "no daemon" line until then.

The unit also exports `SPEAKD_HOME`, which is how the Claude Code plugin finds
this checkout after Claude Code has copied it elsewhere. Add the same line to
your shell profile — hooks run from your shell, not from systemd:

```bash
export SPEAKD_HOME=/path/to/speakd
```

## Using it

```bash
uv run speakd &                                        # start the daemon
uv run speakctl enqueue "Hello." --source mine         # speak; returns at once
uv run speakctl hush                                   # stop everything, drop the queues
uv run speakctl hush   --source mine                   # stop one channel; the rest speak on
uv run speakctl cancel --source mine                   # skip this one, that queue continues
uv run speakctl pause  --source mine
uv run speakctl resume --source mine
uv run speakctl seek --by -1                           # back a sentence (--index N to jump)
uv run speakctl speed 1.2                              # listen faster; `speed` alone prints it
uv run speakctl mute                                   # silence everything, at once
uv run speakctl unmute
uv run speakctl mute   --source mine                   # silence one channel only
uv run speakctl disable                                # unload the model (see below)
uv run speakctl enable                                 # load it again, tens of seconds
uv run speakctl subscribe                              # live events, JSON lines
uv run speakctl status                                 # channels, queue, mute, engine
uv run speakctl say "No daemon needed."                # speak locally, in-process
```

`enqueue` returns as soon as the daemon accepts the text, not when it stops
speaking — which is what lets a caller on an agent's critical path get out of the
way. The Claude Code client no longer has such a caller: its hooks only hush and
register, and the prose is enqueued by the follower, which runs outside anyone's
turn and speaks a message the moment it reaches the transcript.

**Mute and disable are two depths of the same switch.** Mute is instant and
instantly undone: a muted channel's text is accepted and *dropped*, never held, so
unmuting does not release ten minutes of backlog — and because it is dropped at
`enqueue`, nothing is synthesised and the channel costs no CPU. What it does not do
is give back the model's memory. `disable` unloads the model and charges tens of
seconds to load it back; while disabled, nothing loads it implicitly, so a stray
enqueue cannot undo the decision by accident. Both survive a restart.

**How much memory `disable` actually returns, measured** — because the honest
answer is less than you would hope:

| | RSS |
|---|---|
| running, model loaded | 2.19 GiB |
| after `speakctl disable` | 1.77 GiB |
| **started** while disabled | **0.03 GiB** |

Unloading at runtime gives back 0.42 GiB, not the two gigabytes the model
occupies: dropping the reference frees the tensors to Python, and torch's
allocator and CPython's arenas keep the address space. If you want the memory
genuinely back, disable and then restart the daemon — because the flag persists,
it comes back up in 32 MB and never builds the model at all.

**`hush` and `cancel` are scoped by the source you name.** With no `--source`
they stop the whole daemon, as they always did; with one they reach that channel
and no other, so silencing a session that has run on leaves the rest talking and
the next channel's queued speech starts straight away. This changed on
2026-09-15 — the verbs used to ignore the source entirely — and it fixes as much
as it adds: the Claude Code hook sends one hush per channel on every prompt, so
under the old meaning typing a prompt in one session silenced a second session
that had said nothing. The response reports the scope the daemon actually
applied, rather than leaving a caller to assume it matched the request.

**`status` reports what is waiting**, in play order, next first — the utterance
being spoken is not in it, having already left the queue. An accepted job also
announces itself on the bus as `queued` with the depth it joined, so a monitor
learns about speech when it is accepted rather than minutes later when it
starts. Both carry the first 200 characters of the text, not the whole of it.

Global mute and per-channel mute are separate flags, not one setting written twice:
a channel stays silent under a global mute whatever its own flag says, and clearing
the global one gives each channel back what it had.

**Briefings.** An agent can tell you what matters instead of having every
response read out: that it finished and what came of it, that it is stuck, or
that it has a question. It decides when, through an MCP server speakd ships,
`speakd-mcp`, whose `brief` tool it calls like any other. There is no
summariser in speakd — the agent doing the work is the one that knows what is
worth saying.

Each session is in one of two modes, beside mute:

| mode | spoken |
|---|---|
| **brief** | what the agent chooses to tell you, and alerts it cannot give itself |
| **full** | every response, in full, as the follower reads it |

The alerts are Claude Code's own "needs your permission" notices, and a plain
"‹session›: finished" when a turn ends without the agent having briefed —
the hooks send those, since an agent waiting at a prompt cannot. Briefings and
alerts are spoken with the session's name in front, because with several
sessions running you need to know who is talking.

**New Claude Code sessions start brief and muted.** Unmute the ones you want
to hear; switch one to full when you are following it closely:

```bash
uv run speakctl mode full --source claude-code:<session>   # every response
uv run speakctl mode brief --source claude-code:<session>  # briefings only
```

Only a session with something that can brief for it has a mode; everything
else — pasted text, notifications, a bare `speakctl enqueue` — is always read in
full. A daemon restart puts every session back to brief and muted.

**What agents are told to brief about** is a standing guide every agent is
handed when it connects: the built-in default, or yours, in
`~/.config/speakd/briefing.md`. Anything you tell an agent in its own
conversation ("only tell me when the migration is done") takes precedence.
The agent can also read and change its session's mode, so "read everything to
me" said to it works too.

The Claude Code plugin ships the MCP server (`clients/claude-code/.mcp.json`).
For other agents, register it once:

```bash
codex mcp add speakd -- /path/to/speakd/.venv/bin/speakd-mcp
```

— or the equivalent `mcpServers` entry for OpenCode. Such an agent's channel is
named after it and its working directory.

The event stream is the same one the GUI reads:

```json
{"event": "queued",   "source_id": "mine", "data": {"text": "One. Two.", "pending": 2}}
{"event": "started",  "source_id": "mine", "data": {"text": "One. Two."}}
{"event": "position", "source_id": "mine", "data": {"text": "One.", "span_start": 0,
                                                    "audio_offset": 0.0, "played_at": 89731.96}}
{"event": "finished", "source_id": "mine", "data": {"cancelled": false, "aborted": false}}
{"event": "metrics",  "source_id": "",     "data": {"rtf": 0.75, "drift": 0.18,
                                                    "mem_bytes": 1632087232, "queue": 2}}
```

Two clocks, deliberately: `audio_offset` is nominal position in the synthesised
audio, `played_at` is `time.monotonic()` when it actually reached the device.
The gap between them is the drift a monitor should show.

`metrics` carries that gap already worked out, with the real-time factor over
the last few segments, the daemon's resident set and its pending count. Once a
second while anything is speaking, and once more as it goes idle so a display
settles rather than freezing on the last busy value — and nothing in between,
because a monitor that has heard nothing since `finished` knows its numbers are
stale in the only way that matters.

## The window

`clients/gui/` is a 384px always-on-top monitor meant to sit beside an editor. It
shows the whole utterance as it was written — markdown rendered, headings, lists
and code included — with the sentence being spoken lit and the view following it,
so you can read along. Below that are a scrubber over the sentences and four
numbers: synthesis speed against realtime, drift, resident memory and queue depth.
A sentence still being synthesised — the first of every message, or one you have
jumped to that was not made yet — shows the same mark at half strength, pulsing,
until its first sound; the daemon announces it as a `preparing` event.

← and → move a sentence back or forward, and Ctrl+click on a sentence plays
from there; a plain click does nothing, so reading and selecting never move
playback. Going back is immediate: the utterance keeps the audio it has already
made. Both still work once the speaking has stopped — the daemon keeps the last
utterance, so ← plays the sentence it ended on again and Ctrl+click replays it
from anywhere (`replay` on the wire, which can only repeat what was said).
− and + change the speed in tenths, from 0.7× to 1.6×. The change is heard at
once: audio already made is time-stretched (pitch kept), and everything after is
synthesised at the new speed, which sounds better than any stretch. Faster
listening makes the synthesiser work harder, so watch the rtf number: above 1×
it can no longer keep ahead. With the window focused, the arrow keys, `-`/`+`
and space do the same.

**Skimming.** Hold Ctrl+→ and the voice ramps up to 1.4× over about a second;
let go and it ramps back to where it was. Ctrl+← does the same down to 0.7×, for
the passage you want to hear carefully. Add Shift to lock it there instead —
Ctrl+Shift+→ skims until you press it again. Plain ← and → still move a sentence.
Every speed change is stretched from the audio as it was made, never from an
earlier stretch, so a ramp does not smear the voice. `speakctl speed 1.4 --ramp 1`
ramps from the command line.

Both off switches are in it, and they follow `speakctl`: mute one from the CLI and
the window's button moves, without a reload. Behind a disclosure — closed by
default, because the window's whole argument is that it is small — are the open
channels, each with a mute of its own and each named by its label rather than its
session id, and a box that speaks text you paste into it (Ctrl+Enter, 8 KiB cap).

The channel list is ranked by relevance: starred channels first, then the ones
you can hear, then muted ones, each by how recently it last tried to speak (a
muted session that just finished still rises). A channel you hide folds away
behind one line and stays there until you show it again; mute is left for
silencing a channel for a moment, without it leaving the list. Each row shows how long ago the channel spoke,
and a channel that can brief has a **brief / full** switch beside skip and mute.

Pasted text is an ordinary channel called `gui`: it appears in the list, it can be
muted on its own, and `hush` reaches it like anything else. The window can do that
one thing and no other — `enqueue` is not on the shell's forwarding allowlist and
sending it through the general command is refused, so a bug in the frontend still
cannot speak arbitrary traffic on another session's channel.

```bash
cd clients/gui/app/src-tauri && cargo run      # the desktop shell
python3 -m http.server 8765 --directory clients/gui   # or a plain tab, on a simulation
```

The second needs no daemon and no Rust: opened at `/pinned.html` in a browser, the
page drives itself from a fixture, which is how the frontend is developed.

## Settings

Typed settings — bool, int, float, string, choice, voice, and voice_map (a
table from language code to voice) — declared by core, by in-process plugins,
and by clients, and stored in one file: `$XDG_CONFIG_HOME/speakd/settings.toml`.
A key is always `<owner>.<name>`; core's owners are `speech`, `http` and
`render`, and a client's is its source id up to the first `:` — `zotero:K`
declares and sets under `zotero`. A declaration a client sent while offline is
remembered, in `settings-schema.json` beside the values, so its settings still
show up before it next connects. A value that fails to validate — hand-edited,
or left behind by a declaration that changed type — falls back to the
default, with a warning on stderr, rather than being served broken.

```bash
uv run speakctl settings              # every owner, as a table: key, value, default, type
uv run speakctl settings speech       # one owner only
uv run speakctl set speech.detect_language false
uv run speakctl set http.port 8642    # some settings only take effect after a restart
```

`set` parses the value by the setting's declared type: a bool takes
`true`/`false`, `on`/`off` or `1`/`0`; a `voice_map` takes `fr=ff_siwis,it=if_sara`.

The window's gear opens a Settings panel in place of the text region, grouped
by owner — core owners first, then everyone else alphabetically — with one
control per type: a switch for bool, a bounded number field for int and
float, an input (or a textarea, multiline) for string, a dropdown for choice,
a voice map's own table of language and voice rows with add and remove. A
value commits as soon as you leave the field; a refusal is shown under the
control and the field reverts to what the daemon actually holds, so it never
claims a setting that did not take. A setting flagged `restart` says so,
in place, rather than pretending the change is already live.

Zotero's loopback HTTP is scoped the same way the socket trusts a client to
scope itself: a request may read its own owner's settings and `speech`'s, and
may set only its own owner's keys — `set_setting` on `speech.*` over HTTP is
always refused, whatever the client asks for.

## Audio files

`render` turns a stretch of text into an mp3, opus, or m4b with chapters —
the daemon side of "turn this paper into a podcast episode", read through
the same pipeline as live speech: cleanup, segmentation and Kokoro. It runs
on its own worker, never the live queue, so a render never delays a sentence
you are actually listening to; the two directions are kept apart on purpose.
Before each sentence the worker checks whether anything is speaking live and
waits if so, so live speech is never held up by more than the one render
sentence already in flight — and the two never reach the engine at the same
moment, since both sides of `render.py`'s worker take the same lock around a
`synthesize()` call that the live pipeline does.

```bash
uv run speakctl render paper.txt --out paper.mp3
uv run speakctl render book.txt --out book.m4b --title "A Book" --no-wait
```

A text file is one part unless it contains form feeds (`\f`), in which case
each chunk becomes a chapter — titled from `--title` (or the file's own name)
when there is only one, and "Part 1", "Part 2", ... otherwise. The output's
extension picks the format. Left to run, `speakctl render` polls and prints
progress until the job is done or fails; `--no-wait` just prints the job id
and returns.

Progress survives a crash or a restart: a work directory under
`$XDG_STATE_HOME/speakd/renders/<job>/` holds a manifest (which part and
sentence come next) and each part's audio as it is made, so a daemon that
comes back mid-render picks up exactly where it left off rather than
resynthesising anything already on disk. A part's audio is stored as headerless
PCM rather than a WAV — a WAV's 32-bit RIFF size field caps a part at 4 GiB
(about 24.8 hours at Kokoro's rate), which a real audiobook is not obliged to
fit inside, and encoding streams that PCM to ffmpeg rather than concatenating
it into one file first, so nothing about a render's length is ever held in
memory or written twice.

`render.mp3_bitrate` (32k/48k/64k/96k, default 64k — mono speech needs
little) is the bitrate for every format, `render.chapter_gap_ms` (default
1500) is the silence between parts, and `speech.sentence_gap_ms` (default
250) is the silence between sentences within a part. `status` reports
`render: {available, jobs}` — `available` is whether ffmpeg was found on
`PATH` at all — and the `render` event carries each job's state, progress
and, once done, its output path, published on every state change and at
most once every two seconds while one is running. `render_cancel` drops a
queued job outright, or flags a running one to unwind at its next check,
without disturbing whatever else is in the queue.

Over HTTP — the transport Zotero's export flow will drive this through —
`out` must resolve to somewhere inside your home directory; a symlink that
would land it elsewhere is refused the same as a literal path outside it
would be.

## Measured

On an i7-10510U with Kokoro on CPU (RTF 0.75). **Read the provenance** — some of
these come from a substitute rig rather than a real device, and the difference
matters:

| | | |
|---|---|---|
| Time to first audio, 417-char passage | 20.2s batched -> **10.9s** streamed | real device |
| Audio after a hush | 1.816s -> **0.256s** | substitute rig |
| `speakctl enqueue` return | **0.27s**, five sentences still queued | real device |
| Claude Code hook, against a 3s budget | 0.25s per tool call -> **0.09s** twice a turn | real device |

The hush figures are a sound *contrast* — both arms ran the identical script —
but the absolute numbers were taken with a sleeping fake sink and want
re-measuring on hardware. That rig reported a clean shutdown for code that
segfaulted inside ALSA the first time it met a sound card, so its silences are
not evidence.

## Clients

**Zotero** — [`clients/zotero/`](clients/zotero/) reads a paper
aloud from inside Zotero 10's reader, with Zotero's own sentence highlight and
follow-scroll showing what is being spoken. speakd does the speaking; the
plugin borrows Zotero's Read Aloud segmentation (reading order, headers and
citations skipped) and its highlight. A paper is a channel like any other,
`zotero:<item>`, named after its title.

Zotero's plugin sandbox cannot open a Unix socket, so the daemon also serves
**loopback HTTP**, on `127.0.0.1:8642`:

- `POST /v1/<verb>` with `{"source_id", "payload"}`, for the verbs a reader
  needs (`enqueue`, `hush`, `cancel`, `pause`, `resume`, `seek`, `replay`,
  `set_label`, `set_speed`, `status`, `render`, `render_cancel`), and
  `GET /v1/events` as server-sent
  events carrying the same JSON as `speakctl subscribe`.
- Every request needs `Authorization: Bearer <token>`. Loopback is not the
  socket's trust boundary — any web page you open can reach 127.0.0.1 — so the
  token is what keeps them out. `speakctl http-token` prints it (made on first
  use, readable by you alone); paste it into the plugin's preferences.
- `SPEAKD_HTTP_PORT` moves it; `SPEAKD_HTTP_PORT=0` turns it off.


**Claude Code** — [`clients/claude-code/`](clients/claude-code/) is a loadable
plugin that speaks a session's responses as they land on disk. The speaking is
done by a follower process the daemon keeps alive, which tails the transcript;
two hooks remain, both once per turn, to stop the speech when a new prompt
arrives and to read the permission prompts aloud. Every hook path exits 0 and
writes nothing to stdout, so a daemon that is not running costs silence and
nothing else. See its [README](clients/claude-code/README.md) for the install.

**Desktop notifications** — a second follower reads chosen notifications
aloud: email, and WhatsApp through Chrome. It listens to the session bus with
`busctl --user monitor`, so it needs systemd and no Python dependency at all.

Rules live in `~/.config/speakd/notifications.toml`, are tried in order, and
the first match decides. Anything matching no rule is **not** spoken — it is an
allow list, so a new chatty app cannot start talking on its own.

```bash
speakctl notify init        # write a starter rules file
speakctl notify recent      # what arrived, and which rule decided it
speakctl notify tap         # watch them arrive live, speaking nothing
speakctl notify test "Google Chrome" "Mamma" "still on for tomorrow?"
```

`recent` and `tap` are not conveniences. What an app calls itself in a
notification is not something you can look up — Chrome puts the sender in the
summary and the message in the body, but the app name it stamps on a WhatsApp
notification has to be observed. Run `speakctl notify tap`, send yourself a
message, and write the rule from what you see.

Each app gets its own channel (`notify:<app>`), so the per-channel mute and the
window's skip button work on them unchanged: silence WhatsApp and keep Claude
Code. They speak in a second voice, `am_michael`, so a notification is
recognisable as one without looking at the screen, and queue at priority 10 —
above Claude Code, which orders the queue without ever cutting off a sentence
already being spoken.

`max_per_minute` (20 by default) is the safety valve. A group chat waking up
produces forty notifications in a minute, and a queue of forty is not something
anyone sits through.

## Development

```bash
uv sync --group dev     # core + dev tooling
uv sync --extra kokoro  # add the speech engine (needs Python < 3.14)
uv run pytest
uv run ruff check . && uv run ruff format --check . && uv run mypy src tests
```

**Where Kokoro runs** is `SPEAKD_DEVICE`: `auto` (the default), `cpu` or `cuda`.
`auto` uses the GPU when one starts and the CPU otherwise — which matters
because the torch build on PyPI for Linux is a CUDA build, and an NVIDIA GPU
older than its cuDNN supports is *visible* to torch and still fails to run the
model. On such a machine, set it for the service so the GPU is never tried:

```bash
mkdir -p ~/.config/systemd/user/speakd.service.d
printf '[Service]\nEnvironment=SPEAKD_DEVICE=cpu\n' \
  > ~/.config/systemd/user/speakd.service.d/local-device.conf
systemctl --user daemon-reload && systemctl --user restart speakd
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
