import { describe, expect, it } from "vitest";
import { Link, type LinkClient, type LinkState } from "../src/link";
import type { AbortSignalLike, CallResult, SpeakdEvent, StreamState } from "../src/speakd";

class FakeSignal implements AbortSignalLike {
  aborted = false;
  private listeners = new Set<() => void>();
  addEventListener(_type: "abort", listener: () => void): void {
    this.listeners.add(listener);
  }
  removeEventListener(_type: "abort", listener: () => void): void {
    this.listeners.delete(listener);
  }
  abort(): void {
    this.aborted = true;
    for (const listener of this.listeners) listener();
  }
}

// A client whose event stream the test drives by hand.
class FakeClient implements LinkClient {
  calls: { verb: string; sourceId: string; payload: Record<string, unknown> }[] = [];
  onEvent: ((event: SpeakdEvent) => void) | null = null;
  onState: ((state: StreamState) => void) | null = null;
  signal: AbortSignalLike | null = null;
  status: CallResult = { kind: "ok", data: { speed: 1.2 } };

  constructor(readonly config: { port: number; token: string }) {}

  async call(verb: string, sourceId: string, payload: Record<string, unknown> = {}): Promise<CallResult> {
    this.calls.push({ verb, sourceId, payload });
    return verb === "status" ? this.status : { kind: "ok", data: {} };
  }

  events(
    onEvent: (event: SpeakdEvent) => void,
    signal: AbortSignalLike,
    onState: (state: StreamState) => void = () => {},
  ): Promise<void> {
    this.onEvent = onEvent;
    this.onState = onState;
    this.signal = signal;
    return new Promise((resolve) => signal.addEventListener("abort", () => resolve()));
  }

  emit(event: string, data: Record<string, unknown> = {}, source_id = ""): void {
    this.onEvent?.({ event, source_id, data });
  }
}

function setup() {
  const clients: FakeClient[] = [];
  const link = new Link({
    makeClient: (config) => {
      const client = new FakeClient(config);
      clients.push(client);
      return client;
    },
    makeAbort: () => {
      const signal = new FakeSignal();
      return { signal, abort: () => signal.abort() };
    },
  });
  const states: LinkState[] = [];
  link.onChange((state) => states.push({ ...state }));
  return { link, clients, states };
}

const flush = () => new Promise((resolve) => setTimeout(resolve, 0));

describe("Link", () => {
  it("is not connected before it is configured, and says no daemon", () => {
    const { link } = setup();
    expect(link.state.stream).toBe("no-daemon");
  });

  it("follows the stream's state, and asks for the speed once connected", async () => {
    const { link, clients } = setup();
    link.configure({ port: 8642, token: "t" });
    const client = clients[0]!;
    expect(client.config).toEqual({ port: 8642, token: "t" });
    client.onState!("connecting");
    expect(link.state.stream).toBe("connecting");
    client.onState!("connected");
    await flush();
    expect(link.state.stream).toBe("connected");
    expect(client.calls.map((call) => call.verb)).toEqual(["status"]);
    expect(link.state.speed).toBe(1.2);
    client.onState!("bad-token");
    expect(link.state.stream).toBe("bad-token");
  });

  it("says connecting until the first answer, then keeps its verdict while it retries", () => {
    const { link, clients } = setup();
    link.configure({ port: 1, token: "t" });
    expect(link.state.stream).toBe("connecting");
    const client = clients[0]!;
    client.onState!("connecting");
    client.onState!("no-daemon");
    client.onState!("connecting");
    expect(link.state.stream).toBe("no-daemon");
    client.onState!("bad-token");
    client.onState!("connecting");
    expect(link.state.stream).toBe("bad-token");
    client.onState!("connected");
    expect(link.state.stream).toBe("connected");
  });

  it("knows which channel is speaking, whether playback is paused, and the speed", () => {
    const { link, clients } = setup();
    link.configure({ port: 1, token: "t" });
    const client = clients[0]!;
    client.onState!("connected");
    client.emit("started", {}, "zotero:A");
    expect(link.state.speakingChannel).toBe("zotero:A");
    client.emit("transport", { paused: true });
    expect(link.state.paused).toBe(true);
    client.emit("speed", { speed: 0.9 });
    expect(link.state.speed).toBe(0.9);
    // Another channel finishing leaves this one speaking.
    client.emit("finished", {}, "agent:x");
    expect(link.state.speakingChannel).toBe("zotero:A");
    client.emit("finished", {}, "zotero:A");
    expect(link.state.speakingChannel).toBe("");
  });

  it("forgets who was speaking when the stream is lost", () => {
    const { link, clients } = setup();
    link.configure({ port: 1, token: "t" });
    const client = clients[0]!;
    client.onState!("connected");
    client.emit("started", {}, "zotero:A");
    client.onState!("no-daemon");
    expect(link.state.speakingChannel).toBe("");
  });

  it("hands every event to its listeners, a failing one costing the others nothing", () => {
    const { link, clients } = setup();
    const heard: string[] = [];
    link.onEvent(() => {
      throw new Error("a listener's bug");
    });
    const off = link.onEvent((event) => heard.push(event.event));
    link.configure({ port: 1, token: "t" });
    clients[0]!.emit("queued");
    off();
    clients[0]!.emit("queued");
    expect(heard).toEqual(["queued"]);
  });

  it("starts over with a new client when reconfigured, and drops the old stream", () => {
    const { link, clients } = setup();
    link.configure({ port: 1, token: "a" });
    link.configure({ port: 2, token: "b" });
    expect(clients).toHaveLength(2);
    expect(clients[0]!.signal!.aborted).toBe(true);
    expect(clients[1]!.signal!.aborted).toBe(false);
    // The old stream's late words are not heard.
    const heard: string[] = [];
    link.onEvent((event) => heard.push(event.event));
    clients[0]!.emit("queued");
    clients[0]!.onState!("connected");
    expect(heard).toEqual([]);
    expect(link.state.stream).not.toBe("connected");
  });

  it("sends verbs through the current client", async () => {
    const { link, clients } = setup();
    link.configure({ port: 1, token: "a" });
    await link.call("hush", "zotero:A", {});
    expect(clients[0]!.calls).toEqual([{ verb: "hush", sourceId: "zotero:A", payload: {} }]);
  });

  it("answers no daemon for a verb before it is configured", async () => {
    const { link } = setup();
    expect((await link.call("hush", "zotero:A", {})).kind).toBe("no-daemon");
  });

  it("stops the stream when closed", () => {
    const { link, clients } = setup();
    link.configure({ port: 1, token: "a" });
    link.close();
    expect(clients[0]!.signal!.aborted).toBe(true);
    expect(link.state.stream).toBe("no-daemon");
  });
});
