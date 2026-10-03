# Handoff — resume here

Last updated 2026-10-03. `main` **is pushed** to https://github.com/sfgartland/speakd.
The branch `claude/beautiful-hypatia-wg0nzy` is pushed too, **not merged**, with
a draft PR open against `main`.

**Resume here first:** check out `claude/beautiful-hypatia-wg0nzy` and work
through "Next → 0. Verify and merge the OpenRouter branch". It adds OpenRouter
as a hosted TTS engine, a secret setting for its API key, and better sentence
splitting. It was built in a cloud session that could not reach openrouter.ai
or build the desktop app, so nothing has been tried against the real API yet.
After that, phase 3 (see "Next → 1"). Nothing is mid-edit.

## What works now

| Piece | State | How to use |
|---|---|---|
| Daemon + desktop app | **The app runs the daemon as its child** (no systemd service any more; `speakd.service` is stopped and disabled). App menu entry **speakd** → `packaging/desktop/speakd-gui`, which runs the newest release/debug build of the shell. Either process dying takes the other down; tray Quit stops both | Open speakd from the menu; `speakctl status`. Restart = Quit in the tray, reopen |
| Engine | **Kokoro on onnxruntime, fed by misaki's G2P** (no torch; venv 6.3 GB → 982 MB, daemon ~520 MB RSS, model load ~11 s). Model files in `~/.cache/speakd/kokoro-onnx/`, downloaded on first use | `uv sync --extra kokoro --extra piper --extra lang` is this machine's set of extras |
| Desktop window | Follow-along text, seek, speed with ramps, replay, preparing highlight, ranked channel list (stars, hide fold), brief/full toggle | the app, or `cd clients/gui/app/src-tauri && cargo run` (connects to a running daemon, starts none) |
| Codex | Hooks in `~/.codex/hooks.json` + `speakd` MCP server in `~/.codex/config.toml`, via `clients/codex/install.sh` (installed 2026-10-01). Tested by unit tests only, **not yet against a live Codex session** | `speakctl mode full --source codex:<id>` |
| Claude Code | Installed as a plugin (`speakd@speakd`, user scope): hooks (prompt, Notification, Stop) plus the `speakd-mcp` server | New sessions start **brief and muted**; unmute one in the window. `SPEAKD_HOME` is exported in `~/.zshenv` |
| OpenCode | Plugin in `~/.config/opencode/plugins/` + MCP entry in `opencode.json`, via `clients/opencode/install.sh`. Sessions start brief and muted like Claude Code. **Verified live end to end** (speech, briefings, mode switching, channel resolution) | `speakctl mode full --source opencode:<id>` |
| Briefings | Agents call `brief` over MCP; sessions switch between brief and full per channel | `speakctl mode full --source claude-code:<id>`; the guide is `~/.config/speakd/briefing.md` |
| Zotero reader | Built, reviewed, verified live in a test Zotero; **not installed in your Zotero yet** | See "Install the Zotero plugin" below |
| Audio export (Zotero menu) | Built: menu, dialog, render, progress, attach/folder storage | Right-click item → **Export audiobook…**; settings in Config Editor (`extensions.speakd-reader.export_*`) |
| Audiobook agent skill | Installed by Claude Code, OpenCode, and Codex installers; teaches agents to prepare text and call `speakctl render` | `clients/claude-code/skills/speakd-audiobook/` |
| Notifications off-switch | `speakctl notify off` stops the reader for good (survives restarts), `on` brings it back, `status` reports it. **Not yet smoke-tested against the live daemon** — takes effect on the daemon's next restart | `speakctl notify off` / `on`; `SPEAKD_NO_NOTIFY` stays the startup hard-off |
| HTTP transport | `127.0.0.1:8642`, token-protected, for the Zotero plugin | `speakctl http-token` prints the token |

Local machine specifics, kept outside the repo:
- `~/.config/speakd/env` (read by the app's launcher) sets `SPEAKD_NO_NOTIFY=1`
  (the notifications reader was annoying and untrusted; delete the line to
  bring it back). `SPEAKD_DEVICE` is gone: there is no GPU path any more.
  The old systemd drop-in in `~/.config/systemd/user/speakd.service.d/` is
  unused while the service stays disabled.
- `~/Downloads/speakd-engine-compare/` holds the torch-vs-ONNX A/B the switch
  was decided on (and the script that made it).
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

## Built 2026-10-01 (merged and pushed)

- **The app owns the daemon** (`6f1257f`, `087b77b`): `clients/gui/app/src-tauri/src/daemon.rs`
  spawns it with `PR_SET_PDEATHSIG`; a watcher closes the shell when it exits;
  single-instance (D-Bus) makes a second launch show the window. The launcher
  waits up to 10 s for a daemon still stopping, and notifies if another holds
  the socket. `packaging/desktop/install-gui.sh` writes the `.desktop` entry.
- **The Codex client** (`a5e16b4`), written by Codex on 2026-09-26 and finished
  here, plus three bug fixes in the Claude Code / OpenCode clients.
- **ONNX Kokoro** (`28368e6`): same voice and pronunciation (A/B'd by ear),
  torch gone. Speed clamped to onnx's 0.5–2.0; downloads verified.
- **Pronunciation for Zotero and renders** (`e4aa7d8`): profiles gained
  `sentence_transforms` (run per sentence after segmentation, keeping exact
  sentence spans), and the `pdf` profile uses it for `pronunciation` — so
  Zotero reading now says "pages 34 to 38". Renders now run the profile's
  transforms at submission (they ran none before) and store prepared
  sentences in the manifest. "p. 12" → "page 12". The segmenter no longer
  splits at "p."/"pp." before a number, and an ellipsis is one "…" pause, at
  most one sentence break.

## Built 2026-10-01: Zotero audiobooks + agent skill (merged)

Plan `docs/superpowers/plans/2026-10-01-zotero-audiobooks.md`, six tasks, each
reviewed, then an Opus whole-branch review and one fix wave.
- `speakctl render` takes `# Title` chapter headings per form-feed part.
- The `speakd-audiobook` skill (`clients/claude-code/skills/`, with
  `scripts/prepare_text.py`) is copied by the Codex and OpenCode installers.
  It drops a trailing "References" outline chapter unless `--keep-references`.
- Zotero: right-click **Export audiobook…** → capture through Read Aloud
  playing nothing → dialog → render → item-pane progress → attach or folder;
  renders resume after a Zotero restart. Verified live in the harness,
  including a 360-page PDF captured in about 6 s (the wait is 180 s).
- **Carry forward:**
  - Export settings are Zotero prefs (`extensions.speakd-reader.export_*`),
    not daemon `zotero.*` settings. **Move them in phase 3**, which builds the
    plugin's `declare_settings` path.
  - Parked: the "Reading the PDF…" notice runs inside the export's `try`
    (`clients/zotero/src/export/entry.ts`); if `Zotero.ProgressWindow` threw,
    the export would abort. A two-line try/catch fixes it.
  - Deferred: the daemon's `estimate_seconds` is always 0 (the plugin falls
    back to a character count); `render_cancel` isn't scoped to the starting
    client; a failed attach leaves the file in `<profile>/speakd-exports/`
    with no startup sweep; `##` subheadings in `.md` input are spoken; an
    all-blank render file exits with the "unreachable" code.
- Re-run `clients/codex/install.sh` and `clients/opencode/install.sh` so the
  skill reaches those agents.

## Built 2026-10-03 on `claude/beautiful-hypatia-wg0nzy` (not merged)

Five commits, built in a cloud session. Lint, mypy and pytest (1525) are green.
The window's browser check passes apart from Google Fonts, which the sandbox
blocks.
- **Sentence splitting** (`segmenter.py`): a sentence up to 1.25× the 90-char
  cap stays whole; longer ones split into even parts at clauses (no fragment
  under 24 chars); word splits are balanced. `speech.merge_chars` (default 0,
  off) joins short sentences after the first for local engines.
- **OpenRouter engine** (`synth/openrouter_engine.py`): Qwen-Audio-3.0-TTS
  Flash by default, a stdlib `urllib` POST asking for PCM, resampled to 24 kHz.
  - Units are segmented after the engine is chosen. OpenRouter gets whole
    sentences merged up to `speech.openrouter_unit_chars` (300); the first unit
    is one sentence.
  - Speed is never sent: audio is made at 1.0, and the player stretches it to
    profile speed × tempo, so a tempo change or a seek never re-requests.
  - Errors: 400 is one bad segment; 401/402/403 are fatal (10-minute holdoff);
    429/5xx/network get one ≤3 s retry.
  - `speech.openrouter_on_failure`:
    - `alert` (default): the utterance ends, an `error` event, a chime (at most
      every 10 s), Kokoro stays unloaded.
    - `silent`: the same without the chime.
    - `local`: the old fallback to Kokoro/Piper from the next unit.
  - **Kokoro is not loaded** at start, and is unloaded on switching, while
    OpenRouter speaks without fallback (`kokoro_wanted()`). `status.engine`
    has `model_needed`, and the window's model button reads "Model not needed".
  - ru/ko/ar are recognised and detected.
- **The API key is a new `secret` setting type** (`settings/secrets.py`):
  `speech.openrouter_api_key`, stored in `~/.config/speakd/secrets/<key>`
  (0600, dir 0700), never in settings.toml.
  - Every verb, event and status reports it only as set/not set; only
    `Settings.secret()` returns it.
  - `speakctl set speech.openrouter_api_key` prompts without echo; an argument
    still works, with a warning.
  - The window has a write-only password field with Clear.
  - `OPENROUTER_API_KEY` in the environment wins; `status` gives `key_source`.

## Next

### 0. Verify and merge the OpenRouter branch

Check out `claude/beautiful-hypatia-wg0nzy`, `uv sync --extra kokoro --extra piper --extra lang`.
1. **The real API.** These are unverified assumptions. Check them with one
   `curl -sD- https://openrouter.ai/api/v1/audio/speech -H "Authorization: Bearer $KEY"
   -d '{"model":"qwen/qwen-audio-3.0-tts-flash","input":"Hello there.","voice":"loongjohn","response_format":"pcm"}' -o /tmp/x.pcm`:
   - Is the voice `loongjohn` accepted? If not, change `DEFAULT_VOICE` in
     `openrouter_engine.py` to one OpenRouter lists for the model.
   - Does `Content-Type` carry `rate=`? Without it 24 kHz is assumed, and a
     different real rate plays at the wrong pitch. Fix `_pcm_rate`'s default if so.
   - Is the body raw s16le PCM, not WAV with a header or JSON?
2. **Live in the daemon:** restart the app, then:
   - Enter the key in Settings and switch `speech.engine` to `openrouter`.
     Check memory drops (Kokoro unloaded) and a long agent reply speaks in
     long units.
   - Change the tempo mid-sentence (no re-request).
   - Try a bad key (chime + error line).
   - Pull the network mid-utterance (chime; the next message retries).
   - Switch back to kokoro (it reloads).
3. **Build the app:** `cd clients/gui/app/src-tauri && cargo build --release`.
4. **Browser check:** `clients/gui/test-browser/settings_check.py` launches
   `channel="chrome"`; it passed here only with Chromium's `executable_path`.
5. Whole-branch review (Opus), then merge to `main`, push, and delete this
   section's "not merged" notes.

Known gaps carried with the branch, none blocking:
- A hush stops audio at once, but the next utterance waits for an in-flight
  OpenRouter request (usually about 1 s, at most the 20 s timeout).
- A render through OpenRouter ignores the profile speed (no player stretch),
  and fails (resumably) on an outage. A 500k-char book costs about $7.50 on Flash.
- With `on_failure=local`, Kokoro may get one 300-char unit after the switch.
- Renders through OpenRouter, and players that cannot time-stretch, ignore the
  profile's speed.
- One failed sentence fails a whole OpenRouter render (it resumes, but does not
  skip the sentence).
- The urllib timeout (20 s) applies per socket operation, not to the whole
  request, so a slow trickle of bytes can outlast it.
- The default profile's prepare step marks pieces as not `exact`, so every unit
  of an utterance reports the whole text as its span (`0..len`) on every engine,
  Kokoro included; follow-along highlighting needs per-unit spans from it.
- The secret file is not encrypted (keyring would need a dependency). The
  socket keeps its default permissions, so another local user could at most
  overwrite the key, never read it.

### 1. Phase 3, language in the clients

`docs/superpowers/plans/2026-09-24-language-clients.md`.
- The Zotero side needs the live harness, so use Opus.
- Depends on `status.languages.supported` being right while the model loads. That's fixed.
- **Includes Piper Task 6's Zotero half** (the plugin's language-cache
  invalidation on a `speech.engine` setting event) — deferred until phase 3.
  Once the OpenRouter branch is merged, `openrouter` changes the supported
  languages too (adds de/ru/ko/ar), and so does the API key being set or
  cleared.

**How to run the work:** background subagents, one worktree per phase.
- Sonnet for closely specified tasks.
- Opus for live Zotero work and the whole-branch review before a merge.

## Deferred and known gaps

- **Housekeeping (needs the user's go-ahead):** ~25 merged worktrees under
  `../speakd-worktrees/` hold untracked `.superpowers/` reports. Removing them
  was blocked as destructive; the plan was to copy each `.superpowers/` into
  `.superpowers/archive/<name>/` first, then `git worktree remove --force` and
  `git branch -d`. Also removable now: `onnx-hybrid` and `pdf-pronounce`
  (merged), and `feat/onnx-engine` (superseded by the ONNX hybrid; never merged).
- **Flaky test:** `test_work_the_daemon_stopped_on_still_says_so` fails about
  1 run in 3, on main before these changes too.

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
- **The main checkout may carry uncommitted work-in-progress.** Check
  `git status` before assuming main's working tree is clean. (The Codex client
  that sat there from 2026-09-26 was committed on 2026-10-01.)
- **The daemon runs as the desktop app's child**, not as a systemd service:
  `packaging/desktop/speakd-gui` (the app-menu entry) starts the shell, which
  starts the daemon, and either one ending ends the other. This machine's
  daemon environment (`SPEAKD_NO_NOTIFY=1`; a leftover `SPEAKD_DEVICE=cpu` is ignored) is in
  `~/.config/speakd/env`; the old `speakd.service` is stopped and disabled.

## Where the record is

- Designs are in `docs/design/` and `docs/superpowers/specs/`; plans are in
  `docs/superpowers/plans/`.
- The Zotero internals reference, with the live-verified sections, is
  `docs/design/2026-09-24-zotero-10-read-aloud-internals.md`.
- `git log` holds the reasoning. Commit messages say why.
