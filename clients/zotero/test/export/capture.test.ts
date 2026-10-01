import { describe, expect, it } from "vitest";
import type { Adoption } from "../../src/bar";
import { ADOPTION_WAIT_MS, captureSegments, type CaptureHandle, type CaptureHost } from "../../src/export/capture";
import type { CaptureResult } from "../../src/session";

const SEGMENTS: CaptureResult = {
  ok: true,
  segments: [
    { text: "A title", pageIndex: 0, anchor: "paragraphStart" },
    { text: "A sentence.", pageIndex: 0, anchor: null },
  ],
};

class FakeHandle implements CaptureHandle {
  adoption: Adoption;
  captures = 0;
  answer: () => Promise<CaptureResult> = async () => SEGMENTS;
  private listeners = new Set<() => void>();

  constructor(adoption: Adoption = { kind: "ready" }) {
    this.adoption = adoption;
  }
  capture(): Promise<CaptureResult> {
    this.captures++;
    return this.answer();
  }
  onChange(listener: () => void): () => void {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }
  adopt(adoption: Adoption): void {
    this.adoption = adoption;
    for (const listener of [...this.listeners]) listener();
  }
  get listening(): number {
    return this.listeners.size;
  }
}

interface Reader {
  name: string;
}

function setup(options: { open?: Reader; handle?: FakeHandle; openHandle?: FakeHandle } = {}) {
  const handle = options.handle ?? new FakeHandle();
  const timers: { task: () => void; ms: number }[] = [];
  const log: string[] = [];
  const host: CaptureHost<Reader> & { failOpen: boolean } = {
    failOpen: false,
    find: (itemID) => {
      log.push(`find ${itemID}`);
      return options.open ?? null;
    },
    open: async (itemID) => {
      log.push(`open ${itemID}`);
      if (host.failOpen) throw new Error("no such item");
      return { name: "background" };
    },
    close: (reader) => log.push(`close ${reader.name}`),
    handleFor: (reader) => (reader.name === "background" ? handle : (options.openHandle ?? handle)),
    later: (task, ms) => {
      const timer = { task, ms };
      timers.push(timer);
      return () => timers.splice(timers.indexOf(timer), 1);
    },
  };
  const elapse = () => {
    for (const timer of timers.splice(0)) timer.task();
  };
  return { handle, host, log, timers, elapse };
}

// Lets the awaits inside captureSegments run.
const settle = () => new Promise((resolve) => setTimeout(resolve, 0));

describe("captureSegments", () => {
  it("reuses a reader that is open already, and leaves it open", async () => {
    const { host, handle, log } = setup({ open: { name: "user's tab" } });
    expect(await captureSegments(7, host)).toEqual(SEGMENTS);
    expect(log).toEqual(["find 7"]);
    expect(handle.captures).toBe(1);
  });

  it("opens the PDF in the background when none is open, and closes it once captured", async () => {
    const { host, log } = setup();
    expect(await captureSegments(7, host)).toEqual(SEGMENTS);
    expect(log).toEqual(["find 7", "open 7", "close background"]);
  });

  it("closes the tab it opened when the capture finds no text", async () => {
    const { host, handle, log } = setup();
    const noText: CaptureResult = { ok: false, problem: { kind: "empty", error: "this document has no text to read" } };
    handle.answer = async () => noText;
    expect(await captureSegments(7, host)).toEqual(noText);
    expect(log.at(-1)).toBe("close background");
  });

  it("closes the tab it opened even when the capture throws", async () => {
    const { host, handle, log } = setup();
    handle.answer = () => Promise.reject(new Error("dead wrapper"));
    const result = await captureSegments(7, host);
    expect(result.ok).toBe(false);
    expect(log.at(-1)).toBe("close background");
  });

  it("waits for the reader it opened to be adopted before capturing", async () => {
    const handle = new FakeHandle({ kind: "pending" });
    const { host, log, timers } = setup({ handle });
    const captured = captureSegments(7, host);
    await settle();
    expect(handle.captures).toBe(0);
    expect(timers.map((timer) => timer.ms)).toEqual([ADOPTION_WAIT_MS]);
    handle.adopt({ kind: "ready" });
    expect(await captured).toEqual(SEGMENTS);
    expect(handle.listening).toBe(0);
    expect(timers).toEqual([]);
    expect(log.at(-1)).toBe("close background");
  });

  it("says why when the reader is not one the plugin can read", async () => {
    const handle = new FakeHandle({ kind: "pending" });
    const { host, log } = setup({ handle });
    const captured = captureSegments(7, host);
    await settle();
    handle.adopt({ kind: "refused", reason: "missing manager._voice" });
    expect(await captured).toEqual({ ok: false, problem: { kind: "zotero", error: "missing manager._voice" } });
    expect(handle.captures).toBe(0);
    expect(log.at(-1)).toBe("close background");
  });

  it("gives up when the reader it opened is never ready", async () => {
    const handle = new FakeHandle({ kind: "pending" });
    const { host, log, elapse } = setup({ handle });
    const captured = captureSegments(7, host);
    await settle();
    elapse();
    const result = await captured;
    expect(result.ok).toBe(false);
    expect(handle.captures).toBe(0);
    expect(handle.listening).toBe(0);
    expect(log.at(-1)).toBe("close background");
  });

  it("says so when the PDF cannot be opened, and closes nothing", async () => {
    const { host, log } = setup();
    host.failOpen = true;
    const result = await captureSegments(7, host);
    expect(result.ok).toBe(false);
    expect(log).toEqual(["find 7", "open 7"]);
  });

  it("opens a tab of its own when the open one was opened before the plugin started", async () => {
    // Zotero restores its tabs before plugins start: those readers stay Zotero's own.
    const openHandle = new FakeHandle({ kind: "unadopted" });
    const { host, handle, log } = setup({ open: { name: "restored tab" }, openHandle });
    expect(await captureSegments(7, host)).toEqual(SEGMENTS);
    expect(openHandle.captures).toBe(0);
    expect(handle.captures).toBe(1);
    expect(log).toEqual(["find 7", "open 7", "close background"]);
  });

  it("says why when the open reader is one the plugin refused, rather than opening another like it", async () => {
    const openHandle = new FakeHandle({ kind: "refused", reason: "speakd reads PDFs only" });
    const { host, log } = setup({ open: { name: "epub" }, openHandle });
    expect(await captureSegments(7, host)).toEqual({ ok: false, problem: { kind: "zotero", error: "speakd reads PDFs only" } });
    expect(openHandle.captures).toBe(0);
    expect(log).toEqual(["find 7"]);
  });
});
