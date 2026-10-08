import { describe, expect, it } from "vitest";
import { buildSections } from "../src/sections";

const segs = (...texts: string[]) => texts.map((text) => ({ text }));

describe("buildSections over tagged segments", () => {
  const tagged = (text: string, tag?: "body" | "drop" | "defer", pageIndex?: number) => ({
    text,
    tag,
    pageIndex,
  });

  it("appends a deferred footnote at the end of the section that owns its page", () => {
    const segments = [
      tagged("Body one of page one.", undefined, 0),
      tagged("Body two of page one.", undefined, 0),
      tagged("1 A footnote deferred.", "defer", 0),
      tagged("Body one of page two.", undefined, 1),
      tagged("Body two of page two.", undefined, 1),
      tagged("2 Another footnote deferred.", "defer", 1),
    ];
    const sections = buildSections(segments, 0, 2, 100);
    expect(sections.map((section) => section.segmentIndices)).toEqual([
      [0, 1, 2],
      [3, 4, 5],
    ]);
    expect(sections[0]!.text).toBe("Body one of page one. Body two of page one. 1 A footnote deferred.");
    expect(sections[1]!.text).toBe("Body one of page two. Body two of page two. 2 Another footnote deferred.");
  });

  it("yields nothing for a page whose every segment is dropped", () => {
    const segments = [
      tagged("1 A dropped citation.", "drop", 0),
      tagged("2 Another dropped.", "drop", 0),
      tagged("The body of page two.", undefined, 1),
      tagged("The body goes on.", undefined, 1),
    ];
    const sections = buildSections(segments, 0, 2, 100);
    expect(sections.map((section) => section.text)).toEqual(["The body of page two. The body goes on."]);
    expect(sections[0]!.segmentIndices).toEqual([2, 3]);
  });

  it("keeps original document indices for deferred segments, and their offsets true", () => {
    const segments = [
      tagged("Body one.", undefined, 0),
      tagged("1 A deferred footnote at length, and then some more.", "defer", 0),
      tagged("Body of the next page.", undefined, 1),
      tagged("2 A second deferred footnote, also long.", "defer", 1),
    ];
    for (const section of buildSections(segments, 0, 2, 20)) {
      section.segmentIndices.forEach((index, k) => {
        const offset = section.offsets[k]!;
        expect(section.text.slice(offset, offset + segments[index]!.text.length)).toBe(
          segments[index]!.text,
        );
      });
    }
    expect(buildSections(segments, 0, 2, 20).map((s) => s.segmentIndices)).toEqual([
      [0, 1],
      [2, 3],
    ]);
  });

  it("still reads a document whose only segments are deferred footnotes", () => {
    const segments = [tagged("1 A lone footnote.", "defer", 0), tagged("2 Another one.", "defer", 0)];
    expect(buildSections(segments, 0, 2, 100).map((s) => s.text)).toEqual(["1 A lone footnote. 2 Another one."]);
  });

  it("rides a deferred footnote of a dropped page with the next section that has body", () => {
    const segments = [
      tagged("Body of the first page.", "drop", 0),
      tagged("1 Its footnote survives.", "defer", 0),
      tagged("Body of the second page.", undefined, 1),
      tagged("The body carries on.", undefined, 1),
    ];
    const sections = buildSections(segments, 0, 2, 100);
    expect(sections.map((section) => section.segmentIndices)).toEqual([[2, 3, 1]]);
    expect(sections[0]!.text).toBe("Body of the second page. The body carries on. 1 Its footnote survives.");
  });

  it("leaves an untagged list exactly as the plain walk built it", () => {
    const plain = segs("One.", "Two two.", "Three.");
    const untagged = plain.map((segment) => ({ ...segment, tag: undefined, pageIndex: undefined }));
    expect(buildSections(untagged, 0, 2, 100)).toEqual(buildSections(plain, 0, 2, 100));
  });
});

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
