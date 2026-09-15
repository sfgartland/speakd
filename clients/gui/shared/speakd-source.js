/**
 * speakd-source.js — the event-source abstraction.
 *
 * The frontend must not know whether its events come from the daemon or a
 * simulation. Both `SimulatedSource` and `ShellSource` below implement the
 * same small interface:
 *
 *   subscribe(handler) -> unsubscribe()
 *     `handler` is called with `{ kind, data, source_id? }` events. `kind`
 *     names one of the event kinds `speakd.daemon.Daemon._publish` emits:
 *
 *       "started"   data: { text }
 *                   One utterance is about to be spoken, and `text` is the
 *                   whole of it. There is NO `segments` list here: the
 *                   daemon segments one unit ahead of playback
 *                   (src/speakd/pipeline.py's depth-one queue), so it cannot
 *                   know segment four's text while segment one is playing.
 *                   The UI discovers segments one at a time from `position`.
 *                   `SimulatedSource` alone adds a `segments` array, as a
 *                   convenience it can afford because it owns its fixture.
 *
 *       "position"  data: { text, span_start, span_end, audio_offset,
 *                            played_at, index, duration?, elapsed? }
 *                   One segment has *started*. The first five fields are
 *                   the daemon's, verbatim. `index` is added by whichever
 *                   source produced the event — the wire has no segment
 *                   number, so `ShellSource` counts positions since the last
 *                   `started` — so that the controller never has to infer
 *                   segment identity from spans. `duration` and `elapsed`
 *                   are `SimulatedSource`-only: a real `position` says that
 *                   a segment began, not how long it runs or how far in
 *                   playback already is.
 *
 *       "metrics"   data: { rtf?, drift?, mem_bytes?, queue? }
 *                   The four numbers in the glance row, contracted in
 *                   docs/design/2026-09-14-metrics-event.md: synthesis speed
 *                   against realtime, how far the measured clock has fallen
 *                   behind the nominal one, the daemon's resident set, and
 *                   utterances accepted but not yet finished. Fires once a
 *                   second while speaking and once more on going idle.
 *                   The daemon does not emit it yet. Every field is optional
 *                   and the controller shows a dash for whatever it has
 *                   never received — a monitor that invents a plausible
 *                   number is worse than one that admits it does not know.
 *
 *       "transport" data: { paused }
 *                   Playback was suspended or taken up again. On the bus so
 *                   that a GUI can *read* the paused state rather than infer
 *                   it from an absence of `position` events, which is also
 *                   what a finished utterance sounds like.
 *
 *       "discarded" data: { count, reason, sources }
 *                   A hush cleared the queue. One event for the whole drain,
 *                   carrying how many utterances went and whose they were.
 *
 *       "declined"  data: { text, kind, reason }
 *       "error"     data: { message }
 *       "finished"  data: { cancelled, aborted }
 *
 *       "link"      data: { connected, detail }
 *                   Not a daemon event: `ShellSource` alone emits it, for
 *                   whether the shell currently holds a subscription at all.
 *                   A monitor whose daemon is not running has to be able to
 *                   say so, since "nothing is speaking" and "nothing is
 *                   listening" look identical from inside the window.
 *
 *   send(verb, payload = {}) -> Promise<{ ok: true, data } | { ok: false, error }>
 *     `verb` is one of the control verbs in src/speakd/protocol.py. The
 *     shell forwards only the five this window's buttons need — pause,
 *     resume, hush, cancel, seek — and refuses the rest, `enqueue` above
 *     all: this window monitors speech and never originates it. `seek` is
 *     forwarded but the daemon answers it with "not implemented" until a
 *     seekable player lands (docs/plans/2026-09-13-streaming-player.md);
 *     `SimulatedSource` honours it locally, owning no real audio device to
 *     be blocked on.
 *
 *   capabilities: { replay: boolean }
 *     Can this source restart speech on its own after a hush, with no other
 *     client re-enqueuing? Only true for `SimulatedSource`, which owns its
 *     own fixture text. A real daemon connection has nothing to replay —
 *     this window only ever *monitors* speech (see "Settled: now-playing
 *     only" in the milestone design) — so after a hush the Play control has
 *     nothing to do until some other source speaks again.
 *
 * The two shapes differ in one more place, at the outermost layer: the wire
 * calls the field `event` where everything above calls it `kind`, and the
 * wire carries `source_id` beside it rather than inside `data`. Renaming is
 * `ShellSource`'s job and happens in exactly one place, `_normalise()`.
 *
 * Swapping which source drives the UI is the one-line choice at the bottom
 * of this file: `resolveSource()`.
 */

// ---------------------------------------------------------------------------
// SimulatedSource
// ---------------------------------------------------------------------------

/**
 * The fixture and clock logic that used to live inline in pinned.html's
 * <script>, moved behind the Source interface. Produces the same behaviour
 * it did there: same segments, same starting point (mid-utterance, paused),
 * same drift/rtf math, same hush/seek/replay semantics — just reported as
 * events instead of direct DOM writes.
 */
export class SimulatedSource {
  constructor() {
    this.capabilities = { replay: true };

    this._segments = [
      { text: "Kant gives the official definition at B25.", dur: 2.31, synth: 1.6 },
      {
        text:
          "He calls transcendental all cognition occupied not so much with objects as " +
          "with our mode of cognition of objects, insofar as this is to be possible a priori.",
        dur: 8.42,
        synth: 6.1,
      },
      { text: "The crucial thing is that the term is reflexive, or second order.", dur: 3.55, synth: 2.4 },
      { text: "It does not name a special class of objects lying beyond the ordinary ones.", dur: 4.48, synth: 5.9 },
      { text: "It names an inquiry that turns back on cognition itself.", dur: 3.02, synth: 2.1 },
    ];
    let cursor = 0;
    for (const seg of this._segments) {
      seg.span_start = cursor;
      seg.span_end = cursor + seg.text.length;
      cursor = seg.span_end + 1;
    }

    // Same starting point as today's inline script: open mid-utterance and
    // paused, so the first frame already shows what this does.
    this._index = 1;
    this._within = 2.4;
    this._playing = false;
    this._hushed = false;
    this._drift = 0.18;
    this._rtf = 0.75;
    this._queue = 2;

    this._listeners = new Set();
    this._raf = null;
    this._last = 0;
  }

  subscribe(handler) {
    this._listeners.add(handler);
    // A fresh subscriber is caught up immediately, same as a real daemon's
    // `subscribe` verb would hand a new client the current state rather
    // than making it wait for the next change.
    this._emitSnapshot(handler);
    return () => this._listeners.delete(handler);
  }

  _emit(event) {
    for (const handler of this._listeners) handler(event);
  }

  _emitSnapshot(handler) {
    handler({
      kind: "started",
      data: {
        source_id: "sim:phd-articulation",
        segments: this._segments.map((seg, i) => ({
          index: i,
          text: seg.text,
          span_start: seg.span_start,
          span_end: seg.span_end,
          audio_offset: null, // unknown ahead of playback for real segments; unused by the simulation
          duration: seg.dur,
        })),
      },
    });
    handler(this._positionEvent());
    handler(this._metricsEvent());
  }

  _positionEvent() {
    const seg = this._segments[this._index];
    return {
      kind: "position",
      data: {
        index: this._index,
        text: seg.text,
        span_start: seg.span_start,
        span_end: seg.span_end,
        audio_offset: null,
        played_at: Date.now() / 1000,
        duration: seg.dur,
        elapsed: this._hushed ? 0 : this._within,
        // Not a daemon field at all — the controller only trusts this to
        // seed its local playing/paused state once, on the very first event
        // it ever sees; DaemonSource never sends it (see the doc comment).
        playing: this._playing,
      },
    };
  }

  _metricsEvent() {
    return { kind: "metrics", data: { rtf: this._rtf, drift: this._drift, queue: this._queue } };
  }

  send(verb, payload = {}) {
    switch (verb) {
      case "pause":
        return this._doPause();
      case "resume":
        return this._doResume();
      case "hush":
        return this._doHush();
      case "seek":
        return this._doSeek(payload);
      case "cancel":
        return this._doHush();
      case "status":
        return Promise.resolve({ ok: true, data: { queue: this._queue, rtf: this._rtf, drift: this._drift } });
      case "enqueue":
      case "set_role":
      case "set_priority":
      case "subscribe":
        // Not exercised by this window (it only monitors — see the module
        // doc comment on `capabilities.replay`), but acknowledged rather
        // than silently dropped.
        return Promise.resolve({ ok: true, data: {} });
      default:
        return Promise.resolve({ ok: false, error: `unknown verb: ${verb}` });
    }
  }

  _doPause() {
    this._playing = false;
    if (this._raf) cancelAnimationFrame(this._raf);
    this._raf = null;
    this._emit(this._positionEvent());
    return Promise.resolve({ ok: true, data: {} });
  }

  _doResume() {
    if (this._playing) return Promise.resolve({ ok: true, data: {} });
    if (this._hushed) {
      // Nothing to resume — replay the fixture from the top. Real speech
      // has no equivalent: a hush is a cancel, and this window cannot
      // originate new text (see `capabilities.replay` above).
      this._hushed = false;
      this._index = 0;
      this._within = 0;
      this._drift = 0;
      this._queue = 2;
      this._emit({ kind: "started", data: { source_id: "sim:phd-articulation" } });
    }
    this._playing = true;
    this._last = performance.now();
    this._raf = requestAnimationFrame((t) => this._tick(t));
    this._emit(this._positionEvent());
    this._emit(this._metricsEvent());
    return Promise.resolve({ ok: true, data: {} });
  }

  _doHush() {
    if (this._raf) cancelAnimationFrame(this._raf);
    this._raf = null;
    this._playing = false;
    this._hushed = true;
    this._queue = 0;
    this._emit({ kind: "finished", data: { cancelled: true, aborted: false } });
    // Reproduces today's coupling of the error line to the hushed state —
    // see pinned.html's original `el("err").hidden = !hushed`. A real hush
    // does not inherently cause a plugin error; this is preserved only
    // because SimulatedSource's contract is to match today's behaviour
    // exactly, `<code>` markup and all (the controller trusts this string
    // enough to render it as HTML — see pinned.html's script).
    this._emit({
      kind: "error",
      data: {
        message:
          "Profile <code>philosophy</code> wants <code>citations</code> — no plugin provides it.",
      },
    });
    return Promise.resolve({ ok: true, data: {} });
  }

  _doSeek(payload) {
    const i = this._segments.findIndex((seg) => seg.span_start === payload.offset);
    if (i === -1) return Promise.resolve({ ok: false, error: "no segment at that offset" });
    this._index = i;
    this._within = 0;
    this._hushed = false;
    this._emit(this._positionEvent());
    return Promise.resolve({ ok: true, data: {} });
  }

  _tick(now) {
    if (!this._playing) return;
    const dt = Math.min((now - this._last) / 1000, 0.25);
    this._last = now;
    this._within += dt;

    const seg = this._segments[this._index];
    const next = this._segments[this._index + 1];
    if (next) {
      const slack = seg.dur - next.synth;
      this._drift = Math.max(0, this._drift + (slack < 0 ? dt * 0.22 : -dt * 0.12));
      this._rtf = Math.min(1.12, Math.max(0.6, next.synth / seg.dur));
    }

    if (this._within >= seg.dur) {
      this._within = 0;
      if (this._index < this._segments.length - 1) {
        this._index++;
      } else {
        this._playing = false;
        this._within = seg.dur;
        this._queue = Math.max(0, this._queue - 1);
        this._emit({ kind: "finished", data: { cancelled: false, aborted: false } });
      }
    }
    this._emit(this._positionEvent());
    this._emit(this._metricsEvent());
    if (this._playing) this._raf = requestAnimationFrame((t) => this._tick(t));
  }
}

// ---------------------------------------------------------------------------
// ShellSource
// ---------------------------------------------------------------------------

/**
 * Reads real speech, through the desktop shell.
 *
 * A webview cannot open a Unix socket, and the socket is the only thing the
 * daemon speaks (see the "Revision, 2026-09-13: the shell reads the socket,
 * not HTTP" section of docs/design/2026-09-13-gui-milestone-design.md). So
 * the Rust side opens it — clients/gui/app/src-tauri/src/bridge.rs — and
 * relays every line it reads as a `speakd://event` Tauri event, plus a
 * `speakd://link` whenever the subscription comes or goes.
 *
 * This class is what turns that back into the interface above: it renames
 * `event` to `kind`, numbers the segments the wire does not number, and
 * sends control verbs back through one Tauri command. It knows the wire
 * format and nothing else knows it — not the Rust relay, which forwards
 * lines without reading them, and not the window, which only ever sees
 * `{ kind, data }`.
 */
export class ShellSource {
  constructor(tauri) {
    this._tauri = tauri;
    this.capabilities = { replay: false }; // this window monitors; it cannot re-originate speech
    this._listeners = new Set();
    // Resolved unlisten functions from `tauri.event.listen`, collected so a
    // source that loses its last subscriber stops listening rather than
    // leaving two live handlers behind for the next one to double up on.
    this._unlisten = [];
    // The wire has no segment number. Positions are counted from the last
    // `started`, which is the only thing that can reset the count: the
    // daemon speaks one utterance at a time, so every position between two
    // `started` events belongs to the same utterance, in order.
    this._index = 0;
    this._sourceId = null;
  }

  subscribe(handler) {
    this._listeners.add(handler);
    if (this._listeners.size === 1) this._attach();
    return () => {
      this._listeners.delete(handler);
      if (this._listeners.size === 0) this._detach();
    };
  }

  async _attach() {
    const events = [];
    try {
      events.push(
        await this._tauri.event.listen("speakd://event", (evt) => this._onWire(evt.payload)),
        await this._tauri.event.listen("speakd://link", (evt) => this._onLink(evt.payload)),
      );
    } catch (err) {
      this._emit({ kind: "error", data: { message: `could not listen to the shell: ${err}` } });
      return;
    }
    // A late unsubscribe, between the await above and here, would otherwise
    // leave these registered for the lifetime of the window.
    if (this._listeners.size === 0) {
      for (const stop of events) stop();
      return;
    }
    this._unlisten = events;
    // Asked for rather than waited for: the relay connects within
    // milliseconds of the process starting, and a Tauri event emitted before
    // this page registered its listener is simply lost. Ordered after the
    // listeners so the answer can only ever be stale in the safe direction —
    // a change arriving in between is heard as well as reported.
    try {
      this._onLink(await this._tauri.core.invoke("speakd_link"));
    } catch (err) {
      this._emit({ kind: "link", data: { connected: false, detail: `shell bridge unavailable: ${err}` } });
    }
  }

  _detach() {
    for (const stop of this._unlisten) {
      try {
        stop();
      } catch {
        /* the window is going away; there is nothing left to tell */
      }
    }
    this._unlisten = [];
  }

  _emit(event) {
    for (const handler of this._listeners) handler(event);
  }

  _onLink(payload) {
    if (!payload) return;
    this._emit({ kind: "link", data: { connected: !!payload.connected, detail: payload.detail || "" } });
  }

  _onWire(wire) {
    if (!wire || typeof wire !== "object") return;
    const event = this._normalise(wire);
    if (event) this._emit(event);
  }

  /** The whole of the wire-to-interface translation, in one place. */
  _normalise(wire) {
    const kind = wire.event;
    if (typeof kind !== "string") return null;
    const source_id = typeof wire.source_id === "string" ? wire.source_id : "";
    const data = wire.data && typeof wire.data === "object" ? wire.data : {};

    if (kind === "started") {
      this._sourceId = source_id;
      this._index = 0;
      return { kind, source_id, data };
    }
    if (kind === "position") {
      // A position for a channel this source has seen no `started` for: the
      // window was opened mid-utterance, or the daemon restarted under it.
      // Numbering from zero is the only honest guess available, and saying
      // so is better than dropping the event.
      if (source_id !== this._sourceId) {
        this._sourceId = source_id;
        this._index = 0;
      }
      return { kind, source_id, data: { ...data, index: this._index++ } };
    }
    return { kind, source_id, data };
  }

  async send(verb, payload = {}) {
    let response;
    try {
      response = await this._tauri.core.invoke("speakd_send", { verb, payload });
    } catch (err) {
      // The shell could not reach the daemon at all, or refused to forward
      // the verb. A refusal *by* the daemon comes back below instead.
      return { ok: false, error: String(err && err.message ? err.message : err) };
    }
    if (response && response.ok) return { ok: true, data: response.data || {} };
    return { ok: false, error: (response && response.error) || `speakd refused ${verb}` };
  }
}

// ---------------------------------------------------------------------------
// Choosing a source
// ---------------------------------------------------------------------------

/**
 * The one-line choice the interface exists for: the shell's socket bridge
 * when this page is running inside the desktop app, the simulation when it
 * is a plain browser tab. There is no third option — the daemon has no
 * transport a browser can reach — so a tab always gets the fixture, which is
 * exactly how this window is developed with no daemon and no Rust.
 */
export async function resolveSource() {
  const tauri = typeof window !== "undefined" ? window.__TAURI__ : undefined;
  return tauri ? new ShellSource(tauri) : new SimulatedSource();
}
