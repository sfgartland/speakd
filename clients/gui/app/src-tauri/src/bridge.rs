//! bridge.rs — the daemon's Unix socket, relayed into the window.
//!
//! The daemon speaks line-delimited JSON on a Unix socket and nothing else:
//! there is no HTTP server and none is planned for this milestone (see the
//! "Revision, 2026-09-13: the shell reads the socket, not HTTP" section of
//! docs/design/2026-09-13-gui-milestone-design.md). A webview cannot open a
//! Unix socket. This shell is a native process, so it opens one on the
//! webview's behalf and relays what comes back.
//!
//! Two directions, deliberately on separate connections:
//!
//!   * **Events.** One long-lived connection that sends `subscribe` once and
//!     then does nothing but read. Every line is re-emitted to the window as
//!     a `speakd://event`, verbatim. This file does not interpret the
//!     daemon's vocabulary — every place that knows what a `position` means
//!     is one more place to change when the daemon grows an event kind.
//!     Renaming `event` to `kind` and numbering segments happens once, in
//!     `ShellSource` (clients/gui/shared/speakd-source.js).
//!
//!   * **Control.** One connection per request, opened and closed around it.
//!     `speakd.transport.SocketServer` turns a connection that subscribes
//!     into an event stream, and drops that whole connection — pending
//!     requests and all — when a subscriber stops draining it. Sharing one
//!     connection would mean a window that fell behind losing its ability to
//!     say `hush`, which is the one thing it must never lose. `speakctl
//!     subscribe` opens two connections for the same reason.
//!
//! Nothing here runs on the UI thread. The reader is its own std thread, and
//! the control commands are `async` so Tauri runs them off the main thread —
//! a plain `#[tauri::command] fn` runs *on* it — with the blocking socket
//! work inside `spawn_blocking`. A window that freezes because speech
//! stopped is worse than one showing stale numbers.

use std::io::{BufRead, BufReader, Write};
use std::os::unix::net::UnixStream;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use serde_json::{json, Value};
use tauri::{AppHandle, Emitter, Runtime};

/// Carries one daemon event line to the window, unchanged.
const EVENT_CHANNEL: &str = "speakd://event";
/// Carries whether the relay currently holds a subscription, and why not.
/// The window has to be able to *say* the daemon is not running rather than
/// sit there showing nothing, which is what a stopped daemon and a quiet one
/// otherwise look like to it.
const LINK_CHANNEL: &str = "speakd://link";

/// Exactly the line `speakctl subscribe` sends. An empty `source_id` is
/// right here: `SUBSCRIBE` is answered inside the transport and never
/// reaches `Daemon.handle`, so it opens no channel — the id is only ever
/// read back in the daemon's "dropped subscriber" diagnostic.
const SUBSCRIBE_LINE: &[u8] = b"{\"verb\":\"subscribe\",\"source_id\":\"\",\"payload\":{}}\n";

/// Named on control requests that do not route by channel. Pause, resume,
/// hush, cancel, seek and set_speed are global, exactly as they are from `speakctl`, and
/// the daemon ignores their `source_id` — so for those this is a label in a
/// log, not a routing decision. `mute` is the exception and says so at
/// `speakd_send`.
const CONTROL_SOURCE_ID: &str = "gui";

/// The verbs the shell will forward. Not an arbitrary passthrough: this
/// window monitors speech that other clients originate (see "Settled:
/// now-playing only" in the milestone design, and its 2026-09-15 amendment).
///
/// `enqueue` is still absent, and deliberately: a bug in the frontend must not
/// be able to make the window speak arbitrary traffic on an arbitrary channel.
/// Pasted text goes through `speakd_say` below, which can do that one thing and
/// nothing else.
///
/// `status` joins the control verbs because the window needs to read mute and
/// engine state at startup — the event stream reports changes, but a window
/// opened against an already-muted daemon has missed them.
///
/// `seek` moves within the utterance being spoken — the arrow buttons, and a
/// click on a sentence — and `set_speed` is the listener's speed. Both are
/// global like pause: they change what is being heard, not what any channel
/// says, so neither can make the window originate speech.
///
/// `set_mode` switches one channel between brief and full narration. It is
/// scoped by the source it names, as a per-channel mute is, and changes how a
/// channel is heard rather than what it says.
///
/// `replay` plays the daemon's last utterance again from a chosen sentence.
/// It carries an index and nothing else, so it can only repeat words a channel
/// has already spoken — never put new ones on it.
///
/// `settings` and `set_setting` are the Settings view's two verbs (§1 of the
/// settings-and-languages design). `declare_settings` is deliberately never
/// forwarded: it is how a *client* — the Zotero plugin, a Claude Code
/// session — declares the settings it owns, and this window is neither.
const FORWARDED: &[&str] = &[
    "pause", "resume", "hush", "cancel", "seek", "replay", "mute", "set_mode", "set_engine",
    "set_speed", "status", "settings", "set_setting",
];

/// The one way the window originates speech.
///
/// Not `enqueue` on the forwarding allowlist: that would let any frontend bug
/// send any payload on any channel. This can do exactly one thing — speak text
/// a person typed, on the window's own channel — so the property `FORWARDED`
/// was protecting mostly survives.
///
/// The source is a literal, never a parameter, so the pasted speech is an
/// ordinary channel: it appears in the channel list, it can be muted on its
/// own, and `hush` reaches it like anything else.
///
/// It is the same string as `CONTROL_SOURCE_ID`, which is a trap worth naming:
/// the paste box's channel and the shell's control label are now one channel,
/// so a global `mute` must go out with an **empty** `source_id`. Passing
/// `CONTROL_SOURCE_ID` there — the obvious simplification of `speakd_send` —
/// would silently turn the window's master mute into a per-channel mute of
/// this box, and nothing else would look wrong.
const SAY_SOURCE: &str = "gui";

/// Matches `speakd.transport._REQUEST_TIMEOUT_SECONDS`. Applied only around
/// a request's reply and around the subscribe ack — never to the event
/// stream, where an idle stream is not a stuck one.
const REQUEST_TIMEOUT: Duration = Duration::from_secs(5);

/// Reconnect backoff. Starts short so restarting the daemon under a running
/// window is not a visible wait, and caps low enough that a window left open
/// overnight beside a stopped daemon is not polling a socket path in a tight
/// loop.
const RECONNECT_MIN: Duration = Duration::from_millis(500);
const RECONNECT_MAX: Duration = Duration::from_secs(5);

/// What the relay last managed, so a window that finishes loading *after*
/// the relay has already connected can ask rather than wait for the next
/// change. Tauri events emitted before the frontend registers its listener
/// are simply lost, and the first of these is emitted within milliseconds of
/// startup.
pub struct LinkState {
    connected: AtomicBool,
    detail: Mutex<String>,
}

impl LinkState {
    pub fn new() -> Self {
        Self {
            connected: AtomicBool::new(false),
            detail: Mutex::new(String::from("connecting to speakd")),
        }
    }

    fn detail(&self) -> String {
        // A poisoned lock is not worth failing a status read over: the only
        // thing under it is a message string.
        match self.detail.lock() {
            Ok(held) => held.clone(),
            Err(poisoned) => poisoned.into_inner().clone(),
        }
    }
}

/// Where the daemon listens. Mirrors `speakd.cli.default_socket_path`,
/// including its treatment of an empty `XDG_RUNTIME_DIR` as unset — Python's
/// `if runtime` is falsey for `""`, and a shell that resolved that to
/// `/speakd/speakd.sock` would look for the daemon somewhere it has never
/// been.
pub fn socket_path() -> PathBuf {
    let base = std::env::var("XDG_RUNTIME_DIR")
        .ok()
        .filter(|value| !value.is_empty())
        .or_else(|| std::env::var("TMPDIR").ok().filter(|value| !value.is_empty()))
        .unwrap_or_else(|| String::from("/tmp"));
    PathBuf::from(base).join("speakd").join("speakd.sock")
}

/// Start the event relay. Returns immediately; everything it does happens on
/// the thread it spawns.
pub fn spawn_relay<R: Runtime>(app: AppHandle<R>, state: Arc<LinkState>) {
    let spawned = std::thread::Builder::new()
        .name(String::from("speakd-relay"))
        .spawn(move || relay_loop(app, state));
    if let Err(err) = spawned {
        // Not fatal, and deliberately not a panic: a window with no daemon
        // link is still a window, and killing the process here would take
        // the tray and the hotkeys with it.
        eprintln!("speakd-shell: could not start the daemon relay: {err}");
    }
}

fn relay_loop<R: Runtime>(app: AppHandle<R>, state: Arc<LinkState>) {
    let path = socket_path();
    let mut backoff = RECONNECT_MIN;
    loop {
        match subscribe_and_read(&app, &state, &path) {
            // A clean end of stream: the daemon stopped, or dropped this
            // subscriber for falling behind. Either way it is worth trying
            // again promptly — a `systemctl --user restart speakd` should
            // not cost the window five seconds of silence.
            Ok(()) => {
                set_link(&app, &state, false, "speakd closed the event stream");
                backoff = RECONNECT_MIN;
            }
            Err(err) => {
                set_link(&app, &state, false, &err);
                backoff = (backoff * 2).min(RECONNECT_MAX);
            }
        }
        std::thread::sleep(backoff);
    }
}

/// Hold one subscription for as long as it lasts. `Ok(())` means the stream
/// ended cleanly; `Err` carries something the window can show a person.
fn subscribe_and_read<R: Runtime>(
    app: &AppHandle<R>,
    state: &LinkState,
    path: &Path,
) -> Result<(), String> {
    let stream = UnixStream::connect(path)
        .map_err(|err| format!("speakd is not listening on {}: {err}", path.display()))?;
    // Bounds the ack only. Cleared below, before the event loop: a socket
    // option is shared by every descriptor for the socket, so leaving it on
    // would turn five quiet seconds of no speech into a reconnect.
    stream
        .set_read_timeout(Some(REQUEST_TIMEOUT))
        .map_err(|err| format!("could not bound the subscribe ack: {err}"))?;

    let mut writing = stream
        .try_clone()
        .map_err(|err| format!("could not split the speakd connection: {err}"))?;
    writing
        .write_all(SUBSCRIBE_LINE)
        .map_err(|err| format!("could not subscribe to speakd: {err}"))?;

    let mut reader = BufReader::new(stream.try_clone().map_err(|err| {
        format!("could not split the speakd connection: {err}")
    })?);
    let mut ack = String::new();
    match reader.read_line(&mut ack) {
        Ok(0) => return Err(String::from("speakd closed the connection without an ack")),
        Ok(_) => {}
        Err(err) => return Err(format!("no answer to subscribe: {err}")),
    }
    let ack: Value = serde_json::from_str(ack.trim())
        .map_err(|err| format!("speakd answered subscribe with something unreadable: {err}"))?;
    if ack.get("ok") != Some(&Value::Bool(true)) {
        let reason = ack.get("error").and_then(Value::as_str).unwrap_or("no reason given");
        return Err(format!("speakd refused the subscription: {reason}"));
    }

    stream
        .set_read_timeout(None)
        .map_err(|err| format!("could not unbound the event stream: {err}"))?;
    set_link(app, state, true, "");

    for line in reader.lines() {
        let line = line.map_err(|err| format!("lost the speakd event stream: {err}"))?;
        if line.trim().is_empty() {
            continue;
        }
        match serde_json::from_str::<Value>(&line) {
            // Relayed whole, keys and all. The window wants `source_id`
            // alongside `event` and `data` — it is what names the channel in
            // the title bar — so nothing is unpacked here.
            Ok(event) if event.is_object() => {
                let _ = app.emit(EVENT_CHANNEL, event);
            }
            // A line the daemon has never sent and has no way to send.
            // Dropped rather than treated as a lost connection, the same
            // tolerance `speakd.transport` shows a malformed request.
            Ok(_) => eprintln!("speakd-shell: ignoring a non-object event line: {line}"),
            Err(err) => eprintln!("speakd-shell: ignoring an unparseable event line ({err}): {line}"),
        }
    }
    Ok(())
}

/// Record and announce the link state, but only when it has actually
/// changed: a window left open beside a stopped daemon would otherwise get
/// a "still not running" event every five seconds forever.
fn set_link<R: Runtime>(app: &AppHandle<R>, state: &LinkState, connected: bool, detail: &str) {
    let was = state.connected.swap(connected, Ordering::Relaxed);
    let detail_changed = match state.detail.lock() {
        Ok(mut held) => {
            let changed = *held != detail;
            if changed {
                *held = String::from(detail);
            }
            changed
        }
        Err(_) => true,
    };
    if was == connected && !detail_changed {
        return;
    }
    let _ = app.emit(LINK_CHANNEL, json!({ "connected": connected, "detail": detail }));
}

/// What the relay has managed so far. Called once by the window on startup,
/// to close the gap between the relay connecting and the frontend having a
/// listener registered. Reads two small locals, so it is safe as a plain
/// synchronous command (which Tauri runs on the main thread).
#[tauri::command]
pub fn speakd_link(state: tauri::State<'_, Arc<LinkState>>) -> Value {
    json!({
        "connected": state.connected.load(Ordering::Relaxed),
        "detail": state.detail(),
    })
}

/// Send one control verb and hand back the daemon's own `Response` object
/// (`{ok, data, error}`) for the frontend to read. `Err` is reserved for not
/// reaching the daemon at all — a refusal *by* the daemon is an `Ok` whose
/// `ok` is false, and the difference is exactly what the window's error line
/// should be able to show.
///
/// `source` exists for `mute` alone, which is the one forwarded verb the
/// daemon routes by `source_id`: an empty one means the global switch and a
/// named one means that channel (§3 of the streaming-narration design). The
/// window therefore has to be able to say which, and it says so here rather
/// than in the payload — this file does not read the daemon's payloads, and
/// a special case for one verb's field would be the first place it did.
/// Letting the frontend name a channel is not the thing `FORWARDED` guards:
/// every verb on that list is a control, `enqueue` is not on it, and muting
/// someone else's channel silences speech rather than originating it.
#[tauri::command]
pub async fn speakd_send(
    verb: String,
    payload: Option<Value>,
    source: Option<String>,
) -> Result<Value, String> {
    if !FORWARDED.contains(&verb.as_str()) {
        return Err(format!("the shell does not forward {verb:?} to speakd"));
    }
    let payload = payload.unwrap_or_else(|| json!({}));
    // `async` alone would only move this off the main thread and onto the
    // async runtime, where a blocking socket read would hold a worker; this
    // puts it somewhere blocking is allowed.
    tauri::async_runtime::spawn_blocking(move || match source {
        Some(source) => request_on(&verb, &source, payload),
        None => request(&verb, payload),
    })
    .await
    .map_err(|err| format!("the speakd request thread died: {err}"))?
}

/// Speak text a person typed into the window, and nothing else.
///
/// Deliberately not reachable through `speakd_send`: the verb, the channel and
/// the shape of the payload are all fixed here, so the only thing a caller
/// controls is the words. See `SAY_SOURCE` above for why the channel is a
/// literal.
#[tauri::command]
pub async fn speakd_say(text: String) -> Result<Value, String> {
    let trimmed = text.trim().to_string();
    if trimmed.is_empty() {
        return Err("nothing to say".into());
    }
    // No length cap: a long paste -- a whole article -- is what the box is
    // for, and the daemon segments and streams it like any other utterance.
    let payload = json!({ "text": trimmed, "kind": "response" });
    tauri::async_runtime::spawn_blocking(move || request_on("enqueue", SAY_SOURCE, payload))
        .await
        .map_err(|err| format!("the speakd request thread died: {err}"))?
}

/// One control request on the window's own control id.
fn request(verb: &str, payload: Value) -> Result<Value, String> {
    request_on(verb, CONTROL_SOURCE_ID, payload)
}

/// One control request, naming the channel it is about. The socket work lives
/// here and only here — `speakd_say` differs from `speakd_send` in what it is
/// allowed to send, not in how it sends it.
fn request_on(verb: &str, source: &str, payload: Value) -> Result<Value, String> {
    let path = socket_path();
    let stream = UnixStream::connect(&path)
        .map_err(|err| format!("speakd is not listening on {}: {err}", path.display()))?;
    stream
        .set_read_timeout(Some(REQUEST_TIMEOUT))
        .map_err(|err| format!("could not bound the {verb} reply: {err}"))?;

    let mut line = serde_json::to_vec(&json!({
        "verb": verb,
        "source_id": source,
        "payload": payload,
    }))
    .map_err(|err| format!("could not encode {verb}: {err}"))?;
    line.push(b'\n');
    (&stream)
        .write_all(&line)
        .map_err(|err| format!("could not send {verb} to speakd: {err}"))?;

    let mut reply = String::new();
    BufReader::new(&stream)
        .read_line(&mut reply)
        .map_err(|err| format!("no answer to {verb}: {err}"))?;
    if reply.trim().is_empty() {
        return Err(format!("speakd closed the connection without answering {verb}"));
    }
    serde_json::from_str(reply.trim())
        .map_err(|err| format!("speakd answered {verb} with something unreadable: {err}"))
}
