// One reader's read, as a speakd channel: what it has enqueued, what the
// daemon says it is speaking, and which Zotero segment that is.
//
// A read is a run of sections on the channel `zotero:<itemKey>`, enqueued
// one ahead: the first at `start`, each next one when the one before it
// starts speaking. Everything the channel knows about what is being said
// comes from the daemon's events, never from guessing at timing; a position
// is a lookup in the plan made when its section started.
//
// Every start and stop begins a new generation. Events that belong to an
// older one -- what the daemon was still saying about a read the user has
// jumped away from -- must not move the highlight, and they are recognised
// by the one thing that tells sections apart: their text.

import { cleanup } from "./cleanup";
import { planHighlights } from "./mapping";
import { apply, identity, type Stage } from "./offsetmap";
import { buildSections, type Section, type SegmentText } from "./sections";
import type { CallResult, SpeakdEvent } from "./speakd";

/** What the channel needs of the client: verbs, and nothing else. */
export interface Caller {
  call(verb: string, sourceId: string, payload?: Record<string, unknown>): Promise<CallResult>;
}

/** Why a read did not happen, for the bar to say. */
export interface Problem {
  kind: "bad-token" | "no-daemon" | "http-error" | "refused" | "declined" | "zotero";
  error: string;
}

/**
 * A cleanup stage for one section's text; `ordinal` is the section's place
 * in the read, so a stage can leave the first alone to keep the first sound
 * quick.
 */
export type Clean = (text: string, ordinal: number) => Stage | Promise<Stage>;

export interface ChannelOptions {
  client: Caller;
  /** `zotero:<itemKey>`. */
  sourceId: string;
  /** The item's title, shown in speakd's window as the channel's label. */
  label: string;
  clean?: Clean;
  /** Sections' sizes, as `buildSections` takes them. */
  firstSize?: number;
  size?: number;
}

/** A section as enqueued: its segments, and the cleaned text the daemon was given. */
interface Entry {
  readonly section: Section;
  readonly stage: Stage;
  started: boolean;
}

/** One start's read. It is replaced, never reused, by the next start. */
interface Read {
  readonly generation: number;
  readonly sections: readonly Section[];
  readonly entries: Entry[];
  /** The next section to enqueue. */
  next: number;
}

/** The section being spoken, and its highlight plan. */
interface Current {
  readonly read: Read;
  readonly entry: Entry;
  readonly plan: Map<number, number>;
}

export class Channel {
  private readonly client: Caller;
  private readonly sourceId: string;
  private readonly label: string;
  private readonly clean: Clean;
  private readonly firstSize: number | undefined;
  private readonly size: number | undefined;

  private _generation = 0;
  private read: Read | null = null;
  private current: Current | null = null;
  // Whether the daemon may hold something of this channel's, queued or
  // speaking. It is what decides whether a stop or a jump needs a hush, so
  // that closing a tab that is not reading sends none, and one that is sends
  // exactly one.
  private live = false;
  // A seek is on its way. A seek past a section's last sentence ends that
  // utterance, which the daemon reports as a cancelled `finished`; that one
  // was asked for and must not read as something else cutting it short.
  private seeking = false;
  private highlighted: number | null = null;
  private _lastIndex: number | null = null;
  private _speakingChannel = "";
  private _paused = false;
  // Starts, stops and enqueues run one at a time, in the order asked for.
  // Otherwise a jump's hush could overtake an enqueue already on its way,
  // and the old section would be spoken after the jump.
  private queue: Promise<void> = Promise.resolve();

  private readonly highlightListeners = new Set<(index: number | null) => void>();
  private readonly problemListeners = new Set<(problem: Problem) => void>();
  private readonly endListeners = new Set<() => void>();

  constructor(options: ChannelOptions) {
    this.client = options.client;
    this.sourceId = options.sourceId;
    this.label = options.label;
    this.clean = options.clean ?? ((text) => cleanup(text));
    this.firstSize = options.firstSize;
    this.size = options.size;
  }

  /** Bumped by every start and stop. */
  get generation(): number {
    return this._generation;
  }

  /** Whether a read is under way: started, and neither finished nor stopped. */
  get reading(): boolean {
    return this.read !== null;
  }

  /** The channel the daemon is speaking on, or "" when it is silent. */
  get speakingChannel(): string {
    return this._speakingChannel;
  }

  /** Whether the voice in the room is this reader's -- what the bar's ←, → and pause wait on. */
  get speaking(): boolean {
    return this._speakingChannel === this.sourceId;
  }

  /** Whether the daemon's playback is paused. Global, like pause itself. */
  get paused(): boolean {
    return this._paused;
  }

  /** The last Zotero segment highlighted, kept past a stop: where play resumes. */
  get lastIndex(): number | null {
    return this._lastIndex;
  }

  /** Hear the Zotero segment to highlight, or null for none. Returns an unsubscribe. */
  onHighlight(listener: (index: number | null) => void): () => void {
    this.highlightListeners.add(listener);
    return () => this.highlightListeners.delete(listener);
  }

  /** Hear why a read failed. Returns an unsubscribe. */
  onProblem(listener: (problem: Problem) => void): () => void {
    this.problemListeners.add(listener);
    return () => this.problemListeners.delete(listener);
  }

  /**
   * Hear a read come to its end: the last sentence of its last section
   * spoken. A stop, a jump or a failure is not an end. Returns an
   * unsubscribe.
   */
  onEnd(listener: () => void): () => void {
    this.endListeners.add(listener);
    return () => this.endListeners.delete(listener);
  }

  /** Resolves once everything asked of the channel so far has been sent and answered. */
  idle(): Promise<void> {
    return this.queue;
  }

  /**
   * Read `segments` from `startIndex` to the end, superseding any read
   * already under way. Resolves once the first section is enqueued, or the
   * read has failed and said why.
   */
  start(segments: readonly SegmentText[], startIndex: number): Promise<void> {
    const generation = this.supersede();
    return this.serially(async () => {
      if (generation !== this._generation) return;
      if (!(await this.hushIfLive(generation))) return;
      const sections = buildSections(segments, startIndex, this.firstSize, this.size);
      if (sections.length === 0) return;
      // On every start, not once: the daemon forgets a channel idle for
      // twelve hours, label and all.
      const labelled = await this.client.call("set_label", this.sourceId, { label: this.label });
      if (generation !== this._generation) return;
      if (labelled.kind !== "ok") {
        this.fail(labelled);
        return;
      }
      const read: Read = { generation, sections, entries: [], next: 0 };
      this.read = read;
      await this.enqueueNext(read);
    });
  }

  /**
   * Stop this reader's read, and only this reader's: a hush on its own
   * channel. The hush goes once, and only when the channel may hold
   * something -- a tab closing that was not reading sends none -- unless
   * `always`: the user pressing Stop wants this channel silent whatever the
   * channel knows of it, a read given up when the stream was lost, say.
   */
  stop(options: { always?: boolean } = {}): Promise<void> {
    const generation = this.supersede();
    return this.serially(async () => {
      await this.hushIfLive(generation, options.always === true);
    });
  }

  /** One sentence back or forward, while this reader is the one speaking. */
  skip(by: number): Promise<CallResult> {
    return this.whileSpeaking(async () => {
      this.seeking = true;
      const result = await this.client.call("seek", this.sourceId, { by });
      // A seek that ended the utterance is cleared by the `finished` it
      // causes, whichever of the two arrives first.
      if (!(result.kind === "ok" && result.data.ended === true)) this.seeking = false;
      return result;
    });
  }

  pause(): Promise<CallResult> {
    return this.whileSpeaking(() => this.client.call("pause", this.sourceId, {}));
  }

  resume(): Promise<CallResult> {
    return this.whileSpeaking(() => this.client.call("resume", this.sourceId, {}));
  }

  /**
   * The event stream is gone -- a dropped connection, a daemon restarted.
   * Whatever it was saying, it will not say how it ended: no `finished` is
   * replayed. So nobody is known to be speaking, and a read under way is
   * given up, as a failure, rather than left waiting on an event that will
   * never come. What the daemon may still hold of this channel's is still
   * hushed by the next start.
   */
  streamLost(): void {
    this._speakingChannel = "";
    this.seeking = false;
    if (this.read !== null) this.fail({ kind: "no-daemon", error: "the connection to speakd was lost" });
  }

  /** Take in one event from the daemon's stream: every event, for every channel. */
  handleEvent(event: SpeakdEvent): void {
    const own = event.source_id === this.sourceId;
    switch (event.event) {
      case "transport":
        this._paused = event.data.paused === true;
        return;
      case "started":
        this._speakingChannel = event.source_id;
        if (own) this.started(event.data);
        return;
      case "finished":
        if (this._speakingChannel === event.source_id) this._speakingChannel = "";
        if (own) this.finished(event.data);
        return;
      case "position":
        if (own) this.position(event.data);
        return;
    }
  }

  // ---- events ----

  private started(data: Record<string, unknown>): void {
    const read = this.read;
    const spoken = typeof data.text === "string" ? data.text : "";
    // The daemon may speak the channel's label before the text, so the
    // section is the text the utterance ends with.
    const entry = read?.entries.find((candidate) => !candidate.started && spoken.endsWith(candidate.stage.text));
    if (read === null || entry === undefined) {
      // A section of a read since superseded, or something else spoken on
      // this channel: nothing here to highlight.
      this.current = null;
      return;
    }
    entry.started = true;
    const segments = Array.isArray(data.segments) ? data.segments.filter(isDaemonSegment) : [];
    const textStart = spoken.length - entry.stage.text.length;
    this.current = { read, entry, plan: planHighlights(entry.section, segments, entry.stage.map, textStart) };
    if (read.entries.every((candidate) => candidate.started) && read.next < read.sections.length) {
      void this.serially(() => this.enqueueNext(read));
    }
  }

  private position(data: Record<string, unknown>): void {
    const current = this.current;
    if (current === null || current.read !== this.read || typeof data.index !== "number") return;
    // A sentence with no segment of its own -- the label, say -- clears the
    // highlight rather than leaving the last sentence's up while it plays.
    this.highlight(current.plan.get(data.index) ?? null);
  }

  private finished(data: Record<string, unknown>): void {
    const current = this.current;
    const seekEnded = this.seeking;
    this.seeking = false;
    if (current === null || current.read !== this.read) return;
    this.current = null;
    const read = current.read;
    if (read.next >= read.sections.length && read.entries.every((entry) => entry.started)) {
      // The last section is over: so is the read.
      this.read = null;
      this.live = false;
      this.highlight(null);
      for (const listener of this.endListeners) listener();
    } else if (data.cancelled === true && !seekEnded) {
      // Cut short by something other than this reader -- a hush or cancel
      // from speakctl or the window. The next section may still come, and
      // the plan picks up again if it does; until then, no highlight on a
      // sentence nobody is reading.
      this.highlight(null);
    }
  }

  // ---- verbs ----

  /** Start a new generation: from now on, nothing of the old read moves the highlight. */
  private supersede(): number {
    this._generation++;
    this.read = null;
    this.current = null;
    this.seeking = false;
    this.highlight(null);
    return this._generation;
  }

  /** Hush this channel if it may be speaking, or `always`; false when the generation moved on meanwhile. */
  private async hushIfLive(generation: number, always = false): Promise<boolean> {
    if (this.live || always) {
      const result = await this.client.call("hush", this.sourceId, {});
      if (result.kind === "ok") this.live = false;
      else this.fail(result);
    }
    return generation === this._generation;
  }

  /** Clean and enqueue the read's next section with speech in it. */
  private async enqueueNext(read: Read): Promise<void> {
    while (read.next < read.sections.length) {
      const ordinal = read.next++;
      const section = read.sections[ordinal]!;
      const stage = await this.cleaned(section.text, ordinal);
      if (read !== this.read) return;
      // A section cleaned to nothing -- a page number alone -- has nothing
      // to say, and the daemon refuses empty text.
      if (stage.text.trim() === "") continue;
      // Known before it is sent: an idle daemon starts a job, and says so,
      // before its answer to the enqueue is on the way.
      const entry: Entry = { section, stage, started: false };
      read.entries.push(entry);
      const result = await this.client.call("enqueue", this.sourceId, { text: stage.text, profile: "pdf" });
      if (result.kind === "ok" && result.data.spoken !== false) {
        // Queued in the daemon whether or not this read is still wanted,
        // so the next stop or jump has to hush it.
        this.live = true;
        return;
      }
      read.entries.splice(read.entries.indexOf(entry), 1);
      if (read !== this.read) return;
      if (result.kind === "ok") {
        const reason = typeof result.data.reason === "string" ? result.data.reason : "declined";
        this.fail({ kind: "declined", error: reason });
      } else {
        this.fail(result);
      }
      return;
    }
  }

  /**
   * The section's text through the cleanup stage, or unchanged when the
   * stage throws or its map does not add up: a failed cleanup costs the
   * cleanup, never the speech.
   */
  private async cleaned(text: string, ordinal: number): Promise<Stage> {
    try {
      return apply(text, await this.clean(text, ordinal));
    } catch {
      return { text, map: identity(text.length) };
    }
  }

  private whileSpeaking(action: () => Promise<CallResult>): Promise<CallResult> {
    if (!this.speaking) {
      return Promise.resolve({
        kind: "refused",
        error: "another channel is speaking, or none is",
        data: {},
      });
    }
    return action();
  }

  private serially(task: () => Promise<void>): Promise<void> {
    const run = this.queue.then(task, task);
    // A failed task must not stop every later one from running.
    this.queue = run.catch(() => {});
    return this.queue;
  }

  private fail(problem: Problem | Exclude<CallResult, { kind: "ok" }>): void {
    this.read = null;
    this.current = null;
    this.highlight(null);
    const reported: Problem = { kind: problem.kind, error: problem.error };
    for (const listener of this.problemListeners) listener(reported);
  }

  private highlight(index: number | null): void {
    if (index === this.highlighted) return;
    this.highlighted = index;
    if (index !== null) this._lastIndex = index;
    for (const listener of this.highlightListeners) listener(index);
  }
}

function isDaemonSegment(value: unknown): value is { index: number; span_start: number; span_end: number } {
  if (typeof value !== "object" || value === null) return false;
  const { index, span_start, span_end } = value as Record<string, unknown>;
  return typeof index === "number" && typeof span_start === "number" && typeof span_end === "number";
}
