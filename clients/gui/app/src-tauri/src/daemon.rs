//! The daemon as this shell's own child, when the shell is started as the
//! installed app (packaging/desktop/speakd-gui sets `SPEAKD_DAEMON`).
//!
//! The two live and die together, with no service manager between them:
//!
//! - The shell ending, however it ends — Quit, a panic, SIGKILL — ends the
//!   daemon: the kernel sends it SIGTERM when its parent goes
//!   (`PR_SET_PDEATHSIG`), which needs nothing of this process to still be
//!   running.
//! - The daemon ending, however it ends, ends the shell: a thread waits on it
//!   and exits the app with the daemon's failure, rather than leaving a window
//!   that monitors nothing.
//!
//! Without `SPEAKD_DAEMON` (`cargo run`, a build started by hand) none of this
//! happens and the shell reads whatever daemon is already running, as before.

use std::os::unix::process::{CommandExt, ExitStatusExt};
use std::process::Command;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use tauri::AppHandle;

/// Path of the `speakd` executable to run as the child.
pub const DAEMON_ENV: &str = "SPEAKD_DAEMON";

/// How long a quit waits for the daemon's own graceful stop before SIGKILL.
const STOP_GRACE: Duration = Duration::from_secs(5);

pub struct Daemon {
    pid: u32,
    /// Set before the shell stops the daemon itself, so the watcher reads the
    /// exit that follows as the quit it is and not as a crash.
    stopping: Arc<AtomicBool>,
    exited: Arc<Mutex<bool>>,
}

/// Starts the daemon if this shell is meant to own one. `Ok(None)`: it is not.
///
/// Must be called on the main thread: the parent-death signal fires when the
/// *thread* that spawned the child exits, and only the main thread lives as
/// long as the process.
pub fn spawn(app: &AppHandle) -> std::io::Result<Option<Daemon>> {
    let Some(program) = std::env::var_os(DAEMON_ENV) else { return Ok(None) };
    let parent = std::process::id() as libc::pid_t;

    let mut command = Command::new(&program);
    // The daemon never needs to know who launched it.
    command.env_remove(DAEMON_ENV);
    unsafe {
        command.pre_exec(move || {
            if libc::prctl(libc::PR_SET_PDEATHSIG, libc::SIGTERM) != 0 {
                return Err(std::io::Error::last_os_error());
            }
            // The parent may have died between fork and prctl, in which case
            // no signal is coming: notice, and go.
            if libc::getppid() != parent {
                libc::_exit(1);
            }
            // Speech is a background comfort; it must never win a scheduling
            // contest against the editor the user is typing into.
            libc::nice(5);
            Ok(())
        });
    }
    let mut child = command.spawn()?;

    let stopping = Arc::new(AtomicBool::new(false));
    let exited = Arc::new(Mutex::new(false));
    let daemon = Daemon { pid: child.id(), stopping: Arc::clone(&stopping), exited: Arc::clone(&exited) };

    let app = app.clone();
    std::thread::Builder::new().name("speakd-daemon".into()).spawn(move || {
        let status = child.wait();
        *exited.lock().unwrap() = true;
        if stopping.load(Ordering::SeqCst) {
            return;
        }
        let code = match &status {
            Ok(s) => {
                eprintln!("speakd-shell: the daemon exited ({s}); closing with it");
                s.code().unwrap_or_else(|| 128 + s.signal().unwrap_or(0))
            }
            Err(e) => {
                eprintln!("speakd-shell: lost the daemon: {e}; closing");
                1
            }
        };
        // A daemon that ended cleanly on its own still leaves nothing to
        // show, so the shell goes either way; only the code differs.
        app.exit(code);
    })?;

    Ok(Some(daemon))
}

impl Daemon {
    /// The shell is quitting: ask the daemon to stop, give it its grace
    /// period, then make sure. Called from the app's Exit event.
    pub fn stop(&self) {
        self.stopping.store(true, Ordering::SeqCst);
        // Already reaped: its pid may belong to someone else by now.
        if *self.exited.lock().unwrap() {
            return;
        }
        let pid = self.pid as libc::pid_t;
        unsafe { libc::kill(pid, libc::SIGTERM) };
        let deadline = Instant::now() + STOP_GRACE;
        while Instant::now() < deadline {
            if *self.exited.lock().unwrap() {
                return;
            }
            std::thread::sleep(Duration::from_millis(50));
        }
        eprintln!("speakd-shell: the daemon did not stop in {STOP_GRACE:?}; killing it");
        unsafe { libc::kill(pid, libc::SIGKILL) };
    }
}
