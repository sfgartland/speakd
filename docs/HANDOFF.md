# Handoff — resume here

Last updated 2026-09-25. `main` is **not yet pushed** to https://github.com/sfgartland/speakd:
everything since the settings core is local. Push it, then check CI.

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

## Built on 2026-09-24 (merged; review fixes merged 2026-09-25 without a second review)

- **Settings core (phase 1):** typed settings, `speakctl settings` / `speakctl set`, the window's gear → Settings view, and HTTP scoping.
- **Languages (phase 2):** per-utterance language (payload > channel > detection > default), `speech.voices`, `speech.unsupported_language`, and `speakctl lang`. Detection is in the `speakd[lang]` extra.
- **Audio export Part A (daemon):** `render` / `render_cancel`, and `speakctl render file.txt --out x.mp3|.opus|.m4b`.
  - Renders are resumable, yield to live speech, and have no length limit (PCM parts streamed into ffmpeg).
  - The fixes from an Opus whole-branch review are merged as well.
- **The window's paste box** has no length cap.

## Next, in order

1. **Phase 3, language in the clients:** `docs/superpowers/plans/2026-09-24-language-clients.md`.
   - The Zotero side needs the live harness, so use Opus.
   - Depends on `status.languages.supported` being right while the model loads. That's fixed.
2. **Audio export Part B, the Zotero export flow:** `docs/superpowers/plans/2026-09-24-audio-export.md`, Tasks B2–B5.
   - B1's pure modules are already on main (`clients/zotero/src/export/`).
3. **Piper beside Kokoro:** spec `docs/superpowers/specs/2026-09-24-piper-engine-design.md`, plan `docs/superpowers/plans/2026-09-24-piper-engine.md`.
   - Approved, on hold by choice.
   - Measured here: Piper medium on one thread costs ~0.13 CPU-s per audio-s, against Kokoro's ~2.1.
   - Supertonic 3 was evaluated and dropped (archived 2026-09-09).
   - Samples are in `~/Music/speakd-tts-compare/`.

**How to run the work:** background subagents, one worktree per phase.
- Sonnet for closely specified tasks.
- Opus for live Zotero work and the whole-branch review before a merge.

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
- **Battery power (deferred by choice, 2026-09-24):** for now the engine is
  switched by hand (Piper as a cheaper engine, being designed). Later: switch
  automatically on battery (`/sys/class/power_supply/AC0/online`), hold
  background renders while on battery, and cap Kokoro's CPU threads.
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
