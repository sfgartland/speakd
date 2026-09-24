// The plugin's one connection to speakd, shared by every reader.
//
// One client and one event stream, however many PDFs are open: every
// reader's channel hears every event and picks out its own. The link also
// keeps the few facts the control bars show that are nobody's channel in
// particular -- whether the daemon is there, which channel is speaking, the
// paused state and the speed, which are the listener's, not a reader's.
//
// Pure: the client and the abort controller are made by whoever creates the
// link (from the plugin sandbox's `fetch` and a window's `AbortController`).

import type { AbortSignalLike, CallResult, SpeakdEvent, StreamState } from "./speakd";

/** What the link needs of a speakd client. `SpeakdClient` is one. */
export interface LinkClient {
  call(verb: string, sourceId: string, payload?: Record<string, unknown>): Promise<CallResult>;
  events(onEvent: (event: SpeakdEvent) => void, signal: AbortSignalLike, onState?: (state: StreamState) => void): Promise<void>;
}

export interface AbortLike {
  readonly signal: AbortSignalLike;
  abort(): void;
}

export interface LinkConfig {
  port: number;
  token: string;
}

export interface LinkOptions {
  makeClient(config: LinkConfig): LinkClient;
  makeAbort(): AbortLike;
}

export interface LinkState {
  /** The event stream: what "no daemon" and "bad token" are read from. */
  stream: StreamState;
  /** The channel the daemon is speaking on, or "" when silent or unknown. */
  speakingChannel: string;
  /** Whether playback is paused. Global, like the verb. */
  paused: boolean;
  /** The listener's speed, once the daemon has said it. */
  speed: number | null;
}

export class Link {
  private readonly options: LinkOptions;
  private client: LinkClient | null = null;
  private abort: AbortLike | null = null;
  private readonly _state: LinkState = { stream: "no-daemon", speakingChannel: "", paused: false, speed: null };
  private readonly eventListeners = new Set<(event: SpeakdEvent) => void>();
  private readonly changeListeners = new Set<(state: LinkState) => void>();

  constructor(options: LinkOptions) {
    this.options = options;
  }

  get state(): Readonly<LinkState> {
    return this._state;
  }

  /** Connect with this port and token, dropping any connection made with others. */
  configure(config: LinkConfig): void {
    this.disconnect();
    const client = this.options.makeClient(config);
    const abort = this.options.makeAbort();
    this.client = client;
    this.abort = abort;
    // Everything the stream says is checked against the connection it came
    // from: an old stream, aborted but not yet unwound, must not speak for
    // the new one.
    const current = () => this.abort === abort;
    void client
      .events(
        (event) => {
          if (current()) this.event(event);
        },
        abort.signal,
        (stream) => {
          if (current()) this.streamState(client, stream);
        },
      )
      .catch(() => {});
  }

  /** Send a verb through the current connection. */
  call(verb: string, sourceId: string, payload: Record<string, unknown> = {}): Promise<CallResult> {
    if (this.client === null) return Promise.resolve({ kind: "no-daemon", error: "speakd is not configured" });
    return this.client.call(verb, sourceId, payload);
  }

  /** Hear every event on the stream. Returns an unsubscribe. */
  onEvent(listener: (event: SpeakdEvent) => void): () => void {
    this.eventListeners.add(listener);
    return () => this.eventListeners.delete(listener);
  }

  /** Hear the link's state change. Returns an unsubscribe. */
  onChange(listener: (state: LinkState) => void): () => void {
    this.changeListeners.add(listener);
    return () => this.changeListeners.delete(listener);
  }

  close(): void {
    this.disconnect();
    this.client = null;
    this.eventListeners.clear();
    this.changeListeners.clear();
  }

  private disconnect(): void {
    this.abort?.abort();
    this.abort = null;
    this.update({ stream: "no-daemon", speakingChannel: "" });
  }

  private streamState(client: LinkClient, stream: StreamState): void {
    if (stream !== "connected") {
      // Whoever was speaking, the link can no longer tell.
      this.update({ stream, speakingChannel: "" });
      return;
    }
    this.update({ stream });
    // The speed is only ever announced when it changes, so ask for it.
    void client.call("status", "", {}).then(
      (result) => {
        if (this.client !== client || result.kind !== "ok") return;
        const speed = result.data.speed;
        if (typeof speed === "number") this.update({ speed });
      },
      () => {},
    );
  }

  private event(event: SpeakdEvent): void {
    switch (event.event) {
      case "started":
        this.update({ speakingChannel: event.source_id });
        break;
      case "finished":
        if (this._state.speakingChannel === event.source_id) this.update({ speakingChannel: "" });
        break;
      case "transport":
        this.update({ paused: event.data.paused === true });
        break;
      case "speed":
        if (typeof event.data.speed === "number") this.update({ speed: event.data.speed });
        break;
    }
    for (const listener of [...this.eventListeners]) {
      try {
        listener(event);
      } catch {
        // One listener's failure is its own; every other still hears.
      }
    }
  }

  private update(change: Partial<LinkState>): void {
    let changed = false;
    for (const key of Object.keys(change) as (keyof LinkState)[]) {
      if (this._state[key] !== change[key]) {
        (this._state as unknown as Record<string, unknown>)[key] = change[key];
        changed = true;
      }
    }
    if (!changed) return;
    for (const listener of [...this.changeListeners]) {
      try {
        listener(this._state);
      } catch {
        // As for events.
      }
    }
  }
}
