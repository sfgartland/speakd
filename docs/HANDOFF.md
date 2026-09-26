# Handoff — resume here

Last updated 2026-09-26. `main` **is pushed** to https://github.com/sfgartland/speakd
(as of `331860e`, and again with any later commits).

## What works now

| Piece | State | How to use |
|---|---|---|
| Daemon | Running as the `speakd` user service | `systemctl --user restart speakd`; `speakctl status` |
| Desktop window | Follow-along text, seek, speed with ramps, replay, preparing highlight, ranked channel list (stars, hide fold), brief/full toggle | `cd clients/gui/app/src-tauri && cargo run` |
| Claude Code | Installed as a plugin (`speakd@speakd`, user scope): hooks (prompt, Notification, Stop) plus the `speakd-mcp` server | New sessions start **brief and muted**; unmute one in the window. `SPEAKD_HOME` is exported in `~/.zshenv` |
| OpenCode | Plugin in `~/.config/opencode/plugins/` + MCP entry in `opencode.json`, via `clients/opencode/install.sh`. Sessions start brief and muted like Claude Code. **Verified live end to end** (speech, briefings, mode switching, channel resolution) | `speakctl mode full --source opencode:<id>` |
| Briefings | Agents call `brief` over MCP; sessions switch between brief and full per channel | `speakctl mode full --source claude-code:<id>`; the guide is `~/.config/speakd/briefing.md` |
| Zotero reader | Built, reviewed, verified live in a test Zotero; **not installed in your Zotero yet** | See "Install the Zotero plugin" below |
| Notifications off-switch | `speakctl notify off` stops the reader for good (survives restarts), `on` brings it back, `status` reports it. **Not yet smoke-tested against the live daemon** — takes effect on the daemon's next restart | `speakctl notify off` / `on`; `SPEAKD_NO_NOTIFY` stays the startup hard-off |
| HTTP transport | `127.0.0.1:8642`, token-protected, for the Zotero plugin | `speakctl http-token` prints the token |

Local machine specifics, kept outside the repo:
- `~/.config/systemd/user/speakd.service.d/local-device.conf` sets `SPEAKD_DEVICE=cpu`.
  This laptop's NVIDIA GPU is too old for torch's CUDA build. Because the engine
  falls back to the CPU anyway, plain `uv sync` / `uv run` are safe again.
- `~/.claude/settings.json.before-speakd-plugin` is the backup from before the
  hand-wired hooks were replaced by the plugin.
- **The Bluetooth speakers eat the first seconds of audio after a silent
  period** (their own standby wakes on signal, not on the link; PipeWire keeps
  the node running, which is not enough). Symptom: only later sentences
  audible. `parecord -d bluez_output…monitor` proves the full audio reaches
  the sink. The deferred fix list has the options.

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

## Built on 2026-09-25

- **The OpenCode client** — everything Claude Code has: `clients/opencode/` is a
  dependency-free plugin (`speakd-mcp` reuses the shared server), sessions start
  brief and muted, briefings/mode-switching verified live end to end.
  - Two bugs found and fixed only by the live E2E: the v1 plugin file must
    default-export `{id, server}` ("Plugin export is not a function"
    otherwise), and `_speaking` never cleared made every prompt-hush stop an
    idle player (see the hush fix below).
  - OpenCode v2 rewrites the plugin system entirely (v1 plugins don't run on
    v2). The signals used survive into v2's event union; the port is an
    entry-shim job documented as Phase D in the plan. Do it when upgrading.
- **The hush-interrupt fix (core daemon):** a channel-scoped hush on a silent
  channel read as "sounding" (stale `_speaking`) and stopped the idle player,
  arming an interrupt the next utterance's first sentence ate whole. Fix:
  `_stop_speaking` only stops the player when an utterance is in flight, and
  each utterance clears interrupts aimed at playback that already ended.
  This one hid under brief-and-muted defaults and the BT speaker quirk; it was
  only found by testing on the system speakers. Regression test in
  `tests/test_daemon.py`.
- **`disable notifications` design** is in
  `docs/superpowers/specs/2026-09-25-disable-notifications-design.md` —
  **built and merged** (see "Built today, continued" below).
- **CI's mypy gate was red on main** (lingua ships no stubs; the optional-extras
  overrides in `pyproject.toml` didn't list it). Fixed on main with the other
  extras: `6dac088`.

## Built today, continued (merged as `e139851`)

- **The notifications off-switch**, per the spec and
  `docs/superpowers/plans/2026-09-25-notifications-off-switch.md` (4 TDD tasks,
  per-task reviews, whole-branch review): `notify_enabled` in `DaemonState`
  (defaults on; a corrupt state file keeps the reader), the daemon owns its
  connectors (`add_connector`/`stop_connectors`, a testable `_shutdown` keeping
  the connectors→silence→server.stop order), the global `set_notify` verb
  (persist first, then stop + per-channel `notify:` hush, then the `notify`
  event; `on` refused under `SPEAKD_NO_NOTIFY`; `off` always persists), and
  `speakctl notify off/on` (socket-only). `set_notify` deliberately stays out
  of the HTTP allowlist. Deferred follow-ups in "Deferred and known gaps".

## Built 2026-09-26 (merged as `331860e`)

- **Piper beside Kokoro** — the whole 7-task plan
  (`docs/superpowers/plans/2026-09-24-piper-engine.md`, spec
  `docs/superpowers/specs/2026-09-24-piper-engine-design.md`), one worktree
  per task, per-task reviews, a final whole-branch review, and the ear check
  confirmed by a human listen. ~16× cheaper than Kokoro on CPU; `de` now has
  a real voice. Switch with `speakctl set speech.engine piper` (window
  Settings also works); voices via `python -m piper.download_voices ...`;
  README's "Piper" section has the details. `en_US-ryan-medium` is downloaded;
  `en_US-ryan-high` was already there.
  - The ear check caught two pre-existing pronunciation gaps and the fixes
    are merged: sentence-final ranges ("34–38.") now rewrite, and "pp." reads
    as "pages". The render went to
    `~/Music/speakd-tts-compare/piper-medium-speakd.wav` (human-confirmed).
  - Deferred small items in "Deferred and known gaps" below.
- **CI fixed twice on the way:** lingua's missing stubs (mypy overrides) and
  the piper tests' scipy skips + the HTTP 413 BrokenPipe flake.

## Next

### 1. Phase 3, language in the clients

`docs/superpowers/plans/2026-09-24-language-clients.md`.
- The Zotero side needs the live harness, so use Opus.
- Depends on `status.languages.supported` being right while the model loads. That's fixed.
- **Includes Piper Task 6's Zotero half** (the plugin's language-cache
  invalidation on a `speech.engine` setting event) — deferred until phase 3.

### 2. Then, as before

- **Audio export Part B, the Zotero export flow:** `docs/superpowers/plans/2026-09-24-audio-export.md`, Tasks B2–B5.
  - B1's pure modules are already on main (`clients/zotero/src/export/`).

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
  - Claude Code and OpenCode sessions are brief and muted by default, and a
    daemon restart resets that.
- **Bluetooth speaker standby** eats the first seconds of audio after silence
  (verified: `parecord` on the bluez monitor shows the full utterance; the
  device gates on signal, not on the link). Options, best first: a standby
  setting on the speaker; a PipeWire-side keep-alive; or a speakd `wake_burst`
  (a short low-level pre-roll before the first sentence after idle — needs
  its own design if chosen).
- **The notifications off-switch** (merged and smoke-tested live on
  2026-09-25: off kills the reader, on revives it, status reports). Small
  follow-ups from the whole-branch review, all pre-existing-or-hypothetical: a
  per-supervisor guard in `stop_connectors()`, one shared constant for the
  `"notify"` connector name (daemon + `__main__` both spell it), and
  `Supervisor.start()` silently no-opping forever if its thread never starts
  (`Daemon.start()` already rolls this back — mirror it in supervise.py).
- **Piper follow-ups** (from the whole-branch review; all Minor, all cheap):
  `reads_years` on the engines is dead surface — pin it in tests or drop it;
  the year rule is English-only (German voices get English year words);
  a resumed render part whose engine needs a missing extra fails with the bare
  error `"piper"`; `has_voice`/`installed_voices` mix `is_file`/`exists`;
  `trim_silence` no-ops on real Piper (noise floor above the Kokoro-tuned
  threshold — edges still <50 ms by Piper's own tightness); on a
  `PiperVoiceError` fallback, `started.engine` says "piper" while Kokoro
  speaks. Defer all.
- **OpenCode v2 port:** entry shim only, per Phase D of the opencode plan.
- **Battery power (deferred by choice, 2026-09-24):** Piper is built and
  switching is available now, by hand, through `speech.engine` (`speakctl set
  speech.engine piper`, or the window's Settings). Automatic switching on
  battery, holding background renders while on battery, and capping Kokoro's
  CPU threads all remain deferred.
- **CI** runs without the kokoro and piper extras, so the real-engine tests are
  skipped there; piper tests needing scipy skip too.

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
- **Old worktrees** under `../speakd-worktrees/` (cc-hooks, gui-tauri, onnx,
  opencode, opencode-js, hush-interrupt, notify-off-switch, fix-ci, piper-years,
  piper-engine, piper-choose, piper-daemon, piper-renders, piper-window,
  piper-docs, …) are kept by convention. All of the ones after `notify-off-switch`
  are fully merged; remove them when it suits you.
- **Commit emails are public** on GitHub (`sfgartland@hotmail.com`).
- **The main checkout may carry uncommitted work-in-progress** (as of
  2026-09-26: a Codex client under `clients/codex/` and edits to the opencode
  client, the mcp server and the cc follower). Check `git status` before
  assuming main's working tree is clean; Piper work happened in worktrees so
  the two don't collide.

## Where the record is

- Designs are in `docs/design/` and `docs/superpowers/specs/`; plans are in
  `docs/superpowers/plans/`.
- The Zotero internals reference, with the live-verified sections, is
  `docs/design/2026-09-24-zotero-10-read-aloud-internals.md`.
- `git log` holds the reasoning. Commit messages say why.
