# Disabling the notifications reader, by hand and for good

Date: 2026-09-25. Status: reviewed against the code; findings folded in. Ready for the plan.

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

- `notify_enabled: bool = True`, persisted beside the others. The daemon holds
  the same flag in memory, read at startup and written by the verb, exactly
  like `muted`.

Every failure to read the file resolves to `True`, as every other flag
resolves to its default — a truncated JSON file must not silently take the
reader away.

`SPEAKD_NO_NOTIFY` keeps its meaning: a startup hard-off for tests and
debugging. It suppresses the connector entirely — it is not even registered
with the daemon — so `notify on` while it is set is refused with a reason that
names it.

## 2. The daemon owns the connectors

- `Daemon` gains `add_connector(name: str, supervisor: Supervisor)`, holding
  them by name, and `Daemon.stop_connectors()`, which stops every connector it
  holds. `Daemon.stop()` also calls `stop_connectors()` first, so a daemon
  stopped without going through `serve()` (tests, future embeddings) still
  takes its readers down; the double stop is idempotent.
- `__main__.serve()` builds each connector's `Supervisor` and registers it
  with the daemon instead of keeping the list. Its shutdown loop becomes a
  `_shutdown(daemon, server, http)` helper — factored out of `serve()` so the
  ordering is testable — that runs `daemon.stop_connectors()`,
  `daemon.silence()`, `server.stop()`, `http.stop()`, in that order. The
  existing ordering intent is preserved: connectors go down *before* the
  daemon goes quiet, so a reader cannot enqueue into a shutting-down daemon
  and have it discarded as unspoken, and `server.stop()` stays after
  `silence()` because its `shutdown()` is what frees a worker wedged writing
  to a stopped subscriber.
  For the notifications connector it consults `daemon.notify_enabled`
  (and, as today, `SPEAKD_NO_NOTIFY`) before starting it. A connector that is
  registered but not started — the reader under `notify_enabled = False` —
  starts through the verb, not on its own.
- The `CONNECTORS` table stays the one-line-per-connector data it is, each
  row gaining its name: `(name, variable, module)`, with the names `follower`
  and `notify`.

## 3. The verb

`set_notify {enabled: bool}`, global (no source id), like `mute`:

- `enabled = False`:
  1. persist `notify_enabled = False` (in memory and on disk) — first, as
     mute does, so a silence that then fails cannot abort the verb after the
     switch has in fact flipped,
  2. stop the notifications supervisor (the child is terminated),
  3. hush every `notify:` channel — text the reader already enqueued must not
     keep talking after it is dead. The mechanism is the channels' own
     convention: the notifications rules name one channel per app,
     `notify:<app>` (see `speakd.clients.notifications.rules`), so the daemon
     hushes each channel whose `source_id` starts with `notify:`, scoped so
     no other session's queue is drained. A sink that refuses to stop is
     reported as an `error` event and the verb still answers `ok`, exactly
     like `_silence_for_mute`; the discarded utterances name the notify
     switch as their reason (`_DISCARDED_NOTIFY`, beside the mute and hush
     texts),
  4. publish `notify {enabled: false}`.
- `enabled = True`: persist `True`, start the supervisor (a no-op if it is
  running), publish `notify {enabled: true}` — persisted first, as on the
  off path, so the two directions keep one order.
- Both are idempotent and answer `ok` either way. `off` works even when no
  notifications connector is registered (it persists and answers `ok`;
  nothing is hushed, because no reader ever ran).
- `enabled = True` with no notifications connector registered (it was
  suppressed by `SPEAKD_NO_NOTIFY`) is refused, naming that variable, before
  anything is persisted.

`status` gains `notify: {enabled: bool}` — the flag, not whether the
child process happens to be alive; `SPEAKD_NO_NOTIFY` overriding it at startup
is the documented exception.

The `set_notify` verb is not added to the HTTP transport's `VERBS` allowlist:
it is a local control verb, and the allowlist stays as it is.

## 4. CLI

- `speakctl notify off` and `speakctl notify on` send the verb. They take
  `--socket` only: the switch is global, so there is deliberately no
  `--source` to mislead with (unlike the `wide` verbs).
- With no daemon, the usual "no daemon" line, exactly like `speakctl mute`.
- `recent`, `tap`, `test` and `init` are untouched: `recent` reads history the
  reader wrote, and `tap` watches the bus itself — neither needs the reader to
  be running.

## Testing

- **State:** `notify_enabled` round-trips, defaults to `True`, and a corrupt
  file falls back to `True`.
- **Verb:** with a fake supervisor registered — off stops it and hushes the
  `notify:` channels and persists; on starts it; both are idempotent; on with
  no connector registered is refused; off with no connector registered still
  persists and answers `ok`; a player that refuses to stop does not fail the
  verb; the `notify` event and the `status` field.
- **Startup:** `_start_children` registers the notifications connector but
  does not start it when the flag is off, and skips it entirely under
  `SPEAKD_NO_NOTIFY` (the existing env-var behaviour, extended).
- **Shutdown:** `_shutdown` stops the connectors before it silences the
  daemon, and before `server.stop()` — the ordering that today's `serve()`
  comment insists on, pinned by a test.
- **CLI:** `speakctl notify off`/`on` send the right verb and payload;
  `set_notify` is not served over HTTP (it stays out of the allowlist).
