# Disabling the notifications reader, by hand and for good

Date: 2026-09-25. Status: designed in conversation, awaiting review of this spec.

## Intent

Written back from the conversation, and agreed:

- **Only the notifications reader is in scope.** The other clients stay as they
  are; the general "disable anything" question is deliberately not answered
  here.
- **`speakctl notify off` stops the reader at once and for good** — the choice
  persists across daemon restarts, like mute and disable do — and
  **`speakctl notify on`** brings it back. A runtime-only toggle was
  considered and rejected: an off switch that forgets itself on restart is a
  trap on a machine where the daemon restarts daily.
- **The daemon owns its connectors.** Today the supervised children live in
  `__main__.serve()`, which starts them after the daemon is up and stops them
  on the way down. For the daemon to stop and start the reader on demand, it
  must hold the supervisors itself.

Success: `speakctl notify off` silences the notifications channel, kills the
reader process, and a daemon restart comes back with the reader still off;
`speakctl notify on` brings it back, and `speakctl status` says which state it
is in.

Out of scope: disabling any other client, the window showing a switch, and
automatic scheduling.

## 1. State

`DaemonState` (the leaf module behind `state.json`, which already holds
`muted`, `disabled` and `speed`) gains:

- `notify_enabled: bool = True`, persisted beside the others.

Every failure to read the file resolves to `True`, as every other flag
resolves to its default — a truncated JSON file must not silently take the
reader away.

`SPEAKD_NO_NOTIFY` keeps its meaning: a startup hard-off for tests and
debugging. It suppresses the connector entirely — it is not even registered
with the daemon — so `notify on` while it is set is refused with a reason that
names it.

## 2. The daemon owns the connectors

- `Daemon` gains `add_connector(name: str, supervisor: Supervisor)`, holding
  them by name, and `Daemon.stop()` stops every connector it holds.
- `__main__.serve()` builds each connector's `Supervisor` and registers it
  with the daemon instead of keeping the list; its shutdown loop goes away.
  For the notifications connector it consults `state.load().notify_enabled`
  (and, as today, `SPEAKD_NO_NOTIFY`) before starting it. A connector that is
  registered but not started — the reader under `notify_enabled = False` —
  starts through the verb, not on its own.
- The `CONNECTORS` table stays the one-line-per-connector data it is.

## 3. The verb

`set_notify {enabled: bool}`, global (no source id), like `mute`:

- `enabled = False`:
  1. stop the notifications supervisor (the child is terminated),
  2. hush every `notify:` channel — text the reader already enqueued must not
     keep talking after it is dead,
  3. persist `notify_enabled = False`,
  4. publish `notify {enabled: false}`.
- `enabled = True`: start the supervisor (a no-op if it is running), persist
  `True`, publish `notify {enabled: true}`.
- Both are idempotent and answer `ok` either way.
- `enabled = True` with no notifications connector registered (it was
  suppressed by `SPEAKD_NO_NOTIFY`) is refused, naming that variable.

`status` gains `notify: {enabled: bool}` — the persisted flag, not whether the
child process happens to be alive; `SPEAKD_NO_NOTIFY` overriding it at startup
is the documented exception.

## 4. CLI

- `speakctl notify off` and `speakctl notify on` send the verb.
- With no daemon, the usual "no daemon" line, exactly like `speakctl mute`.
- `recent`, `tap`, `test` and `init` are untouched: `recent` reads history the
  reader wrote, and `tap` watches the bus itself — neither needs the reader to
  be running.

## Testing

- **State:** `notify_enabled` round-trips, defaults to `True`, and a corrupt
  file falls back to `True`.
- **Verb:** with a fake supervisor registered — off stops it and hushes the
  `notify:` channels and persists; on starts it; both are idempotent; on with
  no connector registered is refused; the `notify` event and the `status`
  field.
- **Startup:** `_start_children` registers the notifications connector but
  does not start it when the flag is off, and skips it entirely under
  `SPEAKD_NO_NOTIFY` (the existing env-var behaviour, extended).
- **CLI:** `speakctl notify off`/`on` send the right verb and payload.
