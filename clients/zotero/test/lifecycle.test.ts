import { describe, expect, it } from "vitest";
import { Channel } from "../src/channel";
import { dismantle } from "../src/lifecycle";
import { Link, type LinkClient } from "../src/link";
import type { AbortSignalLike, CallResult } from "../src/speakd";

const SOURCE = "zotero:ABC123";

// A daemon that answers every verb on a later turn, as a real request is.
class SlowClient implements LinkClient {
  verbs: string[] = [];
  async call(verb: string): Promise<CallResult> {
    this.verbs.push(verb);
    await new Promise((resolve) => setTimeout(resolve, 0));
    return verb === "enqueue" ? { kind: "ok", data: { spoken: true } } : { kind: "ok", data: {} };
  }
  events(_onEvent: unknown, signal: AbortSignalLike): Promise<void> {
    return new Promise((resolve) => signal.addEventListener("abort", () => resolve()));
  }
}

function setup() {
  const client = new SlowClient();
  const link = new Link({
    makeClient: () => client,
    makeAbort: () => {
      const listeners: (() => void)[] = [];
      const signal: AbortSignalLike = {
        aborted: false,
        addEventListener: (_type, listener) => listeners.push(listener),
        removeEventListener: () => {},
      };
      return { signal, abort: () => listeners.forEach((listener) => listener()) };
    },
  });
  link.configure({ port: 8642, token: "t" });
  const channel = new Channel({ client: link, sourceId: SOURCE, label: "A Paper" });
  const parts = {
    unobserve: () => {},
    controls: { unregister: () => {} },
    // What Takeover.uninstall does for a reader mid-read: stop it, and hand
    // back what that queued.
    takeover: {
      uninstall: () => {
        void channel.stop();
        return channel.idle();
      },
    },
    link,
  };
  return { client, link, channel, parts };
}

describe("dismantle", () => {
  it("lets a reader's hush reach speakd before the link is closed", async () => {
    const { client, channel, parts } = setup();
    await channel.start([{ text: "Zero is first." }, { text: "One follows." }], 0);
    expect(client.verbs).toEqual(["set_label", "enqueue"]);
    await dismantle(parts);
    await new Promise((resolve) => setTimeout(resolve, 10));
    expect(client.verbs).toEqual(["set_label", "enqueue", "hush"]);
  });

  it("closes the link even when a reader's last words never get an answer", async () => {
    const { link, parts } = setup();
    let closed = false;
    const close = link.close.bind(link);
    link.close = () => {
      closed = true;
      close();
    };
    await dismantle({ ...parts, takeover: { uninstall: () => new Promise<void>(() => {}) } }, 20);
    expect(closed).toBe(true);
  });
});
