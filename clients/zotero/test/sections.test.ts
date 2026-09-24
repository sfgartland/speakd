import { describe, expect, it } from "vitest";
import { buildSections } from "../src/sections";

const segs = (...texts: string[]) => texts.map((text) => ({ text }));

describe("buildSections", () => {
  it("joins segments with one space and records where each starts", () => {
    const [first, second] = buildSections(segs("One.", "Two two.", "Three."), 0, 2, 100);
    expect(first).toEqual({ segmentIndices: [0, 1], text: "One. Two two.", offsets: [0, 5] });
    expect(second).toEqual({ segmentIndices: [2], text: "Three.", offsets: [0] });
  });

  it("starts at the start index, and names segments by their index in the document", () => {
    const sections = buildSections(segs("a.", "b.", "c.", "d."), 2, 2, 100);
    expect(sections).toEqual([{ segmentIndices: [2, 3], text: "c. d.", offsets: [0, 3] }]);
  });

  it("keeps the first section to firstSize segments, however short they are", () => {
    const sections = buildSections(segs("a.", "b.", "c.", "d.", "e."), 0, 2, 100);
    expect(sections[0]!.segmentIndices).toEqual([0, 1]);
    expect(sections[1]!.segmentIndices).toEqual([2, 3, 4]);
  });

  it("gives a huge first segment a section of its own, and no more than two in any case", () => {
    const huge = "x".repeat(5000);
    const sections = buildSections(segs(huge, "b.", "c."), 0);
    expect(sections[0]!.segmentIndices).toEqual([0]);
    expect(sections[1]!.segmentIndices).toEqual([1, 2]);
  });

  it("fills later sections up to about size characters", () => {
    const ten = "a".repeat(9) + ".";
    const sections = buildSections(segs(ten, ten, ten, ten, ten, ten, ten), 0, 1, 32);
    // Three tens and two spaces are 32; a fourth would pass it.
    expect(sections.map((s) => s.segmentIndices)).toEqual([[0], [1, 2, 3], [4, 5, 6]]);
    for (const section of sections) expect(section.text.length).toBeLessThanOrEqual(32);
  });

  it("puts a segment longer than size in a section alone rather than dropping it", () => {
    const sections = buildSections(segs("a.", "b".repeat(50), "c."), 0, 1, 10);
    expect(sections.map((s) => s.segmentIndices)).toEqual([[0], [1], [2]]);
  });

  it("skips segments with nothing to say", () => {
    const sections = buildSections(segs("a.", "", "  ", "b."), 0, 2, 100);
    expect(sections).toEqual([{ segmentIndices: [0, 3], text: "a. b.", offsets: [0, 3] }]);
  });

  it("is empty past the end of the document", () => {
    expect(buildSections(segs("a."), 1)).toEqual([]);
    expect(buildSections([], 0)).toEqual([]);
  });

  it("offsets point at each segment's text", () => {
    const texts = ["First sentence.", "Second one here.", "Third.", "Fourth, last."];
    for (const section of buildSections(segs(...texts), 0, 2, 30)) {
      section.segmentIndices.forEach((index, k) => {
        const offset = section.offsets[k]!;
        expect(section.text.slice(offset, offset + texts[index]!.length)).toBe(texts[index]);
      });
    }
  });
});
