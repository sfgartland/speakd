# speakd for OpenCode

Speaks OpenCode's prose responses aloud while the session runs: each text
block is enqueued the moment it finishes, a new prompt stops the speech, and
permission prompts are read aloud. It also gives each session a way to
**brief** you instead — the same `speakd-mcp` server Claude Code uses, whose
`brief` tool the agent calls when it has finished, is stuck, or has a
question. Whether a session reads every response (full) or only its
briefings (brief) is chosen per session — see the main README's *Briefings*.

One plugin file does the work OpenCode's own hooks would do in Claude Code:

- `chat.message` stops the speech, starts a new turn and tells the daemon
  this session is live.
- `permission.ask` speaks "OpenCode needs your permission."
- `session.idle` says "finished" when a turn ended in brief mode without a
  briefing.

## Install, once

```bash
clients/opencode/install.sh
```

This copies the plugin into `~/.config/opencode/plugins/` and merges the
`speakd-mcp` entry into `~/.config/opencode/opencode.json`. Restart
OpenCode; a session started afterwards has both. Code changes to the plugin
need only a restart — the installed copy is a plain file; if you would
rather not copy it, symlink it instead:

```bash
ln -sf "$PWD/clients/opencode/plugin/speakd.js" ~/.config/opencode/plugins/speakd.js
```

The installer writes OpenCode v1's config shape (`mcp.speakd` with
`enabled`). On OpenCode v2 the same entry lives under `mcp.servers.speakd`
and `enabled` becomes `disabled: false` — see the main README and the
spec's *V2 readiness*; the plugin file itself needs no change.

## Start the daemon

```bash
speakd &
```

Nothing breaks if you don't: with no daemon listening, every send fails
quietly inside the plugin's two-second budget, and the session runs on in
silence — which is also what you get by simply not starting it on a day you
don't want speech. What the plugin could not say is written to OpenCode's
plugin log (`client.app.log`), not to OpenCode's transcript.

## What gets spoken, and when

Every `text` part the main agent writes, enqueued whole when the part
finishes — earlier than Claude Code, where a whole message flushes at once.
Parts are never re-spoken, sub-agent output is never spoken, and parts
OpenCode itself ignores (outside its context display) are skipped.
