# Handoff — resume here

Last updated 2026-09-24. `main` is pushed to https://github.com/sfgartland/speakd and
CI is green (Python 3.11–3.13 and the Zotero plugin build).

## What works now

| Piece | State | How to use |
|---|---|---|
| Daemon | Running as the `speakd` user service | `systemctl --user restart speakd`; `speakctl status` |
| Desktop window | Follow-along text, seek, speed with ramps, replay, preparing highlight, ranked channel list (stars, hide fold), brief/full toggle | `cd clients/gui/app/src-tauri && cargo run` |
| Claude Code | Installed as a plugin (`speakd@speakd`, user scope): hooks (prompt, Notification, Stop) plus the `speakd-mcp` server | New sessions start **brief and muted**; unmute one in the window. `SPEAKD_HOME` is exported in `~/.zshenv` |
| Briefings | Agents call `brief` over MCP; sessions switch between brief and full per channel | `speakctl mode full --source claude-code:<id>`; the guide is `~/.config/speakd/briefing.md` |
| Zotero reader | Built, reviewed, verified live in a test Zotero; **not installed in your Zotero yet** | See "Install the Zotero plugin" below |
| HTTP transport | `127.0.0.1:8642`, token-protected, for the Zotero plugin | `speakctl http-token` prints the token |

Local machine specifics, kept outside the repo:
- `~/.config/systemd/user/speakd.service.d/local-device.conf` sets `SPEAKD_DEVICE=cpu`.
  This laptop's NVIDIA GPU is too old for torch's CUDA build. Because the engine
  falls back to the CPU anyway, plain `uv sync` / `uv run` are safe again.
- `~/.claude/settings.json.before-speakd-plugin` is the backup from before the
  hand-wired hooks were replaced by the plugin.

### Install the Zotero plugin (not done yet — do it when Zotero can be restarted)

1. `cd clients/zotero && npm ci && npm run build` builds
   `.scaffold/build/speakd-reader.xpi` (already built once on 2026-09-24).
2. In Zotero, go to Tools → Plugins → gear → Install Plugin From File…
3. Paste the output of `speakctl http-token` into Settings → speakd reader.
4. Reopen any PDFs that were open before. Select text, then **Read selection** /
   **Read from here**.

Details and limits are in `clients/zotero/README.md`. Zotero is pinned to 10.0.x.

## Next: typed settings and languages (designed, not built)

**Spec:** `docs/superpowers/specs/2026-09-24-settings-and-languages-design.md`,
approved in conversation. In short:

1. **Settings core.**
   - Typed settings declared by core, by in-process plugins (`PluginContext.setting`)
     and by clients (`declare_settings`).
   - One `~/.config/speakd/settings.toml`.
   - The verbs `settings` / `set_setting`, and the event `setting`.
   - Over HTTP a client can reach only its own owner's keys.
   - A Settings view in the window, and `speakctl settings` / `speakctl set`.
2. **Languages in the daemon.**
   - Language resolution per utterance: payload `lang`, then the channel's
     language, then detection (lingua, lazy, in a `speakd[lang]` extra), then
     the default.
   - Kokoro pipelines per language.
   - `speech.voices` (language → voice) with defaults for all 9 Kokoro languages.
   - `speech.unsupported_language` set to `default` or `decline`.
   - `status.languages`.
3. **Clients.**
   - Zotero sends the document's language, steps aside to Zotero's own Read
     Aloud for unsupported languages (German), and declares `zotero.*` settings.
   - `brief` gains `lang`.
   - The briefing guide moves into the setting `claude-code.briefing_guide`,
     migrated from `briefing.md`.

Out of scope for it: Piper (or another engine) for real German speech, which is
its own project; moving `profiles.toml` / `notifications.toml` into settings.

**To resume:**
1. Write the implementation plan from the spec (the writing-plans skill), one
   phase per plan or one plan in three phases.
2. Execute it. Models that worked well:
   - **Sonnet** for tasks the plan specifies closely: TDD implementation,
     typing and lint fixes.
   - **Opus** for research into undocumented internals, live-tested work on
     Zotero internals, and the final whole-branch review.
   - **Haiku** for mechanical sweeps.
3. Keep phases on separate worktrees when parallel agents commit, so their
   commits never race.

## Deferred and known gaps

- **Zotero plugin:**
  - Not verified live: a real mouse selection, real engine timing, OS media
    keys, two-column / footnote-heavy PDFs, the `ReaderWindow` path, real Zotero
    cloud voices.
  - Readers opened before the plugin loads can't be adopted.
  - Zotero's hidden player loops a −64 dBFS tone to keep media keys working.
  - Stage B (LLM text cleanup) is designed but not built.
  - `startup` can outlive a very early `shutdown` (review minor #9).
- **Briefings:**
  - The first live briefing through the installed plugin (as opposed to
    `--mcp-config`) was cut off by a usage limit. The hook side was verified.
  - Claude Code sessions are brief and muted by default, and a daemon restart
    resets that.
- **Real German speech** needs a second engine.
- **CI** runs without the kokoro extra, so the real-engine tests are skipped there.

## Working on this repo — things that bite

- **`pkill -f <pattern>` matches its own shell** when the pattern appears in the
  command line. Always bracket it: `pkill -f "[s]peakd-zotero-test/profile"`.
- **The Zotero live-test harness** is in `clients/zotero/test-live/` (see the
  plugin README): a headless Zotero on a throwaway profile with its **own
  library** (never `~/Zotero`), a test-only token-protected eval bridge on port
  23129, and a fake-engine daemon on HTTP 8743. Marionette can't drive Zotero,
  which is why the bridge exists. The profile lives in `~/.cache/speakd-zotero-test`.
- **Ruff formats code blocks inside Markdown**, which is why `docs/` is excluded
  in `pyproject.toml`.
- **Old worktrees** under `../speakd-worktrees/` (cc-hooks, gui-tauri, onnx, …)
  predate this session. They're untouched, and some may be stale.
- **Commit emails are public** on GitHub (`sfgartland@hotmail.com`).

## Where the record is

- Designs are in `docs/design/` and `docs/superpowers/specs/`; plans are in
  `docs/superpowers/plans/`.
- The Zotero internals reference, with the live-verified sections, is
  `docs/design/2026-09-24-zotero-10-read-aloud-internals.md`.
- `git log` holds the reasoning. Commit messages say why.
