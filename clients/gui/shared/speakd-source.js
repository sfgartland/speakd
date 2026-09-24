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
 *       "started"   data: { text, segments }
 *                   One utterance is about to be spoken. `text` is the whole
 *                   of it, exactly as it was handed to the daemon — markdown
 *                   and all — and `segments` is every sentence it will be
 *                   spoken as, in order: { index, text, span_start, span_end }.
 *                   The daemon segments before it speaks, so the window can
 *                   show the whole utterance from the first word. A
 *                   segment's `text` is what is *spoken*, after transforms,
 *                   and its span points into `text`; under the markdown
 *                   transform every sentence of a block carries the whole
 *                   block's span. `SimulatedSource` adds each segment's
 *                   `duration`, which it knows because it owns its fixture.
 *
 *       "position"  data: { text, span_start, span_end, audio_offset,
 *                            played_at, index, duration?, elapsed? }
 *                   One segment has *started*. `index` is the segment's
 *                   place in `started.segments`, numbered by the daemon —
 *                   after a seek it is the only thing that says which
 *                   sentence this is. `duration` and `elapsed` are
 *                   `SimulatedSource`-only: a real `position` says that a
 *                   segment began, not how long it runs or how far in
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
 *       "queued"    data: { text, pending }
 *                   A job was accepted and is waiting its turn. `text` is the
 *                   preview the daemon truncates to QUEUE_PREVIEW_CHARS, not
 *                   the whole utterance; the event's `source_id` names the
 *                   channel it will speak on. `pending` is the daemon's count
 *                   of work accepted and unspoken, which a client should read
 *                   as a floor rather than an equality — the wire does not
 *                   settle whether the utterance being spoken right now is in
 *                   it.
 *
 *       "discarded" data: { count, reason, sources }
 *                   A hush, a mute or an unload cleared queued speech. One
 *                   event for the whole drain, carrying how many utterances
 *                   went and whose they were. None is published when nothing
 *                   was dropped: `discarded: 0` is not news.
 *
 *       "declined"  data: { text, kind, reason }
 *       "error"     data: { message }
 *       "finished"  data: { cancelled, aborted }
 *
 *       "mute"      data: { muted, scope }
 *                   An off switch moved, here or in `speakctl`. `scope` is
 *                   "global" or "channel"; for a channel the event's
 *                   `source_id` names which. The two are independent state
 *                   and not one setting written twice — global wins while it
 *                   is set, and clearing it gives each channel back whatever
 *                   it had — so a window that collapsed them into one flag
 *                   could not draw the two switches it is being asked to
 *                   draw (§3, docs/design/2026-09-15-streaming-narration-design.md).
 *
 *       "engine"    data: { state }
 *                   "loading" | "ready" | "unloaded". The model, not the
 *                   audio device: disabling frees some three gigabytes and
 *                   enabling costs tens of seconds to get them back, which is
 *                   why this is a separate switch from mute and why
 *                   "loading" is a state a window has to be able to show.
 *
 *       "speed"     data: { speed }
 *                   The listener's speed multiplier moved, here or in
 *                   `speakctl speed`. 1.0 is the profile's own pace; the
 *                   daemon clamps to 0.7–1.6 in steps of 0.05.
 *
 *       "link"      data: { connected, detail }
 *                   Not a daemon event: `ShellSource` alone emits it, for
 *                   whether the shell currently holds a subscription at all.
 *                   A monitor whose daemon is not running has to be able to
 *                   say so, since "nothing is speaking" and "nothing is
 *                   listening" look identical from inside the window.
 *
 *   send(verb, payload = {}, source) -> Promise<{ ok: true, data } | { ok: false, error }>
 *     `verb` is one of the control verbs in src/speakd/protocol.py. The
 *     shell forwards nine — pause, resume, hush, cancel, seek, mute,
 *     set_engine, set_speed, status — and refuses the rest, `enqueue` above
 *     all: this window monitors speech and originates none except through
 *     `say()` below. `seek` takes `{ index }` or `{ by }` and moves within
 *     the utterance being spoken; past the last sentence it ends it, and
 *     answers `{ index: null, ended: true }`. `set_speed` takes `{ speed }`
 *     and answers with the speed applied, which is not always the one asked
 *     for. `replay` takes `{ index }`: while the last utterance is still
 *     speaking it is a seek, once it is over it is queued again from that
 *     sentence on its own channel, and answers `{ spoken, index }` — or
 *     `{ spoken: false, reason }` when that channel is muted.
 *
 *     `source` is for the three verbs the daemon routes by `source_id` —
 *     `mute`, `hush` and `cancel`. "" is everything, a channel id is that
 *     channel alone, and the two answer in the response's `scope`: "global" or
 *     "channel" for mute, "all" or "channel" for the other two. Every other
 *     verb ignores it.
 *
 *     Omitted is a *third* answer and not a synonym for "": the shell then
 *     supplies its own control id, which is the literal "gui" — the paste
 *     box's channel. A caller that means "everything" has to say so with an
 *     empty string, and has to check the `scope` it gets back, or a window-wide
 *     stop silently becomes a stop of the window's own typing.
 *
 *     `status` also reports the speed, under `speed`, and what is waiting, under `queue`: entries of
 *     { source_id, text } in play order, next first, NOT including whatever is
 *     being spoken. It is the only way to learn a queue that filled before this
 *     client was listening.
 *
 *   say(text) -> Promise<{ ok: true, data } | { ok: false, error }>
 *     Speak text a person typed into the window, on a channel of the
 *     window's own — the one thing this window originates, added 2026-09-15
 *     (§7 of the streaming-narration design). It is not `send("enqueue")`
 *     and cannot be reached that way: the shell command behind it fixes the
 *     verb, the channel and the payload, so the only thing a caller chooses
 *     is the words. Text over 8 KiB is refused here rather than sent.
 *
 *   capabilities: { replay: boolean }
 *     Can this source restart speech on its own after a hush, with no other
 *     client re-enqueuing? Only true for `SimulatedSource`, which owns its
 *     own fixture text. A real daemon connection has nothing to replay —
 *     this window watches speech other clients originate (see "Settled:
 *     now-playing only" in the milestone design) — so after a hush the Play
 *     control has nothing to do until some other source speaks again.
 *     `say()` is not a replay and does not change this: it speaks new text a
 *     person just typed, and it has no memory of what was hushed.
 *
 * The two shapes differ in one more place, at the outermost layer: the wire
 * calls the field `event` where everything above calls it `kind`, and the
 * wire carries `source_id` beside it rather than inside `data`. Renaming is
 * `ShellSource`'s job and happens in exactly one place, `_normalise()`.
 *
 * Swapping which source drives the UI is the one-line choice at the bottom
 * of this file: `resolveSource()`.
 */

/**
 * The channel pasted text speaks on, and the cap past which the box refuses.
 *
 * Both mirror `SAY_SOURCE` and `SAY_MAX_BYTES` in
 * clients/gui/app/src-tauri/src/bridge.rs, which is where they are enforced —
 * the shell fixes the channel and would refuse an over-long paste even if
 * this file did not. They are repeated here so the window can say so
 * immediately instead of a round trip away, and so a plain browser tab
 * behaves the same as the shell.
 */
export const SAY_SOURCE = "gui";
export const SAY_MAX_BYTES = 8192;

/** The channel `SimulatedSource`'s fixture text speaks on. */
const FIXTURE_SOURCE = "sim:phd-articulation";

/**
 * How much of a queued utterance the daemon puts on the wire, in `status`'s
 * `queue` and in the `queued` event. A preview, deliberately: the list exists
 * so someone can tell which utterance is which, and a monitor that shipped
 * whole paragraphs of every waiting job would be a transcript.
 */
const QUEUE_PREVIEW_CHARS = 200;

/** The reasons a drain carries, verbatim from src/speakd/daemon.py. */
const DISCARDED_HUSHED = "discarded unspoken: a hush cleared the queue before this was spoken";
const DISCARDED_MUTED = "discarded unspoken: a mute cleared the queue before this was spoken";

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

    // Markdown, because that is what the daemon is mostly handed, and a
    // simulation reading plain sentences could not show the renderer doing
    // anything. Spans are block-level, as the real markdown transform's are:
    // every sentence of a block carries the whole block's range, and the
    // spoken text has lost its markup.
    const blocks = [
      { src: "## Transcendental, at B25", spoken: [["Transcendental, at B25.", 1.9]] },
      {
        src:
          "Kant gives the official definition at **B25**. He calls transcendental all " +
          "cognition occupied not so much with objects as with our *mode of cognition* of " +
          "objects, insofar as this is to be possible a priori.",
        spoken: [
          ["Kant gives the official definition at B25.", 2.31],
          [
            "He calls transcendental all cognition occupied not so much with objects as with " +
              "our mode of cognition of objects, insofar as this is to be possible a priori.",
            8.42,
          ],
        ],
      },
      {
        src: "- The term is reflexive, or `second order`.\n- It does not name a special class of objects.",
        spoken: [
          ["The term is reflexive, or second order.", 3.2],
          ["It does not name a special class of objects.", 3.0],
        ],
      },
      {
        src: "It names an inquiry that turns back on cognition itself.",
        spoken: [["It names an inquiry that turns back on cognition itself.", 3.02]],
      },
    ];
    this._text = blocks.map((block) => block.src).join("\n\n");
    this._segments = [];
    let cursor = 0;
    for (const block of blocks) {
      for (const [text, dur] of block.spoken) {
        this._segments.push({
          text,
          dur,
          synth: dur * 0.72,
          span_start: cursor,
          span_end: cursor + block.src.length,
        });
      }
      cursor += block.src.length + 2;
    }

    // Same starting point as today's inline script: open mid-utterance and
    // paused, so the first frame already shows what this does.
    this._index = 1;
    this._within = 2.4;
    this._playing = false;
    this._hushed = false;
    this._drift = 0.18;
    this._rtf = 0.75;
    // Accepted and not yet begun, in play order — the list `status` reports
    // under `queue`, and the one the metrics row counts. Seeded with two, so
    // that a browser tab shows the "up next" list doing something before
    // anybody types into the paste box.
    this._pending = SimulatedSource._seedPending();
    // Which channel is sounding, so that a hush, a mute or a skip aimed at one
    // channel can tell whether it covers what is playing. The fixture holds it
    // until something else takes over.
    this._speaking = FIXTURE_SOURCE;
    // An utterance with no segment timeline behind it — pasted text, and
    // anything promoted off the queue — and the timer standing in for the
    // speaking of it. `_utterance` outlives the timer across a pause, which is
    // what lets one be taken up again rather than lost.
    this._utterance = null; // { source_id, text, remaining, startedAt }
    this._utteranceTimer = null;

    // The channels `status` reports. Three, because one channel makes a
    // channel list look like a title bar with extra steps, and the point of
    // the list is choosing between sessions. The labels are what §5's
    // SET_LABEL puts on a real channel — a name, not a session UUID — so a
    // browser tab shows the list the daemon will show.
    this._channels = [
      { source_id: FIXTURE_SOURCE, role: "foreground", priority: 10, profile: "philosophy", label: "PhD articulation" },
      { source_id: "sim:resem-paper", role: "background", priority: 0, profile: "default", label: "Claude Code · ReSem paper" },
      { source_id: "sim:kronikk", role: "background", priority: 0, profile: "default", label: "Claude Code · Kronikk" },
    ];
    // Global and per-channel mute, kept apart exactly as the daemon keeps
    // them: global wins while it is set, and clearing it gives each channel
    // back whatever it had rather than unmuting everything.
    this._muted = false;
    this._channelMuted = new Map();
    // The model, and whether it is on its way in. A real load is tens of
    // seconds; this fakes enough of a wait for the window's busy state to be
    // something a person can see.
    this._engineLoaded = true;
    this._engineLoading = false;
    // The listener's speed multiplier, as the daemon keeps it. The fixture's
    // clock runs this much faster; its durations stay what they were made at.
    this._speed = 1.0;

    this._listeners = new Set();
    this._raf = null;
    this._last = 0;
  }

  /**
   * The fixture's waiting speech, as two other sessions would have left it:
   * one per background channel, so the list is visibly a queue across channels
   * and not one session's backlog. Rebuilt rather than kept, because the
   * replay path restores it after a hush has drained it.
   */
  static _seedPending() {
    return [
      {
        source_id: "sim:resem-paper",
        text:
          "Berger's objection lands on the first sense of organic unity only — the one where " +
          "reason supplies the whole — and section three concedes it there rather than arguing.",
      },
      {
        source_id: "sim:kronikk",
        text: "Kronikken er nede i 6 400 tegn. Avsnittet om lenkeråte er strøket.",
      },
    ];
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
      // Beside `data`, not inside it — that is where the wire carries it, and
      // the window reads it from there to name the channel and to move the
      // dot in the channel list.
      source_id: FIXTURE_SOURCE,
      data: this._startedData(),
    });
    handler(this._positionEvent());
    handler(this._metricsEvent());
  }

  /** What `started` carries for the fixture, shaped as the daemon's. */
  _startedData() {
    return {
      text: this._text,
      segments: this._segments.map((seg, i) => ({
        index: i,
        text: seg.text,
        span_start: seg.span_start,
        span_end: seg.span_end,
        duration: seg.dur,
      })),
    };
  }

  _positionEvent() {
    const seg = this._segments[this._index];
    return {
      kind: "position",
      source_id: FIXTURE_SOURCE,
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
    return { kind: "metrics", data: { rtf: this._rtf, drift: this._drift, queue: this._pending.length } };
  }

  send(verb, payload = {}, source = "") {
    switch (verb) {
      case "pause":
        return this._doPause();
      case "resume":
        return this._doResume();
      case "hush":
        return this._doHush(source);
      case "seek":
        return this._doSeek(payload);
      case "cancel":
        return this._doCancel(source);
      case "mute":
        return this._doMute(payload, source);
      case "set_engine":
        return this._doSetEngine(payload);
      case "set_speed":
        return this._doSetSpeed(payload);
      case "replay":
        return this._doReplay(payload);
      case "status":
        return Promise.resolve({ ok: true, data: this._status() });
      case "enqueue":
      case "set_role":
      case "set_priority":
      case "set_label":
      case "subscribe":
        // Not exercised by this window (it only monitors, and originates
        // through `say()` alone — see the module doc comment), but
        // acknowledged rather than silently dropped.
        return Promise.resolve({ ok: true, data: {} });
      default:
        return Promise.resolve({ ok: false, error: `unknown verb: ${verb}` });
    }
  }

  /**
   * Speak pasted text. Shaped like the daemon's `enqueue`, mute path
   * included: a muted channel's text is *accepted and dropped*, never held,
   * so unmuting does not release a backlog.
   */
  say(text) {
    const trimmed = String(text ?? "").trim();
    if (!trimmed) return Promise.resolve({ ok: false, error: "nothing to say" });
    // Bytes, matching the shell command's own cap so the two agree about a
    // paste full of em dashes.
    if (new TextEncoder().encode(trimmed).length > SAY_MAX_BYTES) {
      return Promise.resolve({ ok: false, error: `that is longer than ${SAY_MAX_BYTES} bytes` });
    }
    // The pasted speech is an ordinary channel, which is the whole point of
    // the fixed source: it can be muted on its own and it shows in the list.
    if (!this._channels.some((c) => c.source_id === SAY_SOURCE)) {
      this._channels.push({
        source_id: SAY_SOURCE,
        role: "foreground",
        priority: 0,
        profile: "default",
        label: "Pasted text",
      });
    }
    if (this._muted || this._channelMuted.get(SAY_SOURCE)) {
      this._emit({ kind: "declined", source_id: SAY_SOURCE, data: { text: trimmed, kind: "response", reason: "muted" } });
      return Promise.resolve({ ok: true, data: { spoken: false, reason: "muted" } });
    }
    if (!this._engineLoaded) {
      this._emit({ kind: "declined", source_id: SAY_SOURCE, data: { text: trimmed, kind: "response", reason: "disabled" } });
      return Promise.resolve({ ok: true, data: { spoken: false, reason: "disabled" } });
    }
    // Accepted, and said to be accepted, before a word of it is spoken: that
    // is what `queued` is for, and a client watching only `started` would
    // never see a job that is waiting. Announced with a `pending` that counts
    // this one, then spoken at once — nothing is holding the worker up — so
    // the window sees the push-then-pop a real acceptance-then-start produces.
    this._emit({
      kind: "queued",
      source_id: SAY_SOURCE,
      data: { text: trimmed.slice(0, QUEUE_PREVIEW_CHARS), pending: this._pending.length + 1 },
    });
    // Pasted text interrupts what is sounding; it does not drain the queue.
    // Keeping those two apart is the whole reason `_silence()` was split.
    this._stopPlayback("");
    this._speak(SAY_SOURCE, trimmed);
    return Promise.resolve({ ok: true, data: { spoken: true } });
  }

  /** Shaped exactly like the daemon's STATUS response. */
  _status() {
    return {
      channels: this._channels.map((c) => ({ ...c, muted: !!this._channelMuted.get(c.source_id) })),
      muted: this._muted,
      engine: { loaded: this._engineLoaded, loading: this._engineLoading },
      speed: this._speed,
      // In play order, next first, and not including what is being spoken —
      // the caller can see that on the "now" line and does not need it twice.
      queue: this._pending.map((job) => ({
        source_id: job.source_id,
        text: job.text.slice(0, QUEUE_PREVIEW_CHARS),
      })),
    };
  }

  _doMute(payload, source) {
    const wanted = payload.muted;
    if (typeof wanted !== "boolean") {
      return Promise.resolve({ ok: false, error: "mute needs a boolean 'muted'" });
    }
    const scope = source ? "channel" : "global";
    if (source) this._channelMuted.set(source, wanted);
    else this._muted = wanted;
    if (wanted) {
      // An off switch that lets the current paragraph finish is not an off
      // switch: muting stops what is playing where it covers it, and drops
      // what that channel had queued — a mute that held speech back would
      // release a backlog the moment it was lifted.
      const stopped = this._stopPlayback(source);
      this._drain(source, DISCARDED_MUTED);
      if (stopped) this._promoteNext();
    }
    this._emit({ kind: "mute", source_id: source || "", data: { muted: wanted, scope } });
    return Promise.resolve({ ok: true, data: { muted: wanted, scope } });
  }

  _doSetEngine(payload) {
    const wanted = payload.loaded;
    if (typeof wanted !== "boolean") {
      return Promise.resolve({ ok: false, error: "set_engine needs a boolean 'loaded'" });
    }
    if (!wanted) {
      // Disabling hushes first and then unloads — the order the daemon uses,
      // since dropping the model out from under a playing utterance is not
      // something the engine is asked to survive. Queued speech goes with it:
      // there is nothing left to speak it with.
      this._stopPlayback("");
      this._drain("", "discarded unspoken: the model was unloaded before this was spoken");
      this._engineLoading = false;
      this._engineLoaded = false;
      this._emit({ kind: "engine", source_id: "", data: { state: "unloaded" } });
      return Promise.resolve({ ok: true, data: { loaded: false } });
    }
    if (this._engineLoaded || this._engineLoading) {
      return Promise.resolve({ ok: true, data: { loaded: this._engineLoaded } });
    }
    // Loaded on a background thread in the daemon, so the socket stays
    // answerable across the tens of seconds it takes; the response says the
    // load started, and the bus says when it finished.
    this._engineLoading = true;
    this._emit({ kind: "engine", source_id: "", data: { state: "loading" } });
    setTimeout(() => {
      this._engineLoading = false;
      this._engineLoaded = true;
      this._emit({ kind: "engine", source_id: "", data: { state: "ready" } });
    }, 1600);
    return Promise.resolve({ ok: true, data: { loading: true } });
  }

  _doSetSpeed(payload) {
    const raw = payload.speed;
    if (typeof raw !== "number" || !Number.isFinite(raw) || raw <= 0) {
      return Promise.resolve({ ok: false, error: "set_speed needs a positive number 'speed'" });
    }
    // The daemon's clamp and step, so the window is told the same answers
    // here as it will be by the real thing.
    this._speed = Math.round(Math.min(1.6, Math.max(0.7, raw)) * 20) / 20;
    this._emit({ kind: "speed", source_id: "", data: { speed: this._speed } });
    return Promise.resolve({ ok: true, data: { speed: this._speed } });
  }

  /** Is the fixture mid-utterance, rather than hushed or run to its end? */
  _fixtureLive() {
    if (this._utterance || this._hushed || this._speaking !== FIXTURE_SOURCE) return false;
    const last = this._segments.length - 1;
    return !(this._index === last && this._within >= this._segments[last].dur);
  }

  _doReplay(payload) {
    const i = payload.index;
    if (!Number.isInteger(i) || i < 0) {
      return Promise.resolve({ ok: false, error: "replay needs a sentence 'index', from 0" });
    }
    if (i >= this._segments.length) {
      return Promise.resolve({ ok: false, error: `no sentence ${i}` });
    }
    // Speaking still: "play from here" is a seek, as the daemon has it.
    if (this._fixtureLive()) return this._doSeek({ index: i });
    if (this._muted || this._channelMuted.get(FIXTURE_SOURCE)) {
      this._emit({ kind: "declined", source_id: FIXTURE_SOURCE, data: { text: this._text, kind: "replay", reason: "muted" } });
      return Promise.resolve({ ok: true, data: { spoken: false, reason: "muted" } });
    }
    // Over: the fixture again, announced whole, from sentence `i`.
    this._clearUtterance();
    this._speaking = FIXTURE_SOURCE;
    this._hushed = false;
    this._index = i;
    this._within = 0;
    this._emit({ kind: "started", source_id: FIXTURE_SOURCE, data: this._startedData() });
    this._playing = true;
    this._last = performance.now();
    this._raf = requestAnimationFrame((t) => this._tick(t));
    this._emit(this._positionEvent());
    this._emit(this._metricsEvent());
    return Promise.resolve({ ok: true, data: { spoken: true, index: i } });
  }

  _doPause() {
    this._playing = false;
    if (this._raf) cancelAnimationFrame(this._raf);
    this._raf = null;
    if (this._suspendUtterance()) {
      // Nothing with a segment clock is sounding, so there is no position to
      // report the pause through. `transport` is the event that exists for
      // exactly this, and it carries the channel actually holding the floor —
      // a fixture position here would put the fixture's text back on the "now"
      // line and move the live dot onto a channel that is not speaking.
      this._emit({ kind: "transport", source_id: this._speaking, data: { paused: true } });
    } else {
      this._emit(this._positionEvent());
    }
    return Promise.resolve({ ok: true, data: {} });
  }

  /**
   * Give the fixture the floor back, taking it from any timer-driven
   * utterance holding it — pasted text, or something promoted off the queue.
   *
   * Both callers below resume *the fixture*, and leaving `_speaking` pointing
   * at whatever spoke last is not a cosmetic slip: it is what a subsequent
   * channel-scoped hush is matched against, so a stale name means a skip aimed
   * at the speaking channel misses, and one aimed at a silent channel stops
   * the speech.
   */
  _takeFloor() {
    this._clearUtterance();
    this._speaking = FIXTURE_SOURCE;
  }

  _doResume() {
    if (this._playing) return Promise.resolve({ ok: true, data: {} });
    // Suspended mid-utterance on a channel that has no segment clock: take
    // that up again rather than pulling the floor back to the fixture, which
    // would silently drop speech a person only asked to pause.
    if (this._utterance) {
      this._runUtterance();
      this._emit({ kind: "transport", source_id: this._speaking, data: { paused: false } });
      return Promise.resolve({ ok: true, data: {} });
    }
    this._takeFloor();
    if (this._hushed) {
      // Nothing to resume — replay the fixture from the top. Real speech
      // has no equivalent: a hush is a cancel, and this window cannot
      // originate new text (see `capabilities.replay` above).
      this._hushed = false;
      this._index = 0;
      this._within = 0;
      this._drift = 0;
      // The replay restores the fixture's queue along with its speech, and
      // says so through the same `queued` events a daemon would. Refilling the
      // window's list by the path it will really be fed by is the only way a
      // browser check exercises that path at all.
      this._pending = SimulatedSource._seedPending();
      this._pending.forEach((job, i) =>
        this._emit({
          kind: "queued",
          source_id: job.source_id,
          data: { text: job.text.slice(0, QUEUE_PREVIEW_CHARS), pending: i + 1 },
        }),
      );
      this._emit({ kind: "started", source_id: FIXTURE_SOURCE, data: this._startedData() });
    }
    this._playing = true;
    this._last = performance.now();
    this._raf = requestAnimationFrame((t) => this._tick(t));
    this._emit(this._positionEvent());
    this._emit(this._metricsEvent());
    return Promise.resolve({ ok: true, data: {} });
  }

  /**
   * Stop what is sounding, if the scope covers it, and say so.
   *
   * The old `_silence()` did this *and* emptied the queue, which was harmless
   * while the only caller was a hush. It is not harmless now: `say()` has to
   * interrupt without draining and a channel-scoped hush has to drain one
   * channel without touching the rest, so stopping and draining are two
   * operations here exactly as they are two in the daemon.
   *
   * Returns whether anything was actually stopped, which is what tells a
   * caller the worker is now free to take up the next queued utterance.
   */
  _stopPlayback(source_id) {
    if (source_id && source_id !== this._speaking) return false;
    if (this._raf) cancelAnimationFrame(this._raf);
    this._raf = null;
    this._clearUtterance();
    this._playing = false;
    this._hushed = true;
    this._emit({
      kind: "finished",
      source_id: this._speaking,
      data: { cancelled: true, aborted: false },
    });
    return true;
  }

  /**
   * Drop queued speech and report it, scoped exactly as the daemon scopes it:
   * an empty `source_id` takes everything, a named one takes that channel
   * alone. No event when nothing went — `discarded: 0` is not news, which is
   * the rule `_drain_queued` in src/speakd/daemon.py follows.
   */
  _drain(source_id, reason) {
    const kept = [];
    const dropped = [];
    for (const job of this._pending) {
      (source_id && job.source_id !== source_id ? kept : dropped).push(job);
    }
    this._pending = kept;
    if (dropped.length) {
      this._emit({
        kind: "discarded",
        source_id: "",
        data: {
          count: dropped.length,
          reason,
          sources: [...new Set(dropped.map((job) => job.source_id))].sort(),
        },
      });
    }
    return dropped.length;
  }

  /**
   * Speak one utterance that has no segment timeline behind it — pasted text
   * and anything promoted off the queue. It ends on its own because a real one
   * does, and a simulation leaving the pip breathing forever would be the one
   * place this visibly parts company with the daemon.
   *
   * It pauses, resumes and stops like anything else here: a timer that ran on
   * under a paused window would be the simulation contradicting the daemon
   * about the plainest thing either does, and this window's whole reason for
   * having a simulation is that the two must not diverge.
   */
  _speak(source_id, text) {
    this._speaking = source_id;
    this._hushed = false;
    this._utterance = { source_id, text, remaining: 1800, startedAt: 0 };
    this._emit({ kind: "started", source_id, data: { text } });
    this._emit(this._metricsEvent());
    this._runUtterance();
  }

  /** Start, or take up again, the timer standing in for `_utterance`. */
  _runUtterance() {
    const job = this._utterance;
    if (!job || this._utteranceTimer) return;
    job.startedAt = performance.now();
    this._utteranceTimer = setTimeout(() => {
      this._utteranceTimer = null;
      this._utterance = null;
      this._emit({ kind: "finished", source_id: job.source_id, data: { cancelled: false, aborted: false } });
      this._promoteNext();
    }, job.remaining);
  }

  /**
   * Hold the timer where it is, keeping what is left of the utterance — the
   * pause path. A `pause` that let pasted or promoted speech run to the end
   * while the window drew itself paused would be the simulation disagreeing
   * with the daemon about the plainest thing either of them does.
   */
  _suspendUtterance() {
    if (!this._utteranceTimer || !this._utterance) return false;
    clearTimeout(this._utteranceTimer);
    this._utteranceTimer = null;
    this._utterance.remaining = Math.max(0, this._utterance.remaining - (performance.now() - this._utterance.startedAt));
    return true;
  }

  /** Drop it altogether, running or suspended — the stop path. */
  _clearUtterance() {
    if (this._utteranceTimer) clearTimeout(this._utteranceTimer);
    this._utteranceTimer = null;
    this._utterance = null;
  }

  /**
   * Take up the next queued utterance, which is the whole point of the skip
   * button: stop one channel and the one behind it starts straight after.
   * Simulating that is not decoration — a simulation where the queue only ever
   * shrank by hand could not show the feature working at all.
   */
  _promoteNext() {
    const next = this._pending.shift();
    if (!next) {
      this._emit(this._metricsEvent()); // the queue metric emptied out
      return;
    }
    this._speak(next.source_id, next.text);
  }

  /**
   * Stop talking. Scoped by the channel the verb arrives on, as the daemon
   * scopes it: an empty `source_id` stops everything, a named one stops that
   * channel and drops what it had queued, leaving the next channel's speech to
   * start immediately. The `scope` in the response is what a caller reads to
   * know which of the two happened — never `discarded`, which is zero both for
   * a global hush over an empty queue and for a hush that never left the
   * caller's own channel.
   */
  _doHush(source = "") {
    const scope = source ? "channel" : "all";
    const stopped = this._stopPlayback(source);
    const discarded = this._drain(source, DISCARDED_HUSHED);
    // Reproduces today's coupling of the error line to the hushed state —
    // see pinned.html's original `el("err").hidden = !hushed`. A real hush
    // does not inherently cause a plugin error; this is preserved only
    // because SimulatedSource's contract is to match today's behaviour
    // exactly, `<code>` markup and all (the controller trusts this string
    // enough to render it as HTML — see pinned.html's script). Kept to the
    // window's own Hush button and no further: a per-channel skip inventing a
    // plugin error would be the simulation saying what the daemon never says.
    if (!source) {
      this._emit({
        kind: "error",
        data: {
          message:
            "Profile <code>philosophy</code> wants <code>citations</code> — no plugin provides it.",
        },
      });
    }
    if (stopped) this._promoteNext();
    return Promise.resolve({ ok: true, data: { discarded, scope } });
  }

  /**
   * The same stop, without the drain. `cancel` skips one utterance and lets
   * the queue run on — an agent superseding its own announcement — where
   * `hush` means stop talking and takes the queue with it. The daemon's
   * HUSH/CANCEL branch spells the distinction out; this window sends neither,
   * but a simulation that collapsed them would be teaching the next caller the
   * wrong thing.
   */
  _doCancel(source = "") {
    const scope = source ? "channel" : "all";
    if (this._stopPlayback(source)) this._promoteNext();
    return Promise.resolve({ ok: true, data: { discarded: 0, scope } });
  }

  _doSeek(payload) {
    const hasIndex = Number.isInteger(payload.index);
    const hasBy = Number.isInteger(payload.by);
    if (hasIndex === hasBy) {
      return Promise.resolve({ ok: false, error: "seek needs one integer, 'index' or 'by'" });
    }
    // Only the fixture has sentences to move between. Pasted and promoted
    // speech has no segment clock here, and a hushed fixture is not speaking
    // — the daemon refuses both the same way.
    if (this._utterance || this._hushed || this._speaking !== FIXTURE_SOURCE) {
      return Promise.resolve({ ok: false, error: "nothing is speaking" });
    }
    const target = Math.max(0, hasIndex ? payload.index : this._index + payload.by);
    if (target >= this._segments.length) {
      this._doCancel("");
      return Promise.resolve({ ok: true, data: { index: null, ended: true } });
    }
    this._index = target;
    this._within = 0;
    this._emit(this._positionEvent());
    return Promise.resolve({ ok: true, data: { index: target } });
  }

  _tick(now) {
    if (!this._playing) return;
    const dt = Math.min((now - this._last) / 1000, 0.25);
    this._last = now;
    this._within += dt * this._speed;

    const seg = this._segments[this._index];
    const next = this._segments[this._index + 1];
    if (next) {
      const slack = seg.dur - next.synth;
      this._drift = Math.max(0, this._drift + (slack < 0 ? dt * 0.22 : -dt * 0.12));
      this._rtf = Math.min(1.12, Math.max(0.6, next.synth / seg.dur));
    }

    let ended = false;
    if (this._within >= seg.dur) {
      this._within = 0;
      if (this._index < this._segments.length - 1) {
        this._index++;
      } else {
        this._playing = false;
        this._within = seg.dur;
        ended = true;
      }
    }
    this._emit(this._positionEvent());
    this._emit(this._metricsEvent());
    if (this._playing) this._raf = requestAnimationFrame((t) => this._tick(t));
    // After the last position of the utterance, not before it: promoting the
    // next one emits a `started`, and a `started` followed by this tick's own
    // `position` would put the finished utterance back on the "now" line.
    if (ended) {
      this._emit({ kind: "finished", source_id: FIXTURE_SOURCE, data: { cancelled: false, aborted: false } });
      this._promoteNext();
    }
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
 * sends control verbs back through the shell's two commands — `speakd_send`
 * for the allowlisted control verbs, `speakd_say` for pasted text and
 * nothing else. It knows the wire
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
      // The daemon numbers segments itself now. Counting positions was a
      // guess that a seek breaks: after going back, the count and the
      // sentence being spoken part company for the rest of the utterance.
      // The count below survives only for a daemon older than that.
      if (typeof data.index === "number") {
        this._sourceId = source_id;
        return { kind, source_id, data };
      }
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

  async send(verb, payload = {}, source) {
    // `undefined` and `""` are different answers and must stay different.
    // Omitting `source` lets the shell supply its own control id, which is
    // the literal "gui" -- the same string as the paste box's channel. An
    // empty string is what the daemon reads as *global*, so a master mute
    // has to send one, and a falsy check here would swallow it and silently
    // mute the paste box instead while the button said "Muted".
    const args = source === undefined ? { verb, payload } : { verb, payload, source };
    return this._invoke("speakd_send", args, `speakd refused ${verb}`);
  }

  /**
   * The window's one way of originating speech. A separate command, not
   * `send("enqueue")` — the shell will not forward that verb at all, and this
   * one fixes the channel and the payload, so a frontend bug can neither send
   * arbitrary traffic nor speak on another session's channel.
   */
  async say(text) {
    const trimmed = String(text ?? "").trim();
    if (!trimmed) return { ok: false, error: "nothing to say" };
    if (new TextEncoder().encode(trimmed).length > SAY_MAX_BYTES) {
      return { ok: false, error: `that is longer than ${SAY_MAX_BYTES} bytes` };
    }
    return this._invoke("speakd_say", { text: trimmed }, "speakd refused the text");
  }

  /** One `{ok, error}` answer, whichever of the two commands produced it. */
  async _invoke(command, args, refusal) {
    let response;
    try {
      response = await this._tauri.core.invoke(command, args);
    } catch (err) {
      // The shell could not reach the daemon at all, or refused to forward
      // the verb. A refusal *by* the daemon comes back below instead.
      return { ok: false, error: String(err && err.message ? err.message : err) };
    }
    if (response && response.ok) return { ok: true, data: response.data || {} };
    return { ok: false, error: (response && response.error) || refusal };
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
