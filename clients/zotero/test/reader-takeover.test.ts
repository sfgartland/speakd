import { describe, expect, it } from "vitest";
import { firstRectYOf, Takeover } from "../src/reader-takeover";

// The four label-mode entry points on a reader the plugin did not adopt: the
// fail-open no-ops of spec §14, the `dumpSegments` precedent. The Adopted
// side needs the reader internals and is exercised live (test-live).

function unadopted() {
  const takeover = new Takeover({ link: {} as never, log: () => {}, warn: () => {} });
  return takeover.handleFor({});
}

describe("an unadopted reader's label entry points", () => {
  it("refuses to enter label mode", async () => {
    expect(await unadopted().enterLabelMode()).toEqual({ ok: false, error: "this reader is not speakd's" });
  });

  it("exits label mode silently", () => {
    expect(() => unadopted().exitLabelMode()).not.toThrow();
  });

  it("refuses to label a segment", async () => {
    expect(await unadopted().labelSegment(0, "body")).toEqual({ ok: false, error: "this reader is not speakd's" });
  });

  it("refuses to export a fixture", async () => {
    expect(await unadopted().exportFixture("/tmp/x")).toEqual({ ok: false, error: "this reader is not speakd's" });
  });
});

describe("firstRectYOf", () => {
  it("reads null geometry from a segment whose rects is empty", () => {
    (globalThis as Record<string, unknown>).Components = { utils: { waiveXrays: (value: unknown) => value } };
    expect(firstRectYOf([])).toBeNull();
    expect(firstRectYOf(undefined)).toBeNull();
    expect(firstRectYOf([{ y: 12 }])).toBe(12);
    expect(firstRectYOf([[1, 7, 3, 4]])).toBe(7);
  });
});
