// Prevents an additional console window on Windows in release builds. Has
// no effect on Linux, where this shell is currently developed and built.
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

mod bridge;
mod hotkeys;

use std::sync::Arc;

use tauri::{
    menu::{Menu, MenuBuilder, MenuItemKind},
    tray::TrayIconBuilder,
    AppHandle, Emitter, Manager, WindowEvent,
};

const TRAY_ID: &str = "main";
const WINDOW_LABEL: &str = "main";

const STATE_ITEM_ID: &str = "state";
const PLAY_PAUSE_ITEM_ID: &str = "play_pause";
const HUSH_ITEM_ID: &str = "hush";
const SHOW_HIDE_ITEM_ID: &str = "show_hide";
const QUIT_ITEM_ID: &str = "quit";

/// Holds the tray's own menu so `report_state` can find and relabel its
/// "State: ..." line later. This — and the tooltip text `report_state`
/// sets — is the *only* state the shell keeps about speech, and it is
/// purely a display cache: the frontend's Source abstraction
/// (clients/gui/shared/speakd-source.js) is what actually knows whether
/// something is playing, paused or hushed, by talking to the daemon. The
/// shell never computes this itself, and never decides anything from it —
/// queue, timeline and channels all stay in the daemon, per the task this
/// window exists for.
struct TrayMenuState(Menu<tauri::Wry>);

/// Drives the pin button in pinned.html's title bar. A plain app command
/// rather than the frontend calling a window API directly, so the real
/// window handle stays entirely on the Rust side and the frontend never
/// needs to know it is running inside Tauri at all beyond feature-detecting
/// `window.__TAURI__`.
#[tauri::command]
fn set_always_on_top(window: tauri::Window, value: bool) -> Result<(), String> {
    window.set_always_on_top(value).map_err(|e| e.to_string())
}

/// Minimise, grow and dismiss, for a window that has no decorations to do it
/// with. Commands of this app's own rather than the window plugin's JS API,
/// for the same reason `set_always_on_top` is one: the frontend then needs to
/// know nothing about running inside Tauri beyond feature-detecting
/// `window.__TAURI__`, and an app command is not ACL-gated, so the capability
/// file stays as small as its own description claims it is.
#[tauri::command]
fn minimize_window(window: tauri::Window) -> Result<(), String> {
    window.minimize().map_err(|e| e.to_string())
}

/// Toggles, and answers with the state it left the window in, so the button
/// reflects what happened rather than what it asked for. `maxWidth` and
/// `maxHeight` in tauri.conf.json still apply, so "maximised" here means 800px
/// wide, not the whole screen.
#[tauri::command]
fn toggle_maximize_window(window: tauri::Window) -> Result<bool, String> {
    let maximized = window.is_maximized().map_err(|e| e.to_string())?;
    if maximized {
        window.unmaximize().map_err(|e| e.to_string())?;
    } else {
        window.maximize().map_err(|e| e.to_string())?;
    }
    Ok(!maximized)
}

/// Hides. Deliberately not `close`, and named for what it does: the
/// close-request handler below already decided that this window hides and only
/// the tray's Quit ends the process. A button doing something else under the
/// same glyph would leave two meanings of "close" in one shell, and the one
/// behind the ✕ would be the destructive one.
#[tauri::command]
fn hide_window(window: tauri::Window) -> Result<(), String> {
    window.hide().map_err(|e| e.to_string())
}

/// The frontend's one report to the shell: what it currently is doing
/// (`"playing"` | `"paused"` | `"hushed"`), sent after every state change
/// (see `reportState()` in pinned.html). Exists only because the tray has
/// to live in Rust and needs a string to show; see `TrayMenuState` above.
#[tauri::command]
fn report_state(app: AppHandle, state: String, tray_menu: tauri::State<TrayMenuState>) -> Result<(), String> {
    let label = match state.as_str() {
        "playing" => "State: Playing",
        "hushed" => "State: Hushed",
        _ => "State: Paused",
    };
    if let Some(MenuItemKind::MenuItem(item)) = tray_menu.0.get(STATE_ITEM_ID) {
        let _ = item.set_text(label);
    }
    if let Some(tray) = app.tray_by_id(TRAY_ID) {
        let _ = tray.set_tooltip(Some(format!("speakd — {state}")));
    }
    Ok(())
}

/// Shared by the tray's Show/Hide item and a left click on the tray icon.
fn toggle_main_window(app: &AppHandle) {
    let Some(window) = app.get_webview_window(WINDOW_LABEL) else { return };
    let visible = window.is_visible().unwrap_or(true);
    if visible {
        let _ = window.hide();
    } else {
        let _ = window.show();
        let _ = window.set_focus();
    }
}

fn main() {
    tauri::Builder::default()
        // Remembers window position and size between launches, within the
        // range tauri.conf.json allows: 320-800 wide, 300-2000 tall. The
        // 384 x 560 default is the size the design was drawn at, not a
        // constraint — this window is dragged to fit whatever it is pinned
        // beside, and only the speech text takes the slack.
        //
        // `maxHeight` in that config is load-bearing and looks arbitrary,
        // so: tao leaves an unset maximum as `i32::MAX` in the X11 size
        // hint it sends, and this window manager answers a hint that large
        // by sizing the window to its *minimum* instead. Measured, with
        // nothing else changed: no `maxHeight` and `minHeight` 340 opened a
        // 340-tall window, `minHeight` 300 opened a 300-tall one, and
        // dropping `minHeight` too opened one a single pixel tall — the
        // configured height never applied at all, and the metrics row and
        // channel pills sat below the window's own edge with the page
        // scrolling inside it to reach them. A finite maximum fixes it.
        .plugin(tauri_plugin_window_state::Builder::new().build())
        .plugin(hotkeys::plugin())
        .invoke_handler(tauri::generate_handler![
            set_always_on_top,
            minimize_window,
            toggle_maximize_window,
            hide_window,
            report_state,
            bridge::speakd_link,
            bridge::speakd_send,
            bridge::speakd_say
        ])
        .setup(|app| {
            hotkeys::register(app.handle())?;

            // Started before the tray and the window handlers below, so
            // that a window which loads fast still finds a link state to
            // ask for (see `speakd_link`). Nothing here blocks: the relay
            // connects on its own thread and reports what it found.
            let link = Arc::new(bridge::LinkState::new());
            app.manage(Arc::clone(&link));
            bridge::spawn_relay(app.handle().clone(), link);

            let menu = MenuBuilder::new(app)
                .text(STATE_ITEM_ID, "State: Paused")
                .separator()
                .text(PLAY_PAUSE_ITEM_ID, "Play / Pause")
                .text(HUSH_ITEM_ID, "Hush")
                .separator()
                .text(SHOW_HIDE_ITEM_ID, "Show / Hide")
                .separator()
                // Not `.quit()`: tauri's predefined Quit item is documented
                // as unsupported on Linux, which is this shell's primary
                // target — a plain item handled below works everywhere.
                .text(QUIT_ITEM_ID, "Quit")
                .build()?;

            TrayIconBuilder::with_id(TRAY_ID)
                .icon(tauri::include_image!("icons/icon.png"))
                .tooltip("speakd — paused")
                .menu(&menu)
                .show_menu_on_left_click(false)
                .on_menu_event(|app, event| match event.id().as_ref() {
                    PLAY_PAUSE_ITEM_ID => {
                        let _ = app.emit("speakd://tray", "play_pause");
                    }
                    HUSH_ITEM_ID => {
                        let _ = app.emit("speakd://tray", "hush");
                    }
                    SHOW_HIDE_ITEM_ID => toggle_main_window(app),
                    QUIT_ITEM_ID => app.exit(0),
                    _ => {}
                })
                .on_tray_icon_event(|tray, event| {
                    if let tauri::tray::TrayIconEvent::Click {
                        button: tauri::tray::MouseButton::Left,
                        button_state: tauri::tray::MouseButtonState::Up,
                        ..
                    } = event
                    {
                        toggle_main_window(tray.app_handle());
                    }
                })
                .build(app)?;

            app.manage(TrayMenuState(menu));

            // The window has no decorations (see tauri.conf.json), so there
            // is no native close button to begin with — but a window
            // manager's own close affordance (Alt+F4, a taskbar entry) can
            // still send this. Hide rather than quit: the tray's Quit item,
            // and only that, ends the process, matching the "system tray
            // with show/hide" requirement rather than making the window
            // unrecoverable once it is dismissed.
            if let Some(window) = app.get_webview_window(WINDOW_LABEL) {
                let window_to_hide = window.clone();
                window.on_window_event(move |event| {
                    if let WindowEvent::CloseRequested { api, .. } = event {
                        api.prevent_close();
                        let _ = window_to_hide.hide();
                    }
                });
            }

            Ok(())
        })
        .run(tauri::generate_context!())
        .expect("error while running speakd-shell");
}
