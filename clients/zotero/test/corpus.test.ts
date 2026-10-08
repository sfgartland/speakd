import { describe, expect, it } from "vitest";
import { tagSegments } from "../src/footnotes";
import footnotesDump from "./fixtures/footnotes.jsonl?raw";
import twocolumnDump from "./fixtures/twocolumn.jsonl?raw";

// The checked-in harness dumps (clients/zotero/test/fixtures/README.md): the
// real segments Zotero's SDT worker produced for the shared corpus. They let
// the filters be tested against real capture shape, rects and all, without a
// live harness.

interface DumpSegment {
  readonly text: string;
  readonly pageIndex: number | null;
  readonly anchor: string | null;
  readonly firstRectY: number | null;
}

function parse(raw: string): DumpSegment[] {
  return raw
    .split("\n")
    .filter((line) => line.trim() !== "")
    .map((line) => JSON.parse(line) as DumpSegment)
    .filter((entry) => typeof entry.text === "string");
}

describe("the checked-in segment dumps", () => {
  it("tags the footnote dump: citations dropped, the year-led body line kept", () => {
    const segments = parse(footnotesDump);
    const { tagged, dropped } = tagSegments(segments, "classify");
    expect(tagged).toHaveLength(segments.length);
    expect(dropped.length).toBeGreaterThan(0);
    // No body paragraph is ever dropped: only footnote lines and, where the
    // fixture repeats one verbatim as a page's last segment, its recurring
    // tail. Every drop is in the audit list.
    expect(dropped.some((entry) => /^P\d+ L\d/.test(entry.text))).toBe(false);
    expect(dropped.some((entry) => entry.why === "footnote")).toBe(true);
    // The body line that opens with a year sits in the body band: kept.
    const year = tagged.find((segment) => segment.text.includes("1994 was the year"));
    expect(year?.tag).not.toBe("drop");
    // No citation footnote is read in place.
    const spoken = tagged
      .filter((segment) => segment.tag !== "drop")
      .map((segment) => segment.text)
      .join(" ");
    expect(spoken).not.toContain("Kant, KrV");
    expect(spoken).not.toContain("op. cit.");
  });

  it("keeps the two-column dump's reading order and the planted cross sentence adjacent", () => {
    const segments = parse(twocolumnDump);
    const { tagged } = tagSegments(segments, "classify");
    const kept = tagged.filter((segment) => segment.tag !== "drop");
    const first = kept.findIndex((segment) => segment.text.startsWith("The cross column sentence begins"));
    expect(first).toBeGreaterThanOrEqual(0);
    expect(kept[first + 1]?.text).toContain("and ends in the rightmost column.");
    expect(kept.map((segment) => segment.text).join(" ")).not.toContain("A Tale of Two Columns");
  });
});
