import { describe, expect, it } from "vitest";
import { buildParts, isBookItemType } from "../../src/export/parts";

function seg(text: string, pageIndex: number, anchor: string | null = null) {
  return { text, pageIndex, anchor };
}

describe("isBookItemType", () => {
  it("treats book and bookSection as book-like", () => {
    expect(isBookItemType("book")).toBe(true);
    expect(isBookItemType("bookSection")).toBe(true);
  });

  it("treats everything else as article-like", () => {
    expect(isBookItemType("journalArticle")).toBe(false);
    expect(isBookItemType("report")).toBe(false);
    expect(isBookItemType("magazineArticle")).toBe(false);
  });
});

describe("buildParts", () => {
  it("builds one part for a whole article, titled with the item title", () => {
    const segments = [seg("Hello.", 0), seg("World.", 0)];
    const parts = buildParts(segments, "My Paper", { kind: "whole" });
    expect(parts).toEqual([{ title: "My Paper", text: "Hello. World." }]);
  });

  it("cuts an article before its references heading when enabled", () => {
    const segments = [
      seg("Intro.", 0, "paragraphStart"),
      seg("Filler one.", 0),
      seg("Filler two.", 0),
      seg("References", 0, "paragraphStart"),
      seg("Smith 2020.", 0),
    ];
    const parts = buildParts(segments, "My Paper", { kind: "whole", skipReferences: true });
    expect(parts).toEqual([{ title: "My Paper", text: "Intro. Filler one. Filler two." }]);
  });

  it("keeps the references section when the cut is disabled", () => {
    const segments = [
      seg("Intro.", 0, "paragraphStart"),
      seg("Filler.", 0),
      seg("Filler two.", 0),
      seg("References", 0, "paragraphStart"),
      seg("Smith 2020.", 0),
    ];
    const parts = buildParts(segments, "My Paper", { kind: "whole", skipReferences: false });
    expect(parts[0]?.text).toContain("Smith 2020.");
  });

  it("builds one part per selected chapter, titled from the outline, and skips front/back matter", () => {
    const segments = [
      seg("Front matter.", 0), // before the first chapter
      seg("Chapter one, page one.", 1),
      seg("Chapter one, page two.", 2),
      seg("Chapter two, start.", 3),
      seg("Chapter two, more.", 4),
      seg("Back matter, index.", 5), // after the last chapter
    ];
    const parts = buildParts(segments, "My Book", {
      kind: "chapters",
      chapters: [
        { title: "Chapter One", startPage: 1, endPage: 2 },
        { title: "Chapter Two", startPage: 3, endPage: 4 },
      ],
    });
    expect(parts).toEqual([
      { title: "Chapter One", text: "Chapter one, page one. Chapter one, page two." },
      { title: "Chapter Two", text: "Chapter two, start. Chapter two, more." },
    ]);
  });

  it("builds one part per bare page range, without chapter titles", () => {
    const segments = [seg("Page 11.", 10), seg("Page 12.", 11), seg("Page 59.", 58)];
    const parts = buildParts(segments, "My Book", {
      kind: "pages",
      ranges: [
        { startPage: 10, endPage: 11 },
        { startPage: 58, endPage: 58 },
      ],
    });
    expect(parts).toEqual([
      { title: "My Book (p. 11-12)", text: "Page 11. Page 12." },
      { title: "My Book (p. 59)", text: "Page 59." },
    ]);
  });

  it("drops parts that end up with no text", () => {
    const segments = [seg("Chapter one text.", 1)];
    const parts = buildParts(segments, "My Book", {
      kind: "chapters",
      chapters: [
        { title: "Chapter One", startPage: 1, endPage: 1 },
        { title: "Empty Chapter", startPage: 2, endPage: 2 },
      ],
    });
    expect(parts).toEqual([{ title: "Chapter One", text: "Chapter one text." }]);
  });

  it("joins a part's segment texts with single spaces", () => {
    const segments = [seg("One.", 0), seg("Two.", 0), seg("Three.", 0)];
    const parts = buildParts(segments, "Title", { kind: "whole" });
    expect(parts[0]?.text).toBe("One. Two. Three.");
  });

  it("drops empty-text segments without adding extra spaces", () => {
    const segments = [seg("One.", 0), seg("", 0), seg("Two.", 0)];
    const parts = buildParts(segments, "Title", { kind: "whole" });
    expect(parts[0]?.text).toBe("One. Two.");
  });
});
