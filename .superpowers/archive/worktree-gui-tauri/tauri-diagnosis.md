# Why `speakd-shell` (Tauri 2) would not build here — diagnosis, fix, alternatives

Date: 2026-09-14. Machine: Fedora 43, GNOME Shell 49.9 on Wayland, i7-10510U, 15 GiB RAM,
rustc/cargo 1.92.0 stable, tauri-cli 2.11.4. Worktree:
`/home/severingartland/programing_linux/speakd-worktrees/gui-tauri` (branch `feat/gui-tauri-shell`).

## TL;DR

- **It was not a memory-tuning problem and not a monomorphisation problem.** It was
  `"frontendDist": "../../"` in `tauri.conf.json`. That path is `clients/gui/`, which *contains*
  `app/src-tauri/target/`. Tauri's `generate_context!` embeds every file under `frontendDist`
  into the binary, with no exclusion filter. Each build therefore brotli-compressed and
  `include_bytes!`-ed the previous builds' output — including the previous builds' own embedded
  asset dumps — so the wall grew with every attempt (662 MB → 3.3 GB → 4.9 GB → 7.0 GB → 13 GB).
- **With `frontendDist` pointed at a directory that holds only the two real frontend files, the
  crate builds in ~4 seconds with a ~680 MB peak.** Measured, not estimated (§2).
- **The Tauri route is viable on this machine.** The 29 GB of poisoned asset dumps have been
  removed; `target/debug/deps` (the actual dependency cache) was left intact.
- **But two of the three things the shell exists for do not work on this desktop the way it is
  written, for reasons that have nothing to do with the build:** Tauri's global-shortcut plugin
  is X11-only (this session has zero XWayland windows, so the grabs would never fire), and
  `alwaysOnTop` is a no-op for native Wayland clients (works only under XWayland). Both are
  fixable, in either Tauri or Python. The GNOME GlobalShortcuts portal is present and answers
  from Python (§3).
- **Ranked recommendation (§4):** 1. PyGObject + WebKit2GTK 4.1 (GTK3) — measured to load the
  unmodified `pinned.html` and to reach every OS facility this app needs, with zero compile and
  one language. 2. Keep Tauri now that it builds, if a single distributable binary matters more
  than the Wayland hotkey work in Rust. Everything else ranks below those two.

---

## 1. Diagnosis (measured)

### 1.1 The mechanism

`clients/gui/app/src-tauri/tauri.conf.json` had:

```json
"build": { "frontendDist": "../../" }
```

Relative to `src-tauri/`, `../../` is `clients/gui/`. `clients/gui/` contains:

```
clients/gui/
├── pinned.html          20 KB   ← the frontend
├── prototype.html       16 KB
├── shared/speakd-source.js  16 KB   ← the frontend
└── app/src-tauri/       ← the Rust project, including target/ (33 GB at start of diagnosis)
```

Everything the app actually needs is **20 files, 0.6 MB** (`find clients/gui -type f -not -path
'*/target/*'`). What got embedded was everything under `clients/gui/`, including `target/`.

`tauri-codegen 2.6.3`, `src/embedded_assets.rs` (from `~/.cargo/registry`), walks `frontendDist` with:

```rust
WalkDir::new(&path).follow_links(true).contents_first(true)
  ...
  Ok(entry) if entry.file_type().is_dir() => None,   // the only filter: skip directory entries
  Ok(entry) => { /* compress all files encountered */ }
```

No ignore list, no size cap, no `.taurignore` support (grepped `tauri-codegen`, `tauri-build`,
`tauri-utils` sources: zero hits). Each file is sha256-hashed, **brotli-compressed** (Tauri's
default `compression` feature is on — `brotli` is in `Cargo.lock`) into
`$OUT_DIR/tauri-codegen-assets/<hash>.<ext>`, and the generated code then references it as:

```rust
const _: &[u8] = include_bytes!(#input);   // the ORIGINAL file, for dependency tracking
include_bytes!(#output)                    // the compressed copy
```

So rustc holds **both** the raw and the compressed bytes of every file. The "30–45 minutes of
single-threaded 97 % CPU" was the proc macro hashing and brotli-compressing tens of gigabytes;
the OOM came when rustc then tried to materialise the `include_bytes!` constants.

### 1.2 The feedback loop — why each attempt "got further and found a bigger wall"

Each attempt's build-script `out/` directory in `target/debug/build/speakd-shell-*/`:

| build-script run | started | `out/tauri-codegen-assets` size | note |
|---|---|---|---|
| `b81aee8a` | 09-13 21:35 | 628 MB | first run; embedded `target/` as it then was |
| `6adeac80` | 09-13 21:48 | 3.3 GB | now also embeds `b81aee8a/out/tauri-codegen-assets` |
| `1543e8bc` | 09-13 22:48 | 4.9 GB | … and the two before |
| `918f8f3e` | 09-13 23:32 | 7.0 GB | |
| `969ecd61` | 09-14 00:28 | **13 GB, 50,295 files** | 11 files > 100 MB; four identical 635 MB blobs (rlibs) |

Compressed outputs are content-addressed and cached (`if !out_path.exists()`), so every new
build-script directory re-embedded all previous ones. `target/` was 34 GB; 29 GB of that was
`tauri-codegen-assets` and only 3.9 GB was `deps/`.

Kernel OOM records (`journalctl -k`), aligned with those directories:

| killed at | anon-rss | total-vm | what had just happened |
|---|---|---|---|
| 09-13 22:45:57 | 7.28 GB | 11.3 GB | embedding 3.3 GB set |
| 09-13 23:26:11 | 8.11 GB | 13.7 GB | 4.9 GB set |
| 09-14 00:27:11 | 6.94 GB | 12.7 GB | 7.0 GB set (the run the Cargo.toml comment blames on incremental) |
| 09-14 01:14:48 | 7.04 GB | 12.9 GB | 13 GB set |
| 09-14 11:25:36 | 10.24 GB | 27.4 GB | + 12 GB swap |
| 09-14 12:25:49 | 12.09 GB | 24.9 GB | + zram off |

Every kill is 40–60 min after the corresponding build-script run — the compression time.

### 1.3 Why "`cargo check` passes in three minutes" was misleading

The only `cargo check` that ever completed is fingerprint `c1a0b9c951b9e458` at **21:46 on
09-13** (its `libspeakd_shell-*.rmeta` exists; no later check fingerprint is complete). Its
dep-info file already listed **10,955 paths under `src-tauri/target`** (`speakd_shell-c1a0…d`,
10,983 lines). By this morning the dep-info from the killed run (`speakd_shell-30793908….d`,
written 11:22) was **118 MB with 161,858 `target/` paths**. Check "passed" only while the
embedded set was ~660 MB; nothing about check vs build distinguished type-checking from codegen —
it was the size of the embedded blob, which grew between the check and the builds.

### 1.4 What it was *not* (each tested after the fix, §2)

- **Monomorphisation.** `cargo llvm-lines --bin speakd-shell`: **568,666 IR lines, 15,776
  instantiations** in total; the largest single item is `main`'s setup closure (51,382 lines,
  9 %), then `tauri::window::plugin::init` closures (74 copies, 3.8 %). That is a small-to-medium
  crate. rustc peak during llvm-lines: 658 MB.
- **Incremental compilation.** Building with the *default* dev profile (`debug=2`,
  `incremental=true`, overriding the uncommitted `[profile.dev]` block via env): 20 s, **1.08 GB**
  peak, 200 MB binary, 205 MB incremental dir. Incremental + full debuginfo cost ~400 MB, not
  6 GB. The comment in the uncommitted `Cargo.toml` attributing the 6.9 GB kill to incremental
  is wrong and should be removed.
- **codegen-units, `-j`, swap, zram.** None touch the cause; they only changed how far the
  brotli/`include_bytes!` stage got before dying.

---

## 2. Making it build — what I changed and what I measured

### 2.1 The change (uncommitted, in the worktree)

1. Created `clients/gui/app/dist/` containing two symlinks (walkdir follows them):
   ```
   dist/pinned.html -> ../../pinned.html
   dist/shared      -> ../../shared
   ```
2. `tauri.conf.json`: `"frontendDist": "../../"` → `"frontendDist": "../dist"`.
   (`tauri.conf.json` is `rerun-if-changed`, so the build script re-ran automatically.)

`git status` in `clients/gui/`: `M app/src-tauri/Cargo.toml` (your earlier profile block, untouched
by me), `M app/src-tauri/tauri.conf.json` (mine); `dist/` is new and untracked. Original config
saved at the scratchpad path `…/scratchpad/tauri.conf.json.orig`.

The durable rule: **`frontendDist` must never be an ancestor of `src-tauri`.** Any of these
satisfy it — the symlink `dist/` (smallest change, keeps "served straight off disk"), moving the
Rust project out of `clients/gui/` (e.g. `clients/gui-shell/src-tauri` with
`frontendDist: "../../gui"`), or moving the HTML into `clients/gui/web/`. `.gitignore` should
also grow `/dist/` if you go with a generated dist instead of committed symlinks.

### 2.2 Measurements after the fix

All runs wrapped in `/usr/bin/time -v` inside `systemd-run --user --scope -p MemoryMax=8G
-p MemorySwapMax=0` (so a runaway would be killed in its own cgroup, not the desktop), with a
1 s sampler recording the largest `rustc` RSS. "peak" = GNU time's max RSS over cargo and all
waited-for children.

| run | wall | peak RSS | result |
|---|---|---|---|
| `cargo check` (profile as in your Cargo.toml) | 47 s (re-checked deps, 327 % CPU) | 1.18 GB | ok |
| `cargo build` | 4.5 s | 678 MB | ok, 23.5 MB binary |
| `touch src/main.rs && cargo build` (leaf only) | 4.2 s | 671 MB (rustc sample 601 MB) | ok |
| `cargo llvm-lines --bin speakd-shell` | 10 s | 658 MB | 568,666 lines / 15,776 copies |
| `cargo build` with default dev profile (`debug=2`, `incremental=true`) | 20 s | 1.08 GB | ok, 200 MB binary |
| `cargo build` after deleting all `tauri-codegen-assets` | 3.8 s | 677 MB | regenerated exactly 2 assets: 6,401 B (`pinned.html`, brotli) + 5,687 B (`speakd-source.js`) |
| `cargo build --release -j 3` (release profile, no release artifacts cached beforehand) | 4 min 05 s | 1.28 GB (rustc sample 1.34 GB) | ok, 285 crates compiled (rest reused), 11.8 MB binary; `target/` now 6.2 GB |

Free memory during the runs was 7–8.5 GB; the 8 GB cap was never approached.

### 2.3 Disk cleanup performed

Deleted `target/debug/build/speakd-shell-*/out/tauri-codegen-assets/` (29 GB total). These were
the *product* of the bug, not a cache of anything; they regenerate in milliseconds for two files.
`target/` went 34 GB → 5.1 GB. **`target/debug/deps` (3.9 GB, three profile variants of all
443 crates) was not touched**, which is why the rebuilds above take seconds.

### 2.4 Recommendations for `Cargo.toml`

- The `[profile.dev]` block is harmless but its comments are wrong (see §1.4). `debug = false`
  is still a sensible choice here: it takes the binary from 200 MB to 23 MB and the leaf build
  from 20 s to 4 s. `incremental = false` makes no measurable difference either way; drop it
  or keep it, but delete the explanation.
- No Tauri feature trimming is needed; the default feature set compiles in 4 s.

### 2.5 Smoke test of the built binary (on the live Wayland session)

Ran a copy of `target/debug/speakd-shell` under `timeout` three times:

- Process ran until `timeout` killed it (exit 124), **stderr empty** — setup, plugin registration
  and window creation all succeeded (a failure in `setup()` would have panicked).
- Tray: `org.kde.StatusNotifierWatcher.RegisteredStatusNotifierItems` (owned by gnome-shell)
  listed `:1.597@/org/ayatana/NotificationItem/tray_icon_tray_app_main`, and
  `busctl status :1.597` gave **the app's PID**. The tray works because the
  `appindicatorsupport@rgcjonas` extension is enabled here.
- Spawned `WebKitWebProcess`, `WebKitNetworkProcess` (webkit2gtk-4.1) and a `bwrap` sandbox.
  Main process RSS 189 MB.

---

## 3. Desktop facts that matter more than the framework (measured)

These apply to *any* GTK/WebKit app on this GNOME 49 Wayland session, Tauri included.

### 3.1 Global hotkeys

- `global-hotkey 0.8.0` (what `tauri-plugin-global-shortcut 2.3.x` wraps) has
  `src/platform_impl/{macos,windows,x11,no-op}` — the Linux backend is `x11rb` `XGrabKey`.
  Zero occurrences of "wayland" in the crate. Registration "succeeds" (it grabs on XWayland's
  root window), but such grabs only see keys pressed while an **XWayland** window is focused.
- `wmctrl -lp` on this session: **no XWayland windows**. Chrome, Thunderbird and kitty are
  native Wayland clients. So, as written, Ctrl+Alt+P / Ctrl+Alt+H would never fire while you
  work.
- The working mechanism is the XDG **GlobalShortcuts portal**: `org.freedesktop.portal.GlobalShortcuts`
  version 1 is on the session bus, and the GNOME backend (`xdg-desktop-portal-gnome 49.0`)
  implements `org.freedesktop.impl.portal.GlobalShortcuts`. From Python (`Gio.DBusProxy`, no
  extra packages), `CreateSession` returned **Response 0** with a session handle. `BindShortcuts`
  then shows a one-time GNOME consent dialog and delivers `Activated` signals. `libportal`'s
  Python binding (`Xdp 1.0`, installed) has no GlobalShortcuts API yet, so this is ~60 lines of
  direct D-Bus. In Rust the equivalent is the `ashpd` crate (async, pulls zbus/tokio) — more
  code and dependencies than the whole current shell.

### 3.2 Always-on-top

- Native Wayland has no keep-above request for ordinary clients; GTK3's `set_keep_above` is
  inert there (GNOME's own window-menu "Always on Top" is user-driven, not app-driven).
- Under XWayland Mutter honours EWMH. Measured with `xprop -id … _NET_WM_STATE` while running:
  - a PyGObject GTK3 window with `set_keep_above(True)`, `GDK_BACKEND=x11` →
    `_NET_WM_STATE_ABOVE` set;
  - the Tauri binary with `GDK_BACKEND=x11` (`alwaysOnTop: true` from config) →
    `_NET_WM_STATE_ABOVE` set;
  - the Tauri binary with default backend → not an X client at all (`wmctrl` lists 0 windows).
- Practical answer for either framework: launch the monitor with `GDK_BACKEND=x11`. (Cost: on a
  fractionally-scaled HiDPI display XWayland windows can look soft; at 100 % scaling there is no
  visible difference.)

### 3.3 Tray

Works via StatusNotifierItem/AppIndicator, because `appindicatorsupport` is enabled (GNOME
ships no tray otherwise). Your own `scripts/attribution/tray.py` already does exactly this with
`AppIndicator3` from PyGObject and is registered in the same watcher list.

---

## 4. Alternatives, weighed on this machine

Criteria: build cost *here*, whether always-on-top / tray / global hotkeys are genuinely
reachable, whether `pinned.html` is reused unchanged, solo-maintainer cost.

### 4.1 PyGObject + WebKit2GTK 4.1 (GTK3) — recommended

I tried to falsify it; what I found:

- **Unmodified `pinned.html` loads.** Test script (scratchpad `wk_load_test2.py`): GTK3 window,
  `WebKit2.WebView`, probe via `evaluate_javascript`. Result: `title: "speakd Pinned Monitor"`,
  `readyState: complete`. One real catch, found and solved: over plain `file://` the inline
  `<script type="module">` **fails** (`resource-error` on the module script; the
  `import … from "./shared/speakd-source.js"` is CORS-blocked; 64 DOM nodes = static markup
  only). Served through a registered custom URI scheme (`WebContext.register_uri_scheme("speakd", …)`
  + `register_uri_scheme_as_cors_enabled`) — the same thing Tauri does with `tauri://localhost`
  — it runs with **zero errors, 74 nodes** (the controller built its extra nodes). That handler
  is ~15 lines. (The two `allow_*_file_urls` settings also work, 74 nodes, but the scheme is
  cleaner.)
- **`window.__TAURI__` surface is tiny**: four call sites in `pinned.html` (`invoke("report_state")`,
  `invoke("set_always_on_top")`, `listen("speakd://hotkey")`, `listen("speakd://tray")`). Inject a
  `UserScript` at document start that defines `window.__TAURI__ = {core:{invoke}, event:{listen}}`
  on top of `window.webkit.messageHandlers` and `evaluate_javascript`, and **`pinned.html` stays
  byte-for-byte unchanged**.
- **GL requirement**: WebKitGTK 2.52 needs a GL context; an offscreen GTK window aborts
  ("GDK is not able to create a GL context"). A normal window is fine. Irrelevant for the app,
  relevant if you ever try to test headless.
- Tray: `AppIndicator3` — proven on this desktop by your own tray.py. Hotkeys: portal, §3.1,
  measured reachable from Python. Always-on-top: `set_keep_above` + `GDK_BACKEND=x11`, measured.
  Frameless drag: `Gtk.Window.begin_move_drag` from a JS mousedown message. Window-state memory:
  a JSON file.
- Python 3.14 vs speakd's `<3.14` pin: separate process on `/usr/bin/python3`, which is what you
  want anyway; it can talk HTTP/SSE to the daemon exactly as `speakd-source.js` does today, or
  speak the Unix socket (`/run/user/1000/speakd/speakd.sock` is listening) natively and push
  events into the webview.
- Build cost: none. Dependencies: `python3-gobject`, `webkit2gtk4.1`, `libappindicator-gtk3` —
  all installed. Estimated size: 250–400 lines of Python, one file.
- Long-term: WebKit2GTK 4.1 is the GTK3 API line; Fedora 43 ships it (2.52.5) alongside
  `webkitgtk6.0`, and Tauri/wry itself depends on 4.1, so it is not going away soon. Same
  engine, same memory footprint (~300 MB with the WebKit helper processes) as Tauri.

Nothing I measured falsifies it. Its real weakness is distribution to *other* machines (it is
"a Python script plus three distro packages", not a `.deb`/AppImage).

### 4.2 Keep Tauri (it builds now)

- Build: 4 s incremental, ~680 MB. Toolchain and 443-crate cache are in place.
- Functional gaps on this desktop: hotkeys need the portal (`ashpd` in Rust — non-trivial),
  always-on-top needs `GDK_BACKEND=x11` (same as Python).
- Maintenance: 210 lines of your Rust on top of 443 crates and a Tauri major line, to obtain
  three OS facilities that GTK exposes directly. Justified if you want `cargo tauri build` to
  hand you a `.deb`/AppImage for others.

### 4.3 The rest, briefly

- **GTK4 + libadwaita (Python)**: worse fit. GTK4 removed `set_keep_above`; there is no GTK4
  AppIndicator binding (you would hand-write StatusNotifierItem over D-Bus). Only the webview
  (`webkitgtk6.0`, installed) is a wash.
- **Electron**: Node 22 is present, Electron is not (~250 MB download). No compile. Runs as
  XWayland by default so always-on-top works; tray via SNI works; global shortcuts are X11
  grabs — same Wayland problem as Tauri. Heaviest option, second runtime to keep updated. Ranks
  third only because it needs no build.
- **Qt / PySide6**: not installed (`pip install pyside6` + QtWebEngine ≈ 300 MB, a second
  browser engine). `QSystemTrayIcon` works, `WindowStaysOnTopHint` works only under `xcb`, no
  global-hotkey API at all. Big dependency, no gain over GTK that is already here. (PyQt5 5.15
  is present, but without WebEngine.)
- **pywebview / `webview` C library**: on Linux these are thin wrappers over the same WebKitGTK;
  they add nothing for tray/hotkeys/keep-above and get in the way of the custom scheme and user
  scripts you need. Not worth the layer.
- **Native GTK widgets, no webview**: reuses none of the finished HTML and costs the most; the
  only prize is memory (~40 MB vs ~300 MB). Worth considering later *only* if the WebKit
  footprint becomes the problem on this RAM-tight machine — that footprint is identical for
  Tauri and PyGObject, so it does not affect the choice between them.

### 4.4 Ranking

1. **PyGObject + WebKit2GTK 4.1 (GTK3)** — zero build, one language, every facility measured
   reachable, HTML unchanged via a custom scheme + `__TAURI__` shim, hotkeys via the portal in
   ~60 lines of D-Bus.
2. **Tauri, kept** — now a 4-second build; choose it if a single distributable binary matters
   more than doing the Wayland hotkey work in Rust and carrying 443 crates.
3. Electron. 4. PySide6. 5. pywebview / webview-C. 6. GTK4/libadwaita. 7. Native widgets (later,
   only for RAM).

---

## 5. State I left behind

- `clients/gui/app/dist/` (two symlinks) and the one-line `tauri.conf.json` change — uncommitted.
- `clients/gui/app/src-tauri/target/`: 6.2 GB (deps intact; asset dumps removed; plus the new `target/release` from §2.2).
- Installed `cargo-llvm-lines 0.4.48` via `cargo binstall` (prebuilt, 4 s) into `~/.cargo/bin`.
- Scratchpad (session-temporary): `rss_sampler.sh`, `wk_load_test*.py`, build logs, the
  original `tauri.conf.json`, two smoke-test binary copies.
- No cargo/rustc/smoke processes left running (verified at the end).
