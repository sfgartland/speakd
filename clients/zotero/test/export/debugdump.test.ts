import { describe, expect, it } from "vitest";
import { dumpSegments } from "../../src/export/debugdump";
import type { CapturedSegment } from "../../src/session";

function seg(
  text: string,
  pageIndex: number,
  anchor: string | null = null,
  rectCount: number | null = null,
  firstRectY: number | null = null,
): CapturedSegment {
  return { text, pageIndex, anchor, rectCount, firstRectY };
}

function lines(dump: string) {
  return dump.split("\n").filter((line) => line !== "");
}

describe("dumpSegments", () => {
  it("writes one JSON line per segment: index, text, page, anchor and geometry", () => {
    const dump = dumpSegments([
      seg("Body sentence.", 0, "paragraphStart", 3, 712.4),
      seg("It continues.", 0, null, 2, 760.1),
    ]);
    const [first, second] = lines(dump);
    expect(JSON.parse(first!)).toEqual({
      index: 0,
      text: "Body sentence.",
      pageIndex: 0,
      anchor: "paragraphStart",
      rectCount: 3,
      firstRectY: 712.4,
    });
    expect(JSON.parse(second!)).toEqual({
      index: 1,
      text: "It continues.",
      pageIndex: 0,
      anchor: null,
      rectCount: 2,
      firstRectY: 760.1,
    });
  });

  it("closes with one summary line per page: segments, how many had rects, min/max firstRectY", () => {
    const dump = dumpSegments([
      seg("Head.", 0, "paragraphStart", 1, 81.2),
      seg("Foot 1.", 0, "paragraphStart", 4, 750.5),
      seg("Foot 2.", 0, null, 2, 791.9),
      seg("Next page.", 1, "paragraphStart", 1, 90.0),
    ]);
    const entries = lines(dump).map((line) => JSON.parse(line));
    expect(entries).toHaveLength(6); // four segments, two page summaries
    expect(entries[4]).toEqual({
      page: 0,
      segments: 3,
      withRects: 3,
      minFirstRectY: 81.2,
      maxFirstRectY: 791.9,
    });
    expect(entries[5]).toEqual({ page: 1, segments: 1, withRects: 1, minFirstRectY: 90, maxFirstRectY: 90 });
  });

  it("emits a segment without rects with null geometry, not dropped, and counts it against its page", () => {
    const dump = dumpSegments([
      seg("Body.", 2, "paragraphStart"),
      seg("Footnote.", 2, "paragraphStart", 1, 770.0),
      seg("An odd one.", 2),
    ]);
    const entries = lines(dump).map((line) => JSON.parse(line));
    // No segment is dropped: all three appear, the rect-less ones with null geometry.
    expect(entries.slice(0, 3)).toEqual([
      { index: 0, text: "Body.", pageIndex: 2, anchor: "paragraphStart", rectCount: null, firstRectY: null },
      { index: 1, text: "Footnote.", pageIndex: 2, anchor: "paragraphStart", rectCount: 1, firstRectY: 770 },
      { index: 2, text: "An odd one.", pageIndex: 2, anchor: null, rectCount: null, firstRectY: null },
    ]);
    expect(entries[3]).toEqual({ page: 2, segments: 3, withRects: 1, minFirstRectY: 770, maxFirstRectY: 770 });
  });

  it("sums a page no segment of which has rects as withRects 0 and null bounds", () => {
    const dump = dumpSegments([seg("Only body.", 5, "paragraphStart")]);
    const entries = lines(dump).map((line) => JSON.parse(line));
    expect(entries[1]).toEqual({ page: 5, segments: 1, withRects: 0, minFirstRectY: null, maxFirstRectY: null });
  });

  it("keeps every line one JSON value, whatever is in a segment's text", () => {
    const dump = dumpSegments([
      seg('A "quoted" text\nacross lines. And a \\ backslash.', 0, null, 1, 1.0),
      seg("", 0),
    ]);
    expect(lines(dump)).toHaveLength(3);
    for (const line of lines(dump)) expect(() => JSON.parse(line)).not.toThrow();
  });

  it("dumps an empty list to no lines", () => {
    expect(dumpSegments([])).toBe("");
  });
});
