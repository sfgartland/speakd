# speakd for Claude Code

Speaks Claude Code's prose responses aloud while the session runs: each block of
text is sent to the `speakd` daemon as it appears, so audio starts on the first
sentence rather than at the end of the answer. A new prompt stops whatever is
being said.

## Install, once

Add this checkout as a plugin marketplace and install from it:

```
/plugin marketplace add /path/to/speakd
/plugin install speakd
```

If you would rather not use a marketplace, point `~/.claude/settings.json` at the
wrapper directly. Every event runs the same command:

```json
{
  "hooks": {
    "Stop": [
      { "hooks": [{ "type": "command", "command": "bash /path/to/speakd/clients/claude-code/hooks/speakd-hook.sh", "timeout": 5 }] }
    ],
    "PostToolUse": [
      { "matcher": "*", "hooks": [{ "type": "command", "command": "bash /path/to/speakd/clients/claude-code/hooks/speakd-hook.sh", "timeout": 5 }] }
    ],
    "Notification": [
      { "hooks": [{ "type": "command", "command": "bash /path/to/speakd/clients/claude-code/hooks/speakd-hook.sh", "timeout": 5 }] }
    ],
    "UserPromptSubmit": [
      { "hooks": [{ "type": "command", "command": "bash /path/to/speakd/clients/claude-code/hooks/speakd-hook.sh", "timeout": 3 }] }
    ]
  }
}
```

The wrapper looks for `speakd-claude-hook` on `PATH` first, then in the
checkout's own `.venv/bin`, then in `~/.local/bin`. Installing speakd with
`uv sync --extra kokoro` in the checkout is enough for the second to resolve.

## Start the daemon

```bash
speakd &
```

Nothing breaks if you don't. With no daemon listening, every hook notes the
missing socket in its log and exits 0 — the session runs on in silence, which is
also what you get by simply not starting it on a day you don't want speech.

## Check it

```bash
tail -f ~/.local/state/speakd/claude-code/hook.log
```

An empty or absent log is the healthy state: it is written to only when
something went wrong. The watermark files beside it — one JSON file per session
— record how far into each transcript has been spoken.

## Known limitation

A long answer with no tool calls arrives as a single block, because the hook
only sees the transcript at the points Claude Code fires it. Audio starts at
that block's first sentence and streams from there, but not before the answer is
complete. Answers interleaved with tool calls do not have this problem: each
prose block speaks while the next tool runs, which is the common case.

Sub-agent output is never spoken.
