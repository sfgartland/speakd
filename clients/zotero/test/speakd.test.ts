import { describe, expect, it } from "vitest";
import { SpeakdClient, type FetchLike, type SpeakdEvent, type StreamState } from "../src/speakd";

const encoder = new TextEncoder();

interface Request {
  url: string;
  method: string;
  headers: Record<string, string>;
  body?: string;
}

// A stubbed fetch that answers each request with the next of `answers`, and
// records what it was asked. An answer is a Response, an Error to reject
// with, or "hang" for a request that is never answered.
function stubFetch(answers: (Response | Error | "hang" | (() => Response))[]) {
  const requests: Request[] = [];
  const fetch: FetchLike = async (url, init) => {
    requests.push({
      url,
      method: init?.method ?? "GET",
      headers: { ...(init?.headers as Record<string, string>) },
      body: init?.body,
    });
    const answer = answers.shift();
    if (answer === undefined || answer === "hang") return new Promise(() => {});
    if (answer instanceof Error) throw answer;
    return typeof answer === "function" ? answer() : answer;
  };
  return { fetch, requests };
}

function json(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

// An event stream delivering `chunks` as they are, then either ending or,
// with `hold`, staying open without another byte.
function stream(chunks: (string | Uint8Array)[], hold = false): Response {
  const queue = [...chunks];
  const body = new ReadableStream<Uint8Array>({
    pull(controller) {
      const next = queue.shift();
      if (next !== undefined) {
        controller.enqueue(typeof next === "string" ? encoder.encode(next) : next);
        return;
      }
      if (hold) return new Promise(() => {});
      controller.close();
    },
  });
  return new Response(body, { status: 200, headers: { "Content-Type": "text/event-stream" } });
}

const sse = (event: SpeakdEvent): string => `data: ${JSON.stringify(event)}\n\n`;

function client(fetch: FetchLike, options: Partial<ConstructorParameters<typeof SpeakdClient>[0]> = {}) {
  return new SpeakdClient({
    port: 8642,
    token: "sekrit",
    fetch,
    timeoutMs: 50,
    idleMs: 200,
    backoff: { initialMs: 1, maxMs: 4 },
    ...options,
  });
}

describe("call", () => {
  it("posts the verb with the token, and answers the daemon's data", async () => {
    const { fetch, requests } = stubFetch([json(200, { ok: true, data: { spoken: true }, error: "" })]);
    const result = await client(fetch).call("enqueue", "zotero:ABC", { text: "Hi.", profile: "pdf" });
    expect(result).toEqual({ kind: "ok", data: { spoken: true } });
    expect(requests).toHaveLength(1);
    const [request] = requests;
    expect(request!.url).toBe("http://127.0.0.1:8642/v1/enqueue");
    expect(request!.method).toBe("POST");
    expect(request!.headers.Authorization).toBe("Bearer sekrit");
    expect(JSON.parse(request!.body!)).toEqual({ source_id: "zotero:ABC", payload: { text: "Hi.", profile: "pdf" } });
  });

  it("tells a refusal by the daemon from a failure to reach it", async () => {
    const { fetch } = stubFetch([json(200, { ok: false, data: {}, error: "nothing is speaking" })]);
    expect(await client(fetch).call("seek", "zotero:ABC", { by: 1 })).toEqual({
      kind: "refused",
      error: "nothing is speaking",
      data: {},
    });
  });

  it("says a 401 is a bad token", async () => {
    const { fetch } = stubFetch([json(401, { ok: false, data: {}, error: "missing or wrong token" })]);
    expect((await client(fetch).call("status", "")).kind).toBe("bad-token");
  });

  it("says a refused connection is no daemon", async () => {
    const { fetch } = stubFetch([new TypeError("NetworkError when attempting to fetch resource.")]);
    const result = await client(fetch).call("status", "");
    expect(result.kind).toBe("no-daemon");
  });

  it("says a port that never answers is no daemon, rather than waiting on it", async () => {
    const { fetch } = stubFetch(["hang"]);
    const result = await client(fetch).call("status", "");
    expect(result.kind).toBe("no-daemon");
  });

  it("reports any other status as an HTTP error, with the daemon's reason", async () => {
    const { fetch } = stubFetch([json(421, { ok: false, data: {}, error: "127.0.0.1 and localhost only" })]);
    expect(await client(fetch).call("status", "")).toEqual({
      kind: "http-error",
      status: 421,
      error: "127.0.0.1 and localhost only",
    });
  });

  it("reports an answer that is not JSON as an HTTP error", async () => {
    const { fetch } = stubFetch([new Response("<html>", { status: 200 })]);
    expect((await client(fetch).call("status", "")).kind).toBe("http-error");
  });
});

describe("health", () => {
  it("gets /v1/health with the token", async () => {
    const { fetch, requests } = stubFetch([json(200, { ok: true })]);
    expect((await client(fetch).health()).kind).toBe("ok");
    expect(requests[0]!.url).toBe("http://127.0.0.1:8642/v1/health");
    expect(requests[0]!.headers.Authorization).toBe("Bearer sekrit");
  });
});

// Run `events` until `until` says it has seen enough, then abort it.
async function collect(
  fetch: FetchLike,
  until: (events: SpeakdEvent[], states: StreamState[]) => boolean,
  options: Partial<ConstructorParameters<typeof SpeakdClient>[0]> = {},
) {
  const events: SpeakdEvent[] = [];
  const states: StreamState[] = [];
  const controller = new AbortController();
  const done = client(fetch, options).events(
    (event) => {
      events.push(event);
      if (until(events, states)) controller.abort();
    },
    controller.signal,
    (state) => {
      states.push(state);
      if (until(events, states)) controller.abort();
    },
  );
  const deadline = setTimeout(() => controller.abort(), 2000);
  await done;
  clearTimeout(deadline);
  return { events, states };
}

describe("events", () => {
  const started: SpeakdEvent = {
    event: "started",
    source_id: "zotero:ABC",
    data: { text: "Grüße. Zwei.", segments: [{ index: 0, text: "Grüße.", span_start: 0, span_end: 6 }] },
  };
  const position: SpeakdEvent = { event: "position", source_id: "zotero:ABC", data: { index: 0 } };

  it("parses events split anywhere across chunks, ignoring comments", async () => {
    const whole = `: speakd events\n\n${sse(started)}: ping\n\n${sse(position)}`;
    const bytes = encoder.encode(whole);
    // One byte at a time: every boundary there is, including inside "data:",
    // inside the JSON and inside the two bytes of "ü".
    const chunks = Array.from(bytes, (byte) => new Uint8Array([byte]));
    const { fetch, requests } = stubFetch([stream(chunks, true)]);
    const { events } = await collect(fetch, (seen) => seen.length === 2);
    expect(events).toEqual([started, position]);
    expect(requests[0]!.url).toBe("http://127.0.0.1:8642/v1/events");
    expect(requests[0]!.headers.Authorization).toBe("Bearer sekrit");
  });

  it("accepts CRLF line ends", async () => {
    const { fetch } = stubFetch([stream([sse(position).replaceAll("\n", "\r\n")], true)]);
    const { events } = await collect(fetch, (seen) => seen.length === 1);
    expect(events).toEqual([position]);
  });

  it("skips a malformed event and keeps reading", async () => {
    const { fetch } = stubFetch([stream(["data: {not json\n\n", "data: 42\n\n", sse(position)], true)]);
    const { events } = await collect(fetch, (seen) => seen.length === 1);
    expect(events).toEqual([position]);
  });

  it("keeps reading when a listener throws", async () => {
    const { fetch } = stubFetch([stream([sse(started), sse(position)], true)]);
    const seen: SpeakdEvent[] = [];
    const controller = new AbortController();
    await client(fetch).events((event) => {
      seen.push(event);
      if (seen.length === 2) controller.abort();
      throw new Error("a listener's bug");
    }, controller.signal);
    expect(seen).toEqual([started, position]);
  });

  it("reconnects when the stream ends, and says so", async () => {
    const { fetch, requests } = stubFetch([stream([sse(started)]), stream([sse(position)], true)]);
    const { events, states } = await collect(fetch, (seen) => seen.length === 2);
    expect(events).toEqual([started, position]);
    expect(requests).toHaveLength(2);
    expect(states).toEqual(["connecting", "connected", "no-daemon", "connecting", "connected"]);
  });

  it("says no daemon while nothing answers, and keeps trying", async () => {
    const refused = new TypeError("NetworkError");
    const { fetch, requests } = stubFetch([refused, refused, stream([sse(position)], true)]);
    const { events, states } = await collect(fetch, (seen) => seen.length === 1);
    expect(events).toEqual([position]);
    expect(requests).toHaveLength(3);
    expect(states.filter((state) => state === "no-daemon")).toHaveLength(2);
  });

  it("says a bad token rather than hanging", async () => {
    const { fetch } = stubFetch([json(401, { ok: false, data: {}, error: "wrong token" })]);
    const { states } = await collect(fetch, (_, seen) => seen.includes("bad-token"));
    expect(states).toEqual(["connecting", "bad-token"]);
  });

  it("gives up on a connection that never answers", async () => {
    const { fetch } = stubFetch(["hang"]);
    const { states } = await collect(fetch, (_, seen) => seen.includes("no-daemon"));
    expect(states).toEqual(["connecting", "no-daemon"]);
  });

  it("reconnects when an open stream goes silent past its pings", async () => {
    const { fetch, requests } = stubFetch([stream([": speakd events\n\n"], true), stream([sse(position)], true)]);
    const { events } = await collect(fetch, (seen) => seen.length === 1, { idleMs: 30 });
    expect(events).toEqual([position]);
    expect(requests).toHaveLength(2);
  });

  it("stops when aborted, with the stream open", async () => {
    const { fetch, requests } = stubFetch([stream([sse(position)], true)]);
    const { events } = await collect(fetch, (seen) => seen.length === 1);
    expect(events).toHaveLength(1);
    expect(requests).toHaveLength(1);
  });

  it("does nothing when aborted before it starts", async () => {
    const { fetch, requests } = stubFetch([]);
    const controller = new AbortController();
    controller.abort();
    await client(fetch).events(() => {}, controller.signal);
    expect(requests).toHaveLength(0);
  });
});
