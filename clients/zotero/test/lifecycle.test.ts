import { describe, expect, it } from "vitest";
import { Channel } from "../src/channel";
import { dismantle, hushOnQuit } from "../src/lifecycle";
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

describe("hushOnQuit", () => {
  it("sends a hush for every reader whose channel may hold something, without waiting on any", () => {
    const sent: string[] = [];
    const call = (verb: string, sourceId: string) => {
      sent.push(`${verb} ${sourceId}`);
      return new Promise<never>(() => {});
    };
    hushOnQuit(
      [
        { sourceId: "zotero:A", holding: true },
        { sourceId: "zotero:B", holding: false },
        { sourceId: "zotero:C", holding: true },
      ],
      call,
    );
    expect(sent).toEqual(["hush zotero:A", "hush zotero:C"]);
  });

  it("goes on to the next reader when a hush throws", () => {
    const sent: string[] = [];
    hushOnQuit(
      [
        { sourceId: "zotero:A", holding: true },
        { sourceId: "zotero:B", holding: true },
      ],
      (verb, sourceId) => {
        sent.push(sourceId);
        if (sourceId === "zotero:A") throw new Error("the link is gone");
        return Promise.resolve();
      },
    );
    expect(sent).toEqual(["zotero:A", "zotero:B"]);
  });
});

describe("Channel.holding", () => {
  it("is true from a start until the read ends or is hushed", async () => {
    const { channel } = setup();
    expect(channel.holding).toBe(false);
    await channel.start([{ text: "Zero is first." }], 0);
    expect(channel.holding).toBe(true);
    await channel.stop();
    expect(channel.holding).toBe(false);
  });
});
