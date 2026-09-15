# Desktop notifications as a speakd source

**Goal.** Read chosen desktop notifications aloud — email and WhatsApp-in-Chrome
to begin with — through the same channels, profiles and mute controls that
already carry Claude Code.

## 1. Where the events come from

`busctl --user monitor --json=short org.freedesktop.Notifications`, as a
supervised child, exactly like the Claude Code follower.

Three alternatives were measured on this machine and rejected:

| Source | Why not |
|---|---|
| `dbus-monitor` | Prints string arguments raw. A body containing a newline or a `"` cannot be parsed back unambiguously — measured: a two-line WhatsApp message produces output no line-based parser can delimit. |
| `gdbus monitor --dest` | Watches signals *from* a name, not method calls *to* it. Produced zero output for `Notify`. |
| A Python D-Bus binding (`jeepney`, `dbus-next`, `gi`) | A new dependency. Installing one means `uv sync`, which prunes the hand-installed CPU-only torch and kokoro this venv is built on. |

`busctl` ships with systemd, emits one JSON object per message with correct
escaping, and flushes per message — measured at 40 ms from `notify-send` to
parsed record, with no `stdbuf` needed.

Not portable beyond systemd Linux. macOS and Windows would each need their own
monitor behind the same `Notification` record; nothing above that seam changes.

## 2. Every notification crosses the bus twice

Measured. One `notify-send` produces two `Notify` method calls:

```
sender=:1.1127 -> destination=:1.32     the app, to the notification daemon
sender=:1.32   -> destination=:1.19     that daemon, relaying to gnome-shell
```

`:1.32` owns `org.freedesktop.Notifications` (here a gjs shell extension, not
gnome-shell itself). Read naively, every notification is spoken twice.

**The rule:** a notification is a `Notify` addressed *to* the owner of
`org.freedesktop.Notifications`.

- `destination == owner` → accept. This is the call that asks for a
  notification to be shown, whatever sent it — including
  `xdg-desktop-portal` relaying a sandboxed app, which is how a Flatpak
  browser would appear.
- `sender == owner` → drop. The daemon's own relay outward.
- Neither → the owner may have changed (shell restart, extension reload).
  Re-resolve it, at most once every 5 s, and re-test.

A `(app, summary, body)` dedup over a 2 s window sits behind that rule. It is
not redundant: it covers a desktop that relays through three hops, and the
failure it prevents — hearing everything twice — is exactly the kind that makes
someone turn the feature off rather than report it.

## 3. Filtering

`~/.config/speakd/notifications.toml`. First match wins; anything that matches
no rule is not spoken.

```toml
max_per_minute = 20        # a safety valve on a chatty group chat

[[rule]]
name = "whatsapp"
app = "*chrome*"           # case-insensitive glob
speak = true
say = "{summary} says. {body}"

[[rule]]
name = "mail"
app = "thunderbird"
say = "Email. {summary}. {body}"
```

Match fields: `app`, `summary`, `body` (globs), `urgency`
(`low`/`normal`/`critical`). Absent means "anything". `say` is a template over
`{app} {summary} {body}`; an unknown placeholder is a load error naming the
rule, following `profiles.py`, because a rule that silently says nothing is an
afternoon spent wondering why.

With no config file, a built-in ruleset speaks Chrome, Thunderbird and
Evolution — what was actually asked for — so the feature does something on the
day it is installed. `speakctl notify init` writes that ruleset out to edit.

**Chrome's app name for a WhatsApp notification is not known from here.** It
cannot be: it takes a real WhatsApp message to observe. This is why §4 exists,
and why the starter rule matches `*chrome*` rather than a guess at a per-site
name.

## 4. A record of what arrived

The last 100 notifications, with the decision taken and the rule that took it,
as JSONL in the state directory. `speakctl notify recent` prints them;
`speakctl notify tap` streams them live.

Without this, "it didn't read my WhatsApp" is unanswerable — the notification
that did or did not arrive is gone. Bodies are stored as they were spoken;
they are already being read aloud in the room, so this adds no exposure a
`--clear` cannot undo.

## 5. Channels

One channel per app: `notify:<slug>`, labelled with the app name, so the
existing per-channel mute and the GUI's skip button work on notifications
unchanged — mute WhatsApp, keep Claude Code.

Opened `FOREGROUND` at priority 10. Priority orders the queue without cutting
off what is speaking, so a message from a person is said before the rest of an
AI monologue, and never over the top of it.

## 6. Module contract

`src/speakd/clients/notifications/`

**`monitor.py`** — the bus, and nothing else.

```python
@dataclass(frozen=True)
class Notification:
    app: str
    summary: str
    body: str
    urgency: int = 1  # 0 low, 1 normal, 2 critical
    desktop_entry: str = ""  # the "desktop-entry" hint, "" if absent
    when: float = 0.0


class Monitor:
    def __init__(
        self,
        *,
        owner: Callable[[], str | None],
        dedup_seconds: float = 2.0,
        reresolve_seconds: float = 5.0,
        now: Callable[[], float] = time.time,
    ) -> None: ...
    def feed(self, line: str) -> Notification | None:
        """One line of busctl JSON in; a notification worth considering out."""


def stream(
    monitor: Monitor, stop: threading.Event, argv: Sequence[str] | None = None
) -> Generator[Notification, None, None]: ...
def busctl_owner() -> str | None: ...
```

All the judgement lives in `feed`, which is pure given its clock — so the
double-delivery rule, the owner change and the dedup are tested with strings,
not processes.

`stream` is a `Generator`, not an `Iterator`, and the difference is
load-bearing twice over. Its `finally` is what reaps `busctl`, and that runs
only when the generator is exhausted, closed or collected — so `follow.py`
iterates it inside `contextlib.closing`, and an exception leaving the loop
cannot strand a `busctl` reading the bus for nobody. It must also return
promptly when `stop` is set while it is blocked on a read. That was found by
running it against the real bus, where a `stop` over a quiet bus hung until the
next notification happened to arrive. Left unfixed it is worse than a hang:
`follow.py` installs a SIGTERM handler that only sets `stop`, so SIGTERM stops
killing the process, the supervisor's terminate times out after five seconds
and gives up, and the orphan goes on reading the user's mail aloud into a
socket that is no longer there.

**`rules.py`** — the config, and nothing else.

```python
@dataclass(frozen=True)
class Rule:
    name: str
    app: str = "*"
    summary: str = "*"
    body: str = "*"
    urgency: str = ""
    speak: bool = True
    say: str = "{summary}. {body}"
    profile: str = "notification"
    max_chars: int = 220


@dataclass(frozen=True)
class Ruleset:
    rules: tuple[Rule, ...] = ()
    max_per_minute: int = 20


@dataclass(frozen=True)
class Verdict:
    speak: bool
    text: str = ""
    channel: str = ""
    label: str = ""
    profile: str = ""
    rule: str = ""
    reason: str = ""


DEFAULT_RULES: Ruleset


def load_rules(path: Path) -> Ruleset: ...  # ValueError names the rule
def decide(note: Notification, rules: Ruleset) -> Verdict: ...
def render(template: str, note: Notification, max_chars: int) -> str: ...


class RateLimit:
    def __init__(self, per_minute: int, *, now: Callable[[], float] = time.time) -> None: ...
    def allow(self) -> bool: ...
```

**`follow.py`** — the loop: monitor → rules → rate limit → history → `enqueue`.
The supervised child, `speakd-notify-follow`, suppressed by
`SPEAKD_NO_NOTIFY=1`.

**`history.py`** — the ring from §4.

## 7. Shared, because there are now two clients

`clients/claude_code/send.py` moves to `clients/send.py`, and the hook log
helper to `clients/log.py`. Both were already generic; the second client is
what makes reaching across into the first one's package obviously wrong.
