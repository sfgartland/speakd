# Briefings from agents, and a channel list ranked by relevance

Date: 2026-09-24. Status: designed in conversation, awaiting review of this spec.

## Intent

Written back from the conversation, and agreed:

- **The situation** is mostly ambient awareness. Several agents run while the
  user does something else, and the user wants to hear only what matters:
  **done** (with the gist), **problems**, and **questions for them**. Sometimes
  the user follows one session closely instead, and wants every response read
  aloud, which is today's behaviour. They choose per session.
- **The judgement stays with the agent.** The milestone-1 plan had a summarise
  transform, a second model with no context guessing what matters in text the
  first one wrote. It is dropped. The agent doing the work knows what changed
  and what the user asked to hear about, so the agent decides when to speak.
  This matches the project's own rule: the intelligence lives in the agents,
  and the daemon stays dumb and fast.
- **The user instructs the agent** at two levels: a standing guide kept by
  speakd and handed to every agent, and per-session instructions given in that
  session's conversation.
- **The channel list must stay usable** with many sessions: ranked by
  relevance, with the irrelevant ones folded away.

Success: with three sessions working, the user hears "ReSem paper: tests
pass, ready for review" from one, "Kronikk is waiting for your permission to
run Bash" from another, and nothing at all from a session they have muted.
They can switch any session to full narration in one click and back.

Out of scope: a summariser of any kind in the daemon. Agents speaking
spontaneously outside their own turns, which no agent here can do. Scheduling
("tell me at 5pm").

## Part A: Briefings

### A1. Kinds, modes and capability (daemon)

Every enqueue carries a `kind`. Three kinds matter here:

| kind        | who sends it                          | what it is |
|-------------|---------------------------------------|------------|
| `response`  | the follower, `speakctl enqueue`      | the full text of a message |
| `brief`     | an agent, through the MCP server      | what the agent decided to tell the user |
| `attention` | hooks                                 | the agent cannot report this itself: waiting for permission, a turn that ended without a briefing |

Every channel gains two fields:

- `briefs: bool`, meaning a briefer exists for this channel: an agent
  connected through speakd's MCP server, or a client with its own logic.
  Default false.
- `mode: "full" | "brief"`, which only means something when `briefs` is true.
  A channel with `briefs = false` is always treated as full.

**Which kinds are spoken:**

| effective mode | response | brief | attention |
|----------------|----------|-------|-----------|
| full           | spoken   | declined `full mode` | spoken |
| brief          | declined `brief mode` | spoken | spoken |

Other kinds (notifications' own, `gui`, and so on) keep today's behaviour and
are unaffected by mode. The check runs in `Daemon._enqueue` after the mute
check and before `decide()`. A decline is published as `declined` with its
reason, as every other decline is.

Mute still overrides everything: a muted channel speaks nothing, of any kind.

**Label prefix.** A `brief` or `attention` utterance is spoken with the
channel's label in front ("ReSem paper: tests pass"). With several sessions
running, the listener needs to know who is talking. When there is no label,
nothing is added.

**Verbs:**

- `set_mode {mode}`: `mode` is `"full"` or `"brief"`. It is scoped to the
  request's `source_id`, and refused on a channel with `briefs = false`
  ("this channel has no briefer"). It publishes `mode {mode}` on the channel.
- `set_capabilities {briefs: bool}`: a client declares that it can brief.
  It is idempotent and publishes `mode` with the effective mode when that
  changes.
- `status`: each channel additionally reports `briefs`, `mode`,
  `last_output` (see B1) and `briefed_this_turn`.

**Turn tracking.** Each channel keeps `briefed_this_turn: bool`. A spoken
`brief` sets it. A `hush` on the channel clears it; the prompt hook already
hushes on every `UserPromptSubmit`, so a turn begins with it clear. An
`attention` enqueue carrying `"unless_briefed": true` is declined with reason
`already briefed` when it is set. This lets the Stop hook stay silent when
the agent has already said its piece.

**Defaults.** A channel whose id starts with `claude-code:` and has no
further `:` (so the session's main channel, not its old `:notify` channel)
starts `briefs = true`, `mode = "brief"`, `muted = true`. This replaces
`build_channels`' current start-muted-only rule. Everything else keeps its
current defaults.

### A2. The MCP server: `speakd-mcp`

A console script, `speakd-mcp`, speaking MCP over stdio (JSON-RPC 2.0,
newline-delimited). It is hand-written, not built on the MCP SDK, because the
SDK brings pydantic, anyio and httpx and this project has kept its
dependencies lean on purpose. The subset needed is small: `initialize`,
`notifications/initialized`, `tools/list`, `tools/call`, and `ping`.
Anything else answers JSON-RPC error -32601.

**Its channel.** Resolved once at startup:

1. **Claude Code.** Walk up this process's ancestors (`/proc/<pid>/stat`,
   Linux; up to 8 levels) and look each pid up in the session registry. The
   prompt hook records the pid of the Claude Code process that ran it (see A3).
   A match gives `claude-code:<session_id>`.
2. **Anything else** (Codex, OpenCode, or Claude Code before its first prompt
   registers the session): `agent:<client name>:<nearest ancestor pid>`, where
   the client name comes from `initialize.params.clientInfo.name`, and the
   label is `<client name> · <basename of cwd>`.

   Step 1 is retried on each tool call until it succeeds, so a Claude Code
   session that registers after the server started is found on its first
   briefing.

On connecting to the daemon it sends `set_capabilities {briefs: true}` and,
for case 2, `set_label`.

**Tools:**

- `brief(text: string, kind: "done" | "problem" | "question" | "progress")`
  enqueues `kind: "brief"` with the text. It answers with the daemon's verdict
  as structured content and a one-line text summary: `{spoken: bool, mode,
  muted, reason?}`. Every call therefore tells the agent the current mode.
  - `kind` is carried in the payload as `brief_kind` for monitors.
  - It is not spoken differently.
- `briefing_status()` answers `{mode, muted, briefs, guide}`, where `guide` is
  the standing guide's text.
- `set_mode(mode: "full" | "brief")` switches its own channel, for when the
  user says so in the conversation. It answers like `briefing_status`.

**Instructions.** The `initialize` result carries `instructions`: a short fixed
preamble (what the tools are, and that in full mode everything is already read
aloud so briefing is pointless there) followed by the standing guide.

**The standing guide** is `$XDG_CONFIG_HOME/speakd/briefing.md` when it
exists, otherwise a built-in default:

> Brief the user by voice with the `brief` tool. They are usually doing
> something else and listening, not reading.
> - When you finish a task: one or two sentences of outcome (`done`).
> - When you are blocked, something failed, or you had to change plan:
>   what and why (`problem`).
> - When you need a decision or answer from them: the question, so it can be
>   answered by voice or at the keyboard (`question`), then end your turn.
> - On tasks longer than about ten minutes: a sentence at real milestones
>   (`progress`). Never narrate routine steps, file edits or tool calls.
> - Under about 25 words. No markdown, no code, no paths unless essential.
> - Follow what the user asks for in this conversation over this guide.
> - Call `briefing_status` if unsure of the mode; in `full` mode, do not brief.

**Failure.** No daemon means `brief` answers `{spoken: false, reason: "speakd
is not running"}`; it never errors the tool call, because a missing voice must
not derail the agent's work.

**Shipping.**
- The Claude Code plugin gains `.mcp.json`, running `speakd-mcp` through
  `$SPEAKD_HOME`, the same way the hooks run.
- For Codex and OpenCode, the README documents the one-line MCP registration.

### A3. Hooks (Claude Code)

- **UserPromptSubmit:** unchanged, plus the registry records `claude_pid`, the
  nearest ancestor of the hook process whose command name is `claude`
  (falling back to the grandparent). This is how `speakd-mcp` finds its
  session.
- **Notification:** now enqueues `kind: "attention"` on the session's **main**
  channel, prefixed by label through A1, instead of a separate `:notify`
  channel. The prompt hook then hushes one channel instead of two.
- **Stop (new):** enqueues `kind: "attention"`, text `finished`,
  `unless_briefed: true` on the main channel. In brief mode this says
  "‹label›: finished" only when the agent sent no briefing this turn. In full
  mode the response has just been read aloud, so `attention` would repeat it;
  the Stop hook therefore also sends `only_in_mode: "brief"`, and the daemon
  declines it in full mode with reason `full mode`.

This adds one hook firing per turn, at the end of the turn and outside the
user's critical path. It uses the lean `send` path, not `speakd.cli`.

### A4. CLI and window

- `speakctl mode brief|full --source <id>` sets the mode.
  `speakctl status` shows the new fields.
- Window: a channel row with `briefs` shows a two-state `brief | full` toggle
  beside skip and mute. Rows without it show nothing there. A `mode` event
  moves it, as `mute` events move mute.

## Part B: A ranked channel list

### B1. Daemon

Each channel keeps `last_output: float`, a wall-clock time
(`time.time()`, so it is comparable across restarts of the window) of the
last `enqueue` *attempt* on it: spoken, declined or muted alike. A muted
session that just finished still counts as having spoken up. It is reported
in `status` and carried as `at` on `queued` and `declined` events, so a window
keeps it current without polling.

### B2. Window

- **Order.** Starred channels first, then audible, then muted. Within each
  group, the most recent `last_output` first; ties fall back to label.
- **Star.** Each row gets a star toggle. Starred rows are pinned to the top
  and never folded. Stars are kept in the window (`localStorage`, keyed by
  source id, wrapped in try/catch) because sessions are short-lived and the
  daemon need not know.
- **Fold.** Muted, unstarred channels sit behind a `N muted` line inside the
  channel disclosure, closed by default. Opening it is remembered for the
  window's lifetime.
- **Rows** show relative last output ("2m", "1h") in faint mono, so the order
  explains itself.

## Order of work

Part B first: small, useful on its own, and it gives Part A's new fields
somewhere to show. Then A1 (daemon), A2 (MCP), A3 (hooks), A4 (CLI/window),
and README.

## Testing

- **Daemon:** the kinds × modes table, capability refusal, label prefix, turn
  tracking and `unless_briefed`, `only_in_mode`, defaults for `claude-code:`
  channels, `last_output` updated on declined and muted enqueues too, and
  `status` fields.
- **MCP:** spawn `speakd-mcp` as a subprocess against a test daemon socket and
  talk JSON-RPC over its stdin/stdout: initialize (instructions contain the
  guide), tools/list, `brief` in each mode, unknown method, and no daemon.
  Ancestry lookup is tested with a fake `/proc` reader.
- **Hooks:** Notification → attention on the main channel, and Stop →
  `unless_briefed`, with sample payloads.
- **Window:** browser run on the simulation, which gains `briefs`, `mode` and
  `last_output` on its channels: order, star, fold and the mode toggle.
- **End to end:** register `speakd-mcp` with a real Claude Code session, ask it
  to do a small task, and hear the briefing.
