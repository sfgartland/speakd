# speakd for Claude Code

Speaks Claude Code's prose responses aloud while the session runs. A follower
process tails the session transcript and sends each block of text to the
`speakd` daemon as it lands on disk — while the next tool is still running,
rather than after it. A new prompt stops whatever is being said.

Two hooks, both firing once per turn: `UserPromptSubmit`, which stops the
speech and tells the follower this session is live, and `Notification`, which
speaks the permission prompts. Neither reads the transcript, so a tool call
costs nothing at all.

## Install, once

Add this checkout as a plugin marketplace and install from it:

```
/plugin marketplace add /path/to/speakd
/plugin install speakd
```

If you would rather not use a marketplace, point `~/.claude/settings.json` at the
wrapper directly. Both events run the same command:

```json
{
  "hooks": {
    "Notification": [
      { "hooks": [{ "type": "command", "command": "bash \"/path/to/speakd/clients/claude-code/hooks/speakd-hook.sh\"", "timeout": 5 }] }
    ],
    "UserPromptSubmit": [
      { "hooks": [{ "type": "command", "command": "bash \"/path/to/speakd/clients/claude-code/hooks/speakd-hook.sh\"", "timeout": 3 }] }
    ]
  }
}
```

If you installed an earlier version, delete any `Stop` and `PostToolUse` entries
you added by hand. They are no-ops now — the follower has already spoken what
they used to — but each one still costs an interpreter start-up per tool call.

### Tell it where speakd lives

`/plugin install` **copies** the plugin into `~/.claude/plugins/cache/`, so the
installed copy has no way to find the checkout it came from. Point it back with
`SPEAKD_HOME`, in whatever your shell reads at login, so Claude Code inherits it:

```bash
export SPEAKD_HOME=/path/to/speakd
```

The wrapper looks for `speakd-claude-hook` on `PATH` first, then at
`$SPEAKD_HOME/.venv/bin`, then in its own `../../../.venv/bin` (which resolves
only when the plugin is run from inside the checkout, as the `settings.json`
install above does), then in `~/.local/bin`. Finding none of them, it writes one
line to the log saying so and exits 0 — so the symptom of a missing `SPEAKD_HOME`
is a line in the log, not silence.

## Start the daemon

```bash
speakd &
```

The daemon spawns the follower and restarts it if it dies, so there is one thing
to start and one thing to stop.

Nothing breaks if you don't start it. With no daemon listening, every hook notes
the missing socket in its log and exits 0 — the session runs on in silence, which
is also what you get by simply not starting it on a day you don't want speech.

## Check it

```bash
tail -f ~/.local/state/speakd/claude-code/hook.log
```

An empty or absent log is the healthy state: it is written to only when
something went wrong — a daemon that is not listening, or a wrapper that could
not find the entry point at all. Two kinds of file sit beside it, one of each per
session: a `.session.json` is how a prompt tells the follower a session is live,
and a `.json` watermark records how far into that session's transcript has been
spoken.

## What gets spoken, and when

Every prose block the main agent writes, as soon as Claude Code flushes it to the
transcript. That flush happens when a message completes, not sentence by
sentence — its content blocks land within a couple of milliseconds of each other,
measured — so the message is the smallest unit there is to speak, and the
follower speaks each one the moment it appears. A long answer with no tool calls
used to wait for the next hook to notice it; it no longer waits for anything.

Sub-agent output is never spoken.
