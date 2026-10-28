import { describe, expect, it } from "vitest";
import { buildParts, isBookItemType } from "../../src/export/parts";

function seg(
  text: string,
  pageIndex: number,
  anchor: string | null = null,
  firstRectY: number | null = null,
) {
  return { text, pageIndex, anchor, firstRectY };
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

describe("buildParts with the cleanup filters", () => {
  // Six pages of a paper with a running head, a citation footnote and a prose
  // footnote on every page, as the filters see them: rect y running bottom-up
  // (footnotes low, body high).
  function paper() {
    const segments = [];
    for (let n = 0; n < 6; n++) {
      segments.push(seg("Sample Journal", n, "paragraphStart", 770));
      segments.push(seg(`Body of page ${n} opens.`, n, "paragraphStart", 740));
      segments.push(seg("1 Kant, KrV, B25.", n, "paragraphStart", 140));
      segments.push(
        seg(
          `2 A footnote of page ${n} holds real prose, and it argues its case across the sentences at length.`,
          n,
          "paragraphStart",
          120,
        ),
      );
    }
    return segments;
  }

  it("drops running heads and citation footnotes, deferring kept ones to the part's end", () => {
    const parts = buildParts(
      paper(),
      "My Book",
      { kind: "pages", ranges: [{ startPage: 0, endPage: 2 }] },
      { footnotes: "classify" },
    );
    expect(parts).toHaveLength(1);
    expect(parts[0]?.text).not.toContain("Sample Journal");
    expect(parts[0]?.text).not.toContain("Kant, KrV");
    // The prose footnote was kept -- and read after the part's body, not inline.
    expect(parts[0]?.text).toContain("holds real prose");
    expect(parts[0]?.text?.endsWith("at length.")).toBe(true);
  });

  it("takes the footnote policy: keep_all keeps the citation too, drop_all keeps neither", () => {
    const deferAll = buildParts(
      paper(),
      "My Book",
      { kind: "pages", ranges: [{ startPage: 0, endPage: 0 }] },
      { footnotes: "keep_all" },
    );
    expect(deferAll[0]?.text).toContain("Kant, KrV");
    expect(deferAll[0]?.text?.endsWith("at length.")).toBe(true);
    const dropAll = buildParts(
      paper(),
      "My Book",
      { kind: "pages", ranges: [{ startPage: 0, endPage: 0 }] },
      { footnotes: "drop_all" },
    );
    expect(dropAll[0]?.text).not.toContain("Kant, KrV");
    expect(dropAll[0]?.text).not.toContain("holds real prose");
  });

  it("cleans each part's text: ligatures, hyphenation, page numbers, whitespace", () => {
    const segments = [
      seg("A \uFB01le of a hyphen-\nated word.", 0, "paragraphStart"),
      seg("12 Next   page.", 0),
    ];
    const parts = buildParts(segments, "My Paper", { kind: "whole" });
    expect(parts).toEqual([{ title: "My Paper", text: "A file of a hyphenated word. Next page." }]);
  });

  it("drops only the classify footnotes the Stage B batch labelled citations", () => {
    const selection = { kind: "pages" as const, ranges: [{ startPage: 0, endPage: 0 }] };
    // Page 0 of the paper: [head, body, citation, prose]; the prose footnote
    // (index 3) is the `classify` residue.
    const kept = buildParts(paper(), "My Book", selection, { footnotes: "classify" });
    expect(kept[0]?.text).toContain("holds real prose");
    const dropped = buildParts(paper(), "My Book", selection, {
      footnotes: "classify",
      footnoteKinds: new Map([[3, "citation"]]),
    });
    expect(dropped[0]?.text).not.toContain("holds real prose");
    // A substantive label keeps it, at the part's end as ever.
    const substantive = buildParts(paper(), "My Book", selection, {
      footnotes: "classify",
      footnoteKinds: new Map([[3, "substantive"]]),
    });
    expect(substantive[0]?.text).toContain("holds real prose");
  });
});

describe("buildParts with label overrides", () => {
  it("keeps a labelled junk segment out of every part's text", () => {
    const segments = [seg("Body one.", 0), seg("Junk to drop.", 0), seg("Body two.", 0)];
    const parts = buildParts(segments, "A Paper", { kind: "whole" }, { overrides: new Map([[1, "junk"]]) });
    expect(parts[0]?.text).toBe("Body one. Body two.");
  });

  it("keeps a labelled body segment the heuristics would drop", () => {
    const segments = [];
    for (let n = 0; n < 6; n++) {
      segments.push(seg("Sample Journal", n, "paragraphStart", 770));
      segments.push(
        seg(
          `Body of page ${n} opens with a paragraph long enough that the recurrence filter never mistakes it for a head.`,
          n,
          "paragraphStart",
          740,
        ),
      );
    }
    const parts = buildParts(segments, "A Paper", { kind: "whole" }, { overrides: new Map([[0, "body"]]) });
    expect(parts[0]?.text).toContain("Sample Journal");
  });

  it("is unchanged when no map is given", () => {
    const segments = [seg("One.", 0), seg("Two.", 0)];
    expect(buildParts(segments, "A Paper", { kind: "whole" })).toEqual(
      buildParts(segments, "A Paper", { kind: "whole" }, {}),
    );
  });
});

describe("buildParts footnote placement", () => {
  // Page 0: a paragraph that runs on past the page break, one footnote;
  // page 1: the paragraph's tail, then a new paragraph; page 2: one more.
  function flow() {
    return [
      seg("Para one opens", 0, "paragraphStart", 760),
      seg("and runs down the page and", 0, null, 740),
      seg("1 A note of page zero, Kant 1781.", 0, "paragraphStart", 100),
      seg("over the break to finish.", 1, null, 760),
      seg("Para two is next.", 1, "paragraphStart", 740),
      seg("Para three.", 2, "paragraphStart", 760),
    ];
  }
  const text = (placement: "after-paragraph" | "inline" | "page-end" | "chapter-end") =>
    buildParts(flow(), "T", { kind: "whole" }, { footnotes: "keep_all", placement })[0]!.text;

  it("after-paragraph (the default) finishes the straddling paragraph, then reads the note", () => {
    const expected =
      "Para one opens and runs down the page and over the break to finish. A note of page zero, Kant 1781. Para two is next. Para three.";
    // (The cleanup rules drop a bare marker digit that opens a sentence.)
    expect(text("after-paragraph")).toBe(expected);
    expect(buildParts(flow(), "T", { kind: "whole" }, { footnotes: "keep_all" })[0]!.text).toBe(expected);
  });

  it("inline leaves the note where it sits in the flow", () => {
    expect(text("inline")).toBe(
      "Para one opens and runs down the page and 1 A note of page zero, Kant 1781. over the break to finish. Para two is next. Para three.",
    );
  });

  it("page-end reads the note as the next page starts, mid-paragraph if need be", () => {
    expect(text("page-end")).toBe(
      "Para one opens and runs down the page and 1 A note of page zero, Kant 1781. over the break to finish. Para two is next. Para three.",
    );
  });

  it("chapter-end reads every note at the part's end", () => {
    expect(text("chapter-end")).toBe(
      "Para one opens and runs down the page and over the break to finish. Para two is next. Para three. A note of page zero, Kant 1781.",
    );
  });
});
