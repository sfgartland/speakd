import { describe, expect, it } from "vitest";
import { outlineToChapters } from "../../src/export/outline";

describe("outlineToChapters", () => {
  it("returns null for a book with no outline", () => {
    expect(outlineToChapters(null, 199)).toBeNull();
    expect(outlineToChapters(undefined, 199)).toBeNull();
    expect(outlineToChapters([], 199)).toBeNull();
  });

  it("turns each top-level entry into a range from its page to the page before the next", () => {
    const outline = [
      { title: "Chapter 1", pageIndex: 0 },
      { title: "Chapter 2", pageIndex: 20 },
      { title: "Chapter 3", pageIndex: 45 },
    ];
    expect(outlineToChapters(outline, 59)).toEqual([
      { title: "Chapter 1", startPage: 0, endPage: 19 },
      { title: "Chapter 2", startPage: 20, endPage: 44 },
      { title: "Chapter 3", startPage: 45, endPage: 59 },
    ]);
  });

  it("runs the last chapter to the document's last page", () => {
    const outline = [{ title: "Only chapter", pageIndex: 3 }];
    expect(outlineToChapters(outline, 10)).toEqual([{ title: "Only chapter", startPage: 3, endPage: 10 }]);
  });

  it("flattens nested items into their top-level chapter, rather than making them separate chapters", () => {
    const outline = [
      {
        title: "Part One",
        pageIndex: 0,
        items: [
          { title: "1.1 Introduction", pageIndex: 1 },
          { title: "1.2 Background", pageIndex: 8 },
        ],
      },
      { title: "Part Two", pageIndex: 20 },
    ];
    expect(outlineToChapters(outline, 30)).toEqual([
      { title: "Part One", startPage: 0, endPage: 19 },
      { title: "Part Two", startPage: 20, endPage: 30 },
    ]);
  });
});
