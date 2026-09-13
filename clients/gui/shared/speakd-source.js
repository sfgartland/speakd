/**
 * speakd-source.js — the event-source abstraction.
 *
 * The frontend must not know whether its events come from a daemon or a
 * simulation. Both `SimulatedSource` and `DaemonSource` below implement the
 * same small interface:
 *
 *   subscribe(handler) -> unsubscribe()
 *     `handler` is called with `{ kind, data }` events. `kind` is one of the
 *     five the daemon already emits (see docs/design/2026-09-13-speakd-design.md
 *     and docs/plans/2026-09-13-daemon.md):
 *
 *       "started"   data: { source_id, segments? }
 *                   `segments`, when present, is the FULL pre-known list of
 *                   { index, text, span_start, span_end, audio_offset, duration }.
 *                   This is a SimulatedSource-only convenience: the real
 *                   daemon segments one unit ahead of playback
 *                   (src/speakd/pipeline.py's depth-one queue), so it cannot
 *                   know segment 4's duration while segment 1 is still
 *                   playing. A real `started` will not carry `segments`; the
 *                   UI discovers them one at a time from `position` instead.
 *
 *       "position"  data: { index, text, span_start, span_end, audio_offset,
 *                            played_at, duration?, elapsed? }
 *                   The first five fields plus `played_at` are exactly what
 *                   the daemon design commits to. `index` is a convenience
 *                   both sources add so the controller never has to infer
 *                   segment identity from spans. `duration` and `elapsed`
 *                   are SimulatedSource-only (see above) — a real `position`
 *                   announces that a segment has *started*, not how long it
 *                   will run or how far into it playback already is.
 *
 *       "metrics"   data: { rtf?, drift?, queue? }
 *                   NOT part of the daemon's given event vocabulary — there
 *                   is no defined channel yet for synthesis speed, clock
 *                   drift or queue depth (see the "Settled: the window is
 *                   pinned..." section of the milestone design). Only
 *                   SimulatedSource emits it, purely to reproduce the
 *                   mockup's live-updating metrics row. The controller
 *                   treats every field as optional and shows a dash for
 *                   whatever it never receives.
 *
 *       "error"     data: { message }
 *       "finished"  data: { cancelled, aborted }
 *       "declined"  data: { reason }
 *
 *   send(verb, payload = {}) -> Promise<{ ok: true, data } | { ok: false, error }>
 *     `verb` is one of the control verbs in src/speakd/protocol.py: enqueue,
 *     cancel, hush, pause, resume, seek, set_role, set_priority, subscribe,
 *     status. `pause`, `resume` and `seek` are not implemented in the daemon
 *     yet (a streaming player is a prerequisite — see
 *     docs/plans/2026-09-13-streaming-player.md) and DaemonSource expects
 *     them to fail; SimulatedSource honours them locally since it owns no
 *     real audio device to be blocked on.
 *
 *   capabilities: { replay: boolean }
 *     Can this source restart speech on its own after a hush, with no other
 *     client re-enqueuing? Only true for SimulatedSource, which owns its own
 *     fixture text. A real daemon connection has nothing to replay — this
 *     window only ever *monitors* speech, it never originates it (see
 *     "Settled: now-playing only" in the milestone design) — so after a hush
 *     the real Play control has nothing to do until some other source speaks
 *     again.
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
// DaemonSource
// ---------------------------------------------------------------------------

/**
 * NOT YET FUNCTIONAL. There is no HTTP/SSE transport in the daemon yet
 * (that is Part 3 of docs/design/2026-09-13-gui-milestone-design.md, still
 * unbuilt) — this is a stub shaped the way it will eventually work, so
 * wiring it up later is a swap of `resolveSource()`'s choice, not a rewrite.
 *
 * Endpoint shape is provisional and deliberately unremarkable: an SSE
 * stream at `GET {baseUrl}/events` with one named event per `kind`, and one
 * `POST {baseUrl}/{verb}` route per control verb — the natural HTTP analogue
 * of `speakd.protocol.Request`, and compatible with "any TTS server with a
 * simple REST API" per the design doc's `opencode-voice-plugin` note. When
 * the real transport lands, only the URLs in this file need to change.
 */
export class DaemonSource {
  constructor(baseUrl) {
    this.baseUrl = baseUrl.replace(/\/$/, "");
    this.capabilities = { replay: false }; // this window only monitors; it cannot re-originate speech
    this._listeners = new Set();
    this._es = null;
  }

  subscribe(handler) {
    this._listeners.add(handler);
    if (!this._es) this._connect();
    return () => {
      this._listeners.delete(handler);
      if (this._listeners.size === 0) this._disconnect();
    };
  }

  _connect() {
    let es;
    try {
      es = new EventSource(`${this.baseUrl}/events`);
    } catch (err) {
      this._emit({ kind: "error", data: { message: `could not open event stream: ${err}` } });
      return;
    }
    this._es = es;
    for (const kind of ["started", "position", "error", "finished", "declined"]) {
      es.addEventListener(kind, (evt) => {
        let data = {};
        try {
          data = JSON.parse(evt.data);
        } catch {
          /* malformed payload — drop it rather than crash the window */
        }
        this._emit({ kind, data });
      });
    }
    es.onerror = () => {
      this._emit({ kind: "error", data: { message: "lost connection to speakd" } });
    };
  }

  _disconnect() {
    if (this._es) this._es.close();
    this._es = null;
  }

  _emit(event) {
    for (const handler of this._listeners) handler(event);
  }

  async send(verb, payload = {}) {
    try {
      const res = await fetch(`${this.baseUrl}/${verb}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      if (!res.ok) {
        const text = await res.text().catch(() => res.statusText);
        return { ok: false, error: text || `HTTP ${res.status}` };
      }
      const data = await res.json().catch(() => ({}));
      return { ok: true, data };
    } catch (err) {
      // No daemon has ever answered here yet — this is the expected result
      // today, for every verb, not a bug in this file.
      return { ok: false, error: `could not reach speakd: ${err.message || err}` };
    }
  }
}

// ---------------------------------------------------------------------------
// Choosing a source
// ---------------------------------------------------------------------------

/** Provisional — no port has been settled for the HTTP transport upstream. */
const DEFAULT_DAEMON_URL = "http://127.0.0.1:7481";

async function daemonReachable(baseUrl, timeoutMs = 800) {
  try {
    const res = await fetch(`${baseUrl}/status`, { signal: AbortSignal.timeout(timeoutMs) });
    return res.ok;
  } catch {
    return false;
  }
}

/**
 * The one-line choice the interface exists for: use the daemon if it is
 * reachable, otherwise fall back to the simulation. `window.SPEAKD_DAEMON_URL`
 * (set by the Tauri shell, or a `?daemon=` query param for the browser-tab
 * case) overrides the provisional default.
 */
export async function resolveSource() {
  const baseUrl =
    (typeof window !== "undefined" && window.SPEAKD_DAEMON_URL) ||
    new URLSearchParams(typeof location !== "undefined" ? location.search : "").get("daemon") ||
    DEFAULT_DAEMON_URL;
  return (await daemonReachable(baseUrl)) ? new DaemonSource(baseUrl) : new SimulatedSource();
}
