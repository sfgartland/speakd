# An OpenCode client, with the same support Claude Code has

Date: 2026-09-25. Status: designed in conversation, awaiting review of this spec.

## Intent

Written back from the conversation, and agreed:

- **OpenCode should get what Claude Code has, not a lesser version of it.**
  Today OpenCode's only integration is a manually registered `speakd-mcp`,
  which briefs on a generic per-process channel `agent:opencode:<pid>`. It has
  no response speaking, no hush on a new prompt, no permission alerts, no
  "finished" backstop, no brief/muted defaults, and no install flow.
- **A plugin is the whole client.** OpenCode has no Claude-style hooks and no
  transcript JSONL (sessions live in a private SQLite database), but it does
  have in-process Bun plugins with `chat.message` and `permission.ask` hooks
  and a live event stream (`message.updated`, `message.part.updated`,
  `session.idle`, `session.created`/`updated`). One plugin file can do what
  the two hooks and the follower do for Claude Code, and it can do one thing
  Claude Code cannot: see text *as it streams*.
- **The daemon-side pieces are shared, not copied.** Session registration,
  brief/muted channel defaults, and MCP ancestry resolution all exist for
  Claude Code; they are generalised to a `client` field rather than
  re-implemented per agent.

Success: with an OpenCode session running, its responses are spoken as its
text parts complete; a new prompt stops the speech; permission prompts are
read aloud; a turn that ends in brief mode without a briefing says
"finished"; the session's channel is `opencode:<session id>`, starts brief
and muted, and `speakd-mcp` finds it by process ancestry exactly as it finds
Claude Code sessions.

Out of scope: token-level streaming into the audio pipeline (the daemon has
no append verb, and each enqueue is an utterance of its own — see *Rejected
alternatives*); reading OpenCode's SQLite database; SDK-based followers;
sentence-level incremental speaking.

## Part A: The plugin

`clients/opencode/plugin/speakd.js` — one self-contained, dependency-free
file, copied to `~/.config/opencode/plugins/` by the installer. It uses
`node:net` (present in both Bun and Node, so it also runs under vitest) to
speak the daemon's JSON-lines protocol over the Unix socket. Nothing it does
may break OpenCode: every handler is guarded, every socket call bounded at
two seconds, and a missing daemon is silence, not an error.

### A1. Event map

| Claude Code piece | Plugin equivalent |
|---|---|
| `UserPromptSubmit` (hush + register) | `chat.message` hook: hush with `new_turn`, write the registration file |
| `Notification` (permission) | `permission.asked` event: enqueue `attention` "OpenCode needs your permission." |
| `Stop` ("finished" backstop) | `session.status` event with `status.type === "idle"`: flush, then enqueue `attention` "finished" with `unless_briefed` and `only_in_mode: "brief"` |
| Follower (speak responses) | `message.updated` and `message.part.updated` events: buffer text parts, enqueue them when they complete |
| Transcript ai-title labels | `session.created`/`session.updated` events: `set_label` from the session's title or directory |

Two of these choices are deliberate dodges around dead or dying APIs,
verified against the OpenCode sources:

- **`permission.ask` the hook is dead code** in v1: declared in the plugin
  types but never invoked (the v1 permission service has no call site for
  it). The `permission.asked` *event* is delivered at runtime, and the event
  name also exists in v2's event union. The plugin reads only
  `properties.sessionID` from it, which both payload shapes carry.
- **`session.idle` is deprecated** and slated to vanish; `session.status`
  with `status.type === "idle"` is the supported spelling, present in both
  v1 and v2 (`SessionStatus = {type: "idle"} | {type: "retry", ...} |
  {type: "busy"}`).

The permission alert is a fixed sentence, like Claude's hook: a permission's
own title names the tool and can be long, and the point is to tell the
listener *that* OpenCode is waiting, not to read the dialog.

### A2. What is spoken, and when

OpenCode streams text into `message.part.updated` events — each carries the
part's accumulated text and a `delta`. Speaking each delta would cut words in
half, so the plugin **buffers per part and enqueues the part whole when it
finishes** — which for OpenCode is when a text block ends, usually before
later tool calls, so it is strictly earlier than Claude Code's
message-completion granularity. The three ways a part is spoken, in order:

1. `message.part.updated` where `part.time.end` is set (the part finished).
2. `message.updated` where `info.time.completed` is set (the message
   finished; flushes any parts still buffered).
3. `session.idle` (the turn ended; flushes whatever remains).

A part is spoken at most once, enforced by a `spoken` set per message, and
parts are enqueued in arrival order. On `message.part.removed` the buffer
entry is dropped.

**Filtering.** Only `type: "text"` parts of assistant messages are spoken.
A part with `ignored: true` is dropped (it is excluded from OpenCode's own
context display and would be noise). Sub-agent output is never spoken:
assistant messages carry `agent` in their stored data (verified in the live
database, though absent from the SDK's published types — the plugin reads it
defensively and falls back to the parent user message's `agent`), and only
messages whose agent is the session's main agent are spoken. The main agent
is the `agent` on the first `chat.message` the plugin sees for the session
(defaulting to `"build"`, OpenCode's default agent).

### A3. Registration

On `chat.message` the plugin writes the same one-file-per-session
registration the Claude hook writes, in the shared format (see Part B),
with `client: "opencode"`, `agent_pid: process.pid` and the session's
directory. `process.pid` is the OpenCode process because plugins run inside
it — and the resolver's any-ancestor match (Part B2) makes this robust even
if a launcher sits between OpenCode and the processes it starts. No
transcript path is recorded (`""`): there is no transcript to tail, and the
follower ignores non-Claude registrations.

### A4. Labels

`session.created`/`session.updated` carry the session's `title` and
`directory`. The plugin sends `set_label` with `OpenCode · <title>`, or
`OpenCode · <directory basename>` before a title exists, re-sending only
when the label changes, as the Claude follower does.

## Part B: Daemon-side generalisation

### B1. Channel defaults

`__main__._claude_code_defaults` becomes `_agent_defaults`: a channel id
with exactly one `:` whose prefix is `claude-code` **or** `opencode` starts
`briefs = true`, `mode = "brief"`, `muted = true`. Everything else is
unchanged. `speakctl mode full --source opencode:<id>` and the window's
toggle then work on OpenCode sessions as they do on Claude Code sessions.

### B2. The session registry moves and gains a client

`speakd.clients.claude_code.registry` moves to `speakd.clients.registry`,
shared by both agents:

- `Registration` gains `client: str`; `claude_pid` is renamed `agent_pid`.
- `register(session_id, transcript, cwd, *, client, agent_pid=None)` writes
  `<state dir>/<client>/<slug>.session.json`, one directory per client —
  Claude Code's files stay exactly where they are today.
- `live()` reads every `*.session.json` under the state root, so the Claude
  follower filters `reg.client == "claude-code"` and `speakd-mcp`'s resolver
  sees both clients.

`watermark._slug` moves to `speakd.clients.registry`; the watermark module
imports it, so the slug stays identical for existing files.

### B3. MCP ancestry resolution

`session.resolve` changes from "look up the nearest non-launcher ancestor"
to "walk this process's ancestry and return the **first** pid with a live
registration", as `client:session_id`. This is strictly more robust (it
covers whatever process OpenCode names itself and whatever launchers sit in
between) and it keeps every Claude Code behaviour the existing tests pin
down: a terminal shared with a Claude session does not capture another
agent, because the shared terminal's pid is never registered, and `/clear`'s
newest-wins rule still applies per pid. The `agent:<client>:<pid>` fallback
for unregistered agents is unchanged, so OpenCode run without the plugin
keeps today's behaviour.

## Part C: Install

`clients/opencode/install.sh`:

- copies `plugin/speakd.js` to `~/.config/opencode/plugins/`;
- merges into `~/.config/opencode/opencode.json` (created if absent, other
  keys preserved; JSON edited by python3, a dependency speakd already has):

```json
"mcp": { "speakd": { "type": "local", "command": ["bash", "<abs checkout>/clients/opencode/speakd-mcp.sh"], "enabled": true } }
```

- `speakd-mcp.sh` is the same PATH → `$SPEAKD_HOME` venv → checkout venv →
  `~/.local/bin` lookup as Claude Code's wrapper, exec'ing `speakd-mcp`.

The plugin needs no `SPEAKD_HOME` for itself: it talks to the daemon through
the standard socket path (`$XDG_RUNTIME_DIR/speakd/speakd.sock`), which the
daemon owns. Only the MCP wrapper needs the checkout location, and the
installer writes that as an absolute path.

## V2 readiness

OpenCode v2 ships a rewritten plugin system — **v1 plugin implementations do
not run in v2** (the SDK was cut over to v2 exclusively). This spec targets
v1 (the installed version, 1.18.32) and keeps the v2 port to the entry
shim:

- Every *signal* the plugin uses exists in v2's event union:
  `message.updated`, `message.part.updated`, `permission.asked`,
  `session.status` — only their payload shapes drift (v2's
  `message.part.updated` is `{sessionID, part, time}`, and the plugin
  already reads `part.sessionID` defensively).
- The v1→v2 entry mapping is documented: `event` →
  `ctx.event.subscribe()`, `chat.message` → `ctx.session.hook("prompt", …)`,
  `permission.ask` → `ctx.permission.hook("evaluate", …)` (neither of which
  this plugin uses), and a plugin may export both `server` (v1) and `setup`
  (v2) from one file. The `Speaker`/`Signals`/socket/registration core is
  runtime-agnostic and ports unchanged.
- Plugin *discovery* is unchanged: `~/.config/opencode/plugins/` still loads
  direct `.js` files.
- The MCP *config* shape changes: v1 `mcp.<name>` with `enabled` becomes v2
  `mcp.servers.<name>` with `disabled` (inverted). The installer writes the
  v1 shape; the v2 shape is a two-line edit. `speakd-mcp`'s protocol
  revision (2025-06-18) is within v2's `legacy` handshake range.

The v2 plugin API itself is still churning (domain hooks are being added),
so the port is listed as a follow-up task in the plan rather than written
blind today.

## Rejected alternatives

- **Tailing OpenCode's SQLite database.** It works (WAL mode, parts grow in
  place mid-stream — verified against a live database), but the schema is
  internal, already carries a `data_migration` table, and permission prompts
  and turn boundaries are not derivable from it, so a small plugin would be
  needed anyway. Not chosen.
- **An SDK-subscribed follower.** Public API and push-based, but it needs a
  Bun/Node process supervised by the daemon and the OpenCode server
  reachable over HTTP — more moving parts than a plugin for no functional
  gain.
- **Sentence-level streaming.** The daemon has no way to append to a queued
  utterance, so streaming input would arrive as one utterance per chunk,
  breaking the window's whole-utterance view and seek. Part completion is
  the finest granularity that keeps those intact.

## Risks

- **Event payload drift.** The v1 SDK's published `Event` type is frozen and
  wrong (32 members against 88 delivered at runtime); `message.part.updated`
  carries `sessionID` on the properties at runtime but not in the v1 type.
  The plugin reads `part.sessionID` with a properties-level fallback and
  never trusts the types. OpenCode v2 shipped days ago; the entry shim is
  the only thing that changes with it (see *V2 readiness*).
- **`chat.message` firing for synthetic messages** (auto-continue after
  compaction) would hush mid-turn. Hushed speech is at most the current
  sentence, and the hook input is inspected during implementation; if
  synthetic messages carry a distinguishing field, the hush is skipped for
  them.
- **Plugin lifetime.** Speech ends when OpenCode ends, exactly as the Claude
  transcript stops growing when Claude Code ends. A plugin reload starts
  clean: no history is re-spoken because the plugin only reacts to live
  events.

## Testing

- **Daemon:** `opencode:` and `claude-code:` channels start brief and
  muted; everything else is unchanged.
- **Registry:** per-client directories, `client` and `agent_pid` round-trip,
  `live()` reads both clients, the follower ignores non-Claude
  registrations, old files without `client` are skipped quietly.
- **MCP resolution:** an OpenCode registration is found by ancestry and
  yields `opencode:<id>`; newest-wins per pid; the unregistered fallback is
  unchanged; a shared terminal still does not capture another agent.
- **Plugin (vitest, Node):** the socket client against a real Unix-socket
  test server; registration file shape against a temp state dir; the
  speaker's buffering, part-completion enqueue, at-most-once, sub-agent
  skipping, ignored-part dropping and status-idle flush; hush and
  permission-asked signals; label changes; and that the plugin factory never
  throws on malformed events.
- **End to end:** install into a scratch `XDG_CONFIG_HOME`, run a real
  OpenCode session, hear a response, a briefing, and a permission prompt;
  hush on the next prompt; confirm the channel appears as `opencode:<id>`
  brief and muted.
