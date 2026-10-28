import { describe, expect, it } from "vitest";
import { classificationResidue, planFootnotes, tagSegments, type FootnotePolicy } from "../src/footnotes";

// Geometry as Stage 0 measured it (docs/design/2026-10-08-pdf-text-cleanup-stage0.md
// (c)): an 842-pt page, rect y running bottom-up -- body paragraphs high
// (y ~700-770), footnotes low (y ~84-140), the bottom band the lowest ~25% of
// the page's observed y-range.
function seg(
  text: string,
  pageIndex: number,
  anchor: string | null = null,
  firstRectY: number | null = null,
) {
  return { text, anchor, pageIndex, firstRectY };
}

// A page with body paragraphs and a citation-shaped footnote block.
function citedPage(pageIndex: number) {
  return [
    seg("The first body paragraph opens the page.", pageIndex, "paragraphStart", 770),
    seg("A continuation of the body, not a new paragraph.", pageIndex, null, 756),
    seg("The second body paragraph follows.", pageIndex, "paragraphStart", 728),
    seg("1 Kant, KrV, B25.", pageIndex, "paragraphStart", 140),
    seg("2 Heidegger 1962, p. 65.", pageIndex, "paragraphStart", 126),
    seg("3 Ibid.", pageIndex, "paragraphStart", 112),
    seg("4 Cf.", pageIndex, "paragraphStart", 98),
    seg("KrV, A51/B75.", pageIndex, null, 84),
  ];
}

// A page whose footnote block holds clear prose and a borderline case.
function prosePage(pageIndex: number) {
  return [
    seg("The page's body opens here.", pageIndex, "paragraphStart", 760),
    seg(
      "1 The inaugurating gesture of the lecture is reversed here. What that reversal means " +
        "for the order of asking is worked out across three pages of close reading.",
      pageIndex,
      "paragraphStart",
      140,
    ),
    seg(
      "2 What the reversal means for the asking's order is not yet settled anywhere.",
      pageIndex,
      "paragraphStart",
      126,
    ),
  ];
}

describe("planFootnotes", () => {
  it("detects footnote candidates by marker and bottom band, and their continuations", () => {
    const plan = planFootnotes(citedPage(0), "classify");
    expect([...plan.tags]).toEqual([
      "body",
      "body",
      "body",
      "drop",
      "drop",
      "drop",
      "drop",
      "drop",
    ]);
    // Every dropped segment is in the plan's drops: the filter's audit trail.
    expect(plan.drops).toEqual([3, 4, 5, 6, 7]);
  });

  it("classifies: citation shape drops, clear multi-sentence prose defers, the in-between is classify", () => {
    const plan = planFootnotes(prosePage(1), "classify");
    expect([...plan.tags]).toEqual(["body", "defer", "classify"]);
    expect(plan.drops).toEqual([]);
  });

  it("keeps a body paragraph that opens with a year, and one that opens with a small count", () => {
    const plan = planFootnotes(
      [
        seg("1994 was the year the reading began in earnest.", 0, "paragraphStart", 750),
        seg("The body carries on.", 0, null, 736),
        seg("12 of them were counted on the last line of the page.", 0, "paragraphStart", 140),
        seg("A marker-less paragraph in the band is body still.", 0, "paragraphStart", 126),
      ],
      "classify",
    );
    // The count-led line in the band has no second signal (no "12" cited in
    // the body, no neighbouring note): it is body, as the test's name says.
    expect([...plan.tags]).toEqual(["body", "body", "body", "body"]);
  });

  it("keeps a continuation whose candidate sits on the previous page: kept beats dropped", () => {
    const plan = planFootnotes(
      [
        seg("The body of a later page.", 3, "paragraphStart", 760),
        seg("KrV, A51/B75.", 3, null, 140),
        seg("5 See op. cit., pp. 12-20.", 3, "paragraphStart", 126),
      ],
      "classify",
    );
    // The stray continuation is kept; the new candidate is dropped on shape.
    expect([...plan.tags]).toEqual(["body", "body", "drop"]);
  });

  it("plans two pages of footnotes independently", () => {
    const plan = planFootnotes([...citedPage(0), ...prosePage(1)], "classify");
    expect([...plan.tags.slice(0, 8)]).toEqual(["body", "body", "body", "drop", "drop", "drop", "drop", "drop"]);
    expect([...plan.tags.slice(8)]).toEqual(["body", "defer", "classify"]);
  });

  it("falls back to marker-only detection when a page carries no rects, never throwing", () => {
    const plan = planFootnotes(
      [
        seg("A body paragraph.", 0, "paragraphStart", null),
        seg("1 Kant, KrV, B25.", 0, "paragraphStart", null),
        seg("2 Heidegger 1962, p. 65.", 0, "paragraphStart", null),
        seg("The next body paragraph after the notes.", 0, "paragraphStart", null),
      ],
      "classify",
    );
    expect([...plan.tags]).toEqual(["body", "drop", "drop", "body"]);
  });

  it("needs sequential markers for the no-rects fallback: a lone count or year stays body", () => {
    const plan = planFootnotes(
      [
        seg("12 of them were counted here.", 1, "paragraphStart", null),
        seg("The body follows the count.", 1, "paragraphStart", null),
        seg("1994 was the year the reading began.", 1, "paragraphStart", null),
        seg("n1 A lone note without a successor.", 1, "paragraphStart", null),
      ],
      "classify",
    );
    expect([...plan.tags]).toEqual(["body", "body", "body", "body"]);
    expect(plan.drops).toEqual([]);
  });

  it("marks every candidate and continuation the same way under drop_all and defer_all", () => {
    const segments = [...citedPage(0), ...prosePage(1)];
    const dropAll = planFootnotes(segments, "drop_all");
    expect(dropAll.drops).toEqual([3, 4, 5, 6, 7, 9, 10]);
    expect(dropAll.tags.every((tag, i) => (i >= 3 && i <= 7) || i >= 9 ? tag === "drop" : tag === "body")).toBe(true);
    const deferAll = planFootnotes(segments, "defer_all");
    expect(deferAll.drops).toEqual([]);
    expect(deferAll.tags.every((tag, i) => (i >= 3 && i <= 7) || i >= 9 ? tag === "defer" : tag === "body")).toBe(true);
  });

  it("takes the bottom band's share as an option, so Stage 0's tuning is one number", () => {
    // A count-led paragraph in the middle of the page: outside the default
    // bottom band it is body; with the band widened to the whole page it is a
    // candidate, and its shape is in-between -- `classify`, kept for now.
    const segments = [
      seg("The body opens with a claim.12", 0, "paragraphStart", 770),
      seg("A low body paragraph, ending well above the notes.", 0, null, 300),
      seg("12 of them were counted on the last line, which is body text still.", 0, "paragraphStart", 500),
    ];
    expect([...planFootnotes(segments, "classify").tags]).toEqual(["body", "body", "body"]);
    expect([...planFootnotes(segments, "classify", { bottomBand: 1 }).tags]).toEqual(["body", "body", "classify"]);
  });

  it("plans an empty list to nothing", () => {
    const plan = planFootnotes([], "classify");
    expect([...plan.tags]).toEqual([]);
    expect(plan.drops).toEqual([]);
  });

  it("treats a pageless segment as its predecessor's, like capturedSegments", () => {
    const plan = planFootnotes(
      [
        seg("The body cites a note.1", 0, "paragraphStart", 760),
        seg("1 Kant, KrV, B25.", 0, "paragraphStart", 140),
        seg("A wrapped tail of the note.", 0, null, null),
        seg("2 Heidegger 1962, p. 65.", 2, "paragraphStart", 126),
      ],
      "classify",
    );
    // The pageless continuation sits in page 0's group, in a band it has no y
    // for: kept, never dropped on a guess.
    expect([...plan.tags]).toEqual(["body", "drop", "body", "drop"]);
  });

  it("recognises an OCR-mangled footnote marker in the bottom band", () => {
    // The marker OCR'd as 'il' (for '11') still opens a footnote when a
    // neighbouring note numbers one off it: a letter-lookalike marker is
    // taken only on a second signal.
    const plan = planFootnotes(
      [
        seg("The body paragraph.", 0, "paragraphStart", 760),
        seg("il found recently that these words aie quoted from A. N. Whitehead,", 0, "paragraphStart", 140),
        seg("who does not of course share the opinion which they express.", 0, null, 130),
        seg("12 Another note, numbered one after it.", 0, "paragraphStart", 120),
      ],
      "defer_all",
    );
    expect([...plan.tags]).toEqual(["body", "defer", "defer", "defer"]);
  });

  it("keeps a body paragraph in the band that opens with a lone 'I '", () => {
    // A lone confusable is not a footnote marker: even drop_all leaves it
    // alone, so a body paragraph is never dropped on geometry alone.
    const plan = planFootnotes(
      [
        seg("The body opens the page.", 0, "paragraphStart", 760),
        seg("I looked at the man speechless.", 0, "paragraphStart", 140),
      ],
      "drop_all",
    );
    expect([...plan.tags]).toEqual(["body", "body"]);
  });
});

describe("planFootnotes false positives", () => {
  it("keeps a short numbered list item in the band: '2. The second premise is false.'", () => {
    const plan = planFootnotes(
      [
        seg("The argument has two premises.", 0, "paragraphStart", 760),
        seg("1. The first premise is true.", 0, "paragraphStart", 140),
        seg("2. The second premise is false.", 0, "paragraphStart", 126),
      ],
      "classify",
    );
    expect([...plan.tags]).toEqual(["body", "body", "body"]);
    expect(plan.drops).toEqual([]);
  });

  it("keeps 'Oil prices rose.' in the band: letter-lookalike alone is not a marker", () => {
    for (const policy of ["classify", "drop_all"] as const) {
      const plan = planFootnotes(
        [
          seg("The body opens the page.", 0, "paragraphStart", 760),
          seg("Oil prices rose.", 0, "paragraphStart", 140),
          seg("Ill advised, the policy failed.", 0, "paragraphStart", 126),
        ],
        policy,
      );
      expect([...plan.tags]).toEqual(["body", "body", "body"]);
    }
  });

  it("takes a lone digit marker when the body cites the same number above", () => {
    const plan = planFootnotes(
      [
        seg("The claim is contested.3", 0, "paragraphStart", 760),
        seg("3 Compare the second edition.", 0, "paragraphStart", 140),
      ],
      "drop_all",
    );
    expect([...plan.tags]).toEqual(["body", "drop"]);
  });
});

describe("tagSegments overrides", () => {
  // Five pages, enough for the running-head recurrence rule to fire: each
  // page opens with the same short paragraph, which the filter drops.
  function recurring() {
    const segments = [];
    for (let n = 0; n < 5; n++) {
      segments.push(seg("A recurring opening clause.", n, "paragraphStart", 770));
      segments.push(seg(`The body of page ${n} opens.`, n, "paragraphStart", 740));
    }
    return segments;
  }

  it("restores a head-filter false positive with a body override", () => {
    const segments = recurring();
    // Unaided the recurring opening is dropped on every page.
    expect(tagSegments(segments, "classify").tagged[0]?.tag).toBe("drop");
    const { tagged } = tagSegments(segments, "classify", new Map([[0, "body"]]));
    expect(tagged[0]?.tag).toBe("body");
    // The unlabelled occurrences are unchanged.
    expect(tagged[2]?.tag).toBe("drop");
  });

  it("makes the OCR'd footnote a candidate under each policy", () => {
    const segments = [
      seg("The body paragraph opens the page.", 0, "paragraphStart", 760),
      seg("il found recently that these words aie quoted from A. N. Whitehead, who does not of course share the opinion which they express.", 0, "paragraphStart", 140),
    ];
    const overrides = new Map([[1, "footnote"]] as const);
    expect(tagSegments(segments, "classify", overrides).tagged[1]?.tag).toBe("classify");
    expect(tagSegments(segments, "drop_all", overrides).tagged[1]?.tag).toBe("drop");
    expect(tagSegments(segments, "defer_all", overrides).tagged[1]?.tag).toBe("defer");
  });

  it("makes a markerless bottom-band paragraph a candidate the heuristics leave as body", () => {
    const segments = [
      seg("The body paragraph opens the page.", 0, "paragraphStart", 760),
      seg("found recently that these words are quoted from a source, and the sentence runs on.", 0, "paragraphStart", 140),
    ];
    expect(tagSegments(segments, "classify").tagged[1]?.tag).toBe("body");
    const overrides = new Map([[1, "footnote"]] as const);
    expect(tagSegments(segments, "classify", overrides).tagged[1]?.tag).toBe("classify");
  });

  it("drops a running-head override on a three-page input the unaided filter never fires on", () => {
    const segments = [
      seg("The body of page 0.", 0, "paragraphStart", 740),
      seg("i6  THE PLACE OF VALUE IN A WORLD OF FACTS", 1, "paragraphStart", 770),
      seg("The body of page 1.", 1, "paragraphStart", 740),
      seg("The body of page 2.", 2, "paragraphStart", 740),
    ];
    expect(tagSegments(segments, "classify").tagged[1]?.tag).toBe("body");
    const { tagged, dropped } = tagSegments(segments, "classify", new Map([[1, "running-head"]]));
    expect(tagged[1]?.tag).toBe("drop");
    expect(dropped).toContainEqual({ text: "i6  THE PLACE OF VALUE IN A WORLD OF FACTS", why: "label" });
  });

  it("drops and audits a junk override with why label", () => {
    const segments = [seg("Body text.", 0, "paragraphStart", 740), seg("Some junk.", 0, "paragraphStart", 720)];
    const { tagged, dropped } = tagSegments(segments, "classify", new Map([[1, "junk"]]));
    expect(tagged[1]?.tag).toBe("drop");
    expect(dropped).toContainEqual({ text: "Some junk.", why: "label" });
  });

  it("treats a heading override as spoken body", () => {
    const segments = [seg("1. Introduction", 0, "paragraphStart", 770), seg("Body.", 0, "paragraphStart", 740)];
    const { tagged } = tagSegments(segments, "classify", new Map([[0, "heading"]]));
    expect(tagged[0]?.tag).toBe("body");
  });

  it("leaves an unlabelled list byte-identical to the heuristics alone", () => {
    const segments = [...recurring(), ...citedPage(5)];
    expect(tagSegments(segments, "classify", new Map())).toEqual(tagSegments(segments, "classify"));
    expect(tagSegments(segments, "classify", undefined)).toEqual(tagSegments(segments, "classify"));
  });
});

describe("classificationResidue", () => {
  it("gathers only the classify tags, with their original document indices", () => {
    const residue = classificationResidue([...citedPage(0), ...prosePage(1)], "classify");
    expect(residue).toEqual([{ index: 10, text: prosePage(1)[2]!.text }]);
  });

  it("is empty under drop_all and defer_all", () => {
    const segments = [...citedPage(0), ...prosePage(1)];
    expect(classificationResidue(segments, "drop_all")).toEqual([]);
    expect(classificationResidue(segments, "defer_all")).toEqual([]);
  });
});
