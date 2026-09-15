//! Global hotkeys — the single most valuable thing this shell provides:
//! pausing or hushing narration without switching away from an editor to
//! find the window. Every binding lives in this one file, in `bindings()`
//! below, so they are easy to find and to change; nothing else in the app
//! touches `tauri_plugin_global_shortcut` directly.
//!
//! Both use Ctrl+Alt. These are *global* shortcuts — active even while some
//! other application has focus — so a plain letter, an arrow key, or a
//! chord an editor already owns (Ctrl+S, ...) would be the wrong choice.
//! Ctrl+Alt+<letter> is not a routine editor or window-manager binding on
//! Linux, Windows or macOS, so it stays out of the way of ordinary typing.
//!
//! A binding does nothing to speech on its own: it only forwards a
//! `speakd://hotkey` Tauri event carrying the action name. The window's own
//! script (clients/gui/pinned.html) is what turns that into a real
//! `source.send(verb, ...)` call through the event-source abstraction. This
//! file never talks to the daemon and never decides what is playing — see
//! the module doc comment on `TrayMenuState` in `main.rs` for why that
//! matters.

use tauri::{AppHandle, Emitter, Runtime};
use tauri_plugin_global_shortcut::{Code, GlobalShortcutExt, Modifiers, Shortcut, ShortcutState};

/// The shell's complete set of global bindings: (shortcut, action name).
/// The action name is exactly what arrives in `pinned.html`'s
/// `speakd://hotkey` listener, and matches the tray menu's action names too
/// (see `main.rs`) so both paths drive the same `handleShellAction()`.
fn bindings() -> [(Shortcut, &'static str); 2] {
    let ctrl_alt = Modifiers::CONTROL | Modifiers::ALT;
    [
        (Shortcut::new(Some(ctrl_alt), Code::KeyP), "play_pause"),
        (Shortcut::new(Some(ctrl_alt), Code::KeyH), "hush"),
    ]
}

/// The plugin instance to hand to `tauri::Builder::plugin`. Shortcuts are
/// registered separately, in `register()`, once the app is set up — see the
/// call in `main.rs`'s `.setup()`.
pub fn plugin<R: Runtime>() -> tauri::plugin::TauriPlugin<R> {
    tauri_plugin_global_shortcut::Builder::new().build()
}

/// Registers every binding in `bindings()` and wires each to emit
/// `speakd://hotkey`. Call once, from `setup()`.
pub fn register<R: Runtime>(app: &AppHandle<R>) -> Result<(), tauri_plugin_global_shortcut::Error> {
    for (shortcut, action) in bindings() {
        app.global_shortcut()
            .on_shortcut(shortcut, move |app, _shortcut, event| {
                // Global shortcuts report both press and release; a
                // narration control should fire once per key-down, not
                // twice.
                if event.state() == ShortcutState::Pressed {
                    let _ = app.emit("speakd://hotkey", action);
                }
            })?;
    }
    Ok(())
}
