import { describe, expect, it } from "vitest";
import { filterRunningHeads, HEADER_MIN_PAGES, HEADER_SHARE } from "../src/segmentfilter";

const seg = (text: string, pageIndex: number | null = null) => ({ text, pageIndex });

// Six pages, each opened by the same running head and closed by a distinct
// sentence, with two body paragraphs between.
function paged(head: string, pages = 6) {
  const out: { text: string; pageIndex: number | null }[] = [];
  for (let page = 0; page < pages; page++) {
    out.push(seg(head, page));
    out.push(seg(`Body paragraph of page ${page}.`, page));
    out.push(seg(`The page's own closing sentence, letter ${"abcdef"[page]}.`, page));
  }
  return out;
}

describe("filterRunningHeads", () => {
  it("drops a head recurring as the opening of every page", () => {
    const out = filterRunningHeads(paged("Sample Journal"));
    expect(out.map((segment) => segment.text)).not.toContain("Sample Journal");
    // Only the heads go: body and closing sentences all stay.
    expect(out).toHaveLength(12);
  });

  it("mirrors the skill's constants: 40% of at least 5 pages", () => {
    expect(HEADER_SHARE).toBe(0.4);
    expect(HEADER_MIN_PAGES).toBe(5);
  });

  it("drops nothing under five pages, however often the head recurs", () => {
    const segments = paged("Sample Journal", 4);
    expect(filterRunningHeads(segments)).toBe(segments);
    expect(filterRunningHeads([])).toEqual([]);
  });

  it("drops nothing when the head recurs on less than 40% of the pages", () => {
    // The head opens 2 of 6 pages (33%): a body paragraph, not a running head.
    const segments: { text: string; pageIndex: number | null }[] = [];
    for (let page = 0; page < 6; page++) {
      segments.push(seg(page < 2 ? "Sample Journal" : `Opening ${"abcdef"[page]}.`, page));
      segments.push(seg(`Body of page ${"abcdef"[page]}.`, page));
    }
    const out = filterRunningHeads(segments);
    expect(out).toHaveLength(segments.length);
    expect(out.map((s) => s.text)).toContain("Sample Journal");
  });

  it("counts digit-normalised heads as one running head", () => {
    const segments: { text: string; pageIndex: number | null }[] = [];
    for (let page = 0; page < 6; page++) {
      segments.push(seg(`Chapter ${page + 1}`, page));
      segments.push(seg(`Body of page ${"abcdef"[page]}.`, page));
      segments.push(seg(`Closing ${"abcdef"[page]}.`, page));
    }
    const out = filterRunningHeads(segments);
    // "Chapter 3" and "Chapter 4" are both "Chapter #": one recurring head.
    expect(out.map((s) => s.text).join(" ")).not.toMatch(/Chapter \d/);
    expect(out).toHaveLength(12);
  });

  it("never drops a page's unique closing sentence", () => {
    const out = filterRunningHeads(paged("Sample Journal"));
    for (let page = 0; page < 6; page++) {
      expect(out.map((s) => s.text)).toContain(`The page's own closing sentence, letter ${"abcdef"[page]}.`);
    }
  });

  it("never drops a body paragraph that opens a page once", () => {
    const segments = paged("Sample Journal", 6).map((segment, index) =>
      index === 3 ? { ...segment, text: "A one-off opener on the second page." } : segment,
    );
    const out = filterRunningHeads(segments);
    expect(out.map((s) => s.text)).toContain("A one-off opener on the second page.");
  });

  it("groups a segment without a page onto its predecessor's page", () => {
    // Six pages, each closed by the same footer line -- the page's last
    // segment, of which Zotero lost the page. It still lands in the group,
    // recurs as every page's closing, and is dropped.
    const segments: { text: string; pageIndex: number | null }[] = [];
    for (let page = 0; page < 6; page++) {
      segments.push(seg(`Opening ${"abcdef"[page]}.`, page));
      segments.push(seg(`Body of page ${"abcdef"[page]}.`, page));
      segments.push(seg("(c) The Publisher", null));
    }
    const out = filterRunningHeads(segments);
    expect(out.map((s) => s.text)).not.toContain("(c) The Publisher");
    expect(out).toHaveLength(12);
  });

  it("ignores a long opening paragraph, and a long closing one", () => {
    const long = "A body paragraph of full length, far beyond any running head, with more than eighty characters to spend.";
    const segments: { text: string; pageIndex: number | null }[] = [];
    for (let page = 0; page < 6; page++) {
      segments.push(seg(long + ` Page ${"abcdef"[page]}.`, page));
      segments.push(seg(`Body ${"abcdef"[page]}.`, page));
    }
    expect(filterRunningHeads(segments)).toHaveLength(segments.length);
  });
});
