// One reader's Read Aloud, as Zotero's manager sees it and as speakd plays it.
//
// Zotero's Read Aloud manager builds a playback *controller* for every
// start, jump and voice change, hands it the document's segments and the
// index to start at, and from then on tells it everything the user does:
// pause, play, skip, stop. A `ControllerCore` is the logic of the controller
// the plugin gives Zotero for its own voice. It plays nothing: it turns what
// the manager asks into the channel's verbs, and the channel's highlights
// into the events the manager draws the highlight from.
//
// A `ReaderSession` is the life of those controllers in one reader, and the
// one place that tells a jump from a stop: Zotero destroys the old
// controller before it builds the next, so a controller destroyed with no
// successor by the end of the tick is a stop.
//
// Pure. reader-takeover.ts wraps a core in the content-side object Zotero
// calls, and supplies the hooks; nothing here touches Zotero.

import type { Problem } from "./channel";
import { paragraphTarget, selectionEnd, selectionStart } from "./navigation";
import type { SegmentText } from "./sections";
import type { CallResult, SpeakdEvent } from "./speakd";

/** What the session needs of channel.ts's Channel. */
export interface ChannelLike {
  start(segments: readonly SegmentText[], startIndex: number): Promise<void>;
  /** `always`: hush the channel even when nothing of its own is known queued. */
  stop(options?: { always?: boolean }): Promise<void>;
  skip(by: number): Promise<CallResult>;
  pause(): Promise<CallResult>;
  resume(): Promise<CallResult>;
  handleEvent(event: SpeakdEvent): void;
  /** The event stream was lost: nothing it said about who is speaking stands. */
  streamLost(): void;
  /** Resolves once everything asked of the channel so far has been sent and answered. */
  idle(): Promise<void>;
  readonly reading: boolean;
  readonly speaking: boolean;
  readonly paused: boolean;
  readonly lastIndex: number | null;
  onHighlight(listener: (index: number | null) => void): () => void;
  onEnd(listener: () => void): () => void;
  onProblem(listener: (problem: Problem) => void): () => void;
}

/** A Zotero segment, as far as the session needs one. */
export interface SegmentInfo {
  readonly text: string;
  readonly anchor: string | null;
}

/** What the plugin's own UI asked for when it started a read. */
export type Intent =
  /** Read to the end; `text` is the selection it starts from, when there is one. */
  | { kind: "here"; text?: string }
  /** Read the selection, and stop. */
  | { kind: "selection"; text: string };

/**
 * How long Zotero may take from a start to building the controller: it
 * loads voices first, and waits up to 8 s for its own server's catalogue
 * (reader-takeover.ts `NATIVE_VOICES_TIMEOUT_MS`). A read that has not begun
 * by then never will -- the first-run dialog, a voice that never resolved --
 * and the takeover must not stay on, hiding Zotero's popup, for it.
 */
export const CONTROLLER_WAIT_MS = 12_000;

export interface SessionHooks {
  /** Run `task` once the synchronous work under way is done: a microtask. */
  defer(task: () => void): void;
  /** Run `task` after `ms`. Returns a cancel. */
  later(task: () => void, ms: number): () => void;
  /**
   * The plugin's own read begins (true) or ends (false). Ending closes
   * Zotero's Read Aloud, which clears the highlight, and gives the user's
   * voice back.
   */
  takeover(active: boolean): void;
  /** Bring Zotero's paused state in line with the daemon's. */
  mirrorPause(paused: boolean): void;
}

/** Where a controller's events go: the content-side object Zotero listens to. */
export interface ControllerSink {
  emit(type: string, index: number | null): void;
}

/**
 * Whether starting a read should send a `resume`. Pause is global and
 * survives a hush, so a read stopped while paused would start paused; but
 * `resume` is global too, and must not unpause another channel the user
 * paused -- an agent mid-sentence. So only when nothing is speaking, or this
 * reader is.
 */
export function resumesOnStart(speakingChannel: string, ownChannel: string): boolean {
  return speakingChannel === "" || speakingChannel === ownChannel;
}

type Granularity = "sentence" | "paragraph" | string;

export class ControllerCore {
  private readonly session: ReaderSession;
  readonly segments: readonly SegmentInfo[];
  /** Where Zotero asked this controller to start. */
  readonly start: number;
  /** The last segment it reads: the end of a selection, or of the document. */
  readonly end: number;
  readonly sink: ControllerSink;

  destroyed = false;
  private isBuilt = false;
  private begun = false;
  private completed = false;
  private _paused = false;
  private _speed = 1;
  /** The segment last highlighted by this controller: where it is. */
  private pos: number;
  lastSkipGranularity: Granularity | null = null;
  error: string | null = null;

  /** The array Zotero built this controller from: what tells a rebuild from a new segmentation. */
  readonly source: unknown;

  constructor(
    session: ReaderSession,
    segments: readonly SegmentInfo[],
    start: number,
    end: number,
    sink: ControllerSink,
    source: unknown,
  ) {
    this.session = session;
    this.segments = segments;
    this.source = source;
    this.start = start;
    this.end = end;
    this.sink = sink;
    this.pos = start;
  }

  get paused(): boolean {
    return this._paused;
  }

  /**
   * Zotero's pause and play. Relayed to speakd only while this reader's
   * channel is the one speaking, since the verbs are global; played when it
   * is not reading, the read starts (again) instead.
   */
  set paused(paused: boolean) {
    this._paused = paused;
    if (this.destroyed) return;
    if (!this.begun) {
      // Built paused, and now played. Before it is built, `built` starts it.
      if (!paused && this.isBuilt) this.begin(this.start);
      return;
    }
    const channel = this.session.channel;
    if (paused) {
      if (channel.speaking && !channel.paused) void channel.pause();
    } else if (channel.reading) {
      if (channel.speaking && channel.paused) void channel.resume();
    } else {
      // Finished, failed, or cut off: play reads it again, from the start
      // when it had come to its end, from where it stopped otherwise.
      this.begin(this.completed ? this.start : this.pos);
    }
  }

  /** Zotero's speed is not speakd's: the listener has one speed, set from the bar or speakctl. */
  get speed(): number {
    return this._speed;
  }

  set speed(speed: number) {
    this._speed = speed;
  }

  skipAhead(granularity: Granularity, accelerate: boolean): void {
    this.skip(granularity, accelerate, 1);
  }

  skipBack(granularity: Granularity, accelerate: boolean): void {
    this.skip(granularity, accelerate, -1);
  }

  segmentToAnnotate(): number {
    return this.pos;
  }

  retry(): void {
    if (this.destroyed) return;
    this.error = null;
    this.begin(this.pos);
  }

  destroy(): void {
    if (this.destroyed) return;
    this.destroyed = true;
    this.session.destroyed(this);
  }

  /** Carry on the read `previous` was driving, as its replacement. */
  takeOver(previous: ControllerCore): void {
    this.begun = true;
    this.isBuilt = true;
    this.completed = previous.completed;
    this.pos = previous.pos;
    this.lastSkipGranularity = previous.lastSkipGranularity;
  }

  /** The segment last highlighted, or where it started. */
  get position(): number {
    return this.pos;
  }

  /** Called by the session once Zotero has finished building this controller. */
  built(): void {
    this.isBuilt = true;
    if (!this.destroyed && !this.begun && !this._paused) this.begin(this.start);
  }

  /** The channel highlights segment `index`. */
  highlighted(index: number): void {
    if (this.destroyed || index < this.start || index > this.end) return;
    this.pos = index;
    this.sink.emit("ActiveSegmentChange", index);
  }

  /** The read came to its end: Zotero leaves the last sentence highlighted. */
  ended(): void {
    if (this.destroyed) return;
    this.completed = true;
    this.sink.emit("Complete", null);
  }

  failed(problem: Problem): void {
    if (this.destroyed) return;
    this.error = problem.kind === "no-daemon" ? "network" : "unknown";
    this.sink.emit("Error", null);
  }

  private begin(from: number): void {
    this.begun = true;
    this.completed = false;
    this.pos = from;
    void this.session.channel.start(this.segments.slice(0, this.end + 1), from);
  }

  private skip(granularity: Granularity, accelerate: boolean, direction: 1 | -1): void {
    this.lastSkipGranularity = granularity;
    const channel = this.session.channel;
    // ←/→ move whatever is speaking; they are this reader's only while it is.
    if (this.destroyed || !channel.speaking) return;
    if (granularity === "sentence") {
      void channel.skip(direction * (accelerate ? 5 : 1));
      return;
    }
    // A paragraph is further than a seek can go within a section: start over there.
    const target = paragraphTarget(
      this.segments.map((segment) => segment.anchor),
      this.pos,
      direction,
    );
    if (target !== null && target <= this.end) this.begin(target);
  }
}

export class ReaderSession {
  readonly channel: ChannelLike;
  private readonly hooks: SessionHooks;
  private intent: Intent | null = null;
  private _wanted = false;
  private _problem: Problem | null = null;
  private current: ControllerCore | null = null;
  private closed = false;
  private readonly unsubscribe: (() => void)[];
  private readonly changeListeners = new Set<() => void>();
  /** Cancels the wait for Zotero's controller, while one is on. */
  private cancelWait: (() => void) | null = null;

  constructor(channel: ChannelLike, hooks: SessionHooks) {
    this.channel = channel;
    this.hooks = hooks;
    this.unsubscribe = [
      channel.onHighlight((index) => {
        // The channel clears its highlight on every stop and jump; Zotero
        // cannot clear one short of closing Read Aloud, and should not
        // between two sentences. The next sentence's highlight replaces it.
        if (index !== null) this.current?.highlighted(index);
      }),
      channel.onEnd(() => {
        this.current?.ended();
        this.changed();
      }),
      channel.onProblem((problem) => {
        this._problem = problem;
        this.current?.failed(problem);
        this.changed();
      }),
    ];
  }

  /** Whether the read under way is the plugin's own: what pins speakd's voice. */
  get wanted(): boolean {
    return this._wanted;
  }

  /** Why the last read failed, until the next one starts. */
  get problem(): Problem | null {
    return this._problem;
  }

  /** Whether a controller of the plugin's voice is live in this reader. */
  get live(): boolean {
    return this.current !== null;
  }

  /** The plugin's UI is about to start a read: Zotero's next controller should be speakd's. */
  want(intent: Intent): void {
    if (this.closed) return;
    this.intent = intent;
    this._problem = null;
    if (!this._wanted) {
      this._wanted = true;
      this.hooks.takeover(true);
    }
    this.stopWaiting();
    if (this.current === null) {
      this.cancelWait = this.hooks.later(() => {
        this.cancelWait = null;
        this.giveUp({ kind: "zotero", error: "Zotero's Read Aloud did not start" });
      }, CONTROLLER_WAIT_MS);
    }
    this.changed();
  }

  /**
   * Zotero will build no controller for the read wanted: end the takeover,
   * and say why. A read whose controller came is left alone: its end is the
   * channel's to say.
   */
  giveUp(problem: Problem): void {
    if (this.closed || !this._wanted || this.current !== null) return;
    this.stopWaiting();
    this.intent = null;
    this._problem = problem;
    this._wanted = false;
    this.hooks.takeover(false);
    this.changed();
  }

  /** Zotero's `voice.getController(segments, backwardStopIndex, forwardStopIndex)`, for speakd's voice. */
  createController(
    segments: readonly SegmentInfo[],
    backwardStopIndex: number | null,
    forwardStopIndex: number | null,
    sink: ControllerSink,
    source: unknown = segments,
  ): ControllerCore {
    const last = segments.length - 1;
    const intent = this.intent;
    // The selection bounds the read it started, not the jumps after it.
    this.intent = null;
    let start = Math.min(Math.max(backwardStopIndex ?? 0, 0), Math.max(last, 0));
    if (intent?.text !== undefined) start = selectionStart(segments, start, intent.text);
    let end = forwardStopIndex !== null && forwardStopIndex >= start ? Math.min(forwardStopIndex, last) : last;
    if (intent?.kind === "selection") end = Math.min(end, selectionEnd(segments, start, intent.text));
    // Destroyed this tick, with no word yet on whether that was a stop.
    const previous = this.current?.destroyed ? this.current : null;
    // Zotero rebuilds the controller for reasons of its own -- voices that
    // finish loading late re-apply the voice (B:82441, B:82533) -- starting
    // at the segment being read. Over the same segments, at the sentence
    // being read, that is no jump: the read carries on, bounds and all.
    const rebuild =
      intent === null &&
      previous !== null &&
      previous.source === source &&
      this.channel.reading &&
      start === previous.position;
    if (rebuild) end = previous.end;
    this.stopWaiting();
    const core = new ControllerCore(this, segments, start, end, sink, source);
    this.current = this.closed ? null : core;
    if (this.closed) core.destroyed = true;
    if (rebuild) core.takeOver(previous);
    else this.hooks.defer(() => core.built());
    this.changed();
    return core;
  }

  /** Take in an event from the daemon's stream, after the channel has. */
  handleEvent(event: SpeakdEvent): void {
    if (this.closed) return;
    this.channel.handleEvent(event);
    if (event.event === "transport" && this.current !== null && this.channel.speaking) {
      this.hooks.mirrorPause(event.data.paused === true);
    }
    this.changed();
  }

  /** The link's event stream was lost: the channel's read is given up, and Zotero told. */
  streamLost(): void {
    if (this.closed) return;
    this.channel.streamLost();
    this.changed();
  }

  /** A controller was destroyed; if none replaces it this tick, the read is over. */
  destroyed(core: ControllerCore): void {
    if (core !== this.current) return;
    this.hooks.defer(() => {
      if (this.current === core) this.finish();
    });
  }

  /** The plugin's own stop: the user wants this channel silent, whatever it is known to hold. */
  stop(): void {
    this.finish(true);
  }

  /** The reader's tab closed: hush it, once, and hear nothing more. */
  close(): void {
    if (this.closed) return;
    this.finish();
    this.closed = true;
    for (const off of this.unsubscribe) off();
    this.changeListeners.clear();
  }

  /** Resolves once what the channel has been asked so far -- a stop's hush, say -- has been sent. */
  idle(): Promise<void> {
    return this.channel.idle();
  }

  /** Hear the session's state change, for the bar. Returns an unsubscribe. */
  onChange(listener: () => void): () => void {
    this.changeListeners.add(listener);
    return () => this.changeListeners.delete(listener);
  }

  private stopWaiting(): void {
    this.cancelWait?.();
    this.cancelWait = null;
  }

  private finish(always = false): void {
    if (this.closed) return;
    this.stopWaiting();
    const core = this.current;
    this.current = null;
    if (core !== null) core.destroyed = true;
    this.intent = null;
    void this.channel.stop({ always });
    if (this._wanted) {
      this._wanted = false;
      this.hooks.takeover(false);
    }
    this.changed();
  }

  private changed(): void {
    for (const listener of [...this.changeListeners]) {
      try {
        listener();
      } catch {
        // The bar's failure is the bar's.
      }
    }
  }
}
