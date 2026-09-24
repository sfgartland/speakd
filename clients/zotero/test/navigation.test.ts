import { describe, expect, it } from "vitest";
import { paragraphTarget, selectionEnd, selectionStart } from "../src/navigation";

// Seven segments in three paragraphs: [0,1,2] [3,4] [5,6].
const ANCHORS = ["paragraphStart", null, null, "paragraphStart", null, "paragraphStart", null];

describe("paragraphTarget", () => {
  it("goes ahead to the next paragraph's first segment", () => {
    expect(paragraphTarget(ANCHORS, 0, 1)).toBe(3);
    expect(paragraphTarget(ANCHORS, 4, 1)).toBe(5);
  });

  it("has nowhere to go ahead from the last paragraph", () => {
    expect(paragraphTarget(ANCHORS, 5, 1)).toBeNull();
    expect(paragraphTarget(ANCHORS, 6, 1)).toBeNull();
  });

  it("goes back to the start of the paragraph being read", () => {
    expect(paragraphTarget(ANCHORS, 4, -1)).toBe(3);
    expect(paragraphTarget(ANCHORS, 2, -1)).toBe(0);
  });

  it("goes back a whole paragraph from a paragraph's first segment", () => {
    expect(paragraphTarget(ANCHORS, 3, -1)).toBe(0);
    expect(paragraphTarget(ANCHORS, 5, -1)).toBe(3);
  });

  it("stays at the start of the document going back from it", () => {
    expect(paragraphTarget(ANCHORS, 0, -1)).toBe(0);
  });

  it("counts the document's first segment as a paragraph start without an anchor", () => {
    expect(paragraphTarget([null, null, "paragraphStart", null], 1, -1)).toBe(0);
    expect(paragraphTarget([null, null, "paragraphStart", null], 2, -1)).toBe(0);
  });
});

const SEGMENTS = [
  "The first sentence is here.",
  "A second one follows it.",
  "Then a third, which is longer than the others.",
  "And a fourth.",
].map((text) => ({ text }));

describe("selectionEnd", () => {
  it("ends at the segment holding the selection's last words", () => {
    expect(selectionEnd(SEGMENTS, 0, "The first sentence is here. A second one")).toBe(1);
  });

  it("allows for a selection starting partway into its first segment", () => {
    expect(selectionEnd(SEGMENTS, 1, "one follows it. Then a third")).toBe(2);
  });

  it("ignores line breaks and spacing in the selected text", () => {
    expect(selectionEnd(SEGMENTS, 0, "The first\nsentence   is here.")).toBe(0);
  });

  it("falls back to counting characters when the last words are not in the segments", () => {
    // A citation Zotero elided from its segments ends the selection.
    const selected = "A second one follows it. Then a third, which (Smith 1994)";
    expect(selectionEnd(SEGMENTS, 1, selected)).toBe(2);
  });

  it("never ends before the start, nor past the document", () => {
    expect(selectionEnd(SEGMENTS, 2, "")).toBe(2);
    expect(selectionEnd(SEGMENTS, 2, "x".repeat(500))).toBe(3);
  });
});

describe("selectionStart", () => {
  it("keeps Zotero's start when the selection begins in that segment", () => {
    expect(selectionStart(SEGMENTS, 1, "second one follows it. Then")).toBe(1);
  });

  it("moves on one segment when Zotero lands on the one just before the selection", () => {
    // Zotero picks the first segment ending at or after the selection's
    // start, which can be the sentence ending where the selection begins.
    expect(selectionStart(SEGMENTS, 0, "A second one follows it.")).toBe(1);
  });

  it("stays put when the selection cannot be found in either", () => {
    expect(selectionStart(SEGMENTS, 0, "Nothing like the text")).toBe(0);
    expect(selectionStart(SEGMENTS, 3, "anything")).toBe(3);
  });
});
