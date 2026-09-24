// The client for speakd's loopback HTTP transport (src/speakd/transport_http.py).
//
// Verbs are POSTed to /v1/<verb> and answered {ok, data, error}; the event
// stream is GET /v1/events, one `data:` line of JSON per event. Every request
// carries the token the user pasted into the preferences.
//
// `fetch` is injected, and is the only way out of this module. Zotero's
// plugin sandbox has `fetch` but not `EventSource`, so the stream is read off
// a fetch body; and injecting it is what lets the tests replay a daemon's
// answers chunk by chunk.
//
// Nothing here may hang. A wrong port or token has to become a state the
// control bar can show -- "no daemon", "bad token" -- within a few seconds,
// not a request that waits for ever with the bar still offering "play".

/** The part of `fetch`'s init this client uses. */
export interface FetchInit {
  method?: string;
  headers?: Record<string, string>;
  body?: string;
  signal?: AbortSignalLike;
}

/** The part of a fetch `Response` this client uses. */
export interface ResponseLike {
  readonly status: number;
  readonly body: ReadableStream<Uint8Array> | null;
  text(): Promise<string>;
}

export type FetchLike = (url: string, init?: FetchInit) => Promise<ResponseLike>;

/**
 * An abort signal, as far as this client needs one. The sandbox has no
 * `AbortController` of its own, so the caller brings one from a window.
 */
export interface AbortSignalLike {
  readonly aborted: boolean;
  addEventListener(type: "abort", listener: () => void): void;
  removeEventListener(type: "abort", listener: () => void): void;
}

interface Timers {
  setTimeout(callback: () => void, ms: number): unknown;
  clearTimeout(handle: unknown): void;
}

export interface ClientOptions {
  port: number;
  token: string;
  fetch: FetchLike;
  /** How long a verb, or the stream's first answer, may take before the daemon counts as absent. */
  timeoutMs?: number;
  /**
   * How long an open stream may go without a byte before it is taken for
   * dead. The daemon pings every 15 seconds, so three missed pings.
   */
  idleMs?: number;
  /** The wait between reconnection attempts, doubling from `initialMs` up to `maxMs`. */
  backoff?: { initialMs: number; maxMs: number };
  timers?: Timers;
}

/** Everything a verb can come back as, so each caller has to decide about each. */
export type CallResult =
  | { kind: "ok"; data: Record<string, unknown> }
  /** The daemon heard and said no: "nothing is speaking", "enqueue needs non-empty text". */
  | { kind: "refused"; error: string; data: Record<string, unknown> }
  /** 401: the token in the preferences is not the daemon's. */
  | { kind: "bad-token"; error: string }
  /** Nothing answered on the port, or not in time. */
  | { kind: "no-daemon"; error: string }
  /** Something answered, but not as speakd does. */
  | { kind: "http-error"; status: number; error: string };

/** One event, as the daemon's bus publishes it. */
export interface SpeakdEvent {
  event: string;
  source_id: string;
  data: Record<string, unknown>;
}

export type StreamState = "connecting" | "connected" | "no-daemon" | "bad-token";

const TIMED_OUT = Symbol("timed out");

export class SpeakdClient {
  private readonly base: string;
  private readonly token: string;
  private readonly fetch: FetchLike;
  private readonly timeoutMs: number;
  private readonly idleMs: number;
  private readonly backoff: { initialMs: number; maxMs: number };
  private readonly timers: Timers;

  constructor(options: ClientOptions) {
    // 127.0.0.1 rather than localhost: the daemon checks the Host header
    // against both, but only this one cannot resolve to an IPv6 address the
    // daemon is not listening on.
    this.base = `http://127.0.0.1:${options.port}/v1`;
    this.token = options.token;
    this.fetch = options.fetch;
    this.timeoutMs = options.timeoutMs ?? 5000;
    this.idleMs = options.idleMs ?? 45_000;
    this.backoff = options.backoff ?? { initialMs: 500, maxMs: 10_000 };
    this.timers = options.timers ?? {
      setTimeout: (callback, ms) => globalThis.setTimeout(callback, ms),
      clearTimeout: (handle) => globalThis.clearTimeout(handle as ReturnType<typeof setTimeout>),
    };
  }

  /** Send one verb for `sourceId`, the channel it acts on. */
  async call(verb: string, sourceId: string, payload: Record<string, unknown> = {}): Promise<CallResult> {
    return this.request(`${this.base}/${verb}`, {
      method: "POST",
      headers: { ...this.auth(), "Content-Type": "application/json" },
      body: JSON.stringify({ source_id: sourceId, payload }),
    });
  }

  /** Whether the daemon is there and takes the token: the cheap check for the control bar. */
  async health(): Promise<CallResult> {
    return this.request(`${this.base}/health`, { headers: this.auth() });
  }

  /**
   * Deliver every event on the daemon's stream to `onEvent` until `signal`
   * aborts, reconnecting whenever the stream ends, fails or goes silent,
   * with a growing wait between attempts. `onState` hears what the
   * connection is doing, for the bar. Resolves once aborted; never rejects.
   */
  async events(
    onEvent: (event: SpeakdEvent) => void,
    signal: AbortSignalLike,
    onState: (state: StreamState) => void = () => {},
  ): Promise<void> {
    let delay = this.backoff.initialMs;
    while (!signal.aborted) {
      onState("connecting");
      const outcome = await this.stream(onEvent, signal, onState);
      if (signal.aborted) return;
      if (outcome === "was-connected") delay = this.backoff.initialMs;
      onState(outcome === "bad-token" ? "bad-token" : "no-daemon");
      await this.sleep(delay, signal);
      delay = Math.min(delay * 2, this.backoff.maxMs);
    }
  }

  private auth(): Record<string, string> {
    return { Authorization: `Bearer ${this.token}` };
  }

  private async request(url: string, init: FetchInit): Promise<CallResult> {
    let response: ResponseLike | typeof TIMED_OUT;
    let text: string | typeof TIMED_OUT;
    try {
      response = await this.withTimeout(this.fetch(url, init));
      if (response === TIMED_OUT) return { kind: "no-daemon", error: `no answer in ${this.timeoutMs} ms` };
      text = await this.withTimeout(response.text());
      if (text === TIMED_OUT) return { kind: "no-daemon", error: `no answer in ${this.timeoutMs} ms` };
    } catch (error) {
      return { kind: "no-daemon", error: String(error) };
    }
    const body = parseAnswer(text);
    if (response.status === 401) {
      return { kind: "bad-token", error: body?.error || "missing or wrong token" };
    }
    if (response.status !== 200 || body === null) {
      return {
        kind: "http-error",
        status: response.status,
        error: body?.error || `HTTP ${response.status} is not an answer speakd gives`,
      };
    }
    if (body.ok) return { kind: "ok", data: body.data };
    return { kind: "refused", error: body.error, data: body.data };
  }

  /**
   * One connection to the event stream, read until it ends. The outcome
   * says whether it ever got as far as a 200, which is what resets the
   * backoff: a daemon that restarts should be reconnected to at once, one
   * that is not there should not be hammered.
   */
  private async stream(
    onEvent: (event: SpeakdEvent) => void,
    signal: AbortSignalLike,
    onState: (state: StreamState) => void,
  ): Promise<"was-connected" | "no-daemon" | "bad-token"> {
    let response: ResponseLike | typeof TIMED_OUT;
    try {
      response = await this.withTimeout(this.fetch(`${this.base}/events`, { headers: this.auth(), signal }));
    } catch {
      return "no-daemon";
    }
    if (response === TIMED_OUT) return "no-daemon";
    if (response.status === 401) return "bad-token";
    if (response.status !== 200 || response.body === null) return "no-daemon";

    onState("connected");
    const reader = response.body.getReader();
    const cancel = (): void => {
      reader.cancel().catch(() => {});
    };
    signal.addEventListener("abort", cancel);
    if (signal.aborted) cancel();
    const decoder = new TextDecoder();
    const parser = new SseParser();
    try {
      for (;;) {
        const chunk: ReadableStreamReadResult<Uint8Array> | typeof TIMED_OUT = await this.withTimeout(
          reader.read(),
          this.idleMs,
        );
        if (chunk === TIMED_OUT) {
          // Silent past the daemon's pings: a half-open connection, or a
          // daemon that died without closing it. Only a new one can tell.
          cancel();
          break;
        }
        if (chunk.done) break;
        for (const event of parser.push(decoder.decode(chunk.value, { stream: true }))) {
          try {
            onEvent(event);
          } catch {
            // A listener's bug must not cost every other listener the
            // stream; the listener reports its own failures.
          }
        }
      }
    } catch {
      // Cancelled or broken mid-read: either way, this connection is over.
    } finally {
      signal.removeEventListener("abort", cancel);
    }
    return "was-connected";
  }

  /** `promise`, or TIMED_OUT once `ms` pass first. */
  private withTimeout<T>(promise: Promise<T>, ms = this.timeoutMs): Promise<T | typeof TIMED_OUT> {
    let handle: unknown;
    const timeout = new Promise<typeof TIMED_OUT>((resolve) => {
      handle = this.timers.setTimeout(() => resolve(TIMED_OUT), ms);
    });
    return Promise.race([promise, timeout]).finally(() => this.timers.clearTimeout(handle));
  }

  /** Wait `ms`, or less if `signal` aborts first. */
  private sleep(ms: number, signal: AbortSignalLike): Promise<void> {
    return new Promise((resolve) => {
      const wake = (): void => {
        this.timers.clearTimeout(handle);
        signal.removeEventListener("abort", wake);
        resolve();
      };
      const handle = this.timers.setTimeout(wake, ms);
      signal.addEventListener("abort", wake);
    });
  }
}

/** The daemon's {ok, data, error}, or null for anything else. */
function parseAnswer(text: string): { ok: boolean; data: Record<string, unknown>; error: string } | null {
  let body: unknown;
  try {
    body = JSON.parse(text);
  } catch {
    return null;
  }
  if (typeof body !== "object" || body === null) return null;
  const { ok, data, error } = body as Record<string, unknown>;
  if (typeof ok !== "boolean") return null;
  return {
    ok,
    data: typeof data === "object" && data !== null ? (data as Record<string, unknown>) : {},
    error: typeof error === "string" ? error : "",
  };
}

/**
 * Server-sent events, as far as speakd sends them: `data:` lines, a blank
 * line ending each event, and `:` comments for the greeting and the pings.
 * Text arrives in chunks that may split a line anywhere, so an unfinished
 * line waits for the next chunk.
 */
export class SseParser {
  private buffer = "";
  private data: string[] = [];

  push(chunk: string): SpeakdEvent[] {
    this.buffer += chunk;
    const lines = this.buffer.split("\n");
    this.buffer = lines.pop() ?? "";
    const events: SpeakdEvent[] = [];
    for (const raw of lines) {
      const line = raw.endsWith("\r") ? raw.slice(0, -1) : raw;
      if (line === "") {
        const event = this.dispatch();
        if (event !== null) events.push(event);
      } else if (line.startsWith("data:")) {
        const value = line.slice(5);
        this.data.push(value.startsWith(" ") ? value.slice(1) : value);
      }
      // Comments, and any field speakd does not send, are ignored.
    }
    return events;
  }

  private dispatch(): SpeakdEvent | null {
    if (this.data.length === 0) return null;
    const text = this.data.join("\n");
    this.data = [];
    let value: unknown;
    try {
      value = JSON.parse(text);
    } catch {
      return null;
    }
    if (typeof value !== "object" || value === null) return null;
    const { event, source_id, data } = value as Record<string, unknown>;
    if (typeof event !== "string") return null;
    return {
      event,
      source_id: typeof source_id === "string" ? source_id : "",
      data: typeof data === "object" && data !== null ? (data as Record<string, unknown>) : {},
    };
  }
}
