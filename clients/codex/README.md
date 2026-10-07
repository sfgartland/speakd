# speakd for Codex

The Codex client speaks the main assistant's prose as it appears in the local
session transcript. An interrupt stops current speech; a new prompt lets it finish by default (`speech.on_prompt`, or `speakctl on-prompt hush` to cut it off). Permission
requests and turns that end without a briefing can announce themselves. The
MCP `brief` tool lets Codex send a short update when it finishes, gets stuck,
or needs an answer. Reasoning, tools, and subagent turns are never spoken.

## Install

From the speakd checkout, install the Python entry points and register the
Codex hooks and MCP server:

```bash
uv sync
bash clients/codex/install.sh
```

The installer merges four entries into `${CODEX_HOME:-~/.codex}/hooks.json`
and uses `codex mcp add speakd` to update Codex's MCP configuration. It
preserves unrelated hook entries and can be run again. Existing malformed
`hooks.json` must be repaired before installation; the installer leaves it
untouched. It does not change the active user config until you run it.

Review the hook commands if Codex asks you to trust them. Restart Codex and
the speakd daemon so they pick up the new hook and follower: quit speakd from
its tray and open it again (or restart the `speakd` process you started
yourself). Set `SPEAKD_HOME` to the
checkout if you move the installer wrappers away from it. Hook diagnostics
go to `${SPEAKD_STATE_DIR}/codex/hook.log`, or
`${XDG_STATE_HOME:-~/.local/state}/speakd/codex/hook.log`.

## Listening

New `codex:<session_id>` channels start muted in brief mode. Unmute a Codex
channel in the speakd GUI or CLI. Use full mode to hear the main assistant's
responses as they arrive:

```bash
uv run speakctl mode full --source codex:<session_id>
```

Brief mode speaks the agent's `brief` tool updates and important alerts. The
MCP tools accept an optional `source_id`; pass the current `codex:<session_id>`
when calling one. A `UserPromptSubmit` hook provides that session ID in its
prompt context. The session transcript is an internal Codex format and can
change, so this integration requires a locally running Codex with transcript
access.

Codex's [hook documentation](https://learn.chatgpt.com/docs/hooks) describes
the supported events and trust review. Its [MCP documentation](https://learn.chatgpt.com/docs/extend/mcp)
describes the configuration written by `codex mcp add`.
