# speakd for Claude Code

Speaks Claude Code's prose responses aloud while the session runs. A follower
process tails the session transcript and sends each block of text to the
`speakd` daemon as it lands on disk — while the next tool is still running,
rather than after it. A new prompt stops whatever is being said.

It also gives each session a way to **brief** you instead: an MCP server,
`speakd-mcp`, whose `brief` tool the agent calls when it has finished, is stuck,
or has a question. Whether a session reads every response (full) or only its
briefings (brief) is chosen per session — see the main README's *Briefings*.

Three hooks, each firing once per turn:
- `UserPromptSubmit` stops the speech, starts a new turn and tells the
  follower this session is live.
- `Notification` speaks the permission prompts.
- `Stop` says "finished" when a turn ended in brief mode without a briefing.

None of them reads the transcript, so a tool call costs nothing at all.

## Install, once

Add this checkout as a plugin marketplace and install from it. From a shell:

```bash
claude plugin marketplace add /path/to/speakd
claude plugin install speakd@speakd
```

— or the same inside Claude Code, as `/plugin marketplace add /path/to/speakd`
and `/plugin install speakd@speakd`. This installs the hooks and the MCP server
together; a session started afterwards has both.

**If you wired the hooks into `~/.claude/settings.json` by hand before**, remove
those `speakd-hook.sh` entries first: with the plugin installed as well, every
hook would fire twice.

**After changing the checkout** nothing needs reinstalling for code changes —
the hooks and the MCP launcher run whatever is in the venv. Only a change to
`hooks/hooks.json` or `.mcp.json` needs `claude plugin marketplace update speakd`
and a new session.

### Tell it where speakd lives

`claude plugin install` **copies** the plugin into `~/.claude/plugins/cache/`, so
the installed copy has no way to find the checkout it came from. Point it back
with `SPEAKD_HOME`, somewhere every shell that starts Claude Code reads (for zsh,
`~/.zshenv`):

```bash
export SPEAKD_HOME=/path/to/speakd
```

The hook wrapper and the MCP launcher look the same way: for `speakd-claude-hook`
/ `speakd-mcp` on `PATH` first, then in `$SPEAKD_HOME/.venv/bin`, then in the
checkout they sit in, then in `~/.local/bin`. Finding none, the hook wrapper
writes one line to the log saying so and exits 0, and the MCP launcher reports
the failure to Claude Code — so the symptom of a missing `SPEAKD_HOME` is a line
in the log and a failed `speakd` server in `/mcp`, not silence.

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
